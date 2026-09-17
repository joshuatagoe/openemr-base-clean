<?php

/**
 * Raised when the agent did not accept a bundle (not configured, unreachable,
 * timed out, or answered with anything but 201). Carries a fixed reason code
 * only; transport messages and response bodies are never included.
 *
 * @package   OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Agent;

use RuntimeException;
use Throwable;

final class AgentUnavailableException extends RuntimeException
{
    public const REASON_NOT_CONFIGURED = 'agent_not_configured';
    public const REASON_UNREACHABLE = 'agent_unreachable';
    public const REASON_TIMEOUT = 'agent_timeout';
    public const REASON_REJECTED = 'agent_rejected';
    public const REASON_BAD_RESPONSE = 'agent_bad_response';

    public function __construct(
        private readonly string $reason,
        private readonly ?int $httpStatus = null,
        ?Throwable $previous = null,
    ) {
        parent::__construct("agent unavailable: {$reason}", 0, $previous);
    }

    public function getReason(): string
    {
        return $this->reason;
    }

    public function getHttpStatus(): ?int
    {
        return $this->httpStatus;
    }
}
