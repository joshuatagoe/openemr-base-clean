<?php

/**
 * The agent's acknowledgement of a stored bundle (`POST /v1/bundles` -> 201).
 *
 * @package   OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Agent;

final readonly class BundleAccepted
{
    public function __construct(
        public string $bundleId,
        public string $correlationId,
        public string $patientUuid,
        public string $expiresAt,
    ) {
    }
}
