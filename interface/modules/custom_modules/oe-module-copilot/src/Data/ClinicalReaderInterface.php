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
 * @phpstan-type LabRow array{result_id:int, order_id:int, test_name:string, code:string, value:string, units:string, range:string, abnormal:string, result_status:string, observed_at:string}
 * @phpstan-type OrderRow array{order_id:int, seq:int, test_name:string, code:string, order_status:string, ordered_at:string}
 * @phpstan-type IdentityRow array{fname:string, lname:string, dob:string, sex:string, pubpid:string}
 * @phpstan-type AllergyRow array{id:int, title:string, diagnosis:string, reaction:string, severity:string, begdate:string, enddate:string, activity:int}
 * @phpstan-type ReasonRow array{text:string, date:string, source:string}
 * @phpstan-type MedicationRow array{source_table:string, id:int, drug:string, rxnorm:string, dosage:string, active:int, begdate:string, enddate:string, date_added:string, date_modified:string}
 */
interface ClinicalReaderInterface
{
    /**
     * @return array{pid:int, uuid:string}|null  null when the patient does not exist
     */
    public function findPatient(int $pid): ?array;

    /**
     * The user's uuid (string form), or null when the user does not exist or
     * has no uuid on file. Never creates one: this reader is strictly read-only.
     */
    public function findUserUuid(int $userId): ?string;

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

    /**
     * Ordered tests (one row per procedure_order_code) placed strictly after `$sinceLocal`.
     *
     * @return list<OrderRow>
     * @throws SourceUnavailableException
     */
    public function listLabOrders(int $pid, string $sinceLocal, int $limit): array;

    /**
     * Name, DOB, sex and public id for the panel's identity line (never sent to the agent).
     *
     * @return IdentityRow|null
     * @throws SourceUnavailableException
     */
    public function findIdentity(int $pid): ?array;

    /**
     * Today's scheduled appointment reason (openemr_postcalendar_events.pc_hometext, else pc_title)
     * for the local date `$localDate` (Y-m-d); null when no appointment is on the calendar.
     *
     * @return ReasonRow|null
     * @throws SourceUnavailableException
     */
    public function findAppointmentReason(int $pid, string $localDate): ?array;

    /**
     * The reason recorded on one of the patient's encounters (form_encounter.reason), or null.
     *
     * @return ReasonRow|null
     * @throws SourceUnavailableException
     */
    public function findEncounterReason(int $pid, int $encounter): ?array;

    /**
     * Every medication row from both sources (prescriptions, and lists with type
     * 'medication'), not windowed: a "continue" commitment needs older rows.
     * `active` is the raw flag (prescriptions.active / lists.activity); dates are
     * raw local strings ('' when null).
     *
     * @return list<MedicationRow>
     * @throws SourceUnavailableException
     */
    public function listMedications(int $pid, int $limit): array;

    /**
     * Allergy entries (lists.type = 'allergy') as recorded, active or not. Zero rows means
     * "no allergy entries on file", never "no known allergies".
     *
     * @return list<AllergyRow>
     * @throws SourceUnavailableException
     */
    public function listAllergies(int $pid): array;
}
