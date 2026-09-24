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
use OpenEMR\Modules\Copilot\Authorization\CopilotAuthorizer;
use OpenEMR\Modules\Copilot\Config\CopilotConfig;
use OpenEMR\Modules\Copilot\Controller\DocumentBriefingController;
use OpenEMR\Modules\Copilot\Data\SourceUnavailableException;
use OpenEMR\Modules\Copilot\Data\SqlDocumentReader;
use PHPUnit\Framework\TestCase;

/**
 * Week 2 document briefing: `POST /api/copilot/document-briefing` (module side)
 * and the signed hand-off to `POST /v1/documents/briefing` (agent side).
 * Written from the contract in copilot-agent/app/document_briefing.py.
 */
final class DocumentBriefingTest extends TestCase
{
    private const PID = 42;
    private const OTHER_PID = 77;
    private const USER = 1;
    private const PUUID = '3b9d2c1e-8f7a-4b6c-9d0e-1f2a3b4c5d6e';
    private const SECRET = 'test-only-shared-secret-0123456789abcdef';
    private const TS = 1758100000;
    private const DOC_ID = 913;
    private const DOC_BYTES = "%PDF-1.4 synthetic lab report bytes \x00\x01";
    private const FULL_ACL = ['patients/demo' => true, 'encounters/auth_a' => true, 'encounters/notes' => true, 'patients/med' => true, 'patients/lab' => true, 'patients/appt' => true];

    private CapturingLogger $logger;
    private AuditCapture $audit;

    protected function setUp(): void
    {
        $this->logger = new CapturingLogger();
        $this->audit = new AuditCapture();
    }

    /** @return array{authUserID:int, authUser:string, pid:int} */
    private static function session(int $pid = self::PID): array
    {
        return ['authUserID' => self::USER, 'authUser' => 'dr_smith', 'pid' => $pid];
    }

    /** @return array<string,mixed> */
    private static function agentOk(string $cid): array
    {
        return [
            'correlation_id' => $cid,
            'patient_uuid' => self::PUUID,
            'document_id' => self::DOC_ID,
            'status' => 'ok',
            'degraded_reason' => null,
            'briefing' => ['what_changed' => [], 'needs_attention' => [], 'what_to_consider' => [], 'limitations' => [], 'dropped' => [], 'refusal' => null],
            'rendered_text' => '',
            'provenance' => null,
        ];
    }

    /**
     * @param array<string,string> $bases
     */
    private function controller(FakeDocumentReader $documents, ?FakeAgentClient $agent = null, array $bases = [self::USER . ':' . self::PID => 'primary_provider']): DocumentBriefingController
    {
        return new DocumentBriefingController(
            new CopilotAuthorizer(new FakeAcl(self::FULL_ACL), new FakeRelationships($bases), false),
            new FakeReader(patients: [self::PID => ['pid' => self::PID, 'uuid' => self::PUUID]]),
            $documents,
            $agent ?? new FakeAgentClient(),
            $this->logger,
            $this->audit,
        );
    }

    private static function pdf(): FakeDocumentReader
    {
        return new FakeDocumentReader([self::PID => ['document_id' => self::DOC_ID, 'media_type' => 'application/pdf', 'bytes' => self::DOC_BYTES]]);
    }

    // ------------------------------------------------------------------ //
    // Controller
    // ------------------------------------------------------------------ //

    public function testPostsTheLatestDocumentByPatientUuidAndReturnsAgentJson(): void
    {
        $agent = new FakeAgentClient();
        $result = $this->controller(self::pdf(), $agent)->handleForSession(self::session(), self::PID, 'Anything new?');

        self::assertSame(200, $result['status']);
        self::assertCount(1, $agent->documentPosts);
        $sent = $agent->documentPosts[0]['request'];
        self::assertSame(self::PUUID, $sent['patient_uuid']);
        self::assertArrayNotHasKey('pid', $sent);
        self::assertSame(self::DOC_ID, $sent['document_id']);
        self::assertSame('application/pdf', $sent['media_type']);
        self::assertSame(base64_encode(self::DOC_BYTES), $sent['document_base64']);
        self::assertSame('Anything new?', $sent['question']);
        self::assertSame($agent->documentPosts[0]['cid'], $sent['correlation_id']);
        self::assertSame('ok', $result['body']['status']);
        self::assertSame(self::DOC_ID, $result['body']['document_id']);
        self::assertSame('no-store, private', $result['headers']['Cache-Control']);
    }

    public function testNoDocumentOnFileIsDegradedNotAnError(): void
    {
        $agent = new FakeAgentClient();
        $result = $this->controller(new FakeDocumentReader([]), $agent)->handleForSession(self::session(), self::PID);

        self::assertSame(200, $result['status']);
        self::assertSame('degraded', $result['body']['status']);
        self::assertSame('no_document_on_file', $result['body']['degraded_reason']);
        self::assertNull($result['body']['briefing']);
        self::assertSame(self::PUUID, $result['body']['patient_uuid']);
        self::assertSame([], $agent->documentPosts);
    }

    public function testAgentUnavailableIsDegraded(): void
    {
        $agent = new FakeAgentClient(new AgentUnavailableException(AgentUnavailableException::REASON_TIMEOUT));
        $result = $this->controller(self::pdf(), $agent)->handleForSession(self::session(), self::PID);

        self::assertSame(200, $result['status']);
        self::assertSame('degraded', $result['body']['status']);
        self::assertSame('agent_unavailable', $result['body']['degraded_reason']);
        self::assertSame(self::DOC_ID, $result['body']['document_id']);
    }

    public function testUnreadableDocumentStoreIsDegradedNotARawException(): void
    {
        $reader = new FakeDocumentReader([], new SourceUnavailableException('documents'));
        $result = $this->controller($reader)->handleForSession(self::session(), self::PID);

        self::assertSame(200, $result['status']);
        self::assertSame('degraded', $result['body']['status']);
        self::assertSame('document_unavailable', $result['body']['degraded_reason']);
    }

    public function testStalePanelPidIsRefusedWithoutReadingTheDocument(): void
    {
        $reader = self::pdf();
        $agent = new FakeAgentClient();
        $result = $this->controller($reader, $agent)->handleForSession(self::session(), self::OTHER_PID);

        self::assertSame(409, $result['status']);
        self::assertSame('patient_mismatch', $result['body']['detail']['code']);
        self::assertSame([], $reader->requested);
        self::assertSame([], $agent->documentPosts);
        self::assertSame(DocumentBriefingController::AUDIT_EVENT, $this->audit->events[0]['event']);
        self::assertFalse($this->audit->events[0]['success']);
    }

    public function testNoCareRelationshipIsRefused(): void
    {
        $reader = self::pdf();
        $result = $this->controller($reader, null, [])->handleForSession(self::session(), self::PID);

        self::assertSame(403, $result['status']);
        self::assertSame(CopilotAuthorizer::CODE_NO_CARE_RELATIONSHIP, $result['body']['detail']['code']);
        self::assertSame([], $reader->requested);
    }

    public function testUnauthenticatedIsRefused(): void
    {
        $result = $this->controller(self::pdf())->handleForSession([], null);
        self::assertSame(401, $result['status']);
        self::assertSame([], $this->audit->events);
    }

    public function testAuditRowIsWrittenOnceAndNeverCarriesDocumentContent(): void
    {
        $this->controller(self::pdf())->handleForSession(self::session(), self::PID);

        self::assertCount(1, $this->audit->events);
        $row = $this->audit->events[0];
        self::assertSame('copilot-document-briefing', $row['event']);
        self::assertSame(DocumentBriefingController::AUDIT_EVENT, $row['event']);
        self::assertTrue($row['success']);
        self::assertSame(self::PID, $row['pid']);
        $everything = $row['comment'] . $this->logger->dump();
        self::assertStringNotContainsString(base64_encode(self::DOC_BYTES), $everything);
        self::assertStringNotContainsString('synthetic lab report', $everything);
        self::assertStringNotContainsString(self::PUUID, $everything);
    }

    // ------------------------------------------------------------------ //
    // Guzzle hand-off
    // ------------------------------------------------------------------ //

    /** @param list<Response|ConnectException> $queue */
    private static function client(array $queue, MockHandler $handler, ?CopilotConfig $config = null): GuzzleAgentClient
    {
        foreach ($queue as $item) {
            $handler->append($item);
        }
        return new GuzzleAgentClient(
            $config ?? new CopilotConfig('http://agent.test:8000/', self::SECRET),
            new Client(['handler' => HandlerStack::create($handler)]),
            static fn(): int => self::TS
        );
    }

    /** @return array<string,mixed> */
    private static function request(string $cid): array
    {
        return ['correlation_id' => $cid, 'patient_uuid' => self::PUUID, 'document_id' => self::DOC_ID, 'media_type' => 'application/pdf', 'document_base64' => base64_encode(self::DOC_BYTES), 'question' => null];
    }

    public function testClientSignsExactBytesWithLongTimeoutAndReturnsDecodedBody(): void
    {
        $cid = '7f0e6b2a-3c4d-4e5f-9a1b-2c3d4e5f6a7b';
        $handler = new MockHandler();
        $client = self::client([new Response(200, ['Content-Type' => 'application/json'], json_encode(self::agentOk($cid), JSON_THROW_ON_ERROR))], $handler);

        $decoded = $client->postDocumentBriefing(self::request($cid), $cid);

        self::assertSame('ok', $decoded['status']);
        $request = $handler->getLastRequest();
        self::assertNotNull($request);
        self::assertSame('http://agent.test:8000/v1/documents/briefing', (string) $request->getUri());
        $body = (string) $request->getBody();
        self::assertSame(json_encode(self::request($cid), JSON_THROW_ON_ERROR | JSON_UNESCAPED_SLASHES), $body);
        self::assertSame((string) self::TS, $request->getHeaderLine('X-Copilot-Timestamp'));
        self::assertSame(BundleSigner::sign(self::SECRET, $body, self::TS), $request->getHeaderLine('X-Copilot-Signature'));
        self::assertSame($cid, $request->getHeaderLine('X-Correlation-Id'));
        self::assertEquals(GuzzleAgentClient::DOCUMENT_BRIEFING_TIMEOUT_SECONDS, $handler->getLastOptions()['timeout'] ?? null);
        self::assertEquals(90, GuzzleAgentClient::DOCUMENT_BRIEFING_TIMEOUT_SECONDS);
    }

    public function testClientMapsFailuresAndForeignPatientToReasonCodes(): void
    {
        $cid = '7f0e6b2a-3c4d-4e5f-9a1b-2c3d4e5f6a7b';
        $foreign = self::agentOk($cid);
        $foreign['patient_uuid'] = 'aaaaaaaa-0000-4000-8000-000000000077';
        $cases = [
            [new Response(401, [], '{}'), AgentUnavailableException::REASON_REJECTED],
            [new Response(200, [], 'not json'), AgentUnavailableException::REASON_BAD_RESPONSE],
            [new Response(200, [], json_encode($foreign, JSON_THROW_ON_ERROR)), AgentUnavailableException::REASON_BAD_RESPONSE],
            [new ConnectException('cURL error 28: Operation timed out', new Request('POST', 'http://agent.test:8000/v1/documents/briefing')), AgentUnavailableException::REASON_TIMEOUT],
            [new ConnectException('cURL error 7: Failed to connect', new Request('POST', 'http://agent.test:8000/v1/documents/briefing')), AgentUnavailableException::REASON_UNREACHABLE],
        ];
        foreach ($cases as [$queued, $reason]) {
            try {
                self::client([$queued], new MockHandler())->postDocumentBriefing(self::request($cid), $cid);
                self::fail("expected {$reason}");
            } catch (AgentUnavailableException $e) {
                self::assertSame($reason, $e->getReason());
            }
        }

        $handler = new MockHandler();
        try {
            self::client([new Response(200)], $handler, new CopilotConfig(null, self::SECRET))->postDocumentBriefing(self::request($cid), $cid);
            self::fail('expected not configured');
        } catch (AgentUnavailableException $e) {
            self::assertSame(AgentUnavailableException::REASON_NOT_CONFIGURED, $e->getReason());
        }
        self::assertNull($handler->getLastRequest());
    }
}

/** In-memory stand-in for the core `documents` table reader. */
final class FakeDocumentReader extends SqlDocumentReader
{
    /** @var list<int> */
    public array $requested = [];

    /**
     * @param array<int, array{document_id:int, media_type:string, bytes:string}> $byPid
     */
    public function __construct(private readonly array $byPid, private readonly ?SourceUnavailableException $failure = null)
    {
    }

    public function findLatestDocument(int $pid): ?array
    {
        $this->requested[] = $pid;
        if ($this->failure !== null) {
            throw $this->failure;
        }
        return $this->byPid[$pid] ?? null;
    }
}
