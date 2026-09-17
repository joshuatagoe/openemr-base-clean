<?php

/**
 * DEVELOPMENT-ONLY seed: synthetic patient "Evelyn Demo" for the local stack.
 *
 * Creates, idempotently, the tracer-bullet scenario the copilot adapter and
 * agent are tested against:
 *   - patient Evelyn Demo (synthetic; primary provider = the admin user, so
 *     the care-relationship check passes without any override)
 *   - one encounter (2026-06-10 14:30 local) with a SOAP note whose plan is
 *     "Continue metformin. Repeat HbA1c in three months."
 *   - one lab order/report/result: final Hemoglobin A1c 8.9 % flagged high,
 *     observed 2026-09-12 09:15 local
 *   - Phase 1 additions (added on any run if missing): an uncoded penicillin
 *     allergy, an appointment today with a scheduled reason, and a pending
 *     lipid panel order dated 2026-09-14 (no result)
 *   - Phase 2 addition: an active metformin prescription (started 2026-01-15)
 *
 * Safety: CLI only; requires --confirm-local; refuses when
 * OPENEMR__ENVIRONMENT=prod or when the database host is not a local/compose
 * host. This script is NOT part of the read-only adapter and must never run
 * against a shared or production database.
 *
 * Usage (from the repository root, inside the development container, as the
 * web user):
 *   docker compose -f docker/development-easy/docker-compose.yml exec openemr \
 *     su -s /bin/sh apache -c 'php interface/modules/custom_modules/oe-module-copilot/dev/seed_evelyn_demo.php --confirm-local'
 *
 * Optional flags:
 *   --enable-module        register/enable oe-module-copilot in the modules table
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
// OpenEMR's CLI bootstrap contract (see library/ajax/execute_background_services.php) selects the site via $_GET.
$_GET['site'] = 'default'; // @phpstan-ignore-line
require_once __DIR__ . '/../../../../globals.php';
require_once __DIR__ . '/../src/Support/Scalar.php';

use OpenEMR\Common\Database\QueryUtils;
use OpenEMR\Common\Uuid\UuidRegistry;
use OpenEMR\Modules\Copilot\Support\Scalar;
use OpenEMR\Services\PatientService;

$dbHost = is_array($sqlconf ?? null) ? Scalar::str($sqlconf['host'] ?? null) : '';
if (!in_array($dbHost, ['localhost', '127.0.0.1', 'mysql', 'mariadb'], true)) {
    fwrite(STDERR, "Refusing to run: database host is not a local/compose host.\n");
    exit(2);
}

const SEED_FNAME = 'Evelyn';
const SEED_LNAME = 'Demo';
const SEED_NOTE_DATE = '2026-06-10 14:30:00';
const SEED_PLAN = 'Continue metformin. Repeat HbA1c in three months.';
const SEED_RESULT_DATE = '2026-09-12 09:15:00';
const SEED_ORDER_DATE = '2026-09-10 08:00:00';
const ADMIN_USER_ID = 1;

$firstRowInt = static function (mixed $rows, string $key): int {
    $row = is_array($rows) ? ($rows[0] ?? null) : null;
    return Scalar::int(is_array($row) ? ($row[$key] ?? null) : null);
};

$existing = QueryUtils::fetchRecords(
    "SELECT pid FROM patient_data WHERE fname = ? AND lname = ? ORDER BY pid LIMIT 1",
    [SEED_FNAME, SEED_LNAME]
);
if ($existing !== []) {
    $pid = $firstRowInt($existing, 'pid');
    echo "Evelyn Demo already present (pid {$pid}); nothing seeded.\n";
} else {
    $patientService = new PatientService();
    $result = $patientService->insert([
        'fname' => SEED_FNAME,
        'lname' => SEED_LNAME,
        'DOB' => '1958-04-12',
        'sex' => 'Female',
        'providerID' => ADMIN_USER_ID,
    ]);
    if ($result->hasErrors()) {
        fwrite(STDERR, "Patient insert failed: " . json_encode($result->getValidationMessages()) . "\n");
        exit(1);
    }
    $pid = $firstRowInt($result->getData(), 'pid');
    if ($pid <= 0) {
        fwrite(STDERR, "Patient insert returned no pid.\n");
        exit(1);
    }

    // Encounter (form_encounter + forms pointer), dated in the past.
    $encounter = Scalar::int(QueryUtils::generateId());
    $encounterUuid = UuidRegistry::getRegistryForTable('form_encounter')->createUuid();
    QueryUtils::sqlInsert(
        "INSERT INTO form_encounter SET uuid = ?, date = ?, reason = ?, facility = ?, facility_id = ?, pid = ?, encounter = ?,
            provider_id = ?, pc_catid = 5, class_code = 'AMB', sensitivity = 'normal', billing_facility = ?",
        [$encounterUuid, SEED_NOTE_DATE, 'Diabetes follow-up', 'Your Clinic Name Here', 3, $pid, $encounter, ADMIN_USER_ID, 3]
    );
    QueryUtils::sqlInsert(
        "INSERT INTO forms SET date = ?, encounter = ?, form_name = 'New Patient Encounter', form_id = 1, pid = ?, user = 'admin',
            groupname = 'Default', authorized = 1, deleted = 0, formdir = 'newpatient'",
        [SEED_NOTE_DATE, $encounter, $pid]
    );

    // SOAP note with the plan text (form_soap + forms pointer).
    $soapId = QueryUtils::sqlInsert(
        "INSERT INTO form_soap SET date = ?, pid = ?, user = 'admin', groupname = 'Default', authorized = 1, activity = 1,
            subjective = ?, objective = ?, assessment = ?, plan = ?",
        [SEED_NOTE_DATE, $pid, 'Reports good adherence to metformin.', 'Vitals stable.', 'Type 2 diabetes, above target.', SEED_PLAN]
    );
    QueryUtils::sqlInsert(
        "INSERT INTO forms SET date = ?, encounter = ?, form_name = 'SOAP', form_id = ?, pid = ?, user = 'admin',
            groupname = 'Default', authorized = 1, deleted = 0, formdir = 'soap'",
        [SEED_NOTE_DATE, $encounter, $soapId, $pid]
    );

    // Lab order -> report -> final result (HbA1c 8.9 %, high).
    $orderUuid = UuidRegistry::getRegistryForTable('procedure_order')->createUuid();
    $orderId = QueryUtils::sqlInsert(
        "INSERT INTO procedure_order SET uuid = ?, provider_id = ?, patient_id = ?, encounter_id = ?, date_ordered = ?,
            order_status = 'complete', activity = 1, procedure_order_type = 'laboratory_test'",
        [$orderUuid, ADMIN_USER_ID, $pid, $encounter, SEED_ORDER_DATE]
    );
    QueryUtils::sqlInsert(
        "INSERT INTO procedure_order_code SET procedure_order_id = ?, procedure_order_seq = 1, procedure_code = '4548-4',
            procedure_name = 'Hemoglobin A1c', procedure_type = 'laboratory_test'",
        [$orderId]
    );
    $reportUuid = UuidRegistry::getRegistryForTable('procedure_report')->createUuid();
    $reportId = QueryUtils::sqlInsert(
        "INSERT INTO procedure_report SET uuid = ?, procedure_order_id = ?, procedure_order_seq = 1, date_collected = ?,
            date_report = ?, report_status = 'final'",
        [$reportUuid, $orderId, SEED_ORDER_DATE, SEED_RESULT_DATE]
    );
    $resultUuid = UuidRegistry::getRegistryForTable('procedure_result')->createUuid();
    QueryUtils::sqlInsert(
        "INSERT INTO procedure_result SET uuid = ?, procedure_report_id = ?, result_code = '4548-4', result_text = 'Hemoglobin A1c',
            result = '8.9', units = '%', `range` = '4.0-5.6', abnormal = 'high', result_status = 'final', date = ?",
        [$resultUuid, $reportId, SEED_RESULT_DATE]
    );

    echo "Seeded Evelyn Demo: pid {$pid}, encounter {$encounter}, form_soap {$soapId}, procedure_report {$reportId}.\n";
}

// Phase 1 additions, idempotent by natural key.
if ($pid > 0) {
    $allergy = QueryUtils::fetchRecords("SELECT id FROM lists WHERE pid = ? AND type = 'allergy' AND title = ? LIMIT 1", [$pid, 'Penicillin']);
    if ($allergy === []) {
        QueryUtils::sqlInsert(
            "INSERT INTO lists SET uuid = ?, type = 'allergy', title = ?, begdate = ?, diagnosis = '', activity = 1, pid = ?, reaction = ?, severity_al = ?, `date` = NOW(), user = 'admin', groupname = 'Default'",
            [UuidRegistry::getRegistryForTable('lists')->createUuid(), 'Penicillin', '2019-03-01 00:00:00', $pid, 'rash', 'moderate']
        );
        echo "Added uncoded penicillin allergy.\n";
    }

    $appointment = QueryUtils::fetchRecords(
        "SELECT pc_eid FROM openemr_postcalendar_events WHERE pc_pid = ? AND pc_eventDate = CURDATE() LIMIT 1",
        [(string) $pid]
    );
    if ($appointment === []) {
        QueryUtils::sqlInsert(
            "INSERT INTO openemr_postcalendar_events SET uuid = ?, pc_catid = 5, pc_multiple = 0, pc_aid = ?, pc_pid = ?, pc_title = 'Office Visit',
                pc_hometext = ?, pc_eventDate = CURDATE(), pc_duration = 900, pc_startTime = '10:30:00', pc_endTime = '10:45:00',
                pc_apptstatus = '-', pc_eventstatus = 1, pc_sharing = 0, pc_facility = 3, pc_billing_location = 3, pc_informant = 'admin'",
            [UuidRegistry::getRegistryForTable('openemr_postcalendar_events')->createUuid(), (string) ADMIN_USER_ID, (string) $pid, 'Diabetes f/u; review A1c and lipids']
        );
        echo "Added today's appointment with a scheduled reason.\n";
    }

    $metformin = QueryUtils::fetchRecords("SELECT id FROM prescriptions WHERE patient_id = ? AND drug LIKE 'Metformin%' LIMIT 1", [$pid]);
    if ($metformin === []) {
        QueryUtils::sqlInsert(
            "INSERT INTO prescriptions SET uuid = ?, patient_id = ?, date_added = ?, date_modified = ?, provider_id = ?, start_date = ?, drug = ?,
                rxnorm_drugcode = ?, dosage = ?, quantity = '60', refills = 3, active = 1, txDate = ?, medication = 0",
            [UuidRegistry::getRegistryForTable('prescriptions')->createUuid(), $pid, '2026-01-15 09:00:00', '2026-01-15 09:00:00', ADMIN_USER_ID, '2026-01-15', 'Metformin HCl 500 mg', '861007', '1 tab BID', '2026-01-15']
        );
        echo "Added active metformin prescription.\n";
    }

    $pending = QueryUtils::fetchRecords(
        "SELECT po.procedure_order_id FROM procedure_order po JOIN procedure_order_code oc ON oc.procedure_order_id = po.procedure_order_id
          WHERE po.patient_id = ? AND oc.procedure_name = 'Lipid Panel' LIMIT 1",
        [$pid]
    );
    if ($pending === []) {
        $lipidOrderId = QueryUtils::sqlInsert(
            "INSERT INTO procedure_order SET uuid = ?, provider_id = ?, patient_id = ?, encounter_id = 0, date_ordered = ?,
                order_status = 'pending', activity = 1, procedure_order_type = 'laboratory_test'",
            [UuidRegistry::getRegistryForTable('procedure_order')->createUuid(), ADMIN_USER_ID, $pid, '2026-09-14 08:00:00']
        );
        QueryUtils::sqlInsert(
            "INSERT INTO procedure_order_code SET procedure_order_id = ?, procedure_order_seq = 1, procedure_code = '24331-1',
                procedure_name = 'Lipid Panel', procedure_type = 'laboratory_test'",
            [$lipidOrderId]
        );
        echo "Added pending lipid panel order (no result).\n";
    }
}

if (in_array('--enable-module', $cliArgs, true)) {
    $row = QueryUtils::fetchRecords("SELECT mod_id, mod_active FROM modules WHERE mod_directory = ? LIMIT 1", ['oe-module-copilot']);
    if ($row === []) {
        $next = QueryUtils::fetchRecords("SELECT COALESCE(MAX(mod_id), 0) + 1 AS next_id FROM modules");
        $modId = QueryUtils::sqlInsert(
            "INSERT INTO modules SET mod_id = ?, mod_name = ?, mod_active = 1, mod_ui_name = ?, mod_relative_link = ?,
                mod_directory = ?, type = 0, date = NOW()",
            [$firstRowInt($next, 'next_id'), 'oe-module-copilot', 'Clinical Co-Pilot', 'custom_modules/oe-module-copilot/', 'oe-module-copilot']
        );
        QueryUtils::sqlStatementThrowException("INSERT INTO module_acl_sections VALUES (?,?,0,?,?)", [$modId, 'oe-module-copilot', 'oe-module-copilot', $modId]);
        echo "Registered and enabled oe-module-copilot.\n";
    } else {
        QueryUtils::sqlStatementThrowException("UPDATE modules SET mod_active = 1 WHERE mod_directory = ?", ['oe-module-copilot']);
        echo "oe-module-copilot enabled.\n";
    }
}
