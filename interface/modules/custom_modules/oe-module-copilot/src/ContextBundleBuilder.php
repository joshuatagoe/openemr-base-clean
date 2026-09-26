<?php

/**
 * Transforms OpenEMR records into the agent's ContextBundle contract
 * (copilot-agent/app/contracts.py, schema 1.0).
 *
 * Pure: takes already-read rows, returns an array. No database access, no
 * logging. Rules (ARCHITECTURE.md sections 6 and 11):
 *
 * - The baseline note is chosen by the reader (latest non-blank SOAP plan
 *   before the as-of timestamp); this class only shapes it.
 * - Only lab results and orders dated after the baseline note are included;
 *   the agent's matcher re-applies the strict window.
 * - Empty units become null; result status and abnormal flag are mapped to
 *   the contract's closed vocabularies (OpenEMR's `vhigh`/`vlow` collapse to
 *   `high`/`low`); rows the contract cannot represent are omitted and counted.
 *   Deferred, documented: results with status `cancel`/`error`/blank and
 *   non-numeric (text) results are not carried yet.
 * - Results marked `entered-in-error` (un-filed, ADR-009 section 7) are
 *   excluded deliberately and counted in getExcludedCounts(), not as unmapped.
 * - Pending document facts (candidate values, ADR-011 / contract C5) are added
 *   as `pending_document_facts` only when there are any.
 * - All timestamps are UTC ISO-8601.
 * - An unavailable lab source is declared in data_quality.sources_unavailable;
 *   it is never rendered as an empty list.
 * - Duplicate medication rows (same source, lower(drug), start date and
 *   status) collapse to one record carrying `duplicate_count`; the number
 *   collapsed is reported in data_quality.duplicates_collapsed (AUDIT DATA-004).
 *
 * @package   OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot;

use DateTimeZone;
use InvalidArgumentException;
use OpenEMR\Modules\Copilot\Documents\CandidateMapper;
use OpenEMR\Modules\Copilot\Support\Scalar;
use OpenEMR\Modules\Copilot\Support\UtcDate;

final class ContextBundleBuilder
{
    public const SCHEMA_VERSION = '1.0';
    public const SOURCE_LAB_RESULTS = 'lab_results';
    public const SOURCE_LAB_ORDERS = 'lab_orders';
    public const SOURCE_MEDICATIONS = 'medications';
    public const SOURCE_ALLERGIES = 'allergies';

    /** OpenEMR `ord_status` option ids -> contract LabOrderStatus (anything else -> unknown). */
    private const ORDER_STATUS_MAP = [
        'pending' => 'pending',
        'routed' => 'routed',
        'complete' => 'complete',
        'canceled' => 'canceled',
        'cancelled' => 'canceled',
    ];

    /** OpenEMR `proc_res_status` option ids -> contract LabResultStatus. */
    private const STATUS_MAP = [
        'final' => 'final',
        'prelim' => 'preliminary',
        'correct' => 'corrected',
        // ADR-009 section 7: a clinician-edited filed value is written as `corrected` (core `correct` maps to FHIR unknown).
        'corrected' => 'corrected',
        'incomplete' => 'incomplete',
    ];

    /**
     * OpenEMR `proc_res_abnormal` option ids -> contract AbnormalFlag.
     * `vhigh`/`vlow` ("very high/low") collapse to `high`/`low`: direction is
     * preserved, the magnitude qualifier is not represented by the contract.
     */
    private const ABNORMAL_MAP = [
        'no' => 'no',
        'yes' => 'yes',
        'high' => 'high',
        'low' => 'low',
        'vhigh' => 'high',
        'vlow' => 'low',
    ];

    /**
     * Result statuses excluded on purpose (not "unmapped"): a result un-filed as
     * entered-in-error keeps its history in OpenEMR and FHIR but leaves the
     * active bundle (ADR-009 section 7).
     */
    public const EXCLUDED_RESULT_STATUSES = ['entered-in-error' => 'entered_in_error'];

    /** @var array<string,int> counts of rows the contract could not carry (reported, never logged with content) */
    private array $omitted = ['empty_test_name' => 0, 'non_numeric_value' => 0, 'unmapped_status' => 0, 'unmapped_abnormal_flag' => 0, 'bad_timestamp' => 0, 'orders_omitted' => 0, 'medications_omitted' => 0];

    /** @var array<string,int> counts of rows left out deliberately, by reason */
    private array $excluded = ['entered_in_error' => 0];

    public function __construct(private readonly DateTimeZone $localZone)
    {
    }

    public function getLocalZone(): DateTimeZone
    {
        return $this->localZone;
    }

    /**
     * Build the bundle. `$labResults` is null when the lab source could not be read.
     *
     * @param array{pid:int, uuid:string} $patient
     * @param array{form_soap_id:int, encounter:int, note_date:string, plan:string} $note
     * @param list<array<string,mixed>>|null $labResults  null when the lab results source could not be read
     * @param string|null $userUuid  the authorized user's uuid; omitted from the bundle when unknown
     * @param list<array<string,mixed>>|null $labOrders  null when the orders source could not be read
     * @param list<array<string,mixed>>|null $medications  null when the medications source could not be read
     * @param string|null $nowLocal  local 'Y-m-d H:i:s' used to derive medication status (AUDIT DATA-003)
     * @param list<array<string,mixed>>|null $allergies  null when the allergies source could not be read
     * @param list<array<string,mixed>> $pendingFacts  candidate rows (ProcessingRepositoryInterface::listPendingFacts)
     * @return array{
     *   schema_version:string, correlation_id:string, patient_uuid:string, user_uuid?:string,
     *   prior_note:array{note_id:string, encounter_id:string, note_date:string, plan_text:string},
     *   data_quality:array{sources_unavailable:list<string>, duplicates_collapsed:int},
     *   lab_results:list<array<string,mixed>>,
     *   lab_orders:list<array<string,mixed>>,
     *   medications:list<array<string,mixed>>,
     *   allergies:list<array<string,mixed>>,
     *   pending_document_facts?:list<array<string,mixed>>
     * }
     */
    public function build(string $correlationId, array $patient, array $note, ?array $labResults, ?string $userUuid = null, ?array $labOrders = null, ?array $medications = null, ?string $nowLocal = null, ?array $allergies = null, array $pendingFacts = []): array
    {
        $noteDateUtc = UtcDate::toIso(Scalar::str($note['note_date']), $this->localZone);

        $results = [];
        $orders = [];
        $sourcesUnavailable = [];
        if ($labResults === null) {
            $sourcesUnavailable[] = self::SOURCE_LAB_RESULTS;
        } else {
            foreach ($labResults as $row) {
                $mapped = $this->mapResult($row);
                if ($mapped !== null) {
                    $results[] = $mapped;
                }
            }
        }
        if ($labOrders === null) {
            $sourcesUnavailable[] = self::SOURCE_LAB_ORDERS;
        } else {
            foreach ($labOrders as $row) {
                $mapped = $this->mapOrder($row);
                if ($mapped !== null) {
                    $orders[] = $mapped;
                }
            }
        }
        $meds = [];
        $duplicatesCollapsed = 0;
        if ($medications === null) {
            $sourcesUnavailable[] = self::SOURCE_MEDICATIONS;
        } else {
            $meds = $this->mapMedications($medications, $nowLocal, $duplicatesCollapsed);
        }

        $allergyRows = [];
        if ($allergies === null) {
            $sourcesUnavailable[] = self::SOURCE_ALLERGIES;
        } else {
            $allergyRows = $this->collapseAllergies($allergies, $duplicatesCollapsed);
        }

        $bundle = [
            'schema_version' => self::SCHEMA_VERSION,
            'correlation_id' => $correlationId,
            'patient_uuid' => $patient['uuid'],
            'prior_note' => [
                'note_id' => 'form_soap:' . Scalar::int($note['form_soap_id']),
                'encounter_id' => 'form_encounter:' . Scalar::int($note['encounter']),
                'note_date' => $noteDateUtc,
                'plan_text' => Scalar::str($note['plan']),
            ],
            'data_quality' => [
                'sources_unavailable' => $sourcesUnavailable,
                'duplicates_collapsed' => $duplicatesCollapsed,
            ],
            'lab_results' => $results,
            'lab_orders' => $orders,
            'medications' => $meds,
            'allergies' => $allergyRows,
        ];
        if ($userUuid !== null) {
            $bundle['user_uuid'] = $userUuid;
        }
        // Contract C5: optional, schema stays 1.0. Sent only when there is something to send,
        // so an agent that predates the field keeps accepting bundles without pending facts.
        $pending = $this->mapPendingFacts($pendingFacts);
        if ($pending !== []) {
            $bundle['pending_document_facts'] = $pending;
        }
        return $bundle;
    }

    /** @return array<string,int> */
    public function getOmittedCounts(): array
    {
        return $this->omitted;
    }

    /** @return array<string,int> rows left out on purpose (entered-in-error), counted apart from omissions */
    public function getExcludedCounts(): array
    {
        return $this->excluded;
    }

    /**
     * Chart lab rows in the bundle's `lab_results` shape, with the same mapping,
     * omissions and exclusions (the briefing's `prior_facts`, contract C4).
     *
     * @param list<array<string,mixed>> $rows
     * @return list<array<string,mixed>>
     */
    public function mapLabResults(array $rows): array
    {
        $out = [];
        foreach ($rows as $row) {
            $mapped = $this->mapResult($row);
            if ($mapped !== null) {
                $out[] = $mapped;
            }
        }
        return $out;
    }

    /**
     * Candidate rows as contract C5 PendingDocumentFact objects. Only candidates
     * of non-held documents reach this method (the repository filters).
     *
     * @param list<array<string,mixed>> $rows
     * @return list<array<string,mixed>>
     */
    public function mapPendingFacts(array $rows): array
    {
        $out = [];
        foreach ($rows as $r) {
            $id = Scalar::int($r['id'] ?? null);
            $testName = trim(Scalar::str($r['test_name'] ?? null));
            if ($id <= 0 || $testName === '') {
                continue;
            }
            $page = $r['page'] ?? null;
            $page = is_int($page) && $page > 0 ? $page : null;
            $out[] = [
                'fact_id' => 'copilot_extracted_value:' . $id,
                'document_id' => Scalar::int($r['document_id'] ?? null),
                'test_name' => $testName,
                'value_text' => self::nullableText($r['value_text'] ?? null),
                'unit' => self::nullableText($r['unit'] ?? null),
                'reference_range' => self::nullableText($r['reference_range'] ?? null),
                'abnormal_flag' => self::nullableText($r['abnormal_flag'] ?? null),
                'flag_source' => self::nullableText($r['flag_source'] ?? null) ?? 'unavailable',
                'collection_date' => self::nullableText($r['collection_date'] ?? null),
                'verification_status' => self::nullableText($r['verification_status'] ?? null) ?? 'unverified',
                'page' => $page,
                // C5 rejects a box without a page.
                'bbox' => $page === null ? null : CandidateMapper::bboxToList(self::nullableText($r['bbox'] ?? null)),
                'status' => 'candidate',
            ];
        }
        return $out;
    }

    private static function nullableText(mixed $value): ?string
    {
        $text = trim(Scalar::str($value));
        return $text === '' ? null : $text;
    }

    /**
     * @param array<string,mixed> $row
     * @return array<string,mixed>|null
     */
    private function mapResult(array $row): ?array
    {
        $testName = trim(Scalar::str($row['test_name'] ?? null));
        if ($testName === '') {
            $this->omitted['empty_test_name']++;
            return null;
        }
        $excludedAs = self::EXCLUDED_RESULT_STATUSES[strtolower(trim(Scalar::str($row['result_status'] ?? null)))] ?? null;
        if ($excludedAs !== null) {
            $this->excluded[$excludedAs]++;
            return null;
        }
        $value = trim(Scalar::str($row['value'] ?? null));
        if ($value === '' || !is_numeric($value)) {
            $this->omitted['non_numeric_value']++;
            return null;
        }
        $status = self::STATUS_MAP[strtolower(trim(Scalar::str($row['result_status'] ?? null)))] ?? null;
        if ($status === null) {
            $this->omitted['unmapped_status']++;
            return null;
        }
        try {
            $observedAt = UtcDate::toIso(Scalar::str($row['observed_at'] ?? null), $this->localZone);
        } catch (InvalidArgumentException) {
            $this->omitted['bad_timestamp']++;
            return null;
        }

        $abnormalRaw = strtolower(trim(Scalar::str($row['abnormal'] ?? null)));
        $abnormal = null;
        if ($abnormalRaw !== '') {
            $abnormal = self::ABNORMAL_MAP[$abnormalRaw] ?? null;
            if ($abnormal === null) {
                $this->omitted['unmapped_abnormal_flag']++;
            }
        }

        $units = trim(Scalar::str($row['units'] ?? null));
        $code = trim(Scalar::str($row['code'] ?? null));
        $range = trim(Scalar::str($row['range'] ?? null));
        $orderId = Scalar::int($row['order_id'] ?? null);

        return [
            'result_id' => 'procedure_result:' . Scalar::int($row['result_id'] ?? null),
            'order_id' => $orderId > 0 ? 'procedure_order:' . $orderId : null,
            'test_name' => $testName,
            'code' => $code === '' ? null : $code,
            'value' => $value,
            'units' => $units === '' ? null : $units,
            'abnormal_flag' => $abnormal,
            'range' => $range === '' ? null : $range,
            'status' => $status,
            'observed_at' => $observedAt,
        ];
    }

    /**
     * Medication rows in the bundle's `medications` shape: mapped, status derived, exact
     * duplicates collapsed (first row kept). Shared by the bundle and the document
     * briefing's `chart_medications` (ADR-010), so the two can never disagree.
     *
     * @param list<array<string,mixed>> $rows
     * @param string|null $nowLocal  local 'Y-m-d H:i:s' used to derive status; null = now
     * @return list<array<string,mixed>>
     */
    public function mapMedications(array $rows, ?string $nowLocal, int &$duplicatesCollapsed = 0): array
    {
        $nowUtc = $nowLocal === null ? gmdate(UtcDate::FORMAT) : UtcDate::toIso($nowLocal, $this->localZone);
        $meds = [];
        $byKey = [];
        foreach ($rows as $row) {
            $mapped = $this->mapMedication($row, $nowUtc);
            if ($mapped === null) {
                continue;
            }
            $key = implode('|', [
                Scalar::str($mapped['source_table']),
                strtolower(Scalar::str($mapped['drug_name'])),
                Scalar::str($mapped['started_at'] ?? ''),
                var_export($mapped['active'], true),
            ]);
            if (isset($byKey[$key])) {
                $duplicatesCollapsed++;
                continue; // first row (earliest, lowest id) is kept
            }
            $byKey[$key] = count($meds);
            $meds[] = $mapped;
        }
        return $meds;
    }

    /**
     * Map one medication row from either table; derive status per AUDIT DATA-003:
     * active iff flag = 1 AND (end empty OR end > now); indeterminate (null) when the
     * flag says active but the end date has passed, or the flag says inactive but an
     * end date lies in the future.
     *
     * @param array<string,mixed> $row
     * @return array<string,mixed>|null
     */
    private function mapMedication(array $row, string $nowUtc): ?array
    {
        $source = Scalar::str($row['source_table'] ?? null);
        $id = Scalar::int($row['id'] ?? null);
        $drug = trim(Scalar::str($row['drug'] ?? null));
        if (!in_array($source, ['prescriptions', 'lists'], true) || $id <= 0 || $drug === '') {
            $this->omitted['medications_omitted']++;
            return null;
        }
        $started = $this->optionalIso(Scalar::str($row['begdate'] ?? null)) ?? $this->optionalIso(Scalar::str($row['date_added'] ?? null));
        $ended = $this->optionalIso(Scalar::str($row['enddate'] ?? null));
        $modified = $this->optionalIso(Scalar::str($row['date_modified'] ?? null));
        $added = $this->optionalIso(Scalar::str($row['date_added'] ?? null));
        $timestamp = $modified !== null && $added !== null ? max($modified, $added) : ($modified ?? $added ?? $started);
        $timestampField = $modified !== null && ($added === null || $modified >= $added) ? 'date_modified' : ($added !== null ? 'date_added' : 'begdate');
        if ($timestamp === null) {
            $this->omitted['medications_omitted']++;
            return null;
        }
        $flag = Scalar::int($row['active'] ?? null) === 1;
        $endPassed = $ended !== null && $ended <= $nowUtc;
        $active = $flag && !$endPassed ? true : (!$flag && ($ended === null || $endPassed) ? false : null);
        $flagField = $source === 'prescriptions' ? 'active' : 'activity';
        $endField = $source === 'prescriptions' ? 'end_date' : 'enddate';
        $rxnorm = trim(Scalar::str($row['rxnorm'] ?? null));
        $dosage = trim(Scalar::str($row['dosage'] ?? null));
        return [
            'record_id' => $source . ':' . $id,
            'source_table' => $source,
            'drug_name' => $drug,
            'rxnorm_code' => $rxnorm === '' ? null : $rxnorm,
            'dosage_text' => $dosage === '' ? null : $dosage,
            'active' => $active,
            'status_field' => $flagField . ',' . $endField,
            'status_value' => $flagField . '=' . ($flag ? '1' : '0') . ',' . $endField . '=' . ($ended ?? 'null'),
            'started_at' => $started,
            'ended_at' => $ended,
            'modified_at' => $modified,
            'timestamp' => $timestamp,
            'timestamp_field' => $timestampField,
        ];
    }

    /**
     * Allergy entries as recorded; exact duplicates (same code or lower(title), same begdate)
     * collapse with a count (AUDIT DATA-004). Uncoded stays uncoded; nothing is inferred.
     *
     * @param list<array<string,mixed>> $rows
     * @return list<array<string,mixed>>
     */
    public function collapseAllergies(array $rows, int &$duplicatesCollapsed): array
    {
        $out = [];
        $index = [];
        foreach ($rows as $a) {
            $title = trim(Scalar::str($a['title'] ?? null));
            $id = Scalar::int($a['id'] ?? null);
            if ($title === '' || $id <= 0) {
                continue;
            }
            $code = trim(Scalar::str($a['diagnosis'] ?? null));
            $begRaw = trim(Scalar::str($a['begdate'] ?? null));
            $endRaw = trim(Scalar::str($a['enddate'] ?? null));
            $key = ($code !== '' ? 'code:' . strtolower($code) : 'title:' . strtolower($title)) . '|' . substr($begRaw, 0, 10);
            if (isset($index[$key])) {
                $out[$index[$key]]['duplicate_count'] = Scalar::int($out[$index[$key]]['duplicate_count'] ?? 1) + 1;
                $duplicatesCollapsed++;
                continue;
            }
            $ended = $this->optionalIso($endRaw);
            $reaction = trim(Scalar::str($a['reaction'] ?? null));
            $severity = trim(Scalar::str($a['severity'] ?? null));
            $index[$key] = count($out);
            $out[] = [
                'record_id' => 'lists:' . $id,
                'title' => $title,
                'coded' => $code !== '',
                'code' => $code === '' ? null : $code,
                'reaction' => $reaction === '' ? null : $reaction,
                'severity' => $severity === '' ? null : $severity,
                'active' => Scalar::int($a['activity'] ?? null) === 1 && $ended === null,
                'begdate' => $this->optionalIso($begRaw),
                'enddate' => $ended,
                'duplicate_count' => 1,
            ];
        }
        return $out;
    }

    /** UTC ISO for a local date string, or null when empty/zero/unparseable. */
    private function optionalIso(string $local): ?string
    {
        try {
            return UtcDate::toIso($local, $this->localZone);
        } catch (InvalidArgumentException) {
            return null;
        }
    }

    /**
     * @param array<string,mixed> $row
     * @return array<string,mixed>|null
     */
    private function mapOrder(array $row): ?array
    {
        $testName = trim(Scalar::str($row['test_name'] ?? null));
        $orderId = Scalar::int($row['order_id'] ?? null);
        $seq = Scalar::int($row['seq'] ?? null);
        if ($testName === '' || $orderId <= 0 || $seq <= 0) {
            $this->omitted['orders_omitted']++;
            return null;
        }
        try {
            $orderedAt = UtcDate::toIso(Scalar::str($row['ordered_at'] ?? null), $this->localZone);
        } catch (InvalidArgumentException) {
            $this->omitted['orders_omitted']++;
            return null;
        }
        $code = trim(Scalar::str($row['code'] ?? null));
        $status = self::ORDER_STATUS_MAP[strtolower(trim(Scalar::str($row['order_status'] ?? null)))] ?? 'unknown';
        return [
            'order_id' => 'procedure_order:' . $orderId,
            'sequence' => $seq,
            'test_name' => $testName,
            'code' => $code === '' ? null : $code,
            'status' => $status,
            'ordered_at' => $orderedAt,
        ];
    }
}
