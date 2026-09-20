<?php

/**
 * @package   OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Tests;

use Closure;
use GuzzleHttp\Client;
use GuzzleHttp\Exception\ConnectException;
use GuzzleHttp\Handler\MockHandler;
use GuzzleHttp\HandlerStack;
use GuzzleHttp\Middleware;
use GuzzleHttp\Psr7\Request;
use GuzzleHttp\Psr7\Response;
use OpenEMR\Modules\Copilot\Observability\LangfuseTicketOutcomeReporter;
use OpenEMR\Modules\Copilot\Observability\TicketOutcomeReporterInterface;
use PHPUnit\Framework\TestCase;
use Psr\Http\Message\RequestInterface;

/**
 * The module-side report that makes requests the agent never saw visible in
 * Langfuse: one OTLP span per ticket request, joined to the agent's trace by
 * the correlation id, carrying codes only, sent after the response, and never
 * able to fail the request.
 */
final class LangfuseTicketOutcomeReporterTest extends TestCase
{
    private const CID = '7f0e6b2a-3c4d-4e5f-9a1b-2c3d4e5f6a7b';

    /** @var list<RequestInterface> */
    private array $sent = [];

    /** @var list<Closure(): void> */
    private array $deferred = [];

    private function reporter(MockHandler $handler): LangfuseTicketOutcomeReporter
    {
        $stack = HandlerStack::create($handler);
        $stack->push(Middleware::mapRequest(function (RequestInterface $request): RequestInterface {
            $this->sent[] = $request;
            return $request;
        }));
        return new LangfuseTicketOutcomeReporter(
            'https://langfuse.example/',
            'pk-lf-test',
            'sk-lf-test',
            'production',
            new Client(['handler' => $stack]),
            function (Closure $send): void {
                $this->deferred[] = $send;
            },
            static fn(): int => 1_758_300_000_000_000_000,
        );
    }

    private function flush(): void
    {
        foreach ($this->deferred as $send) {
            $send();
        }
        $this->deferred = [];
    }


    /**
     * @return array{span: array<string,mixed>, attributes: array<string,string>, service: string}
     */
    private function decodeSent(int $index): array
    {
        $document = json_decode((string) $this->sent[$index]->getBody(), true, 512, JSON_THROW_ON_ERROR);
        self::assertIsArray($document);
        $resourceSpans = $document['resourceSpans'] ?? null;
        self::assertIsArray($resourceSpans);
        $resourceSpan = $resourceSpans[0] ?? null;
        self::assertIsArray($resourceSpan);
        $resource = $resourceSpan['resource'] ?? null;
        self::assertIsArray($resource);
        $resourceAttributes = $resource['attributes'] ?? null;
        self::assertIsArray($resourceAttributes);
        $serviceAttribute = $resourceAttributes[0] ?? null;
        self::assertIsArray($serviceAttribute);
        $serviceValue = $serviceAttribute['value'] ?? null;
        self::assertIsArray($serviceValue);
        $service = $serviceValue['stringValue'] ?? null;
        self::assertIsString($service);
        $scopeSpans = $resourceSpan['scopeSpans'] ?? null;
        self::assertIsArray($scopeSpans);
        $scopeSpan = $scopeSpans[0] ?? null;
        self::assertIsArray($scopeSpan);
        $spans = $scopeSpan['spans'] ?? null;
        self::assertIsArray($spans);
        $span = $spans[0] ?? null;
        self::assertIsArray($span);
        $rawAttributes = $span['attributes'] ?? null;
        self::assertIsArray($rawAttributes);
        $attributes = [];
        foreach ($rawAttributes as $attribute) {
            self::assertIsArray($attribute);
            $key = $attribute['key'] ?? null;
            $value = $attribute['value'] ?? null;
            self::assertIsString($key);
            self::assertIsArray($value);
            $stringValue = $value['stringValue'] ?? null;
            self::assertIsString($stringValue);
            $attributes[$key] = $stringValue;
        }
        /** @var array<string,mixed> $span */
        return ['span' => $span, 'attributes' => $attributes, 'service' => $service];
    }

    public function testSpanJoinsTheAgentTraceByCidAndCarriesCodesOnly(): void
    {
        $reporter = $this->reporter(new MockHandler([new Response(200, [], '{}')]));
        $reporter->report(self::CID, TicketOutcomeReporterInterface::OUTCOME_AGENT_UNAVAILABLE, 'agent_timeout');
        self::assertSame([], $this->sent, 'nothing is sent before the response');
        $this->flush();

        self::assertCount(1, $this->sent);
        $request = $this->sent[0];
        self::assertSame('POST', $request->getMethod());
        self::assertSame('https://langfuse.example/api/public/otel/v1/traces', (string) $request->getUri());
        self::assertSame('Basic ' . base64_encode('pk-lf-test:sk-lf-test'), $request->getHeaderLine('Authorization'));

        ['span' => $span, 'attributes' => $attributes, 'service' => $service] = $this->decodeSent(0);
        self::assertSame('7f0e6b2a3c4d4e5f9a1b2c3d4e5f6a7b', $span['traceId'], 'trace id is the cid, as in the agent');
        self::assertSame('ticket.agent_unavailable', $span['name']);
        self::assertSame('1758300000000000000', $span['startTimeUnixNano']);
        self::assertSame('ERROR', $attributes['langfuse.observation.level']);
        self::assertSame('agent_unavailable', $attributes['langfuse.observation.metadata.outcome']);
        self::assertSame('agent_timeout', $attributes['langfuse.observation.metadata.detail']);
        self::assertSame('production', $attributes['langfuse.environment']);
        self::assertSame(self::CID, $attributes['langfuse.observation.metadata.cid']);
        self::assertSame('oe-module-copilot', $service);
        // Only these keys: no user, patient, note or clinical value can be present.
        self::assertSame(
            ['langfuse.observation.type', 'langfuse.observation.level', 'langfuse.observation.metadata.cid', 'langfuse.observation.metadata.outcome', 'langfuse.environment', 'langfuse.observation.metadata.detail', 'langfuse.observation.status_message'],
            array_keys($attributes)
        );
    }

    public function testIssuedIsDefaultLevelWithoutDetail(): void
    {
        $reporter = $this->reporter(new MockHandler([new Response(200, [], '{}')]));
        $reporter->report(self::CID, TicketOutcomeReporterInterface::OUTCOME_ISSUED);
        $this->flush();
        ['span' => $span, 'attributes' => $attributes] = $this->decodeSent(0);
        self::assertSame('ticket.issued', $span['name']);
        self::assertArrayNotHasKey('langfuse.observation.metadata.detail', $attributes);
        self::assertSame('DEFAULT', $attributes['langfuse.observation.level']);
    }

    public function testBackendFailuresAreSwallowed(): void
    {
        $reporter = $this->reporter(new MockHandler([
            new ConnectException('refused', new Request('POST', 'https://langfuse.example/api/public/otel/v1/traces')),
            new Response(500, [], 'boom'),
        ]));
        $reporter->report(self::CID, TicketOutcomeReporterInterface::OUTCOME_ISSUED);
        $reporter->report(self::CID, TicketOutcomeReporterInterface::OUTCOME_NO_PRIOR_NOTE);
        $this->flush();
        self::assertCount(2, $this->sent);
        $this->addToAssertionCount(1); // reaching here without an exception is the assertion
    }
}
