<?php

/**
 * What the dashboard launch page does: redirect (302) or show a message.
 *
 * @package   OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Dashboard;

final class LaunchDecision
{
    private function __construct(
        public readonly int $status,
        public readonly ?string $location,
        public readonly ?string $message,
    ) {
    }

    public static function redirect(string $location): self
    {
        return new self(302, $location, null);
    }

    public static function refuse(int $status, string $message): self
    {
        return new self($status, null, $message);
    }
}
