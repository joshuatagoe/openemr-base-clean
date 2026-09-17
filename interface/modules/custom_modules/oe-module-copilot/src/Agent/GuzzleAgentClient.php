<?php

/**
 * Guzzle implementation of the agent hand-off. The JSON body is encoded once
 * and the signature is computed over exactly those bytes.
 *
 * @package   OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Agent;

use GuzzleHttp\Client;
use GuzzleHttp\ClientInterface;
use GuzzleHttp\Exception\ConnectException;
use GuzzleHttp\RequestOptions;
use JsonException;
use OpenEMR\Modules\Copilot\Config\CopilotConfig;
use OpenEMR\Modules\Copilot\Support\Scalar;
use Psr\Http\Message\ResponseInterface;
use Throwable;

final class GuzzleAgentClient implements AgentClientInterface
{
    public const HEADER_CORRELATION = 'X-Correlation-Id';

    private readonly ClientInterface $http;

    /** @var callable(): int */
    private $clock;

    public function __construct(private readonly CopilotConfig $config, ?ClientInterface $http = null, ?callable $clock = null)
    {
        $this->http = $http ?? new Client([
            RequestOptions::TIMEOUT => $config->agentTimeoutSeconds,
            RequestOptions::CONNECT_TIMEOUT => min(1.0, $config->agentTimeoutSeconds),
            RequestOptions::HTTP_ERRORS => false,
            RequestOptions::ALLOW_REDIRECTS => false,
        ]);
        $this->clock = $clock ?? static fn(): int => time();
    }

    public function postBundle(array $bundle, string $correlationId): BundleAccepted
    {
        $baseUrl = $this->config->agentBaseUrl();
        $secret = $this->config->ticketSecret;
        if (!$this->config->isConfigured() || $baseUrl === null || $secret === null) {
            throw new AgentUnavailableException(AgentUnavailableException::REASON_NOT_CONFIGURED);
        }

        try {
            $body = json_encode($bundle, JSON_THROW_ON_ERROR | JSON_UNESCAPED_SLASHES);
        } catch (JsonException $e) {
            throw new AgentUnavailableException(AgentUnavailableException::REASON_BAD_RESPONSE, null, $e);
        }

        $headers = BundleSigner::headers($secret, $body, ($this->clock)()) + [
            'Content-Type' => 'application/json',
            'Accept' => 'application/json',
            self::HEADER_CORRELATION => $correlationId,
        ];

        try {
            $response = $this->http->request('POST', $baseUrl . '/v1/bundles', [
                RequestOptions::HEADERS => $headers,
                RequestOptions::BODY => $body,
                RequestOptions::HTTP_ERRORS => false,
            ]);
        } catch (ConnectException $e) {
            $reason = str_contains(strtolower($e->getMessage()), 'timed out')
                ? AgentUnavailableException::REASON_TIMEOUT
                : AgentUnavailableException::REASON_UNREACHABLE;
            throw new AgentUnavailableException($reason, null, $e);
        } catch (Throwable $e) {
            throw new AgentUnavailableException(AgentUnavailableException::REASON_UNREACHABLE, null, $e);
        }

        return self::parseAccepted($response, $correlationId, $bundle);
    }

    /**
     * @param array<string,mixed> $bundle
     * @throws AgentUnavailableException
     */
    private static function parseAccepted(ResponseInterface $response, string $correlationId, array $bundle): BundleAccepted
    {
        $status = $response->getStatusCode();
        if ($status !== 201) {
            throw new AgentUnavailableException(AgentUnavailableException::REASON_REJECTED, $status);
        }
        try {
            $decoded = json_decode((string) $response->getBody(), true, 8, JSON_THROW_ON_ERROR);
        } catch (JsonException $e) {
            throw new AgentUnavailableException(AgentUnavailableException::REASON_BAD_RESPONSE, $status, $e);
        }
        if (!is_array($decoded)) {
            throw new AgentUnavailableException(AgentUnavailableException::REASON_BAD_RESPONSE, $status);
        }
        $bundleId = Scalar::str($decoded['bundle_id'] ?? null);
        $cid = Scalar::str($decoded['correlation_id'] ?? null);
        $puuid = Scalar::str($decoded['patient_uuid'] ?? null);
        $expiresAt = Scalar::str($decoded['expires_at'] ?? null);
        // The agent must echo the identifiers it was given; anything else is a wrong or confused peer.
        if (
            $bundleId === '' || $expiresAt === ''
            || $cid !== $correlationId
            || $puuid !== Scalar::str($bundle['patient_uuid'] ?? null)
        ) {
            throw new AgentUnavailableException(AgentUnavailableException::REASON_BAD_RESPONSE, $status);
        }
        return new BundleAccepted($bundleId, $cid, $puuid, $expiresAt);
    }
}
