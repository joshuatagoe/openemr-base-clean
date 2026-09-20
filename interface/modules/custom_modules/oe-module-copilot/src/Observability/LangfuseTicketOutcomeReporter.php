<?php

/**
 * Reports ticket outcomes to the self-hosted Langfuse as one OTLP span per
 * request, named `ticket.<outcome>` (dashboard widgets can group by
 * observation name), with the trace id derived from the correlation id so the
 * span lands in the same trace the agent creates for that briefing. A request
 * the agent never saw is therefore a trace holding only a `ticket.*` span.
 *
 * Attributes are the cid, the outcome and an optional fixed detail code; no
 * user, patient or clinical values (ARCHITECTURE.md section 10). The send is
 * deferred until after the response and bounded by a one-second timeout, and
 * every failure is swallowed after a debug log: observability never delays
 * or fails a briefing.
 *
 * @package   OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Observability;

use Closure;
use GuzzleHttp\Client;
use GuzzleHttp\ClientInterface;
use GuzzleHttp\Exception\GuzzleException;
use GuzzleHttp\RequestOptions;
use Psr\Log\LoggerInterface;
use Psr\Log\NullLogger;

final class LangfuseTicketOutcomeReporter implements TicketOutcomeReporterInterface
{
    public const OTLP_PATH = '/api/public/otel/v1/traces';
    public const SERVICE_NAME = 'oe-module-copilot';
    public const TIMEOUT_SECONDS = 1.0;

    /** Outcomes reported at level ERROR: an eligible request the physician did not get a plan check for, or a module failure. */
    private const ERROR_OUTCOMES = [
        TicketOutcomeReporterInterface::OUTCOME_AGENT_UNAVAILABLE,
        TicketOutcomeReporterInterface::OUTCOME_SOURCE_UNAVAILABLE,
        TicketOutcomeReporterInterface::OUTCOME_INTERNAL_ERROR,
    ];

    private readonly ClientInterface $http;

    /** @var Closure(Closure(): void): void  runs the send; the default defers it to request shutdown */
    private readonly Closure $defer;

    /** @var Closure(): int  unix nanoseconds */
    private readonly Closure $clock;

    private readonly LoggerInterface $logger;

    public function __construct(
        private readonly string $baseUrl,
        private readonly string $publicKey,
        #[\SensitiveParameter] private readonly string $secretKey,
        private readonly string $environment,
        ?ClientInterface $http = null,
        ?Closure $defer = null,
        ?Closure $clock = null,
        ?LoggerInterface $logger = null,
    ) {
        $this->http = $http ?? new Client([
            RequestOptions::TIMEOUT => self::TIMEOUT_SECONDS,
            RequestOptions::CONNECT_TIMEOUT => self::TIMEOUT_SECONDS,
            RequestOptions::HTTP_ERRORS => false,
            RequestOptions::ALLOW_REDIRECTS => false,
        ]);
        $this->defer = $defer ?? static function (Closure $send): void {
            register_shutdown_function($send);
        };
        $this->clock = $clock ?? static fn(): int => (int) (microtime(true) * 1_000_000_000);
        $this->logger = $logger ?? new NullLogger();
    }

    public function report(string $correlationId, string $outcome, ?string $detail = null): void
    {
        $payload = $this->payload($correlationId, $outcome, $detail);
        ($this->defer)(function () use ($payload, $outcome): void {
            try {
                $response = $this->http->request('POST', rtrim($this->baseUrl, '/') . self::OTLP_PATH, [
                    RequestOptions::HEADERS => [
                        'Authorization' => 'Basic ' . base64_encode($this->publicKey . ':' . $this->secretKey),
                        'Content-Type' => 'application/json',
                    ],
                    RequestOptions::BODY => $payload,
                ]);
                if ($response->getStatusCode() >= 300) {
                    $this->logger->debug('copilot ticket outcome report rejected', ['outcome' => $outcome, 'http_status' => $response->getStatusCode()]);
                }
            } catch (GuzzleException $e) {  // transport errors and timeouts; HTTP_ERRORS is off so statuses never throw
                $this->logger->debug('copilot ticket outcome report failed', ['outcome' => $outcome, 'type' => $e::class]);
            }
        });
    }

    /**
     * One OTLP/JSON span. Trace id = the cid's 32 hex digits, matching the agent
     * (`trace_id_for` in copilot-agent/app/observability.py); span id is random.
     */
    public function payload(string $correlationId, string $outcome, ?string $detail): string
    {
        $now = ($this->clock)();
        assert(is_int($now));
        $traceId = str_replace('-', '', strtolower($correlationId));
        $attributes = [
            ['key' => 'langfuse.observation.type', 'value' => ['stringValue' => 'span']],
            ['key' => 'langfuse.observation.level', 'value' => ['stringValue' => in_array($outcome, self::ERROR_OUTCOMES, true) ? 'ERROR' : 'DEFAULT']],
            ['key' => 'langfuse.observation.metadata.cid', 'value' => ['stringValue' => $correlationId]],
            ['key' => 'langfuse.observation.metadata.outcome', 'value' => ['stringValue' => $outcome]],
            ['key' => 'langfuse.environment', 'value' => ['stringValue' => $this->environment]],
        ];
        if ($detail !== null) {
            $attributes[] = ['key' => 'langfuse.observation.metadata.detail', 'value' => ['stringValue' => $detail]];
            $attributes[] = ['key' => 'langfuse.observation.status_message', 'value' => ['stringValue' => $detail]];
        }
        $span = [
            'traceId' => $traceId,
            'spanId' => bin2hex(random_bytes(8)),
            'name' => 'ticket.' . $outcome,
            'kind' => 1,
            'startTimeUnixNano' => (string) $now,
            'endTimeUnixNano' => (string) $now,
            'attributes' => $attributes,
        ];
        $document = [
            'resourceSpans' => [[
                'resource' => ['attributes' => [['key' => 'service.name', 'value' => ['stringValue' => self::SERVICE_NAME]]]],
                'scopeSpans' => [['scope' => ['name' => self::SERVICE_NAME], 'spans' => [$span]]],
            ]],
        ];
        return json_encode($document, JSON_THROW_ON_ERROR | JSON_UNESCAPED_SLASHES);
    }
}
