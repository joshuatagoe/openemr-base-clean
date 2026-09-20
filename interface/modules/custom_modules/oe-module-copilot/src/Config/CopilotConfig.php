<?php

/**
 * Module configuration read from the process environment (ARCHITECTURE.md
 * section 10, "Secrets"): the agent URL and the shared ticket secret live in
 * environment variables of the OpenEMR service, never in the globals table
 * (admin-visible, audit-logged) and never in the repository.
 *
 * @package   OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Config;

final readonly class CopilotConfig
{
    public const ENV_AGENT_URL = 'COPILOT_AGENT_URL';
    public const ENV_TICKET_SECRET = 'COPILOT_TICKET_SECRET';
    /** Same variable names as the agent's tracing settings; all three present enables ticket-outcome reporting. */
    public const ENV_LANGFUSE_BASE_URL = 'LANGFUSE_BASE_URL';
    public const ENV_LANGFUSE_PUBLIC_KEY = 'LANGFUSE_PUBLIC_KEY';
    public const ENV_LANGFUSE_SECRET_KEY = 'LANGFUSE_SECRET_KEY';
    public const ENV_ENVIRONMENT = 'COPILOT_ENVIRONMENT';

    /** Shared secret must be at least this long (matches the agent's minimum). */
    public const MIN_SECRET_LENGTH = 32;

    public function __construct(
        public ?string $agentUrl,
        public ?string $ticketSecret,
        public int $ticketTtlSeconds = 120,
        public float $agentTimeoutSeconds = 2.0,
        public ?string $langfuseBaseUrl = null,
        public ?string $langfusePublicKey = null,
        #[\SensitiveParameter] public ?string $langfuseSecretKey = null,
        public string $environment = 'development',
    ) {
    }

    public static function fromEnvironment(): self
    {
        return new self(
            self::nonEmpty(getenv(self::ENV_AGENT_URL)),
            self::nonEmpty(getenv(self::ENV_TICKET_SECRET)),
            langfuseBaseUrl: self::nonEmpty(getenv(self::ENV_LANGFUSE_BASE_URL)),
            langfusePublicKey: self::nonEmpty(getenv(self::ENV_LANGFUSE_PUBLIC_KEY)),
            langfuseSecretKey: self::nonEmpty(getenv(self::ENV_LANGFUSE_SECRET_KEY)),
            environment: self::nonEmpty(getenv(self::ENV_ENVIRONMENT)) ?? 'development',
        );
    }

    /** Both the agent URL and a sufficiently long secret are present. */
    public function isConfigured(): bool
    {
        return $this->agentUrl !== null
            && $this->ticketSecret !== null
            && strlen($this->ticketSecret) >= self::MIN_SECRET_LENGTH;
    }

    /** Langfuse URL and both keys are present: ticket outcomes are reported. */
    public function hasLangfuse(): bool
    {
        return $this->langfuseBaseUrl !== null && $this->langfusePublicKey !== null && $this->langfuseSecretKey !== null;
    }

    /** Agent base URL without a trailing slash, or null when unset. */
    public function agentBaseUrl(): ?string
    {
        return $this->agentUrl === null ? null : rtrim($this->agentUrl, '/');
    }

    private static function nonEmpty(string|false $value): ?string
    {
        if ($value === false) {
            return null;
        }
        $trimmed = trim($value);
        return $trimmed === '' ? null : $trimmed;
    }
}
