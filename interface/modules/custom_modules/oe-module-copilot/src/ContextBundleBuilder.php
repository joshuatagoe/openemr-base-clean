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
 * - Only lab results observed after the baseline note are included; the
 *   agent's matcher re-applies the strict window.
 * - Empty units become null; result status and abnormal flag are mapped to
 *   the contract's closed vocabularies (OpenEMR's `vhigh`/`vlow` collapse to
 *   `high`/`low`); rows the contract cannot represent are omitted and counted.
 *   Deferred, documented: results with status `cancel`/`error`/blank and
 *   non-numeric (text) results are not carried yet.
 * - All timestamps are UTC ISO-8601.
 * - An unavailable lab source is declared in data_quality.sources_unavailable;
 *   it is never rendered as an empty list.
 *
 * @package   OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot;

use DateTimeZone;
use InvalidArgumentException;
use OpenEMR\Modules\Copilot\Support\Scalar;
use OpenEMR\Modules\Copilot\Support\UtcDate;

final class ContextBundleBuilder
{
    public const SCHEMA_VERSION = '1.0';
    public const SOURCE_LAB_RESULTS = 'lab_results';

    /** OpenEMR `proc_res_status` option ids -> contract LabResultStatus. */
    private const STATUS_MAP = [
        'final' => 'final',
        'prelim' => 'preliminary',
        'correct' => 'corrected',
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

    /** @var array<string,int> counts of rows the contract could not carry (reported, never logged with content) */
    private array $omitted = ['empty_test_name' => 0, 'non_numeric_value' => 0, 'unmapped_status' => 0, 'unmapped_abnormal_flag' => 0, 'bad_timestamp' => 0];

    public function __construct(private readonly DateTimeZone $localZone)
    {
    }

    /**
     * Build the bundle. `$labResults` is null when the lab source could not be read.
     *
     * @param array{pid:int, uuid:string} $patient
     * @param array{form_soap_id:int, encounter:int, note_date:string, plan:string} $note
     * @param list<array<string,mixed>>|null $labResults
     * @return array{
     *   schema_version:string, correlation_id:string, patient_uuid:string,
     *   prior_note:array{note_id:string, encounter_id:string, note_date:string, plan_text:string},
     *   data_quality:array{sources_unavailable:list<string>, duplicates_collapsed:int},
     *   lab_results:list<array<string,mixed>>
     * }
     */
    public function build(string $correlationId, array $patient, array $note, ?array $labResults): array
    {
        $noteDateUtc = UtcDate::toIso(Scalar::str($note['note_date']), $this->localZone);

        $results = [];
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

        return [
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
                'duplicates_collapsed' => 0,
            ],
            'lab_results' => $results,
        ];
    }

    /** @return array<string,int> */
    public function getOmittedCounts(): array
    {
        return $this->omitted;
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

        return [
            'result_id' => 'procedure_result:' . Scalar::int($row['result_id'] ?? null),
            'test_name' => $testName,
            'value' => $value,
            'units' => $units === '' ? null : $units,
            'abnormal_flag' => $abnormal,
            'status' => $status,
            'observed_at' => $observedAt,
        ];
    }
}
