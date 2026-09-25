<?php

/**
 * `GET /api/copilot/documents/{document_id}/values` (Wave 2 step 2): one
 * document's candidate values for the panel's value list and viewer - value,
 * unit, range, flag and its source, collection date, verification status,
 * page and box, and where each stands (candidate / filed / rejected / unfiled).
 * Read-only.
 *
 * The file route's rules: the briefing routes' CopilotAuthorizer on the
 * session's selected patient (CSRF by the local API bridge); the document must
 * be filed to that patient in OpenEMR, not deleted, of an allow-listed type -
 * one 404 for a missing document and another patient's, checked before any
 * value is read; core `can_access()` (403). The session lock is released
 * before any database work. A document held for an identity mismatch
 * (ADR-012) answers with its status and no values: held facts are not shown
 * until a clinician confirms the patient. One audit row per authorized call
 * (`copilot-document-values`) with ids, codes and a count only; the
 * application log carries the correlation id and codes only.
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
use OpenEMR\Modules\Copilot\Data\SchemaStatusInterface;
use OpenEMR\Modules\Copilot\Data\SourceUnavailableException;
use OpenEMR\Modules\Copilot\Documents\CandidateMapper;
use OpenEMR\Modules\Copilot\Documents\DocumentFileSourceInterface;
use OpenEMR\Modules\Copilot\Documents\DocumentValuesReaderInterface;
use OpenEMR\Modules\Copilot\Documents\ProcessingRepositoryInterface;
use OpenEMR\Modules\Copilot\Support\Scalar;
use OpenEMR\Modules\Copilot\Support\SessionRelease;
use Psr\Log\LoggerInterface;
use Ramsey\Uuid\Uuid;
use Symfony\Component\HttpFoundation\JsonResponse;
use Throwable;

/** @phpstan-import-type ValueRow from DocumentValuesReaderInterface */
final class DocumentValuesController
{
    public const AUDIT_EVENT = 'copilot-document-values';

    private const ERRORS = [
        CopilotAuthorizer::CODE_NOT_AUTHENTICATED => [401, 'Authentication is required.'],
        CopilotAuthorizer::CODE_NO_PATIENT_SELECTED => [409, 'No patient is selected in this session.'],
        CopilotAuthorizer::CODE_ACL_DENIED => [403, 'Your role does not permit reading this patient context.'],
        CopilotAuthorizer::CODE_NO_CARE_RELATIONSHIP => [403, 'No care relationship with the selected patient was found.'],
        DocumentFileController::CODE_NOT_FOUND => [404, 'The document was not found.'],
        DocumentFileController::CODE_ACCESS_DENIED => [403, 'Your role does not permit viewing this document.'],
        SchemaStatusInterface::CODE_NOT_INSTALLED => [503, 'The Co-Pilot tables are not installed.'],
        DocumentFileController::CODE_UNAVAILABLE => [503, 'The document store could not be read.'],
        'internal_error' => [500, 'The values could not be read.'],
    ];

    /** @var callable(string, string, bool, string, int): void */
    private $auditWriter;

    private readonly LoggerInterface $logger;

    public function __construct(
        private readonly CopilotAuthorizer $authorizer,
        private readonly SchemaStatusInterface $schema,
        private readonly DocumentFileSourceInterface $documents,
        private readonly DocumentValuesReaderInterface $values,
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
    public function valuesForSession(array $session, int $documentId): array
    {
        $correlationId = Uuid::uuid4()->toString();
        $headers = BriefingTicketController::RESPONSE_HEADERS + [BriefingTicketController::CORRELATION_HEADER => $correlationId];

        $userId = Scalar::positiveIntOrNull($session['authUserID'] ?? null);
        $usernameRaw = Scalar::str($session['authUser'] ?? null);
        $username = $usernameRaw === '' ? null : $usernameRaw;
        $pid = Scalar::positiveIntOrNull($session['pid'] ?? null);

        $decision = $this->authorizer->authorize($userId, $username, $pid);
        if (!$decision['allowed']) {
            $this->logger->info('copilot document values denied', ['cid' => $correlationId, 'code' => $decision['code']]);
            if ($pid !== null && $username !== null && $decision['code'] !== CopilotAuthorizer::CODE_NOT_AUTHENTICATED) {
                ($this->auditWriter)(self::AUDIT_EVENT, $username, false, "cid={$correlationId}; code={$decision['code']}", $pid);
            }
            return $this->error($decision['code'], $correlationId, $headers);
        }
        assert($pid !== null && $username !== null);

        $ref = 'document_id=' . ($documentId > 0 ? $documentId : 'invalid');
        $audit = function (bool $success, string $outcome, string $extra = '') use ($username, $pid, $correlationId, $decision, $ref): void {
            ($this->auditWriter)(self::AUDIT_EVENT, $username, $success, "cid={$correlationId}; basis={$decision['basis']}; {$ref}; outcome={$outcome}{$extra}", $pid);
            $this->logger->info('copilot document values', ['cid' => $correlationId, 'outcome' => $outcome]);
        };

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
            $record = $this->values->findRecord($documentId, $pid);
            $held = $record !== null && $record['status'] === ProcessingRepositoryInterface::STATUS_HELD_IDENTITY;
            $rows = $record === null || $held ? [] : $this->values->listValues($documentId, $pid);
        } catch (SourceUnavailableException $e) {
            $this->logger->error('copilot source unavailable', ['cid' => $correlationId, 'source' => $e->getSource()]);
            $audit(false, DocumentFileController::CODE_UNAVAILABLE);
            return $this->error(DocumentFileController::CODE_UNAVAILABLE, $correlationId, $headers);
        } catch (Throwable $e) {
            $this->logger->error('copilot document values failed', ['cid' => $correlationId, 'type' => $e::class]);
            $audit(false, 'internal_error');
            return $this->error('internal_error', $correlationId, $headers);
        }

        $values = array_map(static fn(array $v): array => [
            'result_index' => $v['result_index'],
            'test_name' => $v['test_name'],
            'value_text' => $v['value_text'],
            'unit' => $v['unit'],
            'reference_range' => $v['reference_range'],
            'abnormal_flag' => $v['abnormal_flag'],
            'flag_source' => $v['flag_source'],
            'collection_date' => $v['collection_date'],
            'verification_status' => $v['verification_status'],
            'page' => $v['page'],
            'bbox' => CandidateMapper::bboxToList($v['bbox']),
            'status' => $v['status'],
            'procedure_result_id' => $v['procedure_result_id'],
        ], $rows);

        $audit(true, 'ok', '; record=' . ($record === null ? 'none' : $record['status']) . '; values=' . count($values) . ($held ? '; withheld=held_identity' : ''));
        return [
            'status' => 200,
            'body' => [
                'correlation_id' => $correlationId,
                'document_id' => $documentId,
                'status' => $record['status'] ?? null,
                'identity_check' => $record['identity_check'] ?? null,
                'error_code' => $record['last_error_code'] ?? null,
                'values' => $values,
            ],
            'headers' => $headers,
        ];
    }

    /** `GET /api/copilot/documents/:did/values` under the local API bridge. */
    public function handleRest(string $documentId, HttpRestRequest $request): JsonResponse
    {
        $id = preg_match('/^[1-9]\d{0,18}$/', $documentId) === 1 ? (int) $documentId : 0;
        $result = $this->valuesForSession(SessionRelease::readAndRelease($request->getSession()), $id);
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
