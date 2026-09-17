<?php

/**
 * Server-to-server hand-off of a ContextBundle to the copilot agent
 * (ARCHITECTURE.md section 5, step 4). Implementations sign the body, bound
 * the wait, and raise AgentUnavailableException on any failure so the caller
 * can degrade explicitly.
 *
 * @package   OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Agent;

interface AgentClientInterface
{
    /**
     * @param array<string,mixed> $bundle  a ContextBundle (schema 1.0) as an array
     * @throws AgentUnavailableException
     */
    public function postBundle(array $bundle, string $correlationId): BundleAccepted;
}
