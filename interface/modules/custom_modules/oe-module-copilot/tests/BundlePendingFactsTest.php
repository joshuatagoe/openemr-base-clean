<?php

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Tests;

use DateTimeZone;
use OpenEMR\Modules\Copilot\Authorization\CopilotAuthorizer;
use OpenEMR\Modules\Copilot\Config\CopilotConfig;
use OpenEMR\Modules\Copilot\ContextBundleBuilder;
use OpenEMR\Modules\Copilot\Controller\BriefingTicketController;
use PHPUnit\Framework\TestCase;

/**
 * ADR-009 section 7 (entered-in-error excluded deliberately, with its own count)
 * and ADR-011 / contract C5 (pending document facts in the follow-up bundle).
 */
final class BundlePendingFactsTest extends TestCase
{
    private const CID = '7f0e6b2a-3c4d-4e5f-9a1b-2c3d4e5f6a7b';
    private const PATIENT = ['pid' => 42, 'uuid' => '3b9d2c1e-8f7a-4b6c-9d0e-1f2a3b4c5d6e'];
    private const NOTE = ['form_soap_id' => 1001, 'encounter' => 501, 'note_date' => '2026-06-10 09:30:00', 'plan' => 'Repeat HbA1c.'];

    /**
     * @param array<string,mixed> $overrides
     * @return array<string,mixed>
     */
    private static function lab(int $id, string $status, array $overrides = []): array
    {
        return $overrides + ['result_id' => $id, 'test_name' => 'Glucose', 'value' => '142', 'units' => 'mg/dL', 'abnormal' => 'high', 'result_status' => $status, 'observed_at' => '2026-09-12 04:15:00'];
    }

    /** @return array<string,mixed> */
    private static function pending(int $id, ?string $bbox = '0.1,0.2,0.55,0.23'): array
    {
        return ['id' => $id, 'document_id' => 913, 'test_name' => 'Hemoglobin A1c', 'value_text' => '7.9', 'unit' => '%', 'reference_range' => '4.0-5.6', 'abnormal_flag' => 'H', 'flag_source' => 'extracted', 'collection_date' => '2026-09-10', 'verification_status' => 'verified_exact', 'page' => 1, 'bbox' => $bbox];
    }

    public function testEnteredInErrorIsExcludedWithItsOwnCountNotAsUnmapped(): void
    {
        $builder = new ContextBundleBuilder(new DateTimeZone('UTC'));
        $bundle = $builder->build(self::CID, self::PATIENT, self::NOTE, [self::lab(1, 'final', ['test_name' => 'Hemoglobin A1c', 'value' => '7.1']), self::lab(2, 'entered-in-error'), self::lab(3, 'cancel')]);

        self::assertSame(['procedure_result:1'], array_column($bundle['lab_results'], 'result_id'));
        self::assertSame(['entered_in_error' => 1], $builder->getExcludedCounts());
        self::assertSame(1, $builder->getOmittedCounts()['unmapped_status'], 'only the genuinely unmapped status is counted there');
    }

    public function testPendingFactsFollowContractC5AndAreOmittedWhenThereAreNone(): void
    {
        $builder = new ContextBundleBuilder(new DateTimeZone('UTC'));
        $without = $builder->build(self::CID, self::PATIENT, self::NOTE, []);
        self::assertArrayNotHasKey('pending_document_facts', $without, 'an older agent never sees the new field unless there is something to send');

        $bundle = $builder->build(self::CID, self::PATIENT, self::NOTE, [], pendingFacts: [self::pending(5), self::pending(6, null)]);
        self::assertSame([
            'fact_id' => 'copilot_extracted_value:5',
            'document_id' => 913,
            'test_name' => 'Hemoglobin A1c',
            'value_text' => '7.9',
            'unit' => '%',
            'reference_range' => '4.0-5.6',
            'abnormal_flag' => 'H',
            'flag_source' => 'extracted',
            'collection_date' => '2026-09-10',
            'verification_status' => 'verified_exact',
            'page' => 1,
            'bbox' => [0.1, 0.2, 0.55, 0.23],
            'status' => 'candidate',
        ], $bundle['pending_document_facts'][0]);
        self::assertNull($bundle['pending_document_facts'][1]['bbox']);
        self::assertSame('1.0', $bundle['schema_version']);
    }

    public function testABoxWithoutAPageIsNotSentBecauseC5RejectsIt(): void
    {
        $builder = new ContextBundleBuilder(new DateTimeZone('UTC'));
        $facts = $builder->mapPendingFacts([['page' => null] + self::pending(7)]);
        self::assertNull($facts[0]['page']);
        self::assertNull($facts[0]['bbox'], 'PendingDocumentFact: bbox requires a page');
    }

    public function testPriorFactsUseTheWeek1LabResultShapeAndDropEnteredInError(): void
    {
        $builder = new ContextBundleBuilder(new DateTimeZone('UTC'));
        $facts = $builder->mapLabResults([self::lab(1, 'final'), self::lab(2, 'entered-in-error')]);
        self::assertSame([[
            'result_id' => 'procedure_result:1',
            'order_id' => null,
            'test_name' => 'Glucose',
            'code' => null,
            'value' => '142',
            'units' => 'mg/dL',
            'abnormal_flag' => 'high',
            'range' => null,
            'status' => 'final',
            'observed_at' => '2026-09-12T04:15:00Z',
        ]], $facts);
    }

    public function testFollowUpBundleCarriesThePatientsPendingFacts(): void
    {
        $repo = new FakeProcessingRepository();
        $repo->records[913] = ['document_id' => 913, 'pid' => 42, 'content_sha256' => str_repeat('a', 64), 'doc_type' => 'lab_pdf', 'status' => 'extracted', 'prompt_version' => 'v', 'attempts' => 1, 'last_error_code' => null, 'identity_check' => 'match', 'extraction_json' => '{}', 'created_at' => '', 'updated_at' => ''];
        $repo->values[913] = [['id' => 5, 'document_id' => 913, 'pid' => 42, 'status' => 'candidate', 'result_index' => 0] + self::pending(5)];
        $repo->records[914] = ['status' => 'held_identity', 'document_id' => 914] + $repo->records[913];
        $repo->values[914] = [['id' => 6, 'document_id' => 914, 'pid' => 42, 'status' => 'candidate', 'result_index' => 0] + self::pending(6)];

        $agent = new FakeAgentClient();
        $controller = new BriefingTicketController(
            new CopilotAuthorizer(new FakeAcl(['patients/demo' => true, 'encounters/auth_a' => true, 'encounters/notes' => true, 'patients/med' => true, 'patients/lab' => true, 'patients/appt' => true]), new FakeRelationships(['1:42' => 'primary_provider'])),
            new FakeReader(patients: [42 => self::PATIENT], notes: [42 => [self::NOTE]]),
            new ContextBundleBuilder(new DateTimeZone('UTC')),
            $agent,
            new CopilotConfig('http://agent.test:8000', 'test-only-shared-secret-0123456789abcdef'),
            new CapturingLogger(),
            new AuditCapture(),
            pendingFacts: $repo,
            schema: new FakeSchemaStatus(true),
        );
        $result = $controller->handleForSession(['authUserID' => 1, 'authUser' => 'dr_smith', 'pid' => 42]);

        self::assertSame(200, $result['status']);
        $sent = $agent->posts[0]['bundle'];
        self::assertSame(['copilot_extracted_value:5'], array_column($sent['pending_document_facts'], 'fact_id'), 'held facts never reach the agent');
    }

    public function testFollowUpBundleWithoutTablesIsTheWeek1Bundle(): void
    {
        $agent = new FakeAgentClient();
        $controller = new BriefingTicketController(
            new CopilotAuthorizer(new FakeAcl(['patients/demo' => true, 'encounters/auth_a' => true, 'encounters/notes' => true, 'patients/med' => true, 'patients/lab' => true, 'patients/appt' => true]), new FakeRelationships(['1:42' => 'primary_provider'])),
            new FakeReader(patients: [42 => self::PATIENT], notes: [42 => [self::NOTE]]),
            new ContextBundleBuilder(new DateTimeZone('UTC')),
            $agent,
            new CopilotConfig('http://agent.test:8000', 'test-only-shared-secret-0123456789abcdef'),
            new CapturingLogger(),
            new AuditCapture(),
            pendingFacts: new FakeProcessingRepository(),
            schema: new FakeSchemaStatus(false),
        );
        $controller->handleForSession(['authUserID' => 1, 'authUser' => 'dr_smith', 'pid' => 42]);
        self::assertArrayNotHasKey('pending_document_facts', $agent->posts[0]['bundle']);
    }
}
