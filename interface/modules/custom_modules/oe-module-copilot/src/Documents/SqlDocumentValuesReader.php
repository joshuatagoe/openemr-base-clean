<?php

/**
 * DocumentValuesReaderInterface over OpenEMR's connection: read-only selects on
 * `copilot_document` / `copilot_extracted_value`, always bound to the pid.
 *
 * @package   OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Documents;

use OpenEMR\Common\Database\QueryUtils;
use OpenEMR\Modules\Copilot\Support\Scalar;

/** @phpstan-import-type ValueRow from DocumentValuesReaderInterface */
final class SqlDocumentValuesReader implements DocumentValuesReaderInterface
{
    public function findRecord(int $documentId, int $pid): ?array
    {
        $rows = QueryUtils::fetchRecords(
            "SELECT status, identity_check, doc_type, last_error_code FROM copilot_document WHERE document_id = ? AND pid = ?",
            [$documentId, $pid]
        );
        $r = $rows[0] ?? null;
        if (!is_array($r)) {
            return null;
        }
        return [
            'status' => Scalar::str($r['status'] ?? null),
            'identity_check' => self::nullable($r['identity_check'] ?? null),
            'doc_type' => Scalar::str($r['doc_type'] ?? null),
            'last_error_code' => self::nullable($r['last_error_code'] ?? null),
        ];
    }

    public function listValues(int $documentId, int $pid): array
    {
        $rows = QueryUtils::fetchRecords(
            "SELECT result_index, test_name, value_text, unit, reference_range, abnormal_flag, flag_source,
                    collection_date, verification_status, page, bbox, status, procedure_result_id
               FROM copilot_extracted_value
              WHERE document_id = ? AND pid = ?
              ORDER BY result_index ASC
              LIMIT 500",
            [$documentId, $pid]
        );
        return array_map(static function (array $r): array {
            $page = Scalar::int($r['page'] ?? null);
            $resultId = Scalar::int($r['procedure_result_id'] ?? null);
            return [
                'result_index' => Scalar::int($r['result_index'] ?? null),
                'test_name' => Scalar::str($r['test_name'] ?? null),
                'value_text' => self::nullable($r['value_text'] ?? null),
                'unit' => self::nullable($r['unit'] ?? null),
                'reference_range' => self::nullable($r['reference_range'] ?? null),
                'abnormal_flag' => self::nullable($r['abnormal_flag'] ?? null),
                'flag_source' => self::nullable($r['flag_source'] ?? null),
                'collection_date' => self::nullable($r['collection_date'] ?? null),
                'verification_status' => Scalar::str($r['verification_status'] ?? null),
                'page' => $page > 0 ? $page : null,
                'bbox' => self::nullable($r['bbox'] ?? null),
                'status' => Scalar::str($r['status'] ?? null),
                'procedure_result_id' => $resultId > 0 ? $resultId : null,
            ];
        }, $rows);
    }

    private static function nullable(mixed $value): ?string
    {
        return $value === null ? null : Scalar::str($value);
    }
}
