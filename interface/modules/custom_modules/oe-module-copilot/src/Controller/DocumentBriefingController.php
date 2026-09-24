<?php

/**
 * `POST /api/copilot/document-briefing` (Week 2): brief the physician from the
 * selected patient's most recent lab document on file.
 *
 * Authorization is the ticket route's, unchanged: the patient is the one
 * selected in the authenticated OpenEMR session; a `pid` in the body is only a
 * staleness check (mismatch -> 409); `CopilotAuthorizer` applies the ACL and
 * care-relationship checks; CSRF is enforced by the local API bridge
 * (APICSRFTOKEN) before this controller runs.
 *
 * The document comes from OpenEMR's own Documents feature (Data\SqlDocumentReader).
 * Its bytes go server to server to the agent's `POST /v1/documents/briefing`,
 * signed like the bundle hand-off, identified by `patient_uuid` - never `pid`.
 * The agent's DocumentBriefingResponse is returned to the browser as-is.
 *
 * Nothing on file, an unreadable document store, or an unreachable agent is a
 * 200 with `status: "degraded"` and a fixed `degraded_reason`, never a raw
 * exception. One audit row (`copilot-document-briefing`) records user, patient,
 * basis and outcome codes; document bytes, extracted values and the patient
 * uuid are never logged.
 *
 * @package   OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Controller;

use OpenEMR\BC\ServiceContainer;
use OpenEMR\Common\Http\HttpRestRequest;
use OpenEMR\Common\Logging\EventAuditLogger;
use OpenEMR\Modules\Copilot\Agent\AgentClientInterface;
use OpenEMR\Modules\Copilot\Agent\AgentUnavailableException;
use OpenEMR\Modules\Copilot\Authorization\CopilotAuthorizer;
use OpenEMR\Modules\Copilot\Data\ClinicalReaderInterface;
use OpenEMR\Modules\Copilot\Data\SourceUnavailableException;
use OpenEMR\Modules\Copilot\Data\SqlDocumentReader;
use OpenEMR\Modules\Copilot\Support\Scalar;
use Psr\Log\LoggerInterface;
use Ramsey\Uuid\Uuid;
use Symfony\Component\HttpFoundation\JsonResponse;
use Throwable;

final class DocumentBriefingController
{
    public const AUDIT_EVENT = 'copilot-document-briefing';

    public const DEGRADED_NO_DOCUMENT = 'no_document_on_file';
    public const DEGRADED_DOCUMENT_UNAVAILABLE = 'document_unavailable';
    public const DEGRADED_AGENT_UNAVAILABLE = 'agent_unavailable';

    /** Matches DocumentBriefingRequest.question's max_length. */
    public const QUESTION_MAX_LENGTH = 500;

    private const ERRORS = [
        CopilotAuthorizer::CODE_NOT_AUTHENTICATED => [401, 'Authentication is required.'],
        CopilotAuthorizer::CODE_NO_PATIENT_SELECTED => [409, 'No patient is selected in this session.'],
        CopilotAuthorizer::CODE_ACL_DENIED => [403, 'Your role does not permit reading this patient context.'],
        CopilotAuthorizer::CODE_NO_CARE_RELATIONSHIP => [403, 'No care relationship with the selected patient was found.'],
        'patient_mismatch' => [409, 'The selected patient changed; reload the patient summary.'],
        'patient_not_found' => [404, 'The selected patient record could not be found.'],
        'internal_error' => [500, 'The document briefing could not be prepared.'],
    ];

    /** @var callable(string, string, bool, string, int): void */
    private $auditWriter;

    private readonly LoggerInterface $logger;

    public function __construct(
        private readonly CopilotAuthorizer $authorizer,
        private readonly ClinicalReaderInterface $reader,
        private readonly SqlDocumentReader $documents,
        private readonly AgentClientInterface $agent,
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
    public function handleForSession(array $session, ?int $requestedPid = null, ?string $question = null): array
    {
        $correlationId = Uuid::uuid4()->toString();
        $headers = BriefingTicketController::RESPONSE_HEADERS + [BriefingTicketController::CORRELATION_HEADER => $correlationId];

        $userId = Scalar::positiveIntOrNull($session['authUserID'] ?? null);
        $usernameRaw = Scalar::str($session['authUser'] ?? null);
        $username = $usernameRaw === '' ? null : $usernameRaw;
        $pid = Scalar::positiveIntOrNull($session['pid'] ?? null);

        $decision = $this->authorizer->authorize($userId, $username, $pid);
        if (!$decision['allowed']) {
            $this->logger->info('copilot document briefing denied', ['cid' => $correlationId, 'code' => $decision['code']]);
            if ($pid !== null && $username !== null && $decision['code'] !== CopilotAuthorizer::CODE_NOT_AUTHENTICATED) {
                ($this->auditWriter)(self::AUDIT_EVENT, $username, false, "cid={$correlationId}; code={$decision['code']}", $pid);
            }
            return $this->error($decision['code'], $correlationId, $headers);
        }
        assert($userId !== null && $pid !== null && $username !== null);

        if ($requestedPid !== null && $requestedPid !== $pid) {
            $this->logger->info('copilot document briefing denied', ['cid' => $correlationId, 'code' => 'patient_mismatch']);
            ($this->auditWriter)(self::AUDIT_EVENT, $username, false, "cid={$correlationId}; code=patient_mismatch", $pid);
            return $this->error('patient_mismatch', $correlationId, $headers);
        }

        $documentId = null;
        $patientUuid = null;
        $outcome = 'ok';
        try {
            $patient = $this->reader->findPatient($pid);
            if ($patient === null) {
                return $this->error('patient_not_found', $correlationId, $headers);
            }
            $patientUuid = $patient['uuid'];

            $document = $this->documents->findLatestDocument($pid);
            if ($document === null) {
                $outcome = self::DEGRADED_NO_DOCUMENT;
                $body = self::degraded($correlationId, $patientUuid, null, $outcome);
            } else {
                $documentId = $document['document_id'];
                $question = $question === null ? null : trim($question);
                $request = [
                    'correlation_id' => $correlationId,
                    'patient_uuid' => $patientUuid,
                    'document_id' => $documentId,
                    'media_type' => $document['media_type'],
                    'document_base64' => base64_encode($document['bytes']),
                    'question' => $question === null || $question === '' ? null : mb_substr($question, 0, self::QUESTION_MAX_LENGTH),
                ];
                unset($document);
                try {
                    $body = $this->agent->postDocumentBriefing($request, $correlationId);
                    $outcome = 'agent_' . Scalar::str($body['status'] ?? 'unknown');
                } catch (AgentUnavailableException $e) {
                    $outcome = $e->getReason();
                    $this->logger->warning('copilot document briefing hand-off failed', [
                        'cid' => $correlationId,
                        'reason' => $e->getReason(),
                        'http_status' => $e->getHttpStatus(),
                    ]);
                    $body = self::degraded($correlationId, $patientUuid, $documentId, self::DEGRADED_AGENT_UNAVAILABLE);
                }
                unset($request);
            }
        } catch (SourceUnavailableException $e) {
            $this->logger->error('copilot source unavailable', ['cid' => $correlationId, 'source' => $e->getSource()]);
            $outcome = self::DEGRADED_DOCUMENT_UNAVAILABLE;
            $body = self::degraded($correlationId, $patientUuid, null, $outcome);
        } catch (Throwable $e) {
            $this->logger->error('copilot document briefing failed', ['cid' => $correlationId, 'type' => $e::class]);
            return $this->error('internal_error', $correlationId, $headers);
        }

        ($this->auditWriter)(
            self::AUDIT_EVENT,
            $username,
            true,
            "cid={$correlationId}; basis={$decision['basis']}; document_id=" . ($documentId ?? 'none') . "; outcome={$outcome}",
            $pid
        );
        $this->logger->info('copilot document briefing', ['cid' => $correlationId, 'basis' => $decision['basis'], 'outcome' => $outcome]);

        return ['status' => 200, 'body' => $body, 'headers' => $headers];
    }

    /** REST route adapter: `POST /api/copilot/document-briefing` under the local API bridge. */
    public function handleRest(HttpRestRequest $request): JsonResponse
    {
        $session = $request->getSession();
        $requestedPid = null;
        $question = null;
        // getRequestBodyJSON() is broken in this checkout; read the raw body.
        $json = json_decode($request->getContent(), true, 4);
        if (is_array($json)) {
            $requestedPid = Scalar::positiveIntOrNull($json['pid'] ?? null);
            $question = is_string($json['question'] ?? null) ? $json['question'] : null;
        }
        $result = $this->handleForSession([
            'authUserID' => $session->get('authUserID'),
            'authUser' => $session->get('authUser'),
            'pid' => $session->get('pid'),
        ], $requestedPid, $question);
        if (!headers_sent()) {
            header_remove('Cache-Control');
        }
        return new JsonResponse($result['body'], $result['status'], $result['headers']);
    }

    /**
     * A DocumentBriefingResponse-shaped body for a failure on this side.
     *
     * @return array<string,mixed>
     */
    private static function degraded(string $correlationId, ?string $patientUuid, ?int $documentId, string $reason): array
    {
        return [
            'correlation_id' => $correlationId,
            'patient_uuid' => $patientUuid,
            'document_id' => $documentId,
            'status' => 'degraded',
            'degraded_reason' => $reason,
            'briefing' => null,
            'rendered_text' => '',
            'provenance' => null,
        ];
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
