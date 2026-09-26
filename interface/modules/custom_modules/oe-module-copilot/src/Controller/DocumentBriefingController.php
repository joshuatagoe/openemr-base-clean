<?php

/**
 * `POST /api/copilot/document-briefing` (Week 2): brief the physician from the
 * selected patient's documents.
 *
 * With the module tables installed (ADR-012), the briefing covers every
 * extracted, non-held document: the stored extractions and the chart's lab
 * history (`prior_facts`) are sent (contract C4) and nothing is re-extracted;
 * when an intake form is among them, the chart's current medications go too
 * (`chart_medications`, ADR-010), read only, for the patient-reported conflict lines;
 * nothing extracted yet is `degraded` / `no_extracted_documents`; documents read
 * whose every value was already filed, rejected or un-filed is `degraded` /
 * `all_values_reviewed` (nothing new to brief - filed values are chart history). Until the
 * tables exist, the legacy single-document path below still runs (the agent
 * accepts `document_base64` for one more release).
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
use OpenEMR\Modules\Copilot\ContextBundleBuilder;
use OpenEMR\Modules\Copilot\Data\ClinicalReaderInterface;
use OpenEMR\Modules\Copilot\Data\SchemaStatusInterface;
use OpenEMR\Modules\Copilot\Documents\ProcessingRepositoryInterface;
use OpenEMR\Modules\Copilot\Data\SourceUnavailableException;
use OpenEMR\Modules\Copilot\Data\DocumentTooLargeException;
use OpenEMR\Modules\Copilot\Data\SqlDocumentReader;
use OpenEMR\Modules\Copilot\Support\Scalar;
use OpenEMR\Modules\Copilot\Support\SessionRelease;
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
    public const DEGRADED_DOCUMENT_TOO_LARGE = 'document_too_large';
    public const DEGRADED_NO_EXTRACTED_DOCUMENTS = 'no_extracted_documents';
    /** Documents were read, and every value read from them was filed, rejected or un-filed: nothing new to brief. */
    public const DEGRADED_ALL_VALUES_REVIEWED = 'all_values_reviewed';

    /** Stored-extraction mode: documents per briefing, and the chart lab history sent as prior_facts. */
    public const MAX_DOCUMENTS = 20;
    public const PRIOR_FACTS_SINCE = '1900-01-01 00:00:00';
    public const PRIOR_FACTS_LIMIT = 500;

    /**
     * ADR-010 medication conflicts: rows read (the reader's own cap) and current entries
     * sent as `chart_medications`. Matches DocumentBriefingRequest's MAX_CHART_MEDICATIONS.
     */
    public const MEDICATION_READ_LIMIT = 500;
    public const MAX_CHART_MEDICATIONS = 200;

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
        private readonly ?ProcessingRepositoryInterface $records = null,
        private readonly ?SchemaStatusInterface $schema = null,
        private readonly ?ContextBundleBuilder $builder = null,
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

            if ($this->usesStoredExtractions()) {
                [$body, $outcome] = $this->briefFromStoredExtractions($correlationId, $pid, $username, $patientUuid, $question);
            } elseif (($document = $this->documents->findLatestDocument($pid, $username)) === null) {
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
        } catch (DocumentTooLargeException $e) {
            $this->logger->info('copilot document too large', ['cid' => $correlationId, 'document_id' => $e->getDocumentId()]);
            $outcome = self::DEGRADED_DOCUMENT_TOO_LARGE;
            $body = self::degraded($correlationId, $patientUuid, $e->getDocumentId(), $outcome);
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

    /** Stored-extraction mode (ADR-012) needs its collaborators and the module tables; otherwise the legacy path runs. */
    private function usesStoredExtractions(): bool
    {
        return $this->records !== null && $this->schema !== null && $this->builder !== null && $this->schema->isReady();
    }

    /**
     * Brief from every extracted document of the patient (contract C4): the stored
     * extractions go to the agent, which does not extract again, plus the chart's
     * lab history as `prior_facts` in the Week 1 bundle shape (entered-in-error
     * excluded). Held documents, and documents the user may not access (core
     * `can_access()`, ADR-008 §3), are never sent.
     *
     * @return array{array<string,mixed>, string}  body, outcome code
     * @throws SourceUnavailableException  when the processing record cannot be read
     */
    private function briefFromStoredExtractions(string $correlationId, int $pid, string $username, string $patientUuid, ?string $question): array
    {
        assert($this->records !== null && $this->builder !== null);
        try {
            $stored = $this->records->listExtractions($pid, self::MAX_DOCUMENTS);
        } catch (Throwable $e) {
            throw new SourceUnavailableException('copilot_document', $e);
        }
        $documents = [];
        foreach ($stored as $row) {
            if (!$this->documents->canAccess($row['document_id'], $username)) {
                continue;
            }
            $extraction = json_decode($row['extraction_json'], true, 64);
            if (!is_array($extraction)) {
                continue;
            }
            // Follow the clinician's review: a filed value is chart history (sent as a prior fact),
            // a rejected or un-filed one is not briefed. Only values still waiting are document facts.
            $reviewed = $row['reviewed_indices'] ?? [];
            if ($reviewed !== [] && is_array($extraction['results'] ?? null)) {
                $kept = [];
                foreach ($extraction['results'] as $index => $result) {
                    if (!in_array($index, $reviewed, true)) {
                        $kept[] = $result;
                    }
                }
                if ($kept === []) {
                    continue;
                }
                $extraction['results'] = $kept;
            }
            $documents[] = ['document_id' => $row['document_id'], 'doc_type' => $row['doc_type'], 'extraction' => $extraction];
        }
        if ($documents === []) {
            // "Nothing read" only when that is true: documents that were read and fully reviewed are a different
            // answer (2026-09-26 production report: a filed-only chart was told no document had been read).
            try {
                $counts = $this->records->countExtractions($pid);
            } catch (Throwable $e) {
                throw new SourceUnavailableException('copilot_document', $e);
            }
            $reason = $counts['extracted'] > 0 && $counts['waiting'] === 0
                ? self::DEGRADED_ALL_VALUES_REVIEWED
                : self::DEGRADED_NO_EXTRACTED_DOCUMENTS;
            return [self::degraded($correlationId, $patientUuid, null, $reason), $reason];
        }

        try {
            $priorFacts = $this->builder->mapLabResults($this->reader->listLabResults($pid, self::PRIOR_FACTS_SINCE, self::PRIOR_FACTS_LIMIT));
        } catch (SourceUnavailableException $e) {
            $this->logger->warning('copilot source unavailable', ['cid' => $correlationId, 'source' => $e->getSource()]);
            $priorFacts = [];
        }

        $question = $question === null ? null : trim($question);
        $request = [
            'correlation_id' => $correlationId,
            'patient_uuid' => $patientUuid,
            'documents' => $documents,
            'prior_facts' => $priorFacts,
            'question' => $question === null || $question === '' ? null : mb_substr($question, 0, self::QUESTION_MAX_LENGTH),
        ];
        // Read only when an intake form is briefed: a lab-only briefing has nothing to compare them with.
        if (in_array('intake_form', array_column($documents, 'doc_type'), true)) {
            $request['chart_medications'] = $this->currentChartMedications($correlationId, $pid);
        }
        try {
            $body = $this->agent->postDocumentBriefing($request, $correlationId);
            return [$body, 'agent_' . Scalar::str($body['status'] ?? 'unknown') . '; documents=' . count($documents) . '; prior_facts=' . count($priorFacts)];
        } catch (AgentUnavailableException $e) {
            $this->logger->warning('copilot document briefing hand-off failed', [
                'cid' => $correlationId,
                'reason' => $e->getReason(),
                'http_status' => $e->getHttpStatus(),
            ]);
            return [self::degraded($correlationId, $patientUuid, null, self::DEGRADED_AGENT_UNAVAILABLE), $e->getReason()];
        }
    }

    /**
     * The chart's current medications (ADR-010) in the Week 1 bundle's shape: every entry not
     * known to be inactive (an indeterminate status still counts as on the list), the newest
     * MAX_CHART_MEDICATIONS of them. Null when the source cannot be read, so the agent says
     * nothing was compared rather than flagging every reported medication as missing.
     *
     * @return list<array<string,mixed>>|null
     */
    private function currentChartMedications(string $correlationId, int $pid): ?array
    {
        assert($this->builder !== null);
        try {
            $rows = $this->reader->listMedications($pid, self::MEDICATION_READ_LIMIT);
        } catch (SourceUnavailableException $e) {
            $this->logger->warning('copilot source unavailable', ['cid' => $correlationId, 'source' => $e->getSource()]);
            return null;
        }
        $current = array_values(array_filter(
            $this->builder->mapMedications($rows, null),
            static fn(array $m): bool => $m['active'] !== false
        ));
        return array_slice($current, -self::MAX_CHART_MEDICATIONS);
    }

    /** REST route adapter: `POST /api/copilot/document-briefing` under the local API bridge. */
    public function handleRest(HttpRestRequest $request): JsonResponse
    {
        // Read the session, then release its lock before the agent hand-off (Support\SessionRelease).
        $session = SessionRelease::readAndRelease($request->getSession());
        $requestedPid = null;
        $question = null;
        // getRequestBodyJSON() is broken in this checkout; read the raw body.
        $json = json_decode($request->getContent(), true, 4);
        if (is_array($json)) {
            $requestedPid = Scalar::positiveIntOrNull($json['pid'] ?? null);
            $question = is_string($json['question'] ?? null) ? $json['question'] : null;
        }
        $result = $this->handleForSession($session, $requestedPid, $question);
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
