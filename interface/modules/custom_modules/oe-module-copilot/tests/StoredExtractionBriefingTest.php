<?php

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Tests;

use DateTimeZone;
use GuzzleHttp\Client;
use GuzzleHttp\Handler\MockHandler;
use GuzzleHttp\HandlerStack;
use GuzzleHttp\Psr7\Response;
use OpenEMR\Modules\Copilot\Agent\AgentUnavailableException;
use OpenEMR\Modules\Copilot\Agent\BundleSigner;
use OpenEMR\Modules\Copilot\Agent\GuzzleAgentClient;
use OpenEMR\Modules\Copilot\Authorization\CopilotAuthorizer;
use OpenEMR\Modules\Copilot\Config\CopilotConfig;
use OpenEMR\Modules\Copilot\ContextBundleBuilder;
use OpenEMR\Modules\Copilot\Controller\DocumentBriefingController;
use OpenEMR\Modules\Copilot\Data\SourceUnavailableException;
use PHPUnit\Framework\TestCase;

/**
 * Contract C4, briefing side: with the module tables present the briefing sends
 * every stored extraction (never re-extracting, never "the newest document") and
 * the chart lab history as prior_facts. Also the signed extract hand-off.
 */
final class StoredExtractionBriefingTest extends TestCase
{
    private const PID = 42;
    private const USER = 1;
    private const PUUID = '3b9d2c1e-8f7a-4b6c-9d0e-1f2a3b4c5d6e';
    private const SECRET = 'test-only-shared-secret-0123456789abcdef';
    private const FULL_ACL = ['patients/demo' => true, 'encounters/auth_a' => true, 'encounters/notes' => true, 'patients/med' => true, 'patients/lab' => true, 'patients/appt' => true];

    private CapturingLogger $logger;
    private AuditCapture $audit;

    protected function setUp(): void
    {
        $this->logger = new CapturingLogger();
        $this->audit = new AuditCapture();
    }

    private static function record(int $id, string $status, string $json): array
    {
        return ['document_id' => $id, 'pid' => self::PID, 'content_sha256' => hash('sha256', (string) $id), 'doc_type' => 'lab_pdf', 'status' => $status, 'prompt_version' => 'v', 'attempts' => 1, 'last_error_code' => null, 'identity_check' => 'match', 'extraction_json' => $json, 'created_at' => '', 'updated_at' => ''];
    }

    /** @param list<array<string,mixed>>|SourceUnavailableException $labs */
    private function controller(FakeProcessingRepository $repo, FakeAgentClient $agent, array|SourceUnavailableException $labs = [], bool $ready = true): DocumentBriefingController
    {
        return new DocumentBriefingController(
            new CopilotAuthorizer(new FakeAcl(self::FULL_ACL), new FakeRelationships([self::USER . ':' . self::PID => 'primary_provider'])),
            new FakeReader(patients: [self::PID => ['pid' => self::PID, 'uuid' => self::PUUID]], labs: [self::PID => $labs]),
            new FakeDocumentReader([self::PID => ['document_id' => 999, 'media_type' => 'application/pdf', 'bytes' => 'legacy-bytes']]),
            $agent,
            $this->logger,
            $this->audit,
            records: $repo,
            schema: new FakeSchemaStatus($ready),
            builder: new ContextBundleBuilder(new DateTimeZone('UTC')),
        );
    }

    public function testSendsEveryExtractedDocumentAndPriorFactsWithoutBytes(): void
    {
        $repo = new FakeProcessingRepository();
        $repo->records[5] = self::record(5, 'extracted', '{"document_id":5,"results":[{"test_name":"Hemoglobin A1c"}]}');
        $repo->records[6] = self::record(6, 'extracted', '{"document_id":6,"results":[]}');
        $repo->records[7] = self::record(7, 'held_identity', '{"document_id":7,"results":[]}');
        $repo->waiting(5, 6, 7);
        $labs = [
            ['result_id' => 1, 'order_id' => 3, 'test_name' => 'Hemoglobin A1c', 'code' => '4548-4', 'value' => '8.4', 'units' => '%', 'range' => '4.0-5.6', 'abnormal' => 'high', 'result_status' => 'final', 'observed_at' => '2026-06-01 08:00:00'],
            ['result_id' => 2, 'order_id' => 3, 'test_name' => 'Glucose', 'code' => '', 'value' => '142', 'units' => 'mg/dL', 'range' => '', 'abnormal' => '', 'result_status' => 'entered-in-error', 'observed_at' => '2026-06-01 08:00:00'],
        ];
        $agent = new FakeAgentClient();

        $result = $this->controller($repo, $agent, $labs)->handleForSession(['authUserID' => self::USER, 'authUser' => 'dr_smith', 'pid' => self::PID], self::PID, 'What changed?');

        self::assertSame(200, $result['status']);
        $sent = $agent->documentPosts[0]['request'];
        self::assertSame([5, 6], array_column($sent['documents'], 'document_id'), 'held documents are never sent');
        self::assertSame('lab_pdf', $sent['documents'][0]['doc_type']);
        self::assertSame('Hemoglobin A1c', $sent['documents'][0]['extraction']['results'][0]['test_name']);
        self::assertSame(['procedure_result:1'], array_column($sent['prior_facts'], 'result_id'), 'entered-in-error is not chart history');
        self::assertSame('What changed?', $sent['question']);
        self::assertArrayNotHasKey('document_base64', $sent);
        self::assertArrayNotHasKey('pid', $sent);
        self::assertSame(self::PUUID, $sent['patient_uuid']);
        $everything = implode("\n", array_column($this->audit->events, 'comment')) . $this->logger->dump();
        self::assertStringNotContainsString('Hemoglobin', $everything);
        self::assertStringNotContainsString(self::PUUID, $everything);
    }

    /**
     * 2026-09-25 review: the briefing must follow the clinician's review. A value already filed is
     * chart history (it reaches the agent as a prior fact), and a rejected or un-filed value must not
     * be briefed at all; only values still waiting for review are sent as document facts.
     */
    public function testReviewedValuesAreNotSentAsDocumentFacts(): void
    {
        $repo = new FakeProcessingRepository();
        $repo->records[5] = self::record(5, 'extracted', '{"document_id":5,"results":[{"test_name":"A"},{"test_name":"B"},{"test_name":"C"},{"test_name":"D"}]}');
        $repo->values[5] = [
            ['id' => 1, 'document_id' => 5, 'pid' => self::PID, 'result_index' => 0, 'status' => 'candidate'],
            ['id' => 2, 'document_id' => 5, 'pid' => self::PID, 'result_index' => 1, 'status' => 'filed'],
            ['id' => 3, 'document_id' => 5, 'pid' => self::PID, 'result_index' => 2, 'status' => 'rejected'],
            ['id' => 4, 'document_id' => 5, 'pid' => self::PID, 'result_index' => 3, 'status' => 'unfiled'],
        ];
        $repo->records[6] = self::record(6, 'extracted', '{"document_id":6,"results":[{"test_name":"E"}]}');
        $repo->values[6] = [['id' => 5, 'document_id' => 6, 'pid' => self::PID, 'result_index' => 0, 'status' => 'filed']];
        $agent = new FakeAgentClient();

        $this->controller($repo, $agent, [])->handleForSession(['authUserID' => self::USER, 'authUser' => 'dr_smith', 'pid' => self::PID], self::PID);

        $sent = $agent->documentPosts[0]['request']['documents'];
        self::assertSame([5], array_column($sent, 'document_id'), 'a document whose values were all reviewed is not sent');
        self::assertSame([['test_name' => 'A']], $sent[0]['extraction']['results'], 'only the value still waiting for review is sent');
    }

    /**
     * The 20 briefing slots go to the newest documents that still have work: a value waiting for
     * review (a `candidate` row - an unreadable lab value is still fileable or rejectable, and an
     * intake item is never reviewable, so both stay waiting). Fully reviewed documents, however new,
     * no longer push older unreviewed ones out.
     */
    public function testFullyReviewedDocumentsDoNotTakeBriefingSlots(): void
    {
        $repo = new FakeProcessingRepository();
        for ($id = 1; $id <= 21; $id++) {
            $repo->records[$id] = self::record($id, 'extracted', '{"document_id":' . $id . ',"results":[{"test_name":"Glucose"}]}');
            $repo->values[$id] = [['id' => $id, 'document_id' => $id, 'pid' => self::PID, 'result_index' => 0, 'status' => 'candidate', 'verification_status' => $id === 21 ? 'unreadable' : 'verified_exact']];
        }
        for ($id = 30; $id <= 34; $id++) {
            $repo->records[$id] = self::record($id, 'extracted', '{"document_id":' . $id . ',"results":[{"test_name":"Glucose"}]}');
            $repo->values[$id] = [['id' => $id, 'document_id' => $id, 'pid' => self::PID, 'result_index' => 0, 'status' => $id % 2 === 0 ? 'filed' : 'rejected']];
        }
        $repo->records[40] = ['doc_type' => 'intake_form'] + self::record(40, 'extracted', '{"document_id":40,"doc_type":"intake_form","current_medications":[]}');
        $repo->values[40] = [['id' => 40, 'document_id' => 40, 'pid' => self::PID, 'result_index' => 0, 'status' => 'candidate']];
        $agent = new FakeAgentClient();

        $this->controller($repo, $agent, [])->handleForSession(['authUserID' => self::USER, 'authUser' => 'dr_smith', 'pid' => self::PID], self::PID);

        $sent = array_column($agent->documentPosts[0]['request']['documents'], 'document_id');
        self::assertSame(array_merge(range(3, 21), [40]), $sent, 'the newest 20 documents with a value waiting, oldest first');
    }

    public function testTheRepositoryListsOnlyDocumentsWithAValueWaiting(): void
    {
        $repo = new FakeProcessingRepository();
        $repo->records[1] = self::record(1, 'extracted', '{"document_id":1,"results":[{"test_name":"A"}]}');
        $repo->values[1] = [['id' => 1, 'document_id' => 1, 'pid' => self::PID, 'result_index' => 0, 'status' => 'candidate']];
        $repo->records[2] = self::record(2, 'extracted', '{"document_id":2,"results":[{"test_name":"A"}]}');
        $repo->values[2] = [['id' => 2, 'document_id' => 2, 'pid' => self::PID, 'result_index' => 0, 'status' => 'unfiled']];
        $repo->records[3] = self::record(3, 'extracted', '{"document_id":3,"results":[]}'); // read, nothing to review
        $repo->records[4] = self::record(4, 'extracted', '{"document_id":4,"results":[{"test_name":"A"},{"test_name":"B"}]}');
        $repo->values[4] = [
            ['id' => 3, 'document_id' => 4, 'pid' => self::PID, 'result_index' => 0, 'status' => 'filed'],
            ['id' => 4, 'document_id' => 4, 'pid' => self::PID, 'result_index' => 1, 'status' => 'candidate'],
        ];

        self::assertSame([1, 4], array_column($repo->listExtractions(self::PID, 20), 'document_id'));
        self::assertSame([4], array_column($repo->listExtractions(self::PID, 1), 'document_id'), 'the newest when capped');
        self::assertSame([0], $repo->listExtractions(self::PID, 20)[1]['reviewed_indices']);
        self::assertSame(['extracted' => 4, 'waiting' => 2], $repo->countExtractions(self::PID));
    }

    /**
     * 2026-09-26 production report: five documents - one read document whose only value was filed,
     * two "already read (same file)" duplicates, two held for identity. The briefing said nothing had
     * been read (`no_extracted_documents`), which was false: everything read had been reviewed.
     */
    public function testEveryReadValueReviewedIsItsOwnReasonNotNothingRead(): void
    {
        $repo = new FakeProcessingRepository();
        $repo->records[10] = self::record(10, 'extracted', '{"document_id":10,"results":[{"test_name":"Hemoglobin A1c"}]}');
        $repo->values[10] = [['id' => 1, 'document_id' => 10, 'pid' => self::PID, 'result_index' => 0, 'status' => 'filed']];
        $repo->records[11] = ['extraction_json' => null] + self::record(11, 'skipped_duplicate', '');
        $repo->records[12] = ['extraction_json' => null] + self::record(12, 'skipped_duplicate', '');
        $repo->records[13] = self::record(13, 'held_identity', '{"document_id":13,"results":[{"test_name":"Glucose"}]}');
        $repo->values[13] = [['id' => 2, 'document_id' => 13, 'pid' => self::PID, 'result_index' => 0, 'status' => 'candidate']];
        $repo->records[14] = self::record(14, 'held_identity', '{"document_id":14,"results":[{"test_name":"Glucose"}]}');
        $repo->values[14] = [['id' => 3, 'document_id' => 14, 'pid' => self::PID, 'result_index' => 0, 'status' => 'candidate']];
        $agent = new FakeAgentClient();

        $result = $this->controller($repo, $agent)->handleForSession(['authUserID' => self::USER, 'authUser' => 'dr_smith', 'pid' => self::PID], self::PID);

        self::assertSame(200, $result['status']);
        self::assertSame('degraded', $result['body']['status']);
        self::assertSame('all_values_reviewed', DocumentBriefingController::DEGRADED_ALL_VALUES_REVIEWED);
        self::assertSame(DocumentBriefingController::DEGRADED_ALL_VALUES_REVIEWED, $result['body']['degraded_reason']);
        self::assertSame([], $agent->documentPosts, 'nothing is sent when nothing is waiting');
        self::assertStringContainsString('outcome=all_values_reviewed', $this->audit->events[0]['comment']);
    }

    public function testOnlyHeldAndDuplicateDocumentsIsStillNothingRead(): void
    {
        $repo = new FakeProcessingRepository();
        $repo->records[11] = ['extraction_json' => null] + self::record(11, 'skipped_duplicate', '');
        $repo->records[13] = self::record(13, 'held_identity', '{"document_id":13,"results":[{"test_name":"Glucose"}]}');
        $repo->values[13] = [['id' => 2, 'document_id' => 13, 'pid' => self::PID, 'result_index' => 0, 'status' => 'candidate']];
        $agent = new FakeAgentClient();

        $result = $this->controller($repo, $agent)->handleForSession(['authUserID' => self::USER, 'authUser' => 'dr_smith', 'pid' => self::PID], self::PID);

        self::assertSame('no_extracted_documents', $result['body']['degraded_reason']);
        self::assertSame([], $agent->documentPosts);
    }

    public function testNothingExtractedYetIsDegradedWithoutAnAgentCall(): void
    {
        $agent = new FakeAgentClient();
        $result = $this->controller(new FakeProcessingRepository(), $agent)->handleForSession(['authUserID' => self::USER, 'authUser' => 'dr_smith', 'pid' => self::PID]);
        self::assertSame(200, $result['status']);
        self::assertSame('degraded', $result['body']['status']);
        self::assertSame('no_extracted_documents', $result['body']['degraded_reason']);
        self::assertSame([], $agent->documentPosts);
    }

    public function testUnreadableChartHistoryStillBriefsWithEmptyPriorFacts(): void
    {
        $repo = new FakeProcessingRepository();
        $repo->records[5] = self::record(5, 'extracted', '{"document_id":5,"results":[]}');
        $repo->waiting(5);
        $agent = new FakeAgentClient();
        $this->controller($repo, $agent, new SourceUnavailableException('lab_results'))->handleForSession(['authUserID' => self::USER, 'authUser' => 'dr_smith', 'pid' => self::PID]);
        self::assertSame([], $agent->documentPosts[0]['request']['prior_facts']);
    }

    public function testWithoutTheModuleTablesTheLegacyPathIsUnchanged(): void
    {
        $agent = new FakeAgentClient();
        $this->controller(new FakeProcessingRepository(), $agent, [], false)->handleForSession(['authUserID' => self::USER, 'authUser' => 'dr_smith', 'pid' => self::PID]);
        self::assertSame(base64_encode('legacy-bytes'), $agent->documentPosts[0]['request']['document_base64']);
    }

    public function testAgentUnavailableIsDegraded(): void
    {
        $repo = new FakeProcessingRepository();
        $repo->records[5] = self::record(5, 'extracted', '{"document_id":5,"results":[]}');
        $repo->waiting(5);
        $agent = new FakeAgentClient(new AgentUnavailableException(AgentUnavailableException::REASON_REJECTED, 422));
        $result = $this->controller($repo, $agent)->handleForSession(['authUserID' => self::USER, 'authUser' => 'dr_smith', 'pid' => self::PID]);
        self::assertSame('agent_unavailable', $result['body']['degraded_reason']);
    }

    // ------------------------------------------------------------------ //
    // Signed extract hand-off
    // ------------------------------------------------------------------ //

    public function testExtractClientSignsPostsAndChecksTheEchoedIds(): void
    {
        $cid = '7f0e6b2a-3c4d-4e5f-9a1b-2c3d4e5f6a7b';
        $request = ['correlation_id' => $cid, 'patient_uuid' => self::PUUID, 'document_id' => 5, 'doc_type' => 'lab_pdf', 'media_type' => 'application/pdf', 'document_base64' => base64_encode('x')];
        $ok = ['correlation_id' => $cid, 'patient_uuid' => self::PUUID, 'document_id' => 5, 'doc_type' => 'lab_pdf', 'status' => 'ok', 'degraded_reason' => null, 'prompt_version' => 'v', 'extraction_model' => 'm', 'extraction' => ['results' => []], 'printed_identity' => null];

        $handler = new MockHandler([new Response(200, [], json_encode($ok, JSON_THROW_ON_ERROR))]);
        $client = new GuzzleAgentClient(new CopilotConfig('http://agent.test:8000', self::SECRET), new Client(['handler' => HandlerStack::create($handler)]), static fn(): int => 1758100000);
        self::assertSame('ok', $client->postDocumentExtraction($request, $cid)['status']);
        $sent = $handler->getLastRequest();
        self::assertNotNull($sent);
        self::assertSame('http://agent.test:8000/v1/documents/extract', (string) $sent->getUri());
        self::assertSame(BundleSigner::sign(self::SECRET, (string) $sent->getBody(), 1758100000), $sent->getHeaderLine('X-Copilot-Signature'));
        self::assertEquals(GuzzleAgentClient::DOCUMENT_EXTRACT_TIMEOUT_SECONDS, $handler->getLastOptions()['timeout'] ?? null);

        $wrongDoc = ['document_id' => 6] + $ok;
        $handler2 = new MockHandler([new Response(200, [], json_encode($wrongDoc, JSON_THROW_ON_ERROR))]);
        $client2 = new GuzzleAgentClient(new CopilotConfig('http://agent.test:8000', self::SECRET), new Client(['handler' => HandlerStack::create($handler2)]), static fn(): int => 1758100000);
        try {
            $client2->postDocumentExtraction($request, $cid);
            self::fail('a response for another document must be rejected');
        } catch (AgentUnavailableException $e) {
            self::assertSame(AgentUnavailableException::REASON_BAD_RESPONSE, $e->getReason());
        }
    }
}
