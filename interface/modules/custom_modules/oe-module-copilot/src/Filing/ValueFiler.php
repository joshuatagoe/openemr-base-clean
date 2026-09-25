<?php

/**
 * Verify and file / reject one extracted lab value (ADR-003, ADR-009 §2).
 *
 * Everything happens in one store transaction, with the document's processing
 * record locked first so filings from one document are serialised: a second
 * click finds the candidate already `filed` and returns its result (dedup
 * layer a); the document's first filing creates the outside-lab order, its
 * seq-1 order code and one reviewed report, later filings reuse that report.
 *
 * Rules: the candidate and its processing record must belong to the patient;
 * the document must be an `extracted` lab document; a rejected value is never
 * filed; `unverified` needs `confirm_unverified`; `unreadable` needs a
 * clinician-entered value. The collection date is never guessed and never the
 * upload date (ADR-009 7b): without an extracted one the clinician must enter
 * one they verified, and the filed result says so; a clinician date cannot
 * override an extracted one. A value the
 * clinician changed is filed `corrected`, otherwise `final`; the extracted
 * value is kept in `comments`. The abnormal flag is filed only when the lab
 * printed it. A same-patient, same-test, same-date, same-value result already
 * in the chart gives a warning (layer c), never a block.
 *
 * Outcomes are fixed codes; nothing here logs.
 *
 * @phpstan-import-type Candidate from FilingStoreInterface
 *
 * @package   OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Filing;

final class ValueFiler
{
    public const OUTCOME_FILED = 'filed';
    public const OUTCOME_ALREADY_FILED = 'already_filed';
    public const OUTCOME_REJECTED = 'rejected';
    public const OUTCOME_ALREADY_REJECTED = 'already_rejected';
    public const OUTCOME_UNFILED = 'unfiled';
    public const OUTCOME_ALREADY_UNFILED = 'already_unfiled';

    public const ERROR_NOT_FOUND = 'value_not_found';
    public const ERROR_NOT_EXTRACTED = 'document_not_extracted';
    public const ERROR_NOT_FILEABLE = 'not_fileable';
    public const ERROR_REJECTED = 'value_rejected';
    public const ERROR_ALREADY_FILED = 'value_already_filed';
    public const ERROR_UNFILED = 'value_unfiled';
    public const ERROR_NOT_FILED = 'value_not_filed';
    public const ERROR_CONFIRMATION_REQUIRED = 'confirmation_required';
    public const ERROR_VALUE_REQUIRED = 'value_required';
    public const ERROR_NO_COLLECTION_DATE = 'collection_date_required';
    public const ERROR_COLLECTION_DATE_CONFLICT = 'collection_date_conflict';

    public const DATE_SOURCE_EXTRACTED = 'extracted';
    public const DATE_SOURCE_CLINICIAN = 'clinician';

    public const WARNING_SAME_RESULT = 'same_result_already_in_chart';

    /** Only lab documents are filed (ADR-010: intake forms stay document evidence). */
    public const FILEABLE_DOC_TYPE = 'lab_pdf';

    /** Extraction flag (printed) -> OpenEMR `proc_res_abnormal` option id (its `codes` column). */
    public const ABNORMAL_OPTIONS = ['H' => 'high', 'L' => 'low', 'HH' => 'vhigh', 'LL' => 'vlow', 'A' => 'yes', 'N' => 'no'];

    public function __construct(private readonly FilingStoreInterface $store)
    {
    }

    /**
     * @param ?string $enteredCollectionDate  a clinician-entered, already validated `Y-m-d` date, or null
     * @return array{outcome:string, procedure_result_id:?int, result_status:?string, warning:?string, verification_status:?string, collection_date_source?:string}
     *   `outcome` is filed / already_filed or one of the ERROR_* codes
     */
    public function file(int $pid, int $userId, int $documentId, int $resultIndex, ?string $filedValue, bool $confirmUnverified, ?string $enteredCollectionDate = null): array
    {
        return $this->store->transaction(function () use ($pid, $userId, $documentId, $resultIndex, $filedValue, $confirmUnverified, $enteredCollectionDate): array {
            $found = $this->lock($pid, $documentId, $resultIndex);
            if (is_string($found)) {
                return self::result($found);
            }
            [$document, $candidate] = $found;
            $verification = $candidate['verification_status'];

            if ($candidate['status'] === 'filed') {
                return self::result(self::OUTCOME_ALREADY_FILED, $candidate['procedure_result_id'], null, null, $verification);
            }
            if ($candidate['status'] === 'unfiled') {
                return self::result(self::ERROR_UNFILED, verification: $verification);
            }
            if ($candidate['status'] !== 'candidate') {
                return self::result(self::ERROR_REJECTED, verification: $verification);
            }
            if ($document['status'] !== 'extracted') {
                return self::result(self::ERROR_NOT_EXTRACTED, verification: $verification);
            }
            if ($document['doc_type'] !== self::FILEABLE_DOC_TYPE) {
                return self::result(self::ERROR_NOT_FILEABLE, verification: $verification);
            }

            $entered = $filedValue === null ? null : trim($filedValue);
            $entered = $entered === '' ? null : $entered;
            if ($verification === 'unreadable' && $entered === null) {
                return self::result(self::ERROR_VALUE_REQUIRED, verification: $verification);
            }
            if ($verification === 'unverified' && !$confirmUnverified) {
                return self::result(self::ERROR_CONFIRMATION_REQUIRED, verification: $verification);
            }
            $extracted = $candidate['value_text'];
            $value = $entered ?? $extracted;
            if ($value === null || $value === '') {
                return self::result(self::ERROR_VALUE_REQUIRED, verification: $verification);
            }
            $extractedDate = $candidate['collection_date'] === '' ? null : $candidate['collection_date'];
            if ($extractedDate !== null && $enteredCollectionDate !== null && $enteredCollectionDate !== $extractedDate) {
                return self::result(self::ERROR_COLLECTION_DATE_CONFLICT, verification: $verification);
            }
            $collectionDate = $extractedDate ?? $enteredCollectionDate;
            if ($collectionDate === null) {
                return self::result(self::ERROR_NO_COLLECTION_DATE, verification: $verification);
            }
            $dateSource = $extractedDate !== null ? self::DATE_SOURCE_EXTRACTED : self::DATE_SOURCE_CLINICIAN;
            $collectedAt = $collectionDate . ' 00:00:00';
            $resultStatus = $entered !== null && $entered !== $extracted ? 'corrected' : 'final';
            $code = self::loincCode($document['extraction_json'], $resultIndex);

            $warning = $this->store->sameResultInChart($pid, $candidate['test_name'], $code, $collectionDate, $value)
                ? self::WARNING_SAME_RESULT
                : null;

            $reportId = $this->store->findDocumentReport($documentId)
                ?? $this->store->createOrderAndReport($pid, $userId, $this->store->findOrCreateOutsideLab(), $collectedAt);

            $flag = $candidate['flag_source'] === 'extracted' ? ($candidate['abnormal_flag'] ?? '') : '';
            $resultId = $this->store->insertResult($reportId, [
                'result_data_type' => is_numeric($value) ? 'N' : 'S',
                'result_code' => $code,
                'result_text' => $candidate['test_name'],
                'date' => $collectedAt,
                'units' => $candidate['unit'] ?? '',
                'result' => $value,
                'range' => $candidate['reference_range'] ?? '',
                'abnormal' => self::ABNORMAL_OPTIONS[$flag] ?? '',
                'comments' => 'Extracted value: ' . ($extracted ?? 'unreadable')
                    . ($dateSource === self::DATE_SOURCE_CLINICIAN ? '; collection date entered by clinician' : ''),
                // ADR-009 7b: 0, not the document id - a core document link replaces the value, range and
                // units with the file name in the order-results screen. The source link is our candidate row.
                'document_id' => 0,
                'result_status' => $resultStatus,
            ]);
            $this->store->markFiled($candidate['id'], $userId, $value, $resultId);

            return ['collection_date_source' => $dateSource] + self::result(self::OUTCOME_FILED, $resultId, $resultStatus, $warning, $verification);
        });
    }

    /**
     * @return array{outcome:string, procedure_result_id:?int, result_status:?string, warning:?string, verification_status:?string}
     *   `outcome` is rejected / already_rejected or one of the ERROR_* codes
     */
    public function reject(int $pid, int $documentId, int $resultIndex): array
    {
        return $this->store->transaction(function () use ($pid, $documentId, $resultIndex): array {
            $found = $this->lock($pid, $documentId, $resultIndex);
            if (is_string($found)) {
                return self::result($found);
            }
            $candidate = $found[1];
            if ($candidate['status'] === 'rejected') {
                return self::result(self::OUTCOME_ALREADY_REJECTED);
            }
            if ($candidate['status'] === 'unfiled') {
                return self::result(self::ERROR_UNFILED);
            }
            if ($candidate['status'] !== 'candidate') {
                return self::result(self::ERROR_ALREADY_FILED);
            }
            $this->store->markRejected($candidate['id']);
            return self::result(self::OUTCOME_REJECTED);
        });
    }

    /**
     * Withdraw a filed value (ADR-009 section 7, 7b): the chart result becomes
     * `entered-in-error` (kept, never deleted) and the candidate `unfiled`,
     * keeping its result link and filing history. It is then neither a pending
     * fact nor fileable again. Idempotent.
     *
     * @return array{outcome:string, procedure_result_id:?int, result_status:?string, warning:?string, verification_status:?string}
     *   `outcome` is unfiled / already_unfiled or one of the ERROR_* codes
     */
    public function unfile(int $pid, int $documentId, int $resultIndex): array
    {
        return $this->store->transaction(function () use ($pid, $documentId, $resultIndex): array {
            $found = $this->lock($pid, $documentId, $resultIndex);
            if (is_string($found)) {
                return self::result($found);
            }
            $candidate = $found[1];
            $resultId = $candidate['procedure_result_id'];
            if ($candidate['status'] === 'unfiled') {
                return self::result(self::OUTCOME_ALREADY_UNFILED, $resultId, 'entered-in-error');
            }
            if ($candidate['status'] !== 'filed' || $resultId === null) {
                return self::result(self::ERROR_NOT_FILED);
            }
            $this->store->markResultEnteredInError($resultId);
            $this->store->markUnfiled($candidate['id']);
            return self::result(self::OUTCOME_UNFILED, $resultId, 'entered-in-error');
        });
    }

    /**
     * The locked processing record and candidate, both this patient's; otherwise
     * the not-found code (the same for missing and for another patient's).
     *
     * @return array{array{pid:int, status:string, doc_type:string, extraction_json:?string}, Candidate}|string
     */
    private function lock(int $pid, int $documentId, int $resultIndex): array|string
    {
        $document = $this->store->lockDocument($documentId);
        if ($document === null || $document['pid'] !== $pid) {
            return self::ERROR_NOT_FOUND;
        }
        $candidate = $this->store->lockCandidate($documentId, $resultIndex);
        if ($candidate === null || $candidate['pid'] !== $pid) {
            return self::ERROR_NOT_FOUND;
        }
        return [$document, $candidate];
    }

    /** The result's LOINC code from the stored extraction, or '' - never guessed. */
    private static function loincCode(?string $extractionJson, int $resultIndex): string
    {
        $extraction = $extractionJson === null ? null : json_decode($extractionJson, true, 64);
        $code = is_array($extraction) && is_array($extraction['results'][$resultIndex] ?? null)
            ? ($extraction['results'][$resultIndex]['loinc_code'] ?? null)
            : null;
        return is_string($code) && preg_match('/^[0-9A-Za-z.\-]{1,31}$/', $code) === 1 ? $code : '';
    }

    /** @return array{outcome:string, procedure_result_id:?int, result_status:?string, warning:?string, verification_status:?string} */
    private static function result(string $outcome, ?int $resultId = null, ?string $resultStatus = null, ?string $warning = null, ?string $verification = null): array
    {
        return ['outcome' => $outcome, 'procedure_result_id' => $resultId, 'result_status' => $resultStatus, 'warning' => $warning, 'verification_status' => $verification];
    }
}
