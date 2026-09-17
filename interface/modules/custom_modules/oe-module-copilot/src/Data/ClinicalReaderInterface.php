<?php

/**
 * Read-only access to the clinical records the ContextBundle needs.
 *
 * Implementations must be strictly read-only and must raise
 * SourceUnavailableException (never return an empty result) when a source
 * cannot be read, so that "could not check" is distinguishable from
 * "checked, nothing there".
 *
 * All date/time strings are OpenEMR local-time DATETIME values.
 *
 * @package   OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Data;

/**
 * @phpstan-type NoteRow array{form_soap_id:int, encounter:int, note_date:string, plan:string}
 * @phpstan-type LabRow array{result_id:int, test_name:string, value:string, units:string, abnormal:string, result_status:string, observed_at:string}
 */
interface ClinicalReaderInterface
{
    /**
     * @return array{pid:int, uuid:string}|null  null when the patient does not exist
     */
    public function findPatient(int $pid): ?array;

    /**
     * Local timestamp of one of the patient's encounters, or null when the
     * encounter does not exist or does not belong to the patient.
     *
     * @throws SourceUnavailableException
     */
    public function findEncounterDate(int $pid, int $encounter): ?string;

    /**
     * The latest SOAP note with non-blank plan text dated strictly before
     * `$asOfLocal`. Ties on date are broken by the highest form_soap id.
     *
     * @return NoteRow|null
     * @throws SourceUnavailableException
     */
    public function findLatestSoapPlanBefore(int $pid, string $asOfLocal): ?array;

    /**
     * Lab results for the patient observed strictly after `$sinceLocal`.
     *
     * @return list<LabRow>
     * @throws SourceUnavailableException
     */
    public function listLabResults(int $pid, string $sinceLocal, int $limit): array;
}
