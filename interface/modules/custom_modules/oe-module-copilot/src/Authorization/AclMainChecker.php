<?php

/**
 * @package   OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Authorization;

use OpenEMR\Common\Acl\AclMain;

final class AclMainChecker implements AclCheckerInterface, AclWriteCheckerInterface
{
    public function check(string $section, string $value, string $username): bool
    {
        return AclMain::aclCheckCore($section, $value, $username);
    }

    public function checkWrite(string $section, string $value, string $username): bool
    {
        return AclMain::aclCheckCore($section, $value, $username, 'write');
    }
}
