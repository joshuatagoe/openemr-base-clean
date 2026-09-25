<?php

/**
 * Reads the module's code version from version.php (the same variables core's
 * InstallerController::getModuleVersionFromFile() reads).
 *
 * @package   OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Migration;

final class ModuleVersion
{
    public static function fromFile(string $path): ?string
    {
        if (!is_file($path)) {
            return null;
        }
        $read = static function (string $file): array {
            $v_major = null;
            $v_minor = null;
            $v_patch = null;
            include $file;
            return [$v_major, $v_minor, $v_patch];
        };
        $parts = $read($path);
        foreach ($parts as $part) {
            if (!is_string($part) || !ctype_digit($part)) {
                return null;
            }
        }
        return implode('.', $parts);
    }
}
