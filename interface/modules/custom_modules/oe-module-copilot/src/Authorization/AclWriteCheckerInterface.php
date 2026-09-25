<?php

/**
 * Write-level phpGACL check (return value `write`), kept apart from
 * AclCheckerInterface so the read-only authorizer's seam is unchanged.
 *
 * @package   OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Authorization;

interface AclWriteCheckerInterface
{
    public function checkWrite(string $section, string $value, string $username): bool;
}
