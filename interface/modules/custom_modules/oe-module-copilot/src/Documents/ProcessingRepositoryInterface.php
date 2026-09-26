<?php

/**
 * The module's processing record (`copilot_document`, ADR-012) and candidate
 * values (`copilot_extracted_value`, ADR-009). Production: SqlProcessingRepository.
 *
 * @phpstan-type RecordRow array{document_id:int, pid:int, content_sha256:string, doc_type:string, status:string, prompt_version:?string, attempts:int, last_error_code:?string, identity_check:?string, created_at:string, updated_at:?string, pending_count:int}
 * @phpstan-type CandidateRow array{result_index:int, test_name:string, value_text:?string, unit:?string, reference_range:?string, abnormal_flag:?string, flag_source:?string, collection_date:?string, verification_status:string, page:?int, bbox:?string}
 * @phpstan-type PendingFactRow array{id:int, document_id:int, test_name:string, value_text:?string, unit:?string, reference_range:?string, abnormal_flag:?string, flag_source:?string, collection_date:?string, verification_status:string, page:?int, bbox:?string}
 *
 * @package   OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Documents;

interface ProcessingRepositoryInterface
{
    public const STATUS_QUEUED = 'queued';
    public const STATUS_PROCESSING = 'processing';
    public const STATUS_EXTRACTED = 'extracted';
    public const STATUS_FAILED = 'failed';
    public const STATUS_SKIPPED_DUPLICATE = 'skipped_duplicate';
    public const STATUS_UNSUPPORTED = 'unsupported';
    public const STATUS_HELD_IDENTITY = 'held_identity';

    /** Candidate statuses that mean a value reached the chart (ADR-009 7b: `unfiled` = filed, then withdrawn). */
    public const FILED_STATUSES = ['filed', 'unfiled'];

    /**
     * Records for these documents of this patient, keyed by document id, each
     * with its count of `candidate` values.
     *
     * @param list<int> $documentIds
     * @return array<int, RecordRow>
     */
    public function findRecords(int $pid, array $documentIds): array;

    /**
     * Another document of the same patient with the same content hash that was
     * already processed (extracted or held), or null.
     */
    public function findSamePatientDuplicate(int $pid, string $sha256, int $excludingDocumentId): ?int;

    /** Whether any other patient has a document with this content hash on record. */
    /** True when the same content is filed, and still live (not deleted or moved away), in another patient's chart. */
    public function hashExistsForOtherPatient(int $pid, string $sha256): bool;

    /**
     * A document moved to this patient in OpenEMR keeps our record under its old patient. Clear that
     * record and its candidates so it is processed afresh here. Returns false (and changes nothing)
     * when a value from it was already filed - that needs a clinician, not an automatic reset.
     */
    public function releaseMovedRecord(int $documentId, int $pid): bool;

    /**
     * Atomically take a document for processing: creates the record in
     * `processing`, or moves an existing `queued`, retryable `failed` (attempts
     * below `$maxAttempts`) or stale `processing` record (untouched for
     * `$staleSeconds`) to `processing` and increments attempts. False when
     * another request holds it or it needs no work - so a document is never
     * processed twice at once.
     */
    public function claim(int $documentId, int $pid, string $sha256, string $docType, int $maxAttempts, int $staleSeconds): bool;

    /**
     * Store a finished extraction and its candidates in one transaction.
     * `$status` is `extracted` or `held_identity`; `$errorCode` is a fixed flag
     * code or null.
     *
     * @param list<CandidateRow> $candidates
     */
    public function saveExtraction(int $documentId, int $pid, string $status, string $identityCheck, ?string $promptVersion, string $extractionJson, array $candidates, ?string $errorCode): void;

    /** Set a terminal or retryable status with a fixed code (never a message). */
    public function markStatus(int $documentId, string $status, ?string $errorCode): void;

    /**
     * A clinician confirmed the patient of a held document (ADR-012 §4a): atomically move it from
     * `held_identity` to `extracted` and record `$resolutionCode` as its code. `identity_check` is
     * left as it was (history). False, changing nothing, unless the record is this patient's and
     * still held.
     */
    public function confirmHeldPatient(int $documentId, int $pid, string $resolutionCode): bool;

    /**
     * Stored lab and intake extractions of this patient's `extracted` documents (not
     * held, not deleted in OpenEMR) that still have a value waiting for review (at least one
     * `candidate` value, see countExtractions): the newest `$limit`, returned oldest first.
     * A fully reviewed document takes no slot.
     *
     * `reviewed_indices`: result positions whose value was filed, rejected or un-filed - the briefing
     * leaves them out (filed values reach it as chart history instead).
     *
     * @return list<array{document_id:int, doc_type:string, extraction_json:string, reviewed_indices:list<int>}>
     */
    public function listExtractions(int $pid, int $limit): array;

    /**
     * How many of this patient's documents were read (`extracted` lab or intake extractions, not held,
     * not deleted in OpenEMR), and how many of those still have a value waiting for review (at least
     * one `candidate` value). Counts only.
     *
     * @return array{extracted:int, waiting:int}
     */
    public function countExtractions(int $pid): array;

    /**
     * `candidate` values of this patient's `extracted` lab documents (held and
     * filed/rejected values excluded; intake items are patient-reported evidence, not pending lab facts).
     *
     * @return list<PendingFactRow>
     */
    public function listPendingFacts(int $pid, int $limit): array;
}
