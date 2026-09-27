<?php

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Tests;

use DateTimeZone;
use OpenEMR\Modules\Copilot\Agent\AgentUnavailableException;
use OpenEMR\Modules\Copilot\Authorization\CopilotAuthorizer;
use OpenEMR\Modules\Copilot\Config\CopilotConfig;
use OpenEMR\Modules\Copilot\ContextBundleBuilder;
use OpenEMR\Modules\Copilot\Controller\BriefingTicketController;
use PHPUnit\Framework\TestCase;

/**
 * Follow-ups see documents read on this chart open: after the panel's processing loop reads a
 * document (or a value is filed, rejected or un-filed), the panel asks for a ticket refresh with
 * `refresh_pending_facts`. The module re-authorizes from the session, re-reads the pending facts and
 * the chart's lab results, and replaces them in the agent's existing bundle
 * (`POST /v1/bundles/{id}/refresh`) - no new bundle, no briefing re-run, no model call - then mints a
 * fresh ticket exactly as a plain refresh does.
 */
final class FollowUpRefreshTest extends TestCase
{
    private const PATIENT = ['pid' => 42, 'uuid' => '3b9d2c1e-8f7a-4b6c-9d0e-1f2a3b4c5d6e'];
    private const NOTE = ['form_soap_id' => 1001, 'encounter' => 501, 'note_date' => '2026-06-10 09:30:00', 'plan' => 'Repeat HbA1c.'];
    private const BUNDLE_ID = 'e6b2abe5-8664-4ebb-bf81-e3d5cca35df4';
    private const BUNDLE_CID = '7f0e6b2a-3c4d-4e5f-9a1b-2c3d4e5f6a7b';
    private const SECRET = 'test-only-shared-secret-0123456789abcdef';
    private const ACL = ['patients/demo' => true, 'encounters/auth_a' => true, 'encounters/notes' => true, 'patients/med' => true, 'patients/lab' => true, 'patients/appt' => true];

    private AuditCapture $audit;
    private CapturingLogger $logger;

    protected function setUp(): void
    {
        $this->audit = new AuditCapture();
        $this->logger = new CapturingLogger();
    }

    private static function repo(): FakeProcessingRepository
    {
        $repo = new FakeProcessingRepository();
        $repo->records[920] = ['document_id' => 920, 'pid' => 42, 'content_sha256' => str_repeat('a', 64), 'doc_type' => 'lab_pdf', 'status' => 'extracted', 'prompt_version' => 'v', 'attempts' => 1, 'last_error_code' => null, 'identity_check' => 'match', 'extraction_json' => '{}', 'created_at' => '', 'updated_at' => '', 'received_at' => '2026-09-26 08:00:00'];
        $repo->values[920] = [['id' => 9, 'document_id' => 920, 'pid' => 42, 'status' => 'candidate', 'result_index' => 0, 'test_name' => 'Hemoglobin A1c', 'value_text' => '9.1', 'unit' => '%', 'reference_range' => '4.0-5.6', 'abnormal_flag' => null, 'flag_source' => 'unavailable', 'collection_date' => '2026-09-24', 'verification_status' => 'verified_exact', 'page' => 1, 'bbox' => null]];
        return $repo;
    }

    private function controller(FakeAgentClient $agent, FakeReader $reader, ?FakeProcessingRepository $repo = null): BriefingTicketController
    {
        return new BriefingTicketController(
            new CopilotAuthorizer(new FakeAcl(self::ACL), new FakeRelationships(['1:42' => 'primary_provider'])),
            $reader,
            new ContextBundleBuilder(new DateTimeZone('UTC')),
            $agent,
            new CopilotConfig('http://agent.test:8000', self::SECRET),
            $this->logger,
            $this->audit,
            static fn(): string => '2026-09-26 10:00:00',
            static fn(): int => 1758880000,
            pendingFacts: $repo ?? self::repo(),
            schema: new FakeSchemaStatus(true),
        );
    }

    private static function reader(): FakeReader
    {
        $lab = ['result_id' => 9001, 'order_id' => 0, 'test_name' => 'Hemoglobin A1c', 'code' => '', 'value' => '8.9', 'units' => '%', 'range' => '', 'abnormal' => 'high', 'result_status' => 'final', 'observed_at' => '2026-09-12 09:15:00'];
        return new FakeReader(patients: [42 => self::PATIENT], notes: [42 => [self::NOTE]], labs: [42 => [$lab]], userUuids: [1 => '9c3f0a2b-1d4e-4f5a-8b6c-7d8e9f0a1b2c']);
    }

    /** @return array<string,mixed> */
    private static function session(int $pid = 42): array
    {
        return ['authUserID' => 1, 'authUser' => 'dr_smith', 'pid' => $pid];
    }

    public function testRefreshReplacesPendingFactsAndLabResultsInTheExistingBundleWithoutANewBriefing(): void
    {
        $agent = new FakeAgentClient();
        $result = $this->controller($agent, self::reader())->handleForSession(self::session(), 42, self::BUNDLE_ID, self::BUNDLE_CID, refreshPendingFacts: true);

        self::assertSame(200, $result['status']);
        self::assertSame([], $agent->posts, 'no new bundle: the briefing is not re-run');
        self::assertSame([], $agent->documentPosts, 'no document briefing either');
        self::assertCount(1, $agent->refreshes);
        $sent = $agent->refreshes[0];
        self::assertSame(self::BUNDLE_ID, $sent['bundle_id']);
        self::assertSame(self::BUNDLE_CID, $sent['request']['correlation_id']);
        self::assertSame(self::PATIENT['uuid'], $sent['request']['patient_uuid'], 'the patient comes from the session, never the panel');
        self::assertSame('9c3f0a2b-1d4e-4f5a-8b6c-7d8e9f0a1b2c', $sent['request']['user_uuid']);
        self::assertSame('form_soap:1001', $sent['request']['prior_note_id']);
        self::assertSame(['copilot_extracted_value:9'], array_column($sent['request']['pending_document_facts'], 'fact_id'));
        self::assertSame('2026-09-26', $sent['request']['pending_document_facts'][0]['received_at']);
        self::assertSame(['procedure_result:9001'], array_column($sent['request']['lab_results'], 'result_id'), 'a value filed since the chart opened is a chart result now');

        $body = $result['body'];
        self::assertSame(self::BUNDLE_ID, $body['bundle_id']);
        self::assertSame(self::BUNDLE_CID, $body['correlation_id']);
        self::assertIsString($body['ticket'], 'a fresh single-use ticket, as a plain refresh');
        self::assertNull($body['sections']);
        self::assertSame([], $body['warnings']);
        self::assertStringContainsString('outcome=pending_refresh', $this->audit->events[0]['comment']);
        self::assertStringContainsString('pending=1', $this->audit->events[0]['comment']);
    }

    public function testAPlainRefreshStillTouchesNothing(): void
    {
        $agent = new FakeAgentClient();
        $reader = self::reader();
        $this->controller($agent, $reader)->handleForSession(self::session(), 42, self::BUNDLE_ID, self::BUNDLE_CID);
        self::assertSame([], $agent->refreshes);
        self::assertSame([], $reader->asOfSeen);
    }

    public function testTheAgentBeingUnreachableStillReturnsTheTicketWithAWarning(): void
    {
        $agent = new FakeAgentClient();
        $agent->refreshFailure = new AgentUnavailableException(AgentUnavailableException::REASON_REJECTED, 404);
        $result = $this->controller($agent, self::reader())->handleForSession(self::session(), 42, self::BUNDLE_ID, self::BUNDLE_CID, refreshPendingFacts: true);
        self::assertSame(200, $result['status']);
        self::assertIsString($result['body']['ticket']);
        self::assertSame(['pending_facts_not_refreshed'], $result['body']['warnings']);
    }

    public function testBindingRulesAreUnchanged(): void
    {
        $agent = new FakeAgentClient();
        $stale = $this->controller($agent, self::reader())->handleForSession(self::session(), 77, self::BUNDLE_ID, self::BUNDLE_CID, refreshPendingFacts: true);
        self::assertSame(409, $stale['status'], 'the panel pid must match the session');
        $bad = $this->controller($agent, self::reader())->handleForSession(self::session(), 42, 'not-a-uuid', self::BUNDLE_CID, refreshPendingFacts: true);
        self::assertSame(400, $bad['status']);
        self::assertSame([], $agent->refreshes);
    }

    public function testNoClinicalContentInLogsOrTheAuditRow(): void
    {
        $agent = new FakeAgentClient();
        $this->controller($agent, self::reader())->handleForSession(self::session(), 42, self::BUNDLE_ID, self::BUNDLE_CID, refreshPendingFacts: true);
        $text = $this->logger->dump() . json_encode($this->audit->events, JSON_THROW_ON_ERROR);
        foreach (['Hemoglobin', '9.1', '8.9', self::PATIENT['uuid']] as $needle) {
            self::assertStringNotContainsString($needle, $text);
        }
    }
}
