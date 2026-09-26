<?php

/**
 * Per-document processing on chart open (ADR-012 section 4, option c) and the
 * document list. Framework-free: every dependency is an interface.
 *
 * process(): for the patient, at most MAX_PER_CALL documents that need work
 * (no record yet, queued, failed with attempts left, or stuck in processing),
 * newest first. For each: read the original bytes, own SHA-256, then
 *  - same patient, same hash, already processed -> `skipped_duplicate`;
 *  - claim (atomic; a document is never processed twice at once);
 *  - agent `/v1/documents/extract` (contract C4);
 *  - compare the printed identity with the chart in PHP (IdentityComparator);
 *    only match / mismatch / missing is kept, the printed values are dropped;
 *  - store the extraction and its candidates: `extracted`, or `held_identity`
 *    on a mismatch or when the same file is on record for another patient
 *    (code `same_file_in_other_chart`; never merged, facts held back).
 * Failures become fixed codes on the record (`failed`, retried up to
 * MAX_ATTEMPTS); `doc_type_not_supported_yet` is terminal (`unsupported`).
 *
 * Logs carry the correlation id, document ids, fixed codes and counts only -
 * never bytes, names, dates of birth, values or file names.
 *
 * @phpstan-import-type RecordRow from ProcessingRepositoryInterface
 *
 * @package   OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Documents;

use JsonException;
use OpenEMR\Modules\Copilot\Agent\AgentClientInterface;
use OpenEMR\Modules\Copilot\Agent\AgentUnavailableException;
use OpenEMR\Modules\Copilot\Data\DocumentTooLargeException;
use OpenEMR\Modules\Copilot\Data\SourceUnavailableException;
use OpenEMR\Modules\Copilot\Data\SqlDocumentReader;
use OpenEMR\Modules\Copilot\Support\Scalar;
use Psr\Log\LoggerInterface;
use Throwable;

final class DocumentProcessor
{
    public const MAX_PER_CALL = 2;
    public const MAX_ATTEMPTS = 3;
    public const STALE_SECONDS = 600;
    public const LIST_LIMIT = 50;

    public const CODE_DUPLICATE = 'same_content_as_other_document';
    public const CODE_OTHER_CHART = 'same_file_in_other_chart';
    /** Informational: the printed name/DOB match this chart, so the copy elsewhere is the misfiled one. */
    public const CODE_OTHER_CHART_NOTED = 'same_file_also_in_other_chart';
    public const CODE_MOVED_AFTER_FILING = 'moved_after_filing';
    public const CODE_BAD_EXTRACTION = 'bad_extraction';
    public const CODE_STORE_FAILED = 'store_failed';
    public const CODE_NOT_SUPPORTED_YET = 'doc_type_not_supported_yet';
    public const CODE_NEEDS_CATEGORY = 'needs_category';
    public const CODE_UNSUPPORTED_MEDIA = 'unsupported_media_type';
    public const CODE_TOO_LARGE = 'document_too_large';
    public const CODE_UNAVAILABLE = 'document_unavailable';
    public const CODE_BUSY = 'already_in_progress';

    /** @var callable(): int */
    private $clock;

    /**
     * @param callable(): int|null $clock unix seconds (stale-claim detection)
     */
    public function __construct(
        private readonly PatientDocumentSourceInterface $documents,
        private readonly ProcessingRepositoryInterface $records,
        private readonly AgentClientInterface $agent,
        private readonly LoggerInterface $logger,
        ?callable $clock = null,
    ) {
        $this->clock = $clock ?? static fn(): int => time();
    }

    /**
     * @param array{lname:string, dob:string}|null $chartIdentity  null when the chart identity could not be read
     * @return array{processed:list<array{document_id:int, outcome:string}>, remaining:int}
     * @throws SourceUnavailableException  when the document list cannot be read
     */
    public function process(int $pid, string $patientUuid, string $username, ?array $chartIdentity, string $correlationId): array
    {
        $listed = $this->documents->listForPatient($pid, $username, self::LIST_LIMIT);
        $records = $this->records->findRecords($pid, array_map(static fn(array $d): int => $d['document_id'], $listed));

        $work = [];
        foreach ($listed as $doc) {
            if ($this->processable($doc) && $this->needsWork($records[$doc['document_id']] ?? null)) {
                $work[] = $doc;
            }
        }
        $processed = [];
        foreach (array_slice($work, 0, self::MAX_PER_CALL) as $doc) {
            $outcome = $this->processOne($pid, $patientUuid, $chartIdentity, $correlationId, $doc);
            $processed[] = ['document_id' => $doc['document_id'], 'outcome' => $outcome];
            $this->logger->info('copilot document processed', ['cid' => $correlationId, 'document_id' => $doc['document_id'], 'outcome' => $outcome]);
        }
        return ['processed' => $processed, 'remaining' => max(0, count($work) - count($processed))];
    }

    /**
     * The patient's documents with their processing state, newest first.
     *
     * @return list<array{document_id:int, doc_type:string, uploaded_at:string, status:string, error_code:?string, identity_check:?string, pending_count:int}>
     * @throws SourceUnavailableException
     */
    public function listDocuments(int $pid, string $username): array
    {
        $listed = $this->documents->listForPatient($pid, $username, self::LIST_LIMIT);
        $records = $this->records->findRecords($pid, array_map(static fn(array $d): int => $d['document_id'], $listed));
        $out = [];
        foreach ($listed as $doc) {
            $docType = DocType::fromCategoryNames($doc['category_names']);
            $record = $records[$doc['document_id']] ?? null;
            if ($record !== null) {
                $status = $record['status'];
                $code = $record['last_error_code'];
                $identity = $record['identity_check'];
                // Held facts are not pending chart updates until a clinician confirms the patient.
                $pending = $status === ProcessingRepositoryInterface::STATUS_EXTRACTED ? $record['pending_count'] : 0;
            } else {
                $code = $this->unprocessableCode($doc);
                $status = $code === null ? ProcessingRepositoryInterface::STATUS_QUEUED : ProcessingRepositoryInterface::STATUS_UNSUPPORTED;
                $identity = null;
                $pending = 0;
            }
            $out[] = [
                'document_id' => $doc['document_id'],
                'doc_type' => $docType,
                'uploaded_at' => $doc['uploaded_at'],
                'status' => $status,
                'error_code' => $code,
                'identity_check' => $identity,
                'pending_count' => $pending,
            ];
        }
        return $out;
    }

    /** @param array{document_id:int, uploaded_at:string, media_type:?string, size:int, category_names:list<string>} $doc */
    private function processable(array $doc): bool
    {
        return $this->unprocessableCode($doc) === null;
    }

    /** @param array{document_id:int, uploaded_at:string, media_type:?string, size:int, category_names:list<string>} $doc */
    private function unprocessableCode(array $doc): ?string
    {
        if (DocType::fromCategoryNames($doc['category_names']) === DocType::UNSUPPORTED) {
            return self::CODE_NEEDS_CATEGORY;
        }
        if ($doc['media_type'] === null) {
            return self::CODE_UNSUPPORTED_MEDIA;
        }
        if ($doc['size'] > SqlDocumentReader::MAX_DOCUMENT_BYTES) {
            return self::CODE_TOO_LARGE;
        }
        return null;
    }

    /** @param RecordRow|null $record */
    private function needsWork(?array $record): bool
    {
        if ($record === null) {
            return true;
        }
        return match ($record['status']) {
            ProcessingRepositoryInterface::STATUS_QUEUED => true,
            ProcessingRepositoryInterface::STATUS_FAILED => $record['attempts'] < self::MAX_ATTEMPTS,
            ProcessingRepositoryInterface::STATUS_PROCESSING => $this->isStale($record['updated_at']),
            default => false,
        };
    }

    private function isStale(?string $updatedAt): bool
    {
        $ts = $updatedAt === null ? false : strtotime($updatedAt);
        return $ts === false || ($this->clock)() - $ts > self::STALE_SECONDS;
    }

    /**
     * @param array{lname:string, dob:string}|null $chartIdentity
     * @param array{document_id:int, uploaded_at:string, media_type:?string, size:int, category_names:list<string>} $doc
     */
    private function processOne(int $pid, string $patientUuid, ?array $chartIdentity, string $correlationId, array $doc): string
    {
        $documentId = $doc['document_id'];
        $docType = DocType::fromCategoryNames($doc['category_names']);
        try {
            $bytes = $this->documents->readBytes($documentId, $pid);
        } catch (DocumentTooLargeException) {
            return self::CODE_TOO_LARGE;
        } catch (SourceUnavailableException) {
            return self::CODE_UNAVAILABLE;
        }
        $sha256 = hash('sha256', $bytes);

        try {
            if (!$this->records->releaseMovedRecord($documentId, $pid)) {
                return self::CODE_MOVED_AFTER_FILING;
            }
            $duplicateOf = $this->records->findSamePatientDuplicate($pid, $sha256, $documentId);
            if (!$this->records->claim($documentId, $pid, $sha256, $docType, self::MAX_ATTEMPTS, self::STALE_SECONDS)) {
                return self::CODE_BUSY;
            }
            if ($duplicateOf !== null) {
                $this->records->markStatus($documentId, ProcessingRepositoryInterface::STATUS_SKIPPED_DUPLICATE, self::CODE_DUPLICATE);
                return ProcessingRepositoryInterface::STATUS_SKIPPED_DUPLICATE;
            }
            $otherChart = $this->records->hashExistsForOtherPatient($pid, $sha256);
        } catch (Throwable $e) {
            $this->logger->error('copilot document record failed', ['cid' => $correlationId, 'document_id' => $documentId, 'type' => $e::class]);
            return self::CODE_STORE_FAILED;
        }

        $request = [
            'correlation_id' => $correlationId,
            'patient_uuid' => $patientUuid,
            'document_id' => $documentId,
            'doc_type' => $docType,
            'media_type' => $doc['media_type'],
            'document_base64' => base64_encode($bytes),
        ];
        unset($bytes);
        try {
            $response = $this->agent->postDocumentExtraction($request, $correlationId);
        } catch (AgentUnavailableException $e) {
            return $this->fail($documentId, ProcessingRepositoryInterface::STATUS_FAILED, $e->getReason());
        } finally {
            unset($request);
        }

        if (Scalar::str($response['status'] ?? null) !== 'ok') {
            $reason = self::code($response['degraded_reason'] ?? null) ?? 'agent_degraded';
            $status = $reason === self::CODE_NOT_SUPPORTED_YET
                ? ProcessingRepositoryInterface::STATUS_UNSUPPORTED
                : ProcessingRepositoryInterface::STATUS_FAILED;
            return $this->fail($documentId, $status, $reason);
        }

        $extraction = $response['extraction'] ?? null;
        if (!is_array($extraction) || !self::attributedTo($extraction, $documentId, $docType)) {
            return $this->fail($documentId, ProcessingRepositoryInterface::STATUS_FAILED, self::CODE_BAD_EXTRACTION);
        }
        // C4 returns the printed identity beside the extraction; C2 also puts it on LabDocument.
        // Only the comparison result is kept (ADR-012), so it is removed before storage.
        $printed = $response['printed_identity'] ?? $extraction['printed_identity'] ?? null;
        $printed = is_array($printed) ? $printed : [];
        unset($extraction['printed_identity']);
        if ($docType === DocType::INTAKE_FORM && is_array($extraction['demographics'] ?? null)) {
            // The agent already strips the written name and DOB; never store them even if sent (ADR-012).
            $extraction['demographics']['name'] = null;
            $extraction['demographics']['date_of_birth'] = null;
        }
        $candidates = match ($docType) {
            DocType::LAB_PDF => CandidateMapper::fromExtraction($extraction),
            DocType::INTAKE_FORM => CandidateMapper::fromIntake($extraction),
            default => [],
        };
        if ($candidates === null) {
            return $this->fail($documentId, ProcessingRepositoryInterface::STATUS_FAILED, self::CODE_BAD_EXTRACTION);
        }
        try {
            $json = json_encode($extraction, JSON_THROW_ON_ERROR | JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE);
        } catch (JsonException) {
            return $this->fail($documentId, ProcessingRepositoryInterface::STATUS_FAILED, self::CODE_BAD_EXTRACTION);
        }

        $identity = $chartIdentity === null
            ? IdentityComparator::MISSING
            : IdentityComparator::compare(
                is_string($printed['name'] ?? null) ? $printed['name'] : null,
                is_string($printed['dob'] ?? null) ? $printed['dob'] : null,
                $chartIdentity['lname'],
                $chartIdentity['dob'],
            );
        unset($printed, $response['printed_identity']);

        // A printed name/DOB that matches this chart outranks the fingerprint: when the same file is
        // also in another chart, that other copy is the misfiled one (its own identity check holds it).
        // Without a match, a copy in another chart is treated as a likely misfiling and held.
        $held = $identity === IdentityComparator::MISMATCH || ($otherChart && $identity !== IdentityComparator::MATCH);
        $status = $held ? ProcessingRepositoryInterface::STATUS_HELD_IDENTITY : ProcessingRepositoryInterface::STATUS_EXTRACTED;
        $promptVersion = Scalar::str($response['prompt_version'] ?? null);
        try {
            $this->records->saveExtraction(
                $documentId,
                $pid,
                $status,
                $identity,
                $promptVersion === '' ? null : mb_substr($promptVersion, 0, 40),
                $json,
                $candidates,
                $otherChart ? ($held ? self::CODE_OTHER_CHART : self::CODE_OTHER_CHART_NOTED) : null,
            );
        } catch (Throwable $e) {
            $this->logger->error('copilot extraction store failed', ['cid' => $correlationId, 'document_id' => $documentId, 'type' => $e::class]);
            return $this->fail($documentId, ProcessingRepositoryInterface::STATUS_FAILED, self::CODE_STORE_FAILED);
        }
        return $status;
    }

    /**
     * The extraction and every citation in it name this document; the briefing
     * contract (C4 StoredDocument) rejects anything else, so it is never stored.
     *
     * @param array<mixed> $extraction
     */
    private static function attributedTo(array $extraction, int $documentId, string $docType): bool
    {
        if (Scalar::int($extraction['document_id'] ?? null) !== $documentId) {
            return false;
        }
        if ($docType === DocType::INTAKE_FORM) {
            if (Scalar::str($extraction['doc_type'] ?? null) !== DocType::INTAKE_FORM) {
                return false;
            }
            $citations = CandidateMapper::intakeCitations($extraction);
        } else {
            $citations = [];
            foreach (is_array($extraction['results'] ?? null) ? $extraction['results'] : [] as $r) {
                if (is_array($r) && is_array($r['citation'] ?? null)) {
                    $citations[] = $r['citation'];
                }
            }
        }
        foreach ($citations as $citation) {
            if (Scalar::str($citation['source_id'] ?? null) !== (string) $documentId) {
                return false;
            }
        }
        return true;
    }

    private function fail(int $documentId, string $status, string $code): string
    {
        try {
            $this->records->markStatus($documentId, $status, $code);
        } catch (Throwable) {
            // The record stays `processing` and is reclaimed once stale.
        }
        return $code;
    }

    /** A fixed code from the agent, or null: lower-case snake case only, so no free text is ever stored. */
    private static function code(mixed $value): ?string
    {
        return is_string($value) && preg_match('/^[a-z][a-z0-9_]{0,63}$/', $value) ? $value : null;
    }
}
