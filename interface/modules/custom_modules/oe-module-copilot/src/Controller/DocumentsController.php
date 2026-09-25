<?php

/**
 * Per-document processing on chart open and the document list (ADR-012).
 *
 *   POST /api/copilot/documents/process  - process up to two unprocessed
 *        documents of the session's patient (DocumentProcessor), then return
 *        the list. The panel calls it when the chart opens.
 *   GET  /api/copilot/documents          - the list only: type, upload date,
 *        status, fixed error code, identity result, pending-fact count.
 *
 * Authorization is the ticket route's, unchanged: the session's selected
 * patient, CopilotAuthorizer (ACL + care relationship), a `pid` from the client
 * only as a staleness check (409), CSRF by the local API bridge. Documents the
 * user may not access (core `can_access()`) are neither listed nor processed.
 * Until the module tables exist, both routes answer 200 with `status:
 * degraded` and `copilot_tables_not_installed`. One audit row per call with
 * codes and counts only.
 *
 * @package   OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Controller;

use OpenEMR\BC\ServiceContainer;
use OpenEMR\Common\Http\HttpRestRequest;
use OpenEMR\Common\Logging\EventAuditLogger;
use OpenEMR\Modules\Copilot\Authorization\CopilotAuthorizer;
use OpenEMR\Modules\Copilot\Data\ClinicalReaderInterface;
use OpenEMR\Modules\Copilot\Data\SchemaStatusInterface;
use OpenEMR\Modules\Copilot\Data\SourceUnavailableException;
use OpenEMR\Modules\Copilot\Documents\DocumentProcessor;
use OpenEMR\Modules\Copilot\Support\Scalar;
use Psr\Log\LoggerInterface;
use Ramsey\Uuid\Uuid;
use Symfony\Component\HttpFoundation\JsonResponse;
use Throwable;

final class DocumentsController
{
    public const AUDIT_PROCESS = 'copilot-document-process';
    public const AUDIT_LIST = 'copilot-document-list';

    public const DEGRADED_DOCUMENTS_UNAVAILABLE = 'documents_unavailable';

    private const ERRORS = [
        CopilotAuthorizer::CODE_NOT_AUTHENTICATED => [401, 'Authentication is required.'],
        CopilotAuthorizer::CODE_NO_PATIENT_SELECTED => [409, 'No patient is selected in this session.'],
        CopilotAuthorizer::CODE_ACL_DENIED => [403, 'Your role does not permit reading this patient context.'],
        CopilotAuthorizer::CODE_NO_CARE_RELATIONSHIP => [403, 'No care relationship with the selected patient was found.'],
        'patient_mismatch' => [409, 'The selected patient changed; reload the patient summary.'],
        'patient_not_found' => [404, 'The selected patient record could not be found.'],
        'internal_error' => [500, 'The documents could not be read.'],
    ];

    /** @var callable(string, string, bool, string, int): void */
    private $auditWriter;

    private readonly LoggerInterface $logger;

    public function __construct(
        private readonly CopilotAuthorizer $authorizer,
        private readonly ClinicalReaderInterface $reader,
        private readonly SchemaStatusInterface $schema,
        private readonly DocumentProcessor $processor,
        ?LoggerInterface $logger = null,
        ?callable $auditWriter = null,
    ) {
        $this->logger = $logger ?? ServiceContainer::getLogger();
        $this->auditWriter = $auditWriter ?? static function (string $event, string $user, bool $success, string $comment, int $pid): void {
            EventAuditLogger::getInstance()->newEvent($event, $user, 'copilot', $success ? 1 : 0, $comment, $pid);
        };
    }

    /**
     * @param array{authUserID?:mixed, authUser?:mixed, pid?:mixed} $session
     * @return array{status:int, body:array<string,mixed>, headers:array<string,string>}
     */
    public function processForSession(array $session, ?int $requestedPid = null): array
    {
        return $this->handle(self::AUDIT_PROCESS, $session, $requestedPid, true);
    }

    /**
     * @param array{authUserID?:mixed, authUser?:mixed, pid?:mixed} $session
     * @return array{status:int, body:array<string,mixed>, headers:array<string,string>}
     */
    public function listForSession(array $session, ?int $requestedPid = null): array
    {
        return $this->handle(self::AUDIT_LIST, $session, $requestedPid, false);
    }

    /** `POST /api/copilot/documents/process` under the local API bridge. */
    public function handleProcessRest(HttpRestRequest $request): JsonResponse
    {
        $json = json_decode($request->getContent(), true, 4);
        $pid = is_array($json) ? Scalar::positiveIntOrNull($json['pid'] ?? null) : null;
        return self::respond($this->processForSession(self::session($request), $pid));
    }

    /** `GET /api/copilot/documents` under the local API bridge. */
    public function handleListRest(HttpRestRequest $request): JsonResponse
    {
        $pid = Scalar::positiveIntOrNull($request->query->get('pid'));
        return self::respond($this->listForSession(self::session($request), $pid));
    }

    /**
     * @param array{authUserID?:mixed, authUser?:mixed, pid?:mixed} $session
     * @return array{status:int, body:array<string,mixed>, headers:array<string,string>}
     */
    private function handle(string $event, array $session, ?int $requestedPid, bool $process): array
    {
        $correlationId = Uuid::uuid4()->toString();
        $headers = BriefingTicketController::RESPONSE_HEADERS + [BriefingTicketController::CORRELATION_HEADER => $correlationId];

        $userId = Scalar::positiveIntOrNull($session['authUserID'] ?? null);
        $usernameRaw = Scalar::str($session['authUser'] ?? null);
        $username = $usernameRaw === '' ? null : $usernameRaw;
        $pid = Scalar::positiveIntOrNull($session['pid'] ?? null);

        $decision = $this->authorizer->authorize($userId, $username, $pid);
        if (!$decision['allowed']) {
            $this->logger->info('copilot documents denied', ['cid' => $correlationId, 'code' => $decision['code']]);
            if ($pid !== null && $username !== null && $decision['code'] !== CopilotAuthorizer::CODE_NOT_AUTHENTICATED) {
                ($this->auditWriter)($event, $username, false, "cid={$correlationId}; code={$decision['code']}", $pid);
            }
            return $this->error($decision['code'], $correlationId, $headers);
        }
        assert($userId !== null && $pid !== null && $username !== null);

        if ($requestedPid !== null && $requestedPid !== $pid) {
            ($this->auditWriter)($event, $username, false, "cid={$correlationId}; code=patient_mismatch", $pid);
            return $this->error('patient_mismatch', $correlationId, $headers);
        }

        if (!$this->schema->isReady()) {
            ($this->auditWriter)($event, $username, true, "cid={$correlationId}; basis={$decision['basis']}; outcome=" . SchemaStatusInterface::CODE_NOT_INSTALLED, $pid);
            $this->logger->warning('copilot documents disabled', ['cid' => $correlationId, 'code' => SchemaStatusInterface::CODE_NOT_INSTALLED]);
            return ['status' => 200, 'body' => self::body($correlationId, 'degraded', SchemaStatusInterface::CODE_NOT_INSTALLED, [], 0, []), 'headers' => $headers];
        }

        try {
            $processed = [];
            $remaining = 0;
            if ($process) {
                $patient = $this->reader->findPatient($pid);
                if ($patient === null) {
                    return $this->error('patient_not_found', $correlationId, $headers);
                }
                $result = $this->processor->process($pid, $patient['uuid'], $username, $this->chartIdentity($pid), $correlationId);
                $processed = $result['processed'];
                $remaining = $result['remaining'];
            }
            $documents = $this->processor->listDocuments($pid, $username);
            $body = self::body($correlationId, 'ok', null, $processed, $remaining, $documents);
            $outcome = 'ok';
        } catch (SourceUnavailableException $e) {
            $this->logger->error('copilot source unavailable', ['cid' => $correlationId, 'source' => $e->getSource()]);
            $outcome = self::DEGRADED_DOCUMENTS_UNAVAILABLE;
            $body = self::body($correlationId, 'degraded', $outcome, [], 0, []);
        } catch (Throwable $e) {
            $this->logger->error('copilot documents failed', ['cid' => $correlationId, 'type' => $e::class]);
            return $this->error('internal_error', $correlationId, $headers);
        }

        $codes = implode(',', array_map(static fn(array $p): string => $p['document_id'] . ':' . $p['outcome'], $processed));
        ($this->auditWriter)(
            $event,
            $username,
            true,
            "cid={$correlationId}; basis={$decision['basis']}; outcome={$outcome}; processed=" . ($codes === '' ? 'none' : $codes) . "; remaining={$remaining}",
            $pid
        );
        $this->logger->info('copilot documents', ['cid' => $correlationId, 'outcome' => $outcome, 'processed' => count($processed), 'remaining' => $remaining]);
        return ['status' => 200, 'body' => $body, 'headers' => $headers];
    }

    /** @return array{lname:string, dob:string}|null */
    private function chartIdentity(int $pid): ?array
    {
        try {
            $identity = $this->reader->findIdentity($pid);
        } catch (SourceUnavailableException) {
            return null;
        }
        return $identity === null ? null : ['lname' => $identity['lname'], 'dob' => $identity['dob']];
    }

    /**
     * @param list<array{document_id:int, outcome:string}> $processed
     * @param list<array<string,mixed>> $documents
     * @return array<string,mixed>
     */
    private static function body(string $correlationId, string $status, ?string $reason, array $processed, int $remaining, array $documents): array
    {
        return [
            'correlation_id' => $correlationId,
            'status' => $status,
            'degraded_reason' => $reason,
            'processed' => $processed,
            'remaining' => $remaining,
            'documents' => $documents,
        ];
    }

    /** @return array{authUserID:mixed, authUser:mixed, pid:mixed} */
    private static function session(HttpRestRequest $request): array
    {
        $session = $request->getSession();
        return ['authUserID' => $session->get('authUserID'), 'authUser' => $session->get('authUser'), 'pid' => $session->get('pid')];
    }

    /** @param array{status:int, body:array<string,mixed>, headers:array<string,string>} $result */
    private static function respond(array $result): JsonResponse
    {
        if (!headers_sent()) {
            header_remove('Cache-Control');
        }
        return new JsonResponse($result['body'], $result['status'], $result['headers']);
    }

    /**
     * @param array<string,string> $headers
     * @return array{status:int, body:array<string,mixed>, headers:array<string,string>}
     */
    private function error(string $code, string $correlationId, array $headers): array
    {
        [$status, $message] = self::ERRORS[$code] ?? self::ERRORS['internal_error'];
        return [
            'status' => $status,
            'body' => ['detail' => ['code' => $code, 'message' => $message, 'correlation_id' => $correlationId]],
            'headers' => $headers,
        ];
    }
}
