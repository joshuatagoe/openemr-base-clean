<?php

/**
 * ProcessingRepositoryInterface over OpenEMR's connection (tables from
 * sql/install.sql). The claim uses single conditional statements plus
 * ROW_COUNT() (not audited, so no audit insert runs between the two), which
 * makes it atomic without a lock. saveExtraction() is one transaction of direct
 * writes only (ADR-009 section 4: nothing that could commit early runs inside).
 * Documents deleted in OpenEMR drop out of the extraction and pending-fact reads.
 *
 * @package   OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Documents;

use OpenEMR\Common\Database\QueryUtils;
use OpenEMR\Modules\Copilot\Support\Scalar;

final class SqlProcessingRepository implements ProcessingRepositoryInterface
{
    public function findRecords(int $pid, array $documentIds): array
    {
        $ids = array_values(array_filter($documentIds, static fn(int $id): bool => $id > 0));
        if ($ids === []) {
            return [];
        }
        $rows = QueryUtils::fetchRecords(
            "SELECT d.document_id, d.pid, d.content_sha256, d.doc_type, d.status, d.prompt_version, d.attempts,
                    d.last_error_code, d.identity_check, d.created_at, d.updated_at,
                    (SELECT COUNT(*) FROM copilot_extracted_value v
                      WHERE v.document_id = d.document_id AND v.status = 'candidate') AS pending_count
               FROM copilot_document d
              WHERE d.pid = ? AND d.document_id IN (" . implode(',', array_fill(0, count($ids), '?')) . ")",
            array_merge([$pid], $ids)
        );
        $out = [];
        foreach ($rows as $r) {
            $id = Scalar::int($r['document_id'] ?? null);
            $out[$id] = [
                'document_id' => $id,
                'pid' => Scalar::int($r['pid'] ?? null),
                'content_sha256' => Scalar::str($r['content_sha256'] ?? null),
                'doc_type' => Scalar::str($r['doc_type'] ?? null),
                'status' => Scalar::str($r['status'] ?? null),
                'prompt_version' => self::nullable($r['prompt_version'] ?? null),
                'attempts' => Scalar::int($r['attempts'] ?? null),
                'last_error_code' => self::nullable($r['last_error_code'] ?? null),
                'identity_check' => self::nullable($r['identity_check'] ?? null),
                'created_at' => Scalar::str($r['created_at'] ?? null),
                'updated_at' => self::nullable($r['updated_at'] ?? null),
                'pending_count' => Scalar::int($r['pending_count'] ?? null),
            ];
        }
        return $out;
    }

    public function findSamePatientDuplicate(int $pid, string $sha256, int $excludingDocumentId): ?int
    {
        $rows = QueryUtils::fetchRecords(
            "SELECT document_id FROM copilot_document
              WHERE pid = ? AND content_sha256 = ? AND document_id <> ? AND status IN ('extracted', 'held_identity')
              ORDER BY document_id ASC LIMIT 1",
            [$pid, $sha256, $excludingDocumentId]
        );
        $id = Scalar::int($rows[0]['document_id'] ?? null);
        return $id > 0 ? $id : null;
    }

    public function hashExistsForOtherPatient(int $pid, string $sha256): bool
    {
        // Only a copy still filed in that chart counts: a copy deleted or moved away in OpenEMR
        // (e.g. the front desk correcting a wrong-patient upload) must not block the correction.
        $rows = QueryUtils::fetchRecords(
            "SELECT 1 AS found FROM copilot_document d
               JOIN documents od ON od.id = d.document_id AND od.foreign_id = d.pid AND od.deleted = 0
              WHERE d.content_sha256 = ? AND d.pid <> ? LIMIT 1",
            [$sha256, $pid]
        );
        return $rows !== [];
    }

    public function releaseMovedRecord(int $documentId, int $pid): bool
    {
        return QueryUtils::inTransaction(static function () use ($documentId, $pid): bool {
            $rows = QueryUtils::fetchRecords("SELECT pid FROM copilot_document WHERE document_id = ? FOR UPDATE", [$documentId]);
            if ($rows === [] || Scalar::int($rows[0]['pid'] ?? null) === $pid) {
                return true;
            }
            $filed = QueryUtils::fetchRecords(
                "SELECT 1 AS found FROM copilot_extracted_value WHERE document_id = ? AND status IN ('"
                . implode("','", self::FILED_STATUSES) . "') LIMIT 1",
                [$documentId]
            );
            if ($filed !== []) {
                return false;
            }
            QueryUtils::sqlStatementThrowException("DELETE FROM copilot_extracted_value WHERE document_id = ?", [$documentId]);
            QueryUtils::sqlStatementThrowException("DELETE FROM copilot_document WHERE document_id = ?", [$documentId]);
            return true;
        });
    }

    public function claim(int $documentId, int $pid, string $sha256, string $docType, int $maxAttempts, int $staleSeconds): bool
    {
        QueryUtils::sqlStatementThrowException(
            "INSERT IGNORE INTO copilot_document (document_id, pid, content_sha256, doc_type, status, attempts, updated_at)
             VALUES (?, ?, ?, ?, 'processing', 1, NOW())",
            [$documentId, $pid, $sha256, $docType],
            true
        );
        if ($this->rowCount() === 1) {
            return true;
        }
        QueryUtils::sqlStatementThrowException(
            "UPDATE copilot_document
                SET status = 'processing', attempts = attempts + 1, content_sha256 = ?, doc_type = ?,
                    last_error_code = NULL, updated_at = NOW()
              WHERE document_id = ? AND pid = ?
                AND (status = 'queued'
                     OR (status = 'failed' AND attempts < ?)
                     OR (status = 'processing' AND (updated_at IS NULL OR updated_at < NOW() - INTERVAL ? SECOND)))",
            [$sha256, $docType, $documentId, $pid, $maxAttempts, $staleSeconds],
            true
        );
        return $this->rowCount() === 1;
    }

    public function saveExtraction(int $documentId, int $pid, string $status, string $identityCheck, ?string $promptVersion, string $extractionJson, array $candidates, ?string $errorCode): void
    {
        QueryUtils::inTransaction(static function () use ($documentId, $pid, $status, $identityCheck, $promptVersion, $extractionJson, $candidates, $errorCode): void {
            QueryUtils::sqlStatementThrowException(
                "UPDATE copilot_document
                    SET status = ?, identity_check = ?, prompt_version = ?, extraction_json = ?, last_error_code = ?, updated_at = NOW()
                  WHERE document_id = ? AND pid = ?",
                [$status, $identityCheck, $promptVersion, $extractionJson, $errorCode, $documentId, $pid]
            );
            foreach ($candidates as $c) {
                QueryUtils::sqlStatementThrowException(
                    "INSERT IGNORE INTO copilot_extracted_value
                        (document_id, pid, result_index, test_name, value_text, unit, reference_range, abnormal_flag,
                         flag_source, collection_date, verification_status, page, bbox, status)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'candidate')",
                    [
                        $documentId, $pid, $c['result_index'], $c['test_name'], $c['value_text'], $c['unit'],
                        $c['reference_range'], $c['abnormal_flag'], $c['flag_source'], $c['collection_date'],
                        $c['verification_status'], $c['page'], $c['bbox'],
                    ]
                );
            }
        });
    }

    public function markStatus(int $documentId, string $status, ?string $errorCode): void
    {
        QueryUtils::sqlStatementThrowException(
            "UPDATE copilot_document SET status = ?, last_error_code = ?, updated_at = NOW() WHERE document_id = ?",
            [$status, $errorCode, $documentId]
        );
    }

    public function confirmHeldPatient(int $documentId, int $pid, string $resolutionCode): bool
    {
        // One conditional statement plus ROW_COUNT(), like claim(): two clicks cannot both resolve it.
        QueryUtils::sqlStatementThrowException(
            "UPDATE copilot_document
                SET status = 'extracted', last_error_code = ?, updated_at = NOW()
              WHERE document_id = ? AND pid = ? AND status = 'held_identity'",
            [$resolutionCode, $documentId, $pid],
            true
        );
        return $this->rowCount() === 1;
    }

    public function listExtractions(int $pid, int $limit): array
    {
        // The newest documents when there are more than the limit, returned oldest first.
        // Only lab extractions: the briefing contract accepts doc_type lab_pdf only (C4).
        $rows = array_reverse(QueryUtils::fetchRecords(
            "SELECT d.document_id, d.doc_type, d.extraction_json
               FROM copilot_document d
               JOIN documents od ON od.id = d.document_id AND od.foreign_id = d.pid AND od.deleted = 0
              WHERE d.pid = ? AND d.status = 'extracted' AND d.doc_type = 'lab_pdf' AND d.extraction_json IS NOT NULL
              ORDER BY d.document_id DESC
              LIMIT " . max(1, min($limit, 50)),
            [$pid]
        ));
        return array_map(static fn(array $r): array => [
            'document_id' => Scalar::int($r['document_id'] ?? null),
            'doc_type' => Scalar::str($r['doc_type'] ?? null),
            'extraction_json' => Scalar::str($r['extraction_json'] ?? null),
        ], $rows);
    }

    public function listPendingFacts(int $pid, int $limit): array
    {
        $rows = QueryUtils::fetchRecords(
            "SELECT v.id, v.document_id, v.test_name, v.value_text, v.unit, v.reference_range, v.abnormal_flag,
                    v.flag_source, v.collection_date, v.verification_status, v.page, v.bbox
               FROM copilot_extracted_value v
               JOIN copilot_document d ON d.document_id = v.document_id AND d.pid = v.pid
               JOIN documents od ON od.id = d.document_id AND od.foreign_id = d.pid AND od.deleted = 0
              WHERE v.pid = ? AND v.status = 'candidate' AND d.status = 'extracted'
              ORDER BY v.document_id ASC, v.result_index ASC
              LIMIT " . max(1, min($limit, 500)),
            [$pid]
        );
        return array_map(static fn(array $r): array => [
            'id' => Scalar::int($r['id'] ?? null),
            'document_id' => Scalar::int($r['document_id'] ?? null),
            'test_name' => Scalar::str($r['test_name'] ?? null),
            'value_text' => self::nullable($r['value_text'] ?? null),
            'unit' => self::nullable($r['unit'] ?? null),
            'reference_range' => self::nullable($r['reference_range'] ?? null),
            'abnormal_flag' => self::nullable($r['abnormal_flag'] ?? null),
            'flag_source' => self::nullable($r['flag_source'] ?? null),
            'collection_date' => self::nullable($r['collection_date'] ?? null),
            'verification_status' => Scalar::str($r['verification_status'] ?? null),
            'page' => ($r['page'] ?? null) === null ? null : Scalar::int($r['page']),
            'bbox' => self::nullable($r['bbox'] ?? null),
        ], $rows);
    }

    private function rowCount(): int
    {
        $rows = QueryUtils::fetchRecords('SELECT ROW_COUNT() AS n', [], true);
        return Scalar::int($rows[0]['n'] ?? null);
    }

    private static function nullable(mixed $value): ?string
    {
        return $value === null ? null : Scalar::str($value);
    }
}
