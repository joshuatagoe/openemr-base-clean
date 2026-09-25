<?php

/**
 * Verify and file / reject one extracted lab value (ADR-003, ADR-009 §2):
 *
 *   POST /api/copilot/documents/{document_id}/values/{result_index}/file
 *        body { "filed_value": string|null, "confirm_unverified": bool }
 *   POST /api/copilot/documents/{document_id}/values/{result_index}/reject
 *
 * Authorization: the briefing routes' CopilotAuthorizer on the session's
 * selected patient (CSRF by the local API bridge), then - filing is signing -
 * `patients/lab` write AND `patients/sign`. The document must still be filed to
 * that patient in OpenEMR (same 404 as a missing value) and pass core
 * `can_access()` (403). Until the module tables exist both routes answer 503
 * `copilot_tables_not_installed`. The rules and the single transaction are in
 * Filing\ValueFiler. One audit row per authorized call (`copilot-value-filed`
 * / `copilot-value-rejected`) with ids and codes only - never test names or
 * values.
 *
 * @package   OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Controller;

use OpenEMR\BC\ServiceContainer;
use OpenEMR\Common\Http\HttpRestRequest;
use OpenEMR\Common\Logging\EventAuditLogger;
use OpenEMR\Modules\Copilot\Authorization\AclCheckerInterface;
use OpenEMR\Modules\Copilot\Authorization\AclWriteCheckerInterface;
use OpenEMR\Modules\Copilot\Authorization\CopilotAuthorizer;
use OpenEMR\Modules\Copilot\Data\SchemaStatusInterface;
use OpenEMR\Modules\Copilot\Data\SourceUnavailableException;
use OpenEMR\Modules\Copilot\Documents\DocumentFileSourceInterface;
use OpenEMR\Modules\Copilot\Filing\ValueFiler;
use OpenEMR\Modules\Copilot\Support\Scalar;
use Psr\Log\LoggerInterface;
use Ramsey\Uuid\Uuid;
use Symfony\Component\HttpFoundation\JsonResponse;
use Throwable;

final class FilingController
{
    public const AUDIT_FILED = 'copilot-value-filed';
    public const AUDIT_REJECTED = 'copilot-value-rejected';

    public const CODE_NOT_PERMITTED = 'filing_not_permitted';
    public const CODE_INVALID_REQUEST = 'invalid_request';
    public const CODE_FAILED = 'filing_failed';
    public const CODE_UNAVAILABLE = 'document_unavailable';

    public const FILED_VALUE_MAX_LENGTH = 255;

    private const ERRORS = [
        CopilotAuthorizer::CODE_NOT_AUTHENTICATED => [401, 'Authentication is required.'],
        CopilotAuthorizer::CODE_NO_PATIENT_SELECTED => [409, 'No patient is selected in this session.'],
        CopilotAuthorizer::CODE_ACL_DENIED => [403, 'Your role does not permit reading this patient context.'],
        CopilotAuthorizer::CODE_NO_CARE_RELATIONSHIP => [403, 'No care relationship with the selected patient was found.'],
        self::CODE_NOT_PERMITTED => [403, 'Filing lab results requires lab write and sign permissions.'],
        DocumentFileController::CODE_ACCESS_DENIED => [403, 'Your role does not permit viewing this document.'],
        ValueFiler::ERROR_NOT_FOUND => [404, 'The value was not found.'],
        ValueFiler::ERROR_NOT_EXTRACTED => [409, 'This document is not ready for filing.'],
        ValueFiler::ERROR_NOT_FILEABLE => [409, 'Values from this kind of document are not filed.'],
        ValueFiler::ERROR_REJECTED => [409, 'This value was rejected and cannot be filed.'],
        ValueFiler::ERROR_ALREADY_FILED => [409, 'This value is already filed.'],
        ValueFiler::ERROR_CONFIRMATION_REQUIRED => [422, 'This value could not be verified on the page; confirm to file it.'],
        ValueFiler::ERROR_VALUE_REQUIRED => [422, 'This value could not be read; enter the value to file it.'],
        ValueFiler::ERROR_NO_COLLECTION_DATE => [422, 'The document gives no collection date for this value.'],
        self::CODE_INVALID_REQUEST => [400, 'The request body is not valid.'],
        SchemaStatusInterface::CODE_NOT_INSTALLED => [503, 'The Co-Pilot tables are not installed; filing is disabled.'],
        self::CODE_UNAVAILABLE => [503, 'The document store could not be read.'],
        self::CODE_FAILED => [500, 'The value could not be filed; nothing was saved.'],
    ];

    /** @var callable(string, string, bool, string, int): void */
    private $auditWriter;

    private readonly LoggerInterface $logger;

    public function __construct(
        private readonly CopilotAuthorizer $authorizer,
        private readonly AclCheckerInterface $acl,
        private readonly AclWriteCheckerInterface $writeAcl,
        private readonly SchemaStatusInterface $schema,
        private readonly DocumentFileSourceInterface $documents,
        private readonly ValueFiler $filer,
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
     * @param array<mixed>|null $body  the decoded JSON body; null when absent or not an object
     * @return array{status:int, body:array<string,mixed>, headers:array<string,string>}
     */
    public function fileForSession(array $session, int $documentId, int $resultIndex, ?array $body): array
    {
        return $this->handle(self::AUDIT_FILED, $session, $documentId, $resultIndex, $body);
    }

    /**
     * @param array{authUserID?:mixed, authUser?:mixed, pid?:mixed} $session
     * @return array{status:int, body:array<string,mixed>, headers:array<string,string>}
     */
    public function rejectForSession(array $session, int $documentId, int $resultIndex): array
    {
        return $this->handle(self::AUDIT_REJECTED, $session, $documentId, $resultIndex, null);
    }

    /** `POST /api/copilot/documents/:did/values/:idx/file` under the local API bridge. */
    public function handleFileRest(string $documentId, string $resultIndex, HttpRestRequest $request): JsonResponse
    {
        $json = json_decode($request->getContent(), true, 4);
        return self::respond($this->fileForSession(self::session($request), self::id($documentId), self::index($resultIndex), is_array($json) ? $json : null));
    }

    /** `POST /api/copilot/documents/:did/values/:idx/reject` under the local API bridge. */
    public function handleRejectRest(string $documentId, string $resultIndex, HttpRestRequest $request): JsonResponse
    {
        return self::respond($this->rejectForSession(self::session($request), self::id($documentId), self::index($resultIndex)));
    }

    /**
     * @param array{authUserID?:mixed, authUser?:mixed, pid?:mixed} $session
     * @param array<mixed>|null $body
     * @return array{status:int, body:array<string,mixed>, headers:array<string,string>}
     */
    private function handle(string $event, array $session, int $documentId, int $resultIndex, ?array $body): array
    {
        $correlationId = Uuid::uuid4()->toString();
        $headers = BriefingTicketController::RESPONSE_HEADERS + [BriefingTicketController::CORRELATION_HEADER => $correlationId];

        $userId = Scalar::positiveIntOrNull($session['authUserID'] ?? null);
        $usernameRaw = Scalar::str($session['authUser'] ?? null);
        $username = $usernameRaw === '' ? null : $usernameRaw;
        $pid = Scalar::positiveIntOrNull($session['pid'] ?? null);

        $decision = $this->authorizer->authorize($userId, $username, $pid);
        if (!$decision['allowed']) {
            $this->logger->info('copilot filing denied', ['cid' => $correlationId, 'code' => $decision['code']]);
            if ($pid !== null && $username !== null && $decision['code'] !== CopilotAuthorizer::CODE_NOT_AUTHENTICATED) {
                ($this->auditWriter)($event, $username, false, "cid={$correlationId}; code={$decision['code']}", $pid);
            }
            return $this->error($decision['code'], $correlationId, $headers);
        }
        assert($userId !== null && $pid !== null && $username !== null);

        $ref = 'document_id=' . ($documentId > 0 ? $documentId : 'invalid') . '; result_index=' . ($resultIndex >= 0 ? $resultIndex : 'invalid');
        $audit = function (bool $success, string $outcome, string $extra = '') use ($event, $username, $pid, $correlationId, $decision, $ref): void {
            ($this->auditWriter)($event, $username, $success, "cid={$correlationId}; basis={$decision['basis']}; {$ref}; outcome={$outcome}{$extra}", $pid);
            $this->logger->info('copilot filing', ['cid' => $correlationId, 'event' => $event, 'outcome' => $outcome]);
        };

        if (!$this->writeAcl->checkWrite('patients', 'lab', $username) || !$this->acl->check('patients', 'sign', $username)) {
            $audit(false, self::CODE_NOT_PERMITTED);
            return $this->error(self::CODE_NOT_PERMITTED, $correlationId, $headers);
        }
        if (!$this->schema->isReady()) {
            $audit(false, SchemaStatusInterface::CODE_NOT_INSTALLED);
            return $this->error(SchemaStatusInterface::CODE_NOT_INSTALLED, $correlationId, $headers);
        }

        $filedValue = null;
        $confirm = false;
        if ($event === self::AUDIT_FILED) {
            $parsed = self::parseBody($body);
            if ($parsed === null) {
                $audit(false, self::CODE_INVALID_REQUEST);
                return $this->error(self::CODE_INVALID_REQUEST, $correlationId, $headers);
            }
            [$filedValue, $confirm] = $parsed;
        }

        try {
            if ($documentId <= 0 || $resultIndex < 0 || $this->documents->findForPatient($documentId, $pid) === null) {
                $audit(false, ValueFiler::ERROR_NOT_FOUND);
                return $this->error(ValueFiler::ERROR_NOT_FOUND, $correlationId, $headers);
            }
            if (!$this->documents->canAccess($documentId, $username)) {
                $audit(false, DocumentFileController::CODE_ACCESS_DENIED);
                return $this->error(DocumentFileController::CODE_ACCESS_DENIED, $correlationId, $headers);
            }
        } catch (SourceUnavailableException $e) {
            $this->logger->error('copilot source unavailable', ['cid' => $correlationId, 'source' => $e->getSource()]);
            $audit(false, self::CODE_UNAVAILABLE);
            return $this->error(self::CODE_UNAVAILABLE, $correlationId, $headers);
        }

        try {
            $result = $event === self::AUDIT_FILED
                ? $this->filer->file($pid, $userId, $documentId, $resultIndex, $filedValue, $confirm)
                : $this->filer->reject($pid, $documentId, $resultIndex);
        } catch (Throwable $e) {
            $this->logger->error('copilot filing failed', ['cid' => $correlationId, 'type' => $e::class]);
            $audit(false, self::CODE_FAILED);
            return $this->error(self::CODE_FAILED, $correlationId, $headers);
        }

        $outcome = $result['outcome'];
        $success = in_array($outcome, [ValueFiler::OUTCOME_FILED, ValueFiler::OUTCOME_ALREADY_FILED, ValueFiler::OUTCOME_REJECTED, ValueFiler::OUTCOME_ALREADY_REJECTED], true);
        $extra = ($result['verification_status'] !== null ? '; verification=' . $result['verification_status'] : '')
            . ($result['procedure_result_id'] !== null ? '; procedure_result_id=' . $result['procedure_result_id'] : '')
            . ($result['result_status'] !== null ? '; result_status=' . $result['result_status'] : '')
            . ($result['warning'] !== null ? '; warning=' . $result['warning'] : '');
        $audit($success, $outcome, $extra);
        if (!$success) {
            return $this->error($outcome, $correlationId, $headers);
        }

        $filed = $event === self::AUDIT_FILED;
        return [
            'status' => 200,
            'body' => [
                'correlation_id' => $correlationId,
                'status' => $filed ? 'filed' : 'rejected',
                'document_id' => $documentId,
                'result_index' => $resultIndex,
                'procedure_result_id' => $result['procedure_result_id'],
                'already_filed' => $outcome === ValueFiler::OUTCOME_ALREADY_FILED,
                'already_rejected' => $outcome === ValueFiler::OUTCOME_ALREADY_REJECTED,
                'result_status' => $result['result_status'],
                'warning' => $result['warning'],
            ],
            'headers' => $headers,
        ];
    }

    /**
     * Both fields optional; `filed_value` a string of at most 255 characters or
     * null, `confirm_unverified` a boolean.
     *
     * @param array<mixed>|null $body
     * @return array{?string, bool}|null  null when invalid
     */
    private static function parseBody(?array $body): ?array
    {
        if ($body === null) {
            return null;
        }
        $value = $body['filed_value'] ?? null;
        $confirm = $body['confirm_unverified'] ?? false;
        if (($value !== null && !is_string($value)) || !is_bool($confirm)) {
            return null;
        }
        if (is_string($value) && mb_strlen($value) > self::FILED_VALUE_MAX_LENGTH) {
            return null;
        }
        return [$value, $confirm];
    }

    private static function id(string $raw): int
    {
        return preg_match('/^[1-9]\d{0,18}$/', $raw) === 1 ? (int) $raw : 0;
    }

    private static function index(string $raw): int
    {
        return preg_match('/^\d{1,5}$/', $raw) === 1 ? (int) $raw : -1;
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
        [$status, $message] = self::ERRORS[$code] ?? [500, 'The request could not be completed.'];
        return [
            'status' => $status,
            'body' => ['detail' => ['code' => $code, 'message' => $message, 'correlation_id' => $correlationId]],
            'headers' => $headers,
        ];
    }
}
