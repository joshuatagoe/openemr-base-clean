<?php

/**
 * Local-to-UTC date normalization for ContextBundle timestamps.
 *
 * OpenEMR stores DATETIME/DATE columns in the server's local time zone with
 * no offset. The agent contract requires timezone-aware ISO-8601 values, so
 * every timestamp leaving the adapter is converted to UTC and rendered as
 * `YYYY-MM-DDTHH:MM:SSZ`.
 *
 * @package   OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Support;

use DateTimeImmutable;
use DateTimeZone;
use InvalidArgumentException;

final class UtcDate
{
    public const FORMAT = 'Y-m-d\TH:i:s\Z';

    /**
     * Convert a local OpenEMR date/datetime string to a UTC ISO-8601 string.
     *
     * A bare DATE is treated as local midnight. Empty or zero dates are
     * rejected rather than silently mapped to the epoch.
     */
    public static function toIso(string $local, DateTimeZone $localZone): string
    {
        $trimmed = trim($local);
        if ($trimmed === '' || str_starts_with($trimmed, '0000-00-00')) {
            throw new InvalidArgumentException('empty or zero date');
        }
        $parsed = DateTimeImmutable::createFromFormat('Y-m-d H:i:s', $trimmed, $localZone)
            ?: DateTimeImmutable::createFromFormat('Y-m-d', $trimmed, $localZone);
        if ($parsed === false) {
            throw new InvalidArgumentException('unparseable date');
        }
        if (strlen($trimmed) === 10) {
            $parsed = $parsed->setTime(0, 0, 0);
        }
        return $parsed->setTimezone(new DateTimeZone('UTC'))->format(self::FORMAT);
    }

    /**
     * The zone OpenEMR is currently using (globals `gbl_time_zone` or PHP default).
     */
    public static function serverZone(): DateTimeZone
    {
        return new DateTimeZone(date_default_timezone_get());
    }
}
