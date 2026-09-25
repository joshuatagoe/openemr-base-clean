<?php

/**
 * The filing chain on OpenEMR's own lab tables (ADR-009 §2, §4, §5).
 *
 * Direct table writes and `UuidRegistry::createUuid()` only, inside one
 * QueryUtils transaction. Core services that back-fill UUIDs call
 * `createMissingUuids()`, which commits whatever transaction is open (ADODB
 * transactions do not nest), so they are never used here; the FHIR read-back
 * happens after commit, elsewhere.
 *
 * The chain matches what the dev-stack evidence showed is needed for every
 * view: `procedure_order` (outside lab, complete, laboratory_test, history
 * order, active) + `procedure_order_code` seq 1 (without it the order/report
 * views and FHIR return nothing) + one reviewed `procedure_report` + one
 * `procedure_result` per filed value.
 *
 * @phpstan-import-type Candidate from FilingStoreInterface
 *
 * @package   OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Filing;

use OpenEMR\Common\Database\QueryUtils;
use OpenEMR\Common\Uuid\UuidRegistry;
use OpenEMR\Modules\Copilot\Support\Scalar;

final class SqlFilingStore implements FilingStoreInterface
{
    public const ORDER_CODE_NAME = 'Outside lab results (from document)';

    public function transaction(callable $work): mixed
    {
        return QueryUtils::inTransaction($work);
    }

    public function lockDocument(int $documentId): ?array
    {
        $rows = QueryUtils::fetchRecords(
            "SELECT pid, status, doc_type, extraction_json FROM copilot_document WHERE document_id = ? FOR UPDATE",
            [$documentId]
        );
        $r = $rows[0] ?? null;
        if (!is_array($r)) {
            return null;
        }
        return [
            'pid' => Scalar::int($r['pid'] ?? null),
            'status' => Scalar::str($r['status'] ?? null),
            'doc_type' => Scalar::str($r['doc_type'] ?? null),
            'extraction_json' => self::nullable($r['extraction_json'] ?? null),
        ];
    }

    public function lockCandidate(int $documentId, int $resultIndex): ?array
    {
        $rows = QueryUtils::fetchRecords(
            "SELECT id, document_id, pid, result_index, test_name, value_text, unit, reference_range, abnormal_flag,
                    flag_source, collection_date, verification_status, status, filed_value, procedure_result_id
               FROM copilot_extracted_value
              WHERE document_id = ? AND result_index = ?
                FOR UPDATE",
            [$documentId, $resultIndex]
        );
        $r = $rows[0] ?? null;
        if (!is_array($r)) {
            return null;
        }
        $resultId = Scalar::int($r['procedure_result_id'] ?? null);
        return [
            'id' => Scalar::int($r['id'] ?? null),
            'document_id' => Scalar::int($r['document_id'] ?? null),
            'pid' => Scalar::int($r['pid'] ?? null),
            'result_index' => Scalar::int($r['result_index'] ?? null),
            'test_name' => Scalar::str($r['test_name'] ?? null),
            'value_text' => self::nullable($r['value_text'] ?? null),
            'unit' => self::nullable($r['unit'] ?? null),
            'reference_range' => self::nullable($r['reference_range'] ?? null),
            'abnormal_flag' => self::nullable($r['abnormal_flag'] ?? null),
            'flag_source' => self::nullable($r['flag_source'] ?? null),
            'collection_date' => self::nullable($r['collection_date'] ?? null),
            'verification_status' => Scalar::str($r['verification_status'] ?? null),
            'status' => Scalar::str($r['status'] ?? null),
            'filed_value' => self::nullable($r['filed_value'] ?? null),
            'procedure_result_id' => $resultId > 0 ? $resultId : null,
        ];
    }

    public function findDocumentReport(int $documentId): ?int
    {
        $rows = QueryUtils::fetchRecords(
            "SELECT pr.procedure_report_id
               FROM copilot_extracted_value v
               JOIN procedure_result pr ON pr.procedure_result_id = v.procedure_result_id
               JOIN procedure_report rp ON rp.procedure_report_id = pr.procedure_report_id
              WHERE v.document_id = ? AND v.procedure_result_id IS NOT NULL
              ORDER BY pr.procedure_result_id ASC LIMIT 1",
            [$documentId]
        );
        $id = Scalar::int($rows[0]['procedure_report_id'] ?? null);
        return $id > 0 ? $id : null;
    }

    public function findOrCreateOutsideLab(): int
    {
        $rows = QueryUtils::fetchRecords(
            "SELECT ppid FROM procedure_providers WHERE name = ? ORDER BY ppid ASC LIMIT 1",
            [self::OUTSIDE_LAB_NAME]
        );
        $id = Scalar::int($rows[0]['ppid'] ?? null);
        if ($id > 0) {
            return $id;
        }
        return (int) QueryUtils::sqlInsert(
            "INSERT INTO procedure_providers SET uuid = ?, name = ?, direction = 'R', active = 1,
                    notes = 'Results filed by a clinician from uploaded documents (Clinical Co-Pilot)'",
            [UuidRegistry::getRegistryForTable('procedure_providers')->createUuid(), self::OUTSIDE_LAB_NAME]
        );
    }

    public function createOrderAndReport(int $pid, int $userId, int $labId, string $collectedAt): int
    {
        $orderId = (int) QueryUtils::sqlInsert(
            "INSERT INTO procedure_order SET uuid = ?, provider_id = ?, patient_id = ?, encounter_id = 0,
                    date_collected = ?, date_ordered = ?, order_status = 'complete', activity = 1,
                    procedure_order_type = 'laboratory_test', lab_id = ?, history_order = '1'",
            [UuidRegistry::getRegistryForTable('procedure_order')->createUuid(), $userId, $pid, $collectedAt, $collectedAt, $labId]
        );
        QueryUtils::sqlInsert(
            "INSERT INTO procedure_order_code SET procedure_order_id = ?, procedure_order_seq = 1, procedure_code = '',
                    procedure_name = ?, procedure_type = 'laboratory_test', do_not_send = 1",
            [$orderId, self::ORDER_CODE_NAME]
        );
        return (int) QueryUtils::sqlInsert(
            "INSERT INTO procedure_report SET uuid = ?, procedure_order_id = ?, procedure_order_seq = 1,
                    date_collected = ?, date_report = ?, source = ?, report_status = 'final', review_status = 'reviewed'",
            [UuidRegistry::getRegistryForTable('procedure_report')->createUuid(), $orderId, $collectedAt, $collectedAt, $userId]
        );
    }

    public function insertResult(int $reportId, array $result): int
    {
        return (int) QueryUtils::sqlInsert(
            "INSERT INTO procedure_result SET uuid = ?, procedure_report_id = ?, result_data_type = ?, result_code = ?,
                    result_text = ?, `date` = ?, units = ?, result = ?, `range` = ?, abnormal = ?, comments = ?,
                    document_id = ?, result_status = ?",
            [
                UuidRegistry::getRegistryForTable('procedure_result')->createUuid(),
                $reportId,
                $result['result_data_type'],
                $result['result_code'],
                $result['result_text'],
                $result['date'],
                $result['units'],
                $result['result'],
                $result['range'],
                $result['abnormal'],
                $result['comments'],
                $result['document_id'],
                $result['result_status'],
            ]
        );
    }

    public function markFiled(int $candidateId, int $userId, string $filedValue, int $resultId): void
    {
        QueryUtils::sqlStatementThrowException(
            "UPDATE copilot_extracted_value
                SET status = 'filed', filed_by = ?, filed_at = NOW(), filed_value = ?, procedure_result_id = ?
              WHERE id = ? AND status = 'candidate'",
            [$userId, $filedValue, $resultId, $candidateId]
        );
    }

    public function markRejected(int $candidateId): void
    {
        QueryUtils::sqlStatementThrowException(
            "UPDATE copilot_extracted_value SET status = 'rejected' WHERE id = ? AND status = 'candidate'",
            [$candidateId]
        );
    }

    public function sameResultInChart(int $pid, string $testName, string $code, string $collectionDate, string $value): bool
    {
        $rows = QueryUtils::fetchRecords(
            "SELECT 1 AS found
               FROM procedure_order po
               JOIN procedure_report rp ON rp.procedure_order_id = po.procedure_order_id
               JOIN procedure_result pr ON pr.procedure_report_id = rp.procedure_report_id
              WHERE po.patient_id = ?
                AND DATE(COALESCE(pr.`date`, rp.date_collected, po.date_collected)) = ?
                AND TRIM(pr.result) = ?
                AND pr.result_status <> 'entered-in-error'
                AND (LOWER(TRIM(pr.result_text)) = LOWER(?) OR (? <> '' AND pr.result_code = ?))
              LIMIT 1",
            [$pid, $collectionDate, $value, trim($testName), $code, $code]
        );
        return $rows !== [];
    }

    private static function nullable(mixed $value): ?string
    {
        return $value === null ? null : Scalar::str($value);
    }
}
