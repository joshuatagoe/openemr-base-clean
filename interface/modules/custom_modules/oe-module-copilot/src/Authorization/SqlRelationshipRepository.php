<?php

/**
 * Care-relationship lookup over OpenEMR tables (read-only, bound parameters).
 *
 * A user is related to a patient when they are the provider or supervisor on
 * any of the patient's encounters (form_encounter.provider_id / supervisor_id),
 * the provider on an appointment for the patient within +/- 7 days
 * (openemr_postcalendar_events.pc_aid, pc_pid is varchar and is compared as a
 * string), or the patient's primary provider (patient_data.providerID).
 *
 * @package   OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Authorization;

use OpenEMR\Common\Database\QueryUtils;

final class SqlRelationshipRepository implements RelationshipRepositoryInterface
{
    public function findBasis(int $userId, int $pid): ?string
    {
        $encounter = QueryUtils::fetchRecords(
            "SELECT 1 AS hit FROM form_encounter WHERE pid = ? AND (provider_id = ? OR supervisor_id = ?) LIMIT 1",
            [$pid, $userId, $userId]
        );
        if ($encounter !== []) {
            return self::BASIS_ENCOUNTER_PROVIDER;
        }

        $appointment = QueryUtils::fetchRecords(
            "SELECT 1 AS hit FROM openemr_postcalendar_events
              WHERE pc_pid = ? AND pc_aid = ?
                AND pc_eventDate BETWEEN DATE_SUB(CURDATE(), INTERVAL 7 DAY) AND DATE_ADD(CURDATE(), INTERVAL 7 DAY)
              LIMIT 1",
            [(string) $pid, (string) $userId]
        );
        if ($appointment !== []) {
            return self::BASIS_APPOINTMENT_PROVIDER;
        }

        $primary = QueryUtils::fetchRecords(
            "SELECT 1 AS hit FROM patient_data WHERE pid = ? AND providerID = ? LIMIT 1",
            [$pid, $userId]
        );
        if ($primary !== []) {
            return self::BASIS_PRIMARY_PROVIDER;
        }

        return null;
    }
}
