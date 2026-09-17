<?php

/**
 * Narrowing helpers for untyped database rows and session values.
 *
 * QueryUtils rows and session entries are `mixed`; these helpers make the
 * narrowing explicit (PHPStan level 10) and choose safe fallbacks: a
 * non-scalar becomes '' or 0, never an exception with the value in it.
 *
 * @package   OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Support;

final class Scalar
{
    public static function str(mixed $value): string
    {
        if (is_string($value)) {
            return $value;
        }
        if (is_int($value) || is_float($value)) {
            return (string) $value;
        }
        if (is_bool($value)) {
            return $value ? '1' : '';
        }
        return '';
    }

    public static function int(mixed $value): int
    {
        if (is_int($value)) {
            return $value;
        }
        if (is_float($value)) {
            return (int) $value;
        }
        if (is_string($value) && is_numeric($value)) {
            return (int) $value;
        }
        return 0;
    }

    /** Positive integer or null (for identifiers that must be present). */
    public static function positiveIntOrNull(mixed $value): ?int
    {
        if (is_int($value)) {
            return $value > 0 ? $value : null;
        }
        if (is_string($value) && $value !== '' && ctype_digit($value)) {
            $int = (int) $value;
            return $int > 0 ? $int : null;
        }
        return null;
    }
}
