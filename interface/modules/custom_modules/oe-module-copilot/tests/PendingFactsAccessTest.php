<?php

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Tests;

use DateTimeZone;
use OpenEMR\Modules\Copilot\Authorization\CopilotAuthorizer;
use OpenEMR\Modules\Copilot\Config\CopilotConfig;
use OpenEMR\Modules\Copilot\ContextBundleBuilder;
use OpenEMR\Modules\Copilot\Controller\BriefingTicketController;
use OpenEMR\Modules\Copilot\Data\SourceUnavailableException;
use PHPUnit\Framework\TestCase;

/**
 * Pending document facts in the ticket bundle follow core `can_access()` like
 * the briefing reader: a value from a document the user may not open never
 * reaches the agent.
 */
final class PendingFactsAccessTest extends TestCase
{
    private const PATIENT = ['pid' => 42, 'uuid' => '3b9d2c1e-8f7a-4b6c-9d0e-1f2a3b4c5d6e', 'fname' => 'Test', 'lname' => 'Patient', 'DOB' => '1960-01-01', 'sex' => 'Female'];
    private const NOTE = ['form_soap_id' => 1, 'encounter' => 7, 'note_date' => '2026-09-01 09:00:00', 'plan' => 'Recheck A1c.'];

    private static function fact(int $id): array
    {
        return ['id' => $id, 'test_name' => 'Glucose', 'value_text' => '142', 'unit' => 'mg/dL', 'reference_range' => '70-99', 'abnormal_flag' => 'H', 'flag_source' => 'extracted', 'collection_date' => '2026-09-01', 'verification_status' => 'verified_exact', 'page' => 1, 'bbox' => null];
    }

    private function repo(): FakeProcessingRepository
    {
        $repo = new FakeProcessingRepository();
        foreach ([913 => 5, 914 => 6] as $doc => $id) {
            $repo->records[$doc] = ['document_id' => $doc, 'pid' => 42, 'content_sha256' => str_repeat('a', 64), 'doc_type' => 'lab_pdf', 'status' => 'extracted', 'prompt_version' => 'v', 'attempts' => 1, 'last_error_code' => null, 'identity_check' => 'match', 'extraction_json' => '{}', 'created_at' => '', 'updated_at' => ''];
            $repo->values[$doc] = [['id' => $id, 'document_id' => $doc, 'pid' => 42, 'status' => 'candidate', 'result_index' => 0] + self::fact($id)];
        }
        return $repo;
    }

    private function bundleFor(FakeFileSource $access): array
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
            pendingFacts: $this->repo(),
            schema: new FakeSchemaStatus(true),
            documentAccess: $access,
        );
        $controller->handleForSession(['authUserID' => 1, 'authUser' => 'dr_smith', 'pid' => 42]);
        return $agent->posts[0]['bundle'];
    }

    public function testFactsFromAnInaccessibleDocumentAreNotSent(): void
    {
        $access = new FakeFileSource(denied: [914]);
        $bundle = $this->bundleFor($access);
        self::assertSame(['copilot_extracted_value:5'], array_column($bundle['pending_document_facts'], 'fact_id'));
        self::assertEqualsCanonicalizing([913, 914], $access->accessChecked, 'one check per document');
    }

    public function testAnAccessCheckFailureSendsNoPendingFacts(): void
    {
        $access = new FakeFileSource();
        $access->accessFailure = new SourceUnavailableException('documents');
        $bundle = $this->bundleFor($access);
        self::assertSame([], $bundle['pending_document_facts'] ?? []);
    }
}
