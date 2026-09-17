<?php

/**
 * Looks up whether a user has a care relationship with a patient
 * (ARCHITECTURE.md section 10; closes AUDIT SEC-002 for the copilot path).
 *
 * @package   OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Authorization;

interface RelationshipRepositoryInterface
{
    public const BASIS_ENCOUNTER_PROVIDER = 'encounter_provider';
    public const BASIS_APPOINTMENT_PROVIDER = 'appointment_provider';
    public const BASIS_PRIMARY_PROVIDER = 'primary_provider';

    /**
     * @return string|null one of the BASIS_* constants, or null when no relationship exists
     */
    public function findBasis(int $userId, int $pid): ?string;
}
