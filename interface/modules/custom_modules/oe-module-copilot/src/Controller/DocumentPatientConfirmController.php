<?php

/**
 * "This is the right patient" (ADR-012 §4a):
 *
 *   POST /api/copilot/documents/{document_id}/confirm-patient   body {"confirm": true}
 *
 * Resolves a document held for an identity check (`held_identity`: the printed
 * name/DOB did not match, or the same file is live in another chart without a
 * matching printed identity). The document moves to `extracted`; its recorded
 * identity result (`identity_check`) is kept as history and the resolution is
 * recorded as the fixed code `identity_confirmed_by_clinician` in its code
 * field (which held the hold code; the hold code goes to the audit row). Its
 * candidate values then become pending facts like any read document's.
 *
 * Authorization: the briefing routes' CopilotAuthorizer on the session's
 * selected patient (CSRF by the local API bridge), then the filing permissions
 * - `patients/lab` write AND `patients/sign` - since the confirmation lets the
 * document's values be filed. The document must be filed to that patient in
 * OpenEMR (one 404 for a missing document and another patient's) and pass core
 * `can_access()` (403); anything but a held record of this patient is 409
 * `not_held`. The body must be exactly an explicit `"confirm": true` (422
 * otherwise). The state change is one conditional update, so two clicks
 * cannot both resolve it. The session lock is released before any database
 * work. One audit row per authorized call (`copilot-document-patient-confirmed`)
 * with ids and codes only - document id, the hold code and identity result -
 * the clinician and time come with the row.
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
use OpenEMR\Modules\Copilot\Documents\ProcessingRepositoryInterface;
use OpenEMR\Modules\Copilot\Support\Scalar;
use OpenEMR\Modules\Copilot\Support\SessionRelease;
use Psr\Log\LoggerInterface;
use Ramsey\Uuid\Uuid;
use Symfony\Component\HttpFoundation\JsonResponse;
use Throwable;

final class DocumentPatientConfirmController
{
    public const AUDIT_EVENT = 'copilot-document-patient-confirmed';

    /** The resolution recorded on the document (its code field) and as the audit outcome. */
    public const CODE_CONFIRMED = 'identity_confirmed_by_clinician';

    public const CODE_NOT_PERMITTED = 'confirm_not_permitted';
    public const CODE_NOT_HELD = 'not_held';
    public const CODE_CONFIRMATION_REQUIRED = 'confirmation_required';
    public const CODE_FAILED = 'confirm_failed';

    private const ERRORS = [
        CopilotAuthorizer::CODE_NOT_AUTHENTICATED => [401, 'Authentication is required.'],
        CopilotAuthorizer::CODE_NO_PATIENT_SELECTED => [409, 'No patient is selected in this session.'],
        CopilotAuthorizer::CODE_ACL_DENIED => [403, 'Your role does not permit reading this patient context.'],
        CopilotAuthorizer::CODE_NO_CARE_RELATIONSHIP => [403, 'No care relationship with the selected patient was found.'],
        self::CODE_NOT_PERMITTED => [403, 'Confirming the patient of a held document requires lab write and sign permissions.'],
        DocumentFileController::CODE_NOT_FOUND => [404, 'The document was not found.'],
        DocumentFileController::CODE_ACCESS_DENIED => [403, 'Your role does not permit viewing this document.'],
        self::CODE_NOT_HELD => [409, 'This document is not held for an identity check.'],
        self::CODE_CONFIRMATION_REQUIRED => [422, 'Confirm explicitly that this document is this patient\'s.'],
        SchemaStatusInterface::CODE_NOT_INSTALLED => [503, 'The Co-Pilot tables are not installed.'],
        DocumentFileController::CODE_UNAVAILABLE => [503, 'The document store could not be read.'],
        self::CODE_FAILED => [500, 'The confirmation could not be saved; nothing was changed.'],
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
        private readonly ProcessingRepositoryInterface $records,
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
    public function confirmForSession(array $session, int $documentId, ?array $body): array
    {
        $correlationId = Uuid::uuid4()->toString();
        $headers = BriefingTicketController::RESPONSE_HEADERS + [BriefingTicketController::CORRELATION_HEADER => $correlationId];

        $userId = Scalar::positiveIntOrNull($session['authUserID'] ?? null);
        $usernameRaw = Scalar::str($session['authUser'] ?? null);
        $username = $usernameRaw === '' ? null : $usernameRaw;
        $pid = Scalar::positiveIntOrNull($session['pid'] ?? null);

        $decision = $this->authorizer->authorize($userId, $username, $pid);
        if (!$decision['allowed']) {
            $this->logger->info('copilot confirm patient denied', ['cid' => $correlationId, 'code' => $decision['code']]);
            if ($pid !== null && $username !== null && $decision['code'] !== CopilotAuthorizer::CODE_NOT_AUTHENTICATED) {
                ($this->auditWriter)(self::AUDIT_EVENT, $username, false, "cid={$correlationId}; code={$decision['code']}", $pid);
            }
            return $this->error($decision['code'], $correlationId, $headers);
        }
        assert($pid !== null && $username !== null);

        $ref = 'document_id=' . ($documentId > 0 ? $documentId : 'invalid');
        $audit = function (bool $success, string $outcome, string $extra = '') use ($username, $pid, $correlationId, $decision, $ref): void {
            ($this->auditWriter)(self::AUDIT_EVENT, $username, $success, "cid={$correlationId}; basis={$decision['basis']}; {$ref}; outcome={$outcome}{$extra}", $pid);
            $this->logger->info('copilot confirm patient', ['cid' => $correlationId, 'outcome' => $outcome]);
        };

        if (!$this->writeAcl->checkWrite('patients', 'lab', $username) || !$this->acl->check('patients', 'sign', $username)) {
            $audit(false, self::CODE_NOT_PERMITTED);
            return $this->error(self::CODE_NOT_PERMITTED, $correlationId, $headers);
        }
        if (!$this->schema->isReady()) {
            $audit(false, SchemaStatusInterface::CODE_NOT_INSTALLED);
            return $this->error(SchemaStatusInterface::CODE_NOT_INSTALLED, $correlationId, $headers);
        }

        try {
            if ($documentId <= 0 || $this->documents->findForPatient($documentId, $pid) === null) {
                $audit(false, DocumentFileController::CODE_NOT_FOUND);
                return $this->error(DocumentFileController::CODE_NOT_FOUND, $correlationId, $headers);
            }
            if (!$this->documents->canAccess($documentId, $username)) {
                $audit(false, DocumentFileController::CODE_ACCESS_DENIED);
                return $this->error(DocumentFileController::CODE_ACCESS_DENIED, $correlationId, $headers);
            }
        } catch (SourceUnavailableException $e) {
            $this->logger->error('copilot source unavailable', ['cid' => $correlationId, 'source' => $e->getSource()]);
            $audit(false, DocumentFileController::CODE_UNAVAILABLE);
            return $this->error(DocumentFileController::CODE_UNAVAILABLE, $correlationId, $headers);
        }

        if (($body['confirm'] ?? null) !== true) {
            $audit(false, self::CODE_CONFIRMATION_REQUIRED);
            return $this->error(self::CODE_CONFIRMATION_REQUIRED, $correlationId, $headers);
        }

        try {
            $record = $this->records->findRecords($pid, [$documentId])[$documentId] ?? null;
            if ($record === null || $record['status'] !== ProcessingRepositoryInterface::STATUS_HELD_IDENTITY) {
                $audit(false, self::CODE_NOT_HELD, '; record=' . ($record === null ? 'none' : $record['status']));
                return $this->error(self::CODE_NOT_HELD, $correlationId, $headers);
            }
            $holdCode = $record['last_error_code'] ?? 'none';
            $identity = $record['identity_check'] ?? 'none';
            $detail = "; hold_code={$holdCode}; identity_check={$identity}";
            if (!$this->records->confirmHeldPatient($documentId, $pid, self::CODE_CONFIRMED)) {
                $audit(false, self::CODE_NOT_HELD, $detail . '; race=1');
                return $this->error(self::CODE_NOT_HELD, $correlationId, $headers);
            }
            $after = $this->records->findRecords($pid, [$documentId])[$documentId] ?? null;
        } catch (Throwable $e) {
            $this->logger->error('copilot confirm patient failed', ['cid' => $correlationId, 'type' => $e::class]);
            $audit(false, self::CODE_FAILED);
            return $this->error(self::CODE_FAILED, $correlationId, $headers);
        }

        $pending = $after['pending_count'] ?? 0;
        $audit(true, self::CODE_CONFIRMED, $detail . '; pending=' . $pending);
        return [
            'status' => 200,
            'body' => [
                'correlation_id' => $correlationId,
                'document_id' => $documentId,
                'status' => ProcessingRepositoryInterface::STATUS_EXTRACTED,
                'identity_check' => $record['identity_check'],
                'error_code' => self::CODE_CONFIRMED,
                'pending_count' => $pending,
            ],
            'headers' => $headers,
        ];
    }

    /** `POST /api/copilot/documents/:did/confirm-patient` under the local API bridge. */
    public function handleRest(string $documentId, HttpRestRequest $request): JsonResponse
    {
        $id = preg_match('/^[1-9]\d{0,18}$/', $documentId) === 1 ? (int) $documentId : 0;
        $json = json_decode($request->getContent(), true, 4);
        $result = $this->confirmForSession(SessionRelease::readAndRelease($request->getSession()), $id, is_array($json) ? $json : null);
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
        [$status, $message] = self::ERRORS[$code] ?? self::ERRORS[self::CODE_FAILED];
        return [
            'status' => $status,
            'body' => ['detail' => ['code' => $code, 'message' => $message, 'correlation_id' => $correlationId]],
            'headers' => $headers,
        ];
    }
}
