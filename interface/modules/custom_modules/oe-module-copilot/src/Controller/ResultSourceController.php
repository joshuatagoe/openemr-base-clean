<?php

/**
 * `GET /api/copilot/results/{procedure_result_id}/source` (ADR-009 7b): the
 * source document of a chart result filed from a document, so the panel can
 * open the page and box a filed value came from. Filed results carry no core
 * document link (`procedure_result.document_id = 0`); the link is the Co-Pilot
 * candidate row (`copilot_extracted_value.document_id` + `procedure_result_id`).
 *
 * The briefing routes' authorizer on the session's selected patient; the
 * candidate must be that patient's and the document still filed to them in
 * OpenEMR (one 404 for missing and another patient's), and core
 * `can_access()` must allow it (403). One audit row, ids and codes only.
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
use OpenEMR\Modules\Copilot\Documents\CandidateMapper;
use OpenEMR\Modules\Copilot\Documents\DocumentFileSourceInterface;
use OpenEMR\Modules\Copilot\Filing\FilingStoreInterface;
use OpenEMR\Modules\Copilot\Support\Scalar;
use OpenEMR\Modules\Copilot\Support\SessionRelease;
use Psr\Log\LoggerInterface;
use Ramsey\Uuid\Uuid;
use Symfony\Component\HttpFoundation\JsonResponse;
use Throwable;

final class ResultSourceController
{
    public const AUDIT_EVENT = 'copilot-result-source';
    public const CODE_NOT_FOUND = 'source_not_found';

    private const ERRORS = [
        CopilotAuthorizer::CODE_NOT_AUTHENTICATED => [401, 'Authentication is required.'],
        CopilotAuthorizer::CODE_NO_PATIENT_SELECTED => [409, 'No patient is selected in this session.'],
        CopilotAuthorizer::CODE_ACL_DENIED => [403, 'Your role does not permit reading this patient context.'],
        CopilotAuthorizer::CODE_NO_CARE_RELATIONSHIP => [403, 'No care relationship with the selected patient was found.'],
        self::CODE_NOT_FOUND => [404, 'No source document was found for this result.'],
        DocumentFileController::CODE_ACCESS_DENIED => [403, 'Your role does not permit viewing this document.'],
        'internal_error' => [500, 'The source could not be looked up.'],
    ];

    /** @var callable(string, string, bool, string, int): void */
    private $auditWriter;

    private readonly LoggerInterface $logger;

    public function __construct(
        private readonly CopilotAuthorizer $authorizer,
        private readonly FilingStoreInterface $store,
        private readonly DocumentFileSourceInterface $documents,
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
    public function sourceForSession(array $session, int $procedureResultId): array
    {
        $correlationId = Uuid::uuid4()->toString();
        $headers = BriefingTicketController::RESPONSE_HEADERS + [BriefingTicketController::CORRELATION_HEADER => $correlationId];

        $userId = Scalar::positiveIntOrNull($session['authUserID'] ?? null);
        $usernameRaw = Scalar::str($session['authUser'] ?? null);
        $username = $usernameRaw === '' ? null : $usernameRaw;
        $pid = Scalar::positiveIntOrNull($session['pid'] ?? null);

        $decision = $this->authorizer->authorize($userId, $username, $pid);
        if (!$decision['allowed']) {
            if ($pid !== null && $username !== null && $decision['code'] !== CopilotAuthorizer::CODE_NOT_AUTHENTICATED) {
                ($this->auditWriter)(self::AUDIT_EVENT, $username, false, "cid={$correlationId}; code={$decision['code']}", $pid);
            }
            return $this->error($decision['code'], $correlationId, $headers);
        }
        assert($pid !== null && $username !== null);

        $source = null;
        try {
            $row = $procedureResultId > 0 ? $this->store->findResultSource($procedureResultId) : null;
            if ($row === null || $row['pid'] !== $pid || $this->documents->findForPatient($row['document_id'], $pid) === null) {
                $outcome = self::CODE_NOT_FOUND;
            } elseif (!$this->documents->canAccess($row['document_id'], $username)) {
                $outcome = DocumentFileController::CODE_ACCESS_DENIED;
            } else {
                $outcome = 'ok';
                $source = $row;
            }
        } catch (Throwable $e) {
            $this->logger->error('copilot result source failed', ['cid' => $correlationId, 'type' => $e::class]);
            $outcome = 'internal_error';
        }

        ($this->auditWriter)(
            self::AUDIT_EVENT,
            $username,
            $outcome === 'ok',
            "cid={$correlationId}; basis={$decision['basis']}; procedure_result_id={$procedureResultId}; outcome={$outcome}" . ($source !== null ? "; document_id={$source['document_id']}" : ''),
            $pid
        );
        if ($source === null) {
            return $this->error($outcome, $correlationId, $headers);
        }
        return [
            'status' => 200,
            'body' => [
                'correlation_id' => $correlationId,
                'procedure_result_id' => $procedureResultId,
                'document_id' => $source['document_id'],
                'result_index' => $source['result_index'],
                'page' => $source['page'],
                'bbox' => CandidateMapper::bboxToList($source['bbox']),
                'status' => $source['status'],
            ],
            'headers' => $headers,
        ];
    }

    /** `GET /api/copilot/results/:rid/source` under the local API bridge. */
    public function handleRest(string $resultId, HttpRestRequest $request): JsonResponse
    {
        $id = preg_match('/^[1-9]\d{0,18}$/', $resultId) === 1 ? (int) $resultId : 0;
        $result = $this->sourceForSession(SessionRelease::readAndRelease($request->getSession()), $id);
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
