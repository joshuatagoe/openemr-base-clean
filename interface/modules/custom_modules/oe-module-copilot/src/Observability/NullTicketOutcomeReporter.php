<?php

/**
 * Reporter used when no observability backend is configured: outcomes stay
 * in the audit log only.
 *
 * @package   OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Observability;

final class NullTicketOutcomeReporter implements TicketOutcomeReporterInterface
{
    public function report(string $correlationId, string $outcome, ?string $detail = null): void
    {
    }
}
