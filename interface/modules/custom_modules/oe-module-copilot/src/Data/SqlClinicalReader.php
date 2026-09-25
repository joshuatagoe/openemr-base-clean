<?php

/**
 * Read-only reader over OpenEMR tables, parameter-bound, through the same
 * audited QueryUtils/sqlStatement path the src/Services classes use.
 *
 * Sources (verified against sql/database.sql and the src/Services layer):
 *  - patient_data.uuid                         -> patient_uuid
 *  - users.uuid                                -> user_uuid (ticket subject)
 *  - form_encounter (pid, encounter, date)     -> as-of timestamp of the current encounter
 *  - forms (formdir='soap', deleted=0) JOIN form_soap ON form_soap.id = forms.form_id
 *      form_soap.id, forms.encounter, form_soap.date, form_soap.plan
 *  - procedure_order (patient_id, date_ordered)
 *      -> procedure_report (procedure_order_id, date_report)
 *      -> procedure_result (procedure_report_id, result_code, result_text, result,
 *         units, range, abnormal, result_status, date)
 *    observed_at = COALESCE(procedure_result.date, procedure_report.date_report,
 *                           procedure_order.date_ordered)
 *  - procedure_order JOIN procedure_order_code (one row per ordered test)
 *  - patient_data (fname, lname, DOB, sex, pubpid)  -> identity line (panel only)
 *  - openemr_postcalendar_events (pc_pid varchar, compared as string) -> scheduled reason
 *  - form_encounter.reason                           -> baseline encounter reason
 *  - lists (type = 'allergy')                        -> allergies as recorded
 *  - prescriptions UNION lists (type = 'medication') -> medications, both kept
 *    with their source_table (AUDIT DATA-001); status derived by the builder
 *
 * Why not ProcedureService::search: it omits procedure_result.date (needed for
 * the evidence window), applies FHIR search-field plumbing for the patient
 * filter, and the public /api/procedure route is not patient-bound.
 *
 * @package   OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Data;

use OpenEMR\Common\Database\QueryUtils;
use OpenEMR\Common\Uuid\UuidRegistry;
use OpenEMR\Modules\Copilot\Support\Scalar;
use Throwable;

final class SqlClinicalReader implements ClinicalReaderInterface
{
    public function findPatient(int $pid): ?array
    {
        $rows = QueryUtils::fetchRecords("SELECT pid, uuid FROM patient_data WHERE pid = ? LIMIT 1", [$pid]);
        $row = $rows[0] ?? null;
        if (!is_array($row) || !is_string($row['uuid'] ?? null) || $row['uuid'] === '') {
            return null;
        }
        return ['pid' => Scalar::int($row['pid'] ?? null), 'uuid' => UuidRegistry::uuidToString($row['uuid'])];
    }

    public function findUserUuid(int $userId): ?string
    {
        $rows = QueryUtils::fetchRecords("SELECT uuid FROM users WHERE id = ? LIMIT 1", [$userId]);
        $row = $rows[0] ?? null;
        if (!is_array($row) || !is_string($row['uuid'] ?? null) || $row['uuid'] === '') {
            return null;
        }
        return UuidRegistry::uuidToString($row['uuid']);
    }

    public function findEncounterDate(int $pid, int $encounter): ?string
    {
        try {
            $rows = QueryUtils::fetchRecords(
                "SELECT `date` FROM form_encounter WHERE pid = ? AND encounter = ? LIMIT 1",
                [$pid, $encounter]
            );
        } catch (Throwable $e) {
            throw new SourceUnavailableException('encounters', $e);
        }
        $row = $rows[0] ?? null;
        $date = is_array($row) ? Scalar::str($row['date'] ?? null) : '';
        return $date === '' ? null : $date;
    }

    public function findLatestSoapPlanBefore(int $pid, string $asOfLocal): ?array
    {
        try {
            $rows = QueryUtils::fetchRecords(
                "SELECT fs.id AS form_soap_id, fo.encounter, fs.date AS note_date, fs.plan
                   FROM forms fo
                   JOIN form_soap fs ON fs.id = fo.form_id
                  WHERE fo.pid = ? AND fo.formdir = 'soap' AND fo.deleted = 0
                    AND fs.pid = ? AND fs.date < ?
                    AND fs.plan IS NOT NULL AND TRIM(fs.plan) <> ''
                  ORDER BY fs.date DESC, fs.id DESC
                  LIMIT 1",
                [$pid, $pid, $asOfLocal]
            );
        } catch (Throwable $e) {
            throw new SourceUnavailableException('soap_notes', $e);
        }
        $row = $rows[0] ?? null;
        if (!is_array($row)) {
            return null;
        }
        return [
            'form_soap_id' => Scalar::int($row['form_soap_id'] ?? null),
            'encounter' => Scalar::int($row['encounter'] ?? null),
            'note_date' => Scalar::str($row['note_date'] ?? null),
            'plan' => Scalar::str($row['plan'] ?? null),
        ];
    }

    public function listLabResults(int $pid, string $sinceLocal, int $limit): array
    {
        try {
            // The NEWEST $limit results after $sinceLocal, returned oldest-first. With Week 1's short
            // since-last-note window every result fits, so this is identical to before; for the Week 2
            // chart history (all time) a cap must drop the oldest results, never the recent ones.
            $rows = QueryUtils::fetchRecords(
                "SELECT * FROM (
                    SELECT pr.procedure_result_id AS result_id,
                           po.procedure_order_id AS order_id,
                           pr.result_text AS test_name,
                           pr.result_code AS code,
                           pr.result AS value,
                           pr.units,
                           pr.`range` AS `range`,
                           pr.abnormal,
                           pr.result_status,
                           COALESCE(pr.date, rp.date_report, po.date_ordered) AS observed_at
                      FROM procedure_order po
                      JOIN procedure_report rp ON rp.procedure_order_id = po.procedure_order_id
                      JOIN procedure_result pr ON pr.procedure_report_id = rp.procedure_report_id
                     WHERE po.patient_id = ?
                       AND COALESCE(pr.date, rp.date_report, po.date_ordered) > ?
                     ORDER BY observed_at DESC, pr.procedure_result_id DESC
                     LIMIT " . max(1, min($limit, 500)) . "
                 ) newest
                 ORDER BY observed_at ASC, result_id ASC",
                [$pid, $sinceLocal]
            );
        } catch (Throwable $e) {
            throw new SourceUnavailableException('lab_results', $e);
        }
        return array_map(static fn(array $r): array => [
            'result_id' => Scalar::int($r['result_id'] ?? null),
            'order_id' => Scalar::int($r['order_id'] ?? null),
            'test_name' => Scalar::str($r['test_name'] ?? null),
            'code' => Scalar::str($r['code'] ?? null),
            'value' => Scalar::str($r['value'] ?? null),
            'units' => Scalar::str($r['units'] ?? null),
            'range' => Scalar::str($r['range'] ?? null),
            'abnormal' => Scalar::str($r['abnormal'] ?? null),
            'result_status' => Scalar::str($r['result_status'] ?? null),
            'observed_at' => Scalar::str($r['observed_at'] ?? null),
        ], $rows);
    }

    public function listLabOrders(int $pid, string $sinceLocal, int $limit): array
    {
        try {
            $rows = QueryUtils::fetchRecords(
                "SELECT po.procedure_order_id AS order_id,
                        oc.procedure_order_seq AS seq,
                        oc.procedure_name AS test_name,
                        oc.procedure_code AS code,
                        po.order_status,
                        po.date_ordered AS ordered_at
                   FROM procedure_order po
                   JOIN procedure_order_code oc ON oc.procedure_order_id = po.procedure_order_id
                  WHERE po.patient_id = ? AND po.activity = 1
                    AND po.date_ordered IS NOT NULL AND po.date_ordered > ?
                  ORDER BY po.date_ordered ASC, po.procedure_order_id ASC, oc.procedure_order_seq ASC
                  LIMIT " . max(1, min($limit, 500)),
                [$pid, $sinceLocal]
            );
        } catch (Throwable $e) {
            throw new SourceUnavailableException('lab_orders', $e);
        }
        return array_map(static fn(array $r): array => [
            'order_id' => Scalar::int($r['order_id'] ?? null),
            'seq' => Scalar::int($r['seq'] ?? null),
            'test_name' => Scalar::str($r['test_name'] ?? null),
            'code' => Scalar::str($r['code'] ?? null),
            'order_status' => Scalar::str($r['order_status'] ?? null),
            'ordered_at' => Scalar::str($r['ordered_at'] ?? null),
        ], $rows);
    }

    public function findIdentity(int $pid): ?array
    {
        try {
            $rows = QueryUtils::fetchRecords("SELECT fname, lname, DOB, sex, pubpid FROM patient_data WHERE pid = ? LIMIT 1", [$pid]);
        } catch (Throwable $e) {
            throw new SourceUnavailableException('identity', $e);
        }
        $row = $rows[0] ?? null;
        if (!is_array($row)) {
            return null;
        }
        return [
            'fname' => Scalar::str($row['fname'] ?? null),
            'lname' => Scalar::str($row['lname'] ?? null),
            'dob' => Scalar::str($row['DOB'] ?? null),
            'sex' => Scalar::str($row['sex'] ?? null),
            'pubpid' => Scalar::str($row['pubpid'] ?? null),
        ];
    }

    public function findAppointmentReason(int $pid, string $localDate): ?array
    {
        try {
            $rows = QueryUtils::fetchRecords(
                "SELECT pc_hometext, pc_title, pc_eventDate, pc_startTime
                   FROM openemr_postcalendar_events
                  WHERE pc_pid = ? AND pc_eventDate = ? AND pc_apptstatus <> 'x'
                  ORDER BY pc_startTime ASC, pc_eid ASC
                  LIMIT 1",
                [(string) $pid, $localDate]
            );
        } catch (Throwable $e) {
            throw new SourceUnavailableException('appointment', $e);
        }
        $row = $rows[0] ?? null;
        if (!is_array($row)) {
            return null;
        }
        $text = trim(Scalar::str($row['pc_hometext'] ?? null));
        if ($text === '') {
            $text = trim(Scalar::str($row['pc_title'] ?? null));
        }
        $date = trim(Scalar::str($row['pc_eventDate'] ?? null) . ' ' . Scalar::str($row['pc_startTime'] ?? null));
        return ['text' => $text, 'date' => $date, 'source' => 'openemr_postcalendar_events'];
    }

    public function findEncounterReason(int $pid, int $encounter): ?array
    {
        try {
            $rows = QueryUtils::fetchRecords(
                "SELECT reason, `date` FROM form_encounter WHERE pid = ? AND encounter = ? LIMIT 1",
                [$pid, $encounter]
            );
        } catch (Throwable $e) {
            throw new SourceUnavailableException('encounters', $e);
        }
        $row = $rows[0] ?? null;
        if (!is_array($row)) {
            return null;
        }
        return [
            'text' => trim(Scalar::str($row['reason'] ?? null)),
            'date' => Scalar::str($row['date'] ?? null),
            'source' => 'form_encounter',
        ];
    }

    public function listMedications(int $pid, int $limit): array
    {
        try {
            $rows = QueryUtils::fetchRecords(
                "SELECT 'prescriptions' AS source_table, p.id, p.drug, COALESCE(p.rxnorm_drugcode, '') AS rxnorm,
                        COALESCE(p.dosage, '') AS dosage, p.active,
                        COALESCE(p.start_date, '') AS begdate, COALESCE(p.end_date, '') AS enddate,
                        COALESCE(p.date_added, '') AS date_added, COALESCE(p.date_modified, '') AS date_modified
                   FROM prescriptions p
                  WHERE p.patient_id = ?
                  UNION ALL
                 SELECT 'lists' AS source_table, l.id, l.title AS drug, COALESCE(l.diagnosis, '') AS rxnorm,
                        '' AS dosage, COALESCE(l.activity, 0) AS active,
                        COALESCE(l.begdate, '') AS begdate, COALESCE(l.enddate, '') AS enddate,
                        COALESCE(l.`date`, '') AS date_added, '' AS date_modified
                   FROM lists l
                  WHERE l.pid = ? AND l.type = 'medication'
                  ORDER BY date_added ASC, id ASC
                  LIMIT " . max(1, min($limit, 500)),
                [$pid, $pid]
            );
        } catch (Throwable $e) {
            throw new SourceUnavailableException('medications', $e);
        }
        return array_map(static fn(array $r): array => [
            'source_table' => Scalar::str($r['source_table'] ?? null),
            'id' => Scalar::int($r['id'] ?? null),
            'drug' => Scalar::str($r['drug'] ?? null),
            'rxnorm' => Scalar::str($r['rxnorm'] ?? null),
            'dosage' => Scalar::str($r['dosage'] ?? null),
            'active' => Scalar::int($r['active'] ?? null),
            'begdate' => Scalar::str($r['begdate'] ?? null),
            'enddate' => Scalar::str($r['enddate'] ?? null),
            'date_added' => Scalar::str($r['date_added'] ?? null),
            'date_modified' => Scalar::str($r['date_modified'] ?? null),
        ], $rows);
    }

    public function listAllergies(int $pid): array
    {
        try {
            $rows = QueryUtils::fetchRecords(
                "SELECT id, title, diagnosis, reaction, severity_al, begdate, enddate, activity
                   FROM lists
                  WHERE pid = ? AND type = 'allergy'
                  ORDER BY begdate ASC, id ASC
                  LIMIT 200",
                [$pid]
            );
        } catch (Throwable $e) {
            throw new SourceUnavailableException('allergies', $e);
        }
        return array_map(static fn(array $r): array => [
            'id' => Scalar::int($r['id'] ?? null),
            'title' => Scalar::str($r['title'] ?? null),
            'diagnosis' => Scalar::str($r['diagnosis'] ?? null),
            'reaction' => Scalar::str($r['reaction'] ?? null),
            'severity' => Scalar::str($r['severity_al'] ?? null),
            'begdate' => Scalar::str($r['begdate'] ?? null),
            'enddate' => Scalar::str($r['enddate'] ?? null),
            'activity' => Scalar::int($r['activity'] ?? null),
        ], $rows);
    }
}
