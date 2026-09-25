<?php

/**
 * Read-only access to one document's processing record and candidate values,
 * for the panel's value list (Wave 2 step 2). Production: SqlDocumentValuesReader.
 *
 * @package   OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Documents;

/**
 * @phpstan-type ValueRow array{result_index:int, test_name:string, value_text:?string, unit:?string, reference_range:?string, abnormal_flag:?string, flag_source:?string, collection_date:?string, verification_status:string, page:?int, bbox:?string, status:string, procedure_result_id:?int}
 */
interface DocumentValuesReaderInterface
{
    /**
     * The processing record of this document when it belongs to `$pid`; null
     * when there is none (not processed yet) or it is another patient's.
     *
     * @return array{status:string, identity_check:?string, doc_type:string, last_error_code:?string}|null
     */
    public function findRecord(int $documentId, int $pid): ?array;

    /**
     * Every candidate value of this document belonging to `$pid`, in result order.
     *
     * @return list<ValueRow>
     */
    public function listValues(int $documentId, int $pid): array;
}
