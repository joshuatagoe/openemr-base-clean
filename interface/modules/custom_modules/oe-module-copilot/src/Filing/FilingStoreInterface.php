<?php

/**
 * Storage for Verify and file / reject (ADR-009). Production: SqlFilingStore.
 *
 * Every method except the two read-only lookups must be called inside
 * transaction(). ADR-009 §4: the SQL store uses direct table writes and
 * `UuidRegistry::createUuid()` only - never ProcedureService or a FHIR
 * service, which commit the open transaction.
 *
 * @phpstan-type Candidate array{id:int, document_id:int, pid:int, result_index:int, test_name:string, value_text:?string, unit:?string, reference_range:?string, abnormal_flag:?string, flag_source:?string, collection_date:?string, verification_status:string, status:string, filed_value:?string, procedure_result_id:?int}
 * @phpstan-type ResultRow array{result_data_type:string, result_code:string, result_text:string, date:string, units:string, result:string, range:string, abnormal:string, comments:string, document_id:int, result_status:string}
 *
 * @package   OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Filing;

interface FilingStoreInterface
{
    /** The `procedure_providers` row filed results are attributed to, created once, found by name. */
    public const OUTSIDE_LAB_NAME = 'Outside lab (document)';

    /**
     * Run `$work` in one database transaction: committed when it returns,
     * rolled back and rethrown when it throws.
     *
     * @template T
     * @param callable():T $work
     * @return T
     */
    public function transaction(callable $work): mixed;

    /**
     * The processing record, locked for the rest of the transaction (so filings
     * from one document are serialised); null when there is none.
     *
     * @return array{pid:int, status:string, doc_type:string, extraction_json:?string}|null
     */
    public function lockDocument(int $documentId): ?array;

    /**
     * The candidate at this position of the document, locked; null when none.
     *
     * @return Candidate|null
     */
    public function lockCandidate(int $documentId, int $resultIndex): ?array;

    /** The `procedure_report` an earlier filing from this document created, or null. */
    public function findDocumentReport(int $documentId): ?int;

    /** Id of the "Outside lab (document)" provider row, creating it the first time. */
    public function findOrCreateOutsideLab(): int;

    /**
     * One outside-lab `procedure_order` (complete, laboratory_test, history
     * order, active), its `procedure_order_code` seq 1, and one reviewed
     * `procedure_report`; returns the report id.
     */
    public function createOrderAndReport(int $pid, int $userId, int $labId, string $collectedAt): int;

    /**
     * @param ResultRow $result
     * @return int the new procedure_result_id
     */
    public function insertResult(int $reportId, array $result): int;

    public function markFiled(int $candidateId, int $userId, string $filedValue, int $resultId): void;

    public function markRejected(int $candidateId): void;

    /**
     * ADR-009 dedup layer (c): a result for this patient with the same test
     * (name, or code when known), collection date and value is already in the
     * chart and not entered-in-error.
     */
    public function sameResultInChart(int $pid, string $testName, string $code, string $collectionDate, string $value): bool;
}
