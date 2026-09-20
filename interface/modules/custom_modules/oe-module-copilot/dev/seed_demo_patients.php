<?php

/**
 * DEVELOPMENT-ONLY seed: one synthetic patient per evidence-state scenario, so
 * manual testing and the demo can walk a schedule where every patient shows a
 * different outcome of the plan check. Complements seed_evelyn_demo.php (the
 * tracer-bullet patient) and mirrors the fixture cases in
 * copilot-agent/fixtures/cases/.
 *
 * Patients (all synthetic; primary provider = admin so the care-relationship
 * check passes; each has an appointment today with a scheduled reason):
 *
 *   Marcus Hale   "Repeat potassium in two weeks. Start lisinopril 10 mg daily."
 *                 potassium result after the note (matching_result_found);
 *                 lisinopril prescription started after the note
 *                 (matching_medication_record_found)
 *   Priya Nair    "Order lipid panel. Continue atorvastatin."
 *                 lipid panel ordered, no result (order_found_no_result);
 *                 atorvastatin active in the medication list (lists table)
 *   Thomas Reyes  "Repeat HbA1c in three months. Stop lisinopril."
 *                 no HbA1c after the note (no_matching_record_found);
 *                 lisinopril still active in both tables (conflicting_records)
 *   Grace Okafor  "Repeat TSH."
 *                 the only TSH result predates the note: not evidence
 *                 (no_matching_record_found) - guards against back-dated proof
 *   Daniel Kim    "Recheck labs at next visit."
 *                 no test named (ambiguous_match); an interval creatinine result
 *                 exists and shows in the interval list as unexplained
 *   Henry Walsh   "Repeat basic metabolic panel. Refer to cardiology."
 *                 BMP reported final then corrected on the same order
 *                 (corrected supersedes; both shown); the referral is kind
 *                 `other` and rendered as not checked; no allergy entries on
 *                 file (rendered as "not confirmed NKA")
 *   Sofia Marin   no prior note at all: the panel shows "no prior plan" and the
 *                 module reports ticket.no_prior_note
 *
 * Safety: identical gates to seed_evelyn_demo.php - CLI only, --confirm-local,
 * refuses on OPENEMR__ENVIRONMENT=prod or a non-local database host unless
 * --target-demo-database is given explicitly (demo instances only).
 * Idempotent by patient name; re-running adds nothing.
 *
 * Usage (inside the development container, as the web user):
 *   docker compose -f docker/development-easy/docker-compose.yml exec openemr \
 *     su -s /bin/sh apache -c 'php interface/modules/custom_modules/oe-module-copilot/dev/seed_demo_patients.php --confirm-local'
 *
 * @package   OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

if (PHP_SAPI !== 'cli') {
    http_response_code(404);
    exit;
}
$cliArgs = $argv ?? [];
if (!in_array('--confirm-local', $cliArgs, true)) {
    fwrite(STDERR, "Refusing to run without --confirm-local (development database only).\n");
    exit(2);
}
$environment = $_ENV['OPENEMR__ENVIRONMENT'] ?? getenv('OPENEMR__ENVIRONMENT');
if ($environment === 'prod') {
    fwrite(STDERR, "Refusing to run: OPENEMR__ENVIRONMENT=prod.\n");
    exit(2);
}

$ignoreAuth = true;
$sessionAllowWrite = true;
$_GET['site'] = 'default'; // @phpstan-ignore-line
require_once __DIR__ . '/../../../../globals.php';
require_once __DIR__ . '/../src/Support/Scalar.php';

use OpenEMR\Common\Database\QueryUtils;
use OpenEMR\Common\Uuid\UuidRegistry;
use OpenEMR\Modules\Copilot\Support\Scalar;
use OpenEMR\Services\PatientService;

$dbHost = is_array($sqlconf ?? null) ? Scalar::str($sqlconf['host'] ?? null) : '';
if (!in_array($dbHost, ['localhost', '127.0.0.1', 'mysql', 'mariadb'], true)) {
    // A non-local host is refused unless the operator states, per run, that this database holds demo data
    // only (e.g. the Railway demo instance). The OPENEMR__ENVIRONMENT=prod refusal above still applies.
    if (!in_array('--target-demo-database', $cliArgs, true)) {
        fwrite(STDERR, "Refusing to run: database host '{$dbHost}' is not a local/compose host. Add --target-demo-database only for a database that holds synthetic demo data.\n");
        exit(2);
    }
    fwrite(STDERR, "Seeding non-local database host '{$dbHost}' because --target-demo-database was given.\n");
}

const ADMIN_USER_ID = 1;
const NOTE_DATE = '2026-06-10 14:30:00';   // every prior note; "after the note" means later than this

$firstRowInt = static function (mixed $rows, string $key): int {
    $row = is_array($rows) ? ($rows[0] ?? null) : null;
    return Scalar::int(is_array($row) ? ($row[$key] ?? null) : null);
};
$uuid = static fn(string $table): string => UuidRegistry::getRegistryForTable($table)->createUuid();

// ----------------------------------------------------------------------------- helpers

$insertEncounter = static function (int $pid, string $date, string $reason) use ($uuid): int {
    $encounter = Scalar::int(QueryUtils::generateId());
    QueryUtils::sqlInsert(
        "INSERT INTO form_encounter SET uuid = ?, date = ?, reason = ?, facility = ?, facility_id = ?, pid = ?, encounter = ?,
            provider_id = ?, pc_catid = 5, class_code = 'AMB', sensitivity = 'normal', billing_facility = ?",
        [$uuid('form_encounter'), $date, $reason, 'Your Clinic Name Here', 3, $pid, $encounter, ADMIN_USER_ID, 3]
    );
    QueryUtils::sqlInsert(
        "INSERT INTO forms SET date = ?, encounter = ?, form_name = 'New Patient Encounter', form_id = 1, pid = ?, user = 'admin',
            groupname = 'Default', authorized = 1, deleted = 0, formdir = 'newpatient'",
        [$date, $encounter, $pid]
    );
    return $encounter;
};

$insertSoap = static function (int $pid, int $encounter, string $date, string $assessment, string $plan): int {
    $soapId = QueryUtils::sqlInsert(
        "INSERT INTO form_soap SET date = ?, pid = ?, user = 'admin', groupname = 'Default', authorized = 1, activity = 1,
            subjective = ?, objective = ?, assessment = ?, plan = ?",
        [$date, $pid, 'Synthetic visit.', 'Vitals stable.', $assessment, $plan]
    );
    QueryUtils::sqlInsert(
        "INSERT INTO forms SET date = ?, encounter = ?, form_name = 'SOAP', form_id = ?, pid = ?, user = 'admin',
            groupname = 'Default', authorized = 1, deleted = 0, formdir = 'soap'",
        [$date, $encounter, $soapId, $pid]
    );
    return Scalar::int($soapId);
};

/** @param list<array{code:string, text:string, value:string, units:string, range:string, abnormal:string, status:string, date:string, report_status?:string}> $results */
$insertOrder = static function (int $pid, int $encounter, string $orderedAt, string $code, string $name, string $orderStatus, array $results) use ($uuid): int {
    $orderId = Scalar::int(QueryUtils::sqlInsert(
        "INSERT INTO procedure_order SET uuid = ?, provider_id = ?, patient_id = ?, encounter_id = ?, date_ordered = ?,
            order_status = ?, activity = 1, procedure_order_type = 'laboratory_test'",
        [$uuid('procedure_order'), ADMIN_USER_ID, $pid, $encounter, $orderedAt, $orderStatus]
    ));
    QueryUtils::sqlInsert(
        "INSERT INTO procedure_order_code SET procedure_order_id = ?, procedure_order_seq = 1, procedure_code = ?,
            procedure_name = ?, procedure_type = 'laboratory_test'",
        [$orderId, $code, $name]
    );
    foreach ($results as $r) {
        $reportId = QueryUtils::sqlInsert(
            "INSERT INTO procedure_report SET uuid = ?, procedure_order_id = ?, procedure_order_seq = 1, date_collected = ?,
                date_report = ?, report_status = ?",
            [$uuid('procedure_report'), $orderId, $orderedAt, $r['date'], $r['report_status'] ?? $r['status']]
        );
        QueryUtils::sqlInsert(
            "INSERT INTO procedure_result SET uuid = ?, procedure_report_id = ?, result_code = ?, result_text = ?,
                result = ?, units = ?, `range` = ?, abnormal = ?, result_status = ?, date = ?",
            [$uuid('procedure_result'), $reportId, $r['code'], $r['text'], $r['value'], $r['units'], $r['range'], $r['abnormal'], $r['status'], $r['date']]
        );
    }
    return $orderId;
};

$insertPrescription = static function (int $pid, string $drug, string $rxnorm, string $startDate, int $active, ?string $endDate = null) use ($uuid): void {
    QueryUtils::sqlInsert(
        "INSERT INTO prescriptions SET uuid = ?, patient_id = ?, date_added = ?, date_modified = ?, provider_id = ?, start_date = ?, end_date = ?, drug = ?,
            rxnorm_drugcode = ?, dosage = ?, quantity = '30', refills = 1, active = ?, txDate = ?, medication = 0",
        [$uuid('prescriptions'), $pid, $startDate . ' 09:00:00', $startDate . ' 09:00:00', ADMIN_USER_ID, $startDate, $endDate, $drug, $rxnorm, '1 tab daily', $active, $startDate]
    );
};

$insertListMedication = static function (int $pid, string $title, string $begdate, ?string $enddate, int $activity) use ($uuid): void {
    QueryUtils::sqlInsert(
        "INSERT INTO lists SET uuid = ?, type = 'medication', title = ?, begdate = ?, enddate = ?, diagnosis = '', activity = ?, pid = ?, `date` = NOW(), user = 'admin', groupname = 'Default'",
        [$uuid('lists'), $title, $begdate, $enddate, $activity, $pid]
    );
};

$insertAllergy = static function (int $pid, string $title, string $reaction) use ($uuid): void {
    QueryUtils::sqlInsert(
        "INSERT INTO lists SET uuid = ?, type = 'allergy', title = ?, begdate = ?, diagnosis = '', activity = 1, pid = ?, reaction = ?, severity_al = ?, `date` = NOW(), user = 'admin', groupname = 'Default'",
        [$uuid('lists'), $title, '2020-01-01 00:00:00', $pid, $reaction, 'moderate']
    );
};

$insertAppointment = static function (int $pid, string $time, string $reason) use ($uuid): void {
    $end = date('H:i:s', strtotime($time) + 900);
    QueryUtils::sqlInsert(
        "INSERT INTO openemr_postcalendar_events SET uuid = ?, pc_catid = 5, pc_multiple = 0, pc_aid = ?, pc_pid = ?, pc_title = 'Office Visit',
            pc_hometext = ?, pc_eventDate = CURDATE(), pc_duration = 900, pc_startTime = ?, pc_endTime = ?,
            pc_apptstatus = '-', pc_eventstatus = 1, pc_sharing = 0, pc_facility = 3, pc_billing_location = 3, pc_informant = 'admin'",
        [$uuid('openemr_postcalendar_events'), (string) ADMIN_USER_ID, (string) $pid, $reason, $time, $end]
    );
};

$createPatient = static function (string $fname, string $lname, string $dob, string $sex) use ($firstRowInt): ?int {
    $existing = QueryUtils::fetchRecords("SELECT pid FROM patient_data WHERE fname = ? AND lname = ? ORDER BY pid LIMIT 1", [$fname, $lname]);
    if ($existing !== []) {
        echo "{$fname} {$lname} already present (pid {$firstRowInt($existing, 'pid')}); skipped.\n";
        return null;
    }
    $result = (new PatientService())->insert(['fname' => $fname, 'lname' => $lname, 'DOB' => $dob, 'sex' => $sex, 'providerID' => ADMIN_USER_ID]);
    if ($result->hasErrors()) {
        fwrite(STDERR, "Patient insert failed for {$fname} {$lname}: " . json_encode($result->getValidationMessages()) . "\n");
        return null;
    }
    $pid = $firstRowInt($result->getData(), 'pid');
    return $pid > 0 ? $pid : null;
};

// ----------------------------------------------------------------------------- scenarios

// 1. Marcus Hale: result after the note; medication started after the note.
if (($pid = $createPatient('Marcus', 'Hale', '1961-02-03', 'Male')) !== null) {
    $enc = $insertEncounter($pid, NOTE_DATE, 'Hypertension follow-up');
    $insertSoap($pid, $enc, NOTE_DATE, 'Hypertension, above goal; potassium borderline.', 'Repeat potassium in two weeks. Start lisinopril 10 mg daily.');
    $insertOrder($pid, $enc, '2026-06-24 08:00:00', '2823-3', 'Potassium', 'complete', [
        ['code' => '2823-3', 'text' => 'Potassium', 'value' => '4.1', 'units' => 'mmol/L', 'range' => '3.5-5.1', 'abnormal' => '', 'status' => 'final', 'date' => '2026-06-25 09:10:00'],
    ]);
    $insertPrescription($pid, 'Lisinopril 10 mg', '314076', '2026-06-11', 1);
    $insertAllergy($pid, 'Sulfa', 'hives');
    $insertAppointment($pid, '09:00:00', 'BP check; review potassium');
    echo "Seeded Marcus Hale (pid {$pid}): result found + medication started after note.\n";
}

// 2. Priya Nair: order pending without a result; continue medication found in the medication list.
if (($pid = $createPatient('Priya', 'Nair', '1974-09-21', 'Female')) !== null) {
    $enc = $insertEncounter($pid, NOTE_DATE, 'Hyperlipidemia follow-up');
    $insertSoap($pid, $enc, NOTE_DATE, 'Hyperlipidemia on statin.', 'Order lipid panel. Continue atorvastatin.');
    $insertOrder($pid, $enc, '2026-09-14 08:00:00', '24331-1', 'Lipid Panel', 'pending', []);
    $insertListMedication($pid, 'Atorvastatin 20 mg', '2025-03-01 00:00:00', null, 1);
    $insertAppointment($pid, '09:20:00', 'Lipid review');
    echo "Seeded Priya Nair (pid {$pid}): order pending + continue medication on file.\n";
}

// 3. Thomas Reyes: no result after the note; stop-order contradicted by active records in both tables.
if (($pid = $createPatient('Thomas', 'Reyes', '1957-11-30', 'Male')) !== null) {
    $enc = $insertEncounter($pid, NOTE_DATE, 'Diabetes and hypertension follow-up');
    $insertSoap($pid, $enc, NOTE_DATE, 'Type 2 diabetes; cough attributed to ACE inhibitor.', 'Repeat HbA1c in three months. Stop lisinopril.');
    $insertPrescription($pid, 'Lisinopril 20 mg', '314077', '2025-08-01', 1);
    $insertListMedication($pid, 'Lisinopril 20 mg', '2025-08-01 00:00:00', null, 1);
    $insertPrescription($pid, 'Metformin HCl 500 mg', '861007', '2025-01-15', 1);
    $insertAllergy($pid, 'Penicillin', 'rash');
    $insertAppointment($pid, '09:40:00', 'Diabetes f/u');
    echo "Seeded Thomas Reyes (pid {$pid}): no matching record + conflicting stop.\n";
}

// 4. Grace Okafor: the only TSH result predates the note, so it is not evidence.
if (($pid = $createPatient('Grace', 'Okafor', '1969-05-14', 'Female')) !== null) {
    $enc = $insertEncounter($pid, NOTE_DATE, 'Hypothyroidism follow-up');
    $insertSoap($pid, $enc, NOTE_DATE, 'Hypothyroidism on levothyroxine.', 'Repeat TSH.');
    $insertOrder($pid, $enc, '2026-05-20 08:00:00', '3016-3', 'TSH', 'complete', [
        ['code' => '3016-3', 'text' => 'TSH', 'value' => '6.8', 'units' => 'mIU/L', 'range' => '0.4-4.0', 'abnormal' => 'high', 'status' => 'final', 'date' => '2026-05-21 10:00:00'],
    ]);
    $insertPrescription($pid, 'Levothyroxine 75 mcg', '966224', '2024-02-01', 1);
    $insertAppointment($pid, '10:00:00', 'Thyroid follow-up');
    echo "Seeded Grace Okafor (pid {$pid}): result before the note is not evidence.\n";
}

// 5. Daniel Kim: unnamed labs -> ambiguous; an interval result the plan does not explain.
if (($pid = $createPatient('Daniel', 'Kim', '1982-07-08', 'Male')) !== null) {
    $enc = $insertEncounter($pid, NOTE_DATE, 'Annual review');
    $insertSoap($pid, $enc, NOTE_DATE, 'Chronic kidney disease stage 2, stable.', 'Recheck labs at next visit.');
    $insertOrder($pid, $enc, '2026-08-30 08:00:00', '2160-0', 'Creatinine', 'complete', [
        ['code' => '2160-0', 'text' => 'Creatinine', 'value' => '1.3', 'units' => 'mg/dL', 'range' => '0.7-1.2', 'abnormal' => 'high', 'status' => 'final', 'date' => '2026-08-31 11:00:00'],
    ]);
    $insertAppointment($pid, '10:20:00', 'Annual review');
    echo "Seeded Daniel Kim (pid {$pid}): ambiguous (unnamed labs) + unexplained interval result.\n";
}

// 6. Henry Walsh: corrected result supersedes final on the same order; a referral is unchecked; no allergy entries.
if (($pid = $createPatient('Henry', 'Walsh', '1950-12-02', 'Male')) !== null) {
    $enc = $insertEncounter($pid, NOTE_DATE, 'Heart failure follow-up');
    $insertSoap($pid, $enc, NOTE_DATE, 'Heart failure with reduced ejection fraction.', 'Repeat basic metabolic panel. Refer to cardiology.');
    $insertOrder($pid, $enc, '2026-07-01 08:00:00', '24320-4', 'Basic Metabolic Panel', 'complete', [
        ['code' => '2951-2', 'text' => 'Sodium', 'value' => '142', 'units' => 'mmol/L', 'range' => '135-145', 'abnormal' => '', 'status' => 'final', 'date' => '2026-07-02 09:00:00'],
        ['code' => '2951-2', 'text' => 'Sodium', 'value' => '138', 'units' => 'mmol/L', 'range' => '135-145', 'abnormal' => '', 'status' => 'corrected', 'date' => '2026-07-02 15:30:00', 'report_status' => 'corrected'],
    ]);
    $insertPrescription($pid, 'Carvedilol 6.25 mg', '200031', '2024-06-01', 1);
    $insertAppointment($pid, '10:40:00', 'HF follow-up; labs and referral');
    echo "Seeded Henry Walsh (pid {$pid}): corrected result + unchecked referral + no allergy entries.\n";
}

// 7. Sofia Marin: no prior note at all.
if (($pid = $createPatient('Sofia', 'Marin', '1990-03-27', 'Female')) !== null) {
    $insertAppointment($pid, '11:00:00', 'New patient visit');
    echo "Seeded Sofia Marin (pid {$pid}): no prior note.\n";
}

echo "Done.\n";
