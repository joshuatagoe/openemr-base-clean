<?php

/**
 * Read-only reader over OpenEMR tables, parameter-bound, through the same
 * audited QueryUtils/sqlStatement path the src/Services classes use.
 *
 * Sources (verified against sql/database.sql and the src/Services layer):
 *  - patient_data.uuid                         -> patient_uuid
 *  - form_encounter (pid, encounter, date)     -> as-of timestamp of the current encounter
 *  - forms (formdir='soap', deleted=0) JOIN form_soap ON form_soap.id = forms.form_id
 *      form_soap.id, forms.encounter, form_soap.date, form_soap.plan
 *  - procedure_order (patient_id, date_ordered)
 *      -> procedure_report (procedure_order_id, date_report)
 *      -> procedure_result (procedure_report_id, result_text, result, units,
 *         abnormal, result_status, date)
 *    observed_at = COALESCE(procedure_result.date, procedure_report.date_report,
 *                           procedure_order.date_ordered)
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
            $rows = QueryUtils::fetchRecords(
                "SELECT pr.procedure_result_id AS result_id,
                        pr.result_text AS test_name,
                        pr.result AS value,
                        pr.units,
                        pr.abnormal,
                        pr.result_status,
                        COALESCE(pr.date, rp.date_report, po.date_ordered) AS observed_at
                   FROM procedure_order po
                   JOIN procedure_report rp ON rp.procedure_order_id = po.procedure_order_id
                   JOIN procedure_result pr ON pr.procedure_report_id = rp.procedure_report_id
                  WHERE po.patient_id = ?
                    AND COALESCE(pr.date, rp.date_report, po.date_ordered) > ?
                  ORDER BY observed_at ASC, pr.procedure_result_id ASC
                  LIMIT " . max(1, min($limit, 500)),
                [$pid, $sinceLocal]
            );
        } catch (Throwable $e) {
            throw new SourceUnavailableException('lab_results', $e);
        }
        return array_map(static fn(array $r): array => [
            'result_id' => Scalar::int($r['result_id'] ?? null),
            'test_name' => Scalar::str($r['test_name'] ?? null),
            'value' => Scalar::str($r['value'] ?? null),
            'units' => Scalar::str($r['units'] ?? null),
            'abnormal' => Scalar::str($r['abnormal'] ?? null),
            'result_status' => Scalar::str($r['result_status'] ?? null),
            'observed_at' => Scalar::str($r['observed_at'] ?? null),
        ], $rows);
    }
}
