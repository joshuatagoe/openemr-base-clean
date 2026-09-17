<?php

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Tests;

use GuzzleHttp\Client;
use GuzzleHttp\Exception\ConnectException;
use GuzzleHttp\Handler\MockHandler;
use GuzzleHttp\HandlerStack;
use GuzzleHttp\Psr7\Request;
use GuzzleHttp\Psr7\Response;
use OpenEMR\Modules\Copilot\Agent\AgentUnavailableException;
use OpenEMR\Modules\Copilot\Agent\BundleSigner;
use OpenEMR\Modules\Copilot\Agent\GuzzleAgentClient;
use OpenEMR\Modules\Copilot\Config\CopilotConfig;
use OpenEMR\Modules\Copilot\Ticket\TicketSigner;
use PHPUnit\Framework\TestCase;

/**
 * Byte-compatibility of the signature and ticket with the agent's reference
 * implementation (copilot-agent/app/security.py), and the Guzzle hand-off's
 * request construction and failure mapping.
 */
final class AgentHandoffTest extends TestCase
{
    // Vectors produced by copilot-agent/app/security.py (sign_body / mint_ticket) with these inputs.
    private const VECTOR_SECRET = 'vector-secret-0123456789abcdef-0123456789';
    private const VECTOR_BODY = '{"schema_version":"1.0","correlation_id":"7f0e6b2a-3c4d-4e5f-9a1b-2c3d4e5f6a7b"}';
    private const VECTOR_TS = 1758100000;
    private const VECTOR_SIG = 'v1=853317f078e3326476cd6abdf2bc4895fa09eea3730a353dc0f60b3fc3391222';
    private const VECTOR_TICKET = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.'
        . 'eyJzdWIiOiI5YzNmMGEyYi0xZDRlLTRmNWEtOGI2Yy03ZDhlOWYwYTFiMmMiLCJwdXVpZCI6IjNiOWQyYzFlLThmN2EtNGI2Yy05ZDBlLTFmMmEzYjRjNWQ2ZSIsImJ1bmRsZV9pZCI6ImU2YjJhYmU1LTg2NjQtNGViYi1iZjgxLWUzZDVjY2EzNWRmNCIsImNpZCI6IjdmMGU2YjJhLTNjNGQtNGU1Zi05YTFiLTJjM2Q0ZTVmNmE3YiIsImp0aSI6IjBmMWUyZDNjLTRiNWEtNDk2OC04Nzc2LTY1NTQ0MzMyMjExMCIsImlhdCI6MTc1ODEwMDAwMCwiZXhwIjoxNzU4MTAwMTIwfQ.'
        . 'CEuotOPYKCQqgp2s5HoTX-dLsQBvWOFuDo9v-swAhaE';

    private const CID = '7f0e6b2a-3c4d-4e5f-9a1b-2c3d4e5f6a7b';
    private const PUUID = '3b9d2c1e-8f7a-4b6c-9d0e-1f2a3b4c5d6e';

    public function testBodySignatureMatchesAgentReferenceVector(): void
    {
        self::assertSame(self::VECTOR_SIG, BundleSigner::sign(self::VECTOR_SECRET, self::VECTOR_BODY, self::VECTOR_TS));
        self::assertSame(
            ['X-Copilot-Timestamp' => '1758100000', 'X-Copilot-Signature' => self::VECTOR_SIG],
            BundleSigner::headers(self::VECTOR_SECRET, self::VECTOR_BODY, self::VECTOR_TS)
        );
        self::assertNotSame(self::VECTOR_SIG, BundleSigner::sign(self::VECTOR_SECRET, self::VECTOR_BODY . ' ', self::VECTOR_TS));
        self::assertNotSame(self::VECTOR_SIG, BundleSigner::sign(self::VECTOR_SECRET, self::VECTOR_BODY, self::VECTOR_TS + 1));
    }

    public function testTicketMatchesAgentReferenceVector(): void
    {
        $ticket = (new TicketSigner(self::VECTOR_SECRET))->mint(
            '9c3f0a2b-1d4e-4f5a-8b6c-7d8e9f0a1b2c',
            self::PUUID,
            'e6b2abe5-8664-4ebb-bf81-e3d5cca35df4',
            self::CID,
            '0f1e2d3c-4b5a-4968-8776-655443322110',
            self::VECTOR_TS,
            120,
        );
        self::assertSame(self::VECTOR_TICKET, $ticket);
    }

    public function testTicketTtlMustBePositive(): void
    {
        $this->expectException(\RuntimeException::class);
        (new TicketSigner(self::VECTOR_SECRET))->mint('u', self::PUUID, 'b', self::CID, 'j', self::VECTOR_TS, 0);
    }

    // ------------------------------------------------------------------ //
    // Guzzle hand-off
    // ------------------------------------------------------------------ //

    /** @return array<string,mixed> */
    private static function bundle(): array
    {
        return [
            'schema_version' => '1.0',
            'correlation_id' => self::CID,
            'patient_uuid' => self::PUUID,
            'prior_note' => ['note_id' => 'form_soap:1001', 'encounter_id' => 'form_encounter:501', 'note_date' => '2026-06-10T14:30:00Z', 'plan_text' => 'Repeat HbA1c in three months.'],
            'data_quality' => ['sources_unavailable' => [], 'duplicates_collapsed' => 0],
            'lab_results' => [],
        ];
    }

    /**
     * @param list<Response|ConnectException> $queue
     */
    private static function client(array $queue, MockHandler $handler, ?CopilotConfig $config = null): GuzzleAgentClient
    {
        foreach ($queue as $item) {
            $handler->append($item);
        }
        $http = new Client(['handler' => HandlerStack::create($handler)]);
        return new GuzzleAgentClient(
            $config ?? new CopilotConfig('http://agent.test:8000/', self::VECTOR_SECRET),
            $http,
            static fn(): int => self::VECTOR_TS
        );
    }

    public function testSuccessfulHandoffSignsExactBytesAndReturnsAccepted(): void
    {
        $accepted = ['bundle_id' => 'e6b2abe5-8664-4ebb-bf81-e3d5cca35df4', 'correlation_id' => self::CID, 'patient_uuid' => self::PUUID, 'expires_at' => '2026-09-17T15:15:00Z'];
        $handler = new MockHandler();
        $client = self::client([new Response(201, ['Content-Type' => 'application/json'], json_encode($accepted, JSON_THROW_ON_ERROR))], $handler);

        $result = $client->postBundle(self::bundle(), self::CID);

        self::assertSame('e6b2abe5-8664-4ebb-bf81-e3d5cca35df4', $result->bundleId);
        self::assertSame(self::CID, $result->correlationId);
        self::assertSame(self::PUUID, $result->patientUuid);
        self::assertSame('2026-09-17T15:15:00Z', $result->expiresAt);

        $request = $handler->getLastRequest();
        self::assertNotNull($request);
        self::assertSame('POST', $request->getMethod());
        self::assertSame('http://agent.test:8000/v1/bundles', (string) $request->getUri());
        self::assertSame(self::CID, $request->getHeaderLine('X-Correlation-Id'));
        self::assertSame('application/json', $request->getHeaderLine('Content-Type'));
        $body = (string) $request->getBody();
        self::assertSame(json_encode(self::bundle(), JSON_THROW_ON_ERROR | JSON_UNESCAPED_SLASHES), $body);
        self::assertSame('1758100000', $request->getHeaderLine('X-Copilot-Timestamp'));
        self::assertSame(BundleSigner::sign(self::VECTOR_SECRET, $body, self::VECTOR_TS), $request->getHeaderLine('X-Copilot-Signature'));
    }

    public function testRejectedTimeoutAndBadResponsesMapToReasonCodes(): void
    {
        $cases = [
            [new Response(401, [], '{"detail":{"code":"invalid_signature"}}'), AgentUnavailableException::REASON_REJECTED, 401],
            [new Response(503, [], ''), AgentUnavailableException::REASON_REJECTED, 503],
            [new Response(201, [], 'not json'), AgentUnavailableException::REASON_BAD_RESPONSE, 201],
            [new Response(201, [], '{"bundle_id":"x","correlation_id":"other","patient_uuid":"' . self::PUUID . '","expires_at":"t"}'), AgentUnavailableException::REASON_BAD_RESPONSE, 201],
            [new Response(201, [], '{"bundle_id":"x","correlation_id":"' . self::CID . '","patient_uuid":"someone-else","expires_at":"t"}'), AgentUnavailableException::REASON_BAD_RESPONSE, 201],
            [new ConnectException('cURL error 28: Operation timed out', new Request('POST', 'http://agent.test:8000/v1/bundles')), AgentUnavailableException::REASON_TIMEOUT, null],
            [new ConnectException('cURL error 7: Failed to connect', new Request('POST', 'http://agent.test:8000/v1/bundles')), AgentUnavailableException::REASON_UNREACHABLE, null],
        ];
        foreach ($cases as [$queued, $reason, $status]) {
            $client = self::client([$queued], new MockHandler());
            try {
                $client->postBundle(self::bundle(), self::CID);
                self::fail("expected AgentUnavailableException({$reason})");
            } catch (AgentUnavailableException $e) {
                self::assertSame($reason, $e->getReason());
                self::assertSame($status, $e->getHttpStatus());
                self::assertStringNotContainsString('invalid_signature', $e->getMessage());
            }
        }
    }

    public function testUnconfiguredClientDoesNotSendAnything(): void
    {
        foreach ([new CopilotConfig(null, self::VECTOR_SECRET), new CopilotConfig('http://agent.test', null), new CopilotConfig('http://agent.test', 'too-short')] as $config) {
            $handler = new MockHandler();
            $client = self::client([new Response(201)], $handler, $config);
            try {
                $client->postBundle(self::bundle(), self::CID);
                self::fail('expected not configured');
            } catch (AgentUnavailableException $e) {
                self::assertSame(AgentUnavailableException::REASON_NOT_CONFIGURED, $e->getReason());
            }
            self::assertNull($handler->getLastRequest());
        }
    }

    public function testConfigFromEnvironmentTrimsAndRequiresBothValues(): void
    {
        putenv(CopilotConfig::ENV_AGENT_URL . '=  http://agent.test:8000/  ');
        putenv(CopilotConfig::ENV_TICKET_SECRET . '=' . self::VECTOR_SECRET);
        try {
            $config = CopilotConfig::fromEnvironment();
            self::assertTrue($config->isConfigured());
            self::assertSame('http://agent.test:8000', $config->agentBaseUrl());
            self::assertSame(120, $config->ticketTtlSeconds);
            self::assertSame(2.0, $config->agentTimeoutSeconds);

            putenv(CopilotConfig::ENV_TICKET_SECRET . '=');
            self::assertFalse(CopilotConfig::fromEnvironment()->isConfigured());
        } finally {
            putenv(CopilotConfig::ENV_AGENT_URL);
            putenv(CopilotConfig::ENV_TICKET_SECRET);
        }
    }
}
