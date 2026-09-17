<?php

/**
 * Narrow seam over OpenEMR's phpGACL check so authorization logic is testable.
 *
 * @package   OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Authorization;

interface AclCheckerInterface
{
    public function check(string $section, string $value, string $username): bool;
}
