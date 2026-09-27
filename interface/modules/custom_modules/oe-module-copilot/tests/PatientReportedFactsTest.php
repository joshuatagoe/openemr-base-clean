<?php

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Tests;

use DateTimeZone;
use OpenEMR\Modules\Copilot\Authorization\CopilotAuthorizer;
use OpenEMR\Modules\Copilot\Config\CopilotConfig;
use OpenEMR\Modules\Copilot\ContextBundleBuilder;
use OpenEMR\Modules\Copilot\Controller\BriefingTicketController;
use OpenEMR\Modules\Copilot\Documents\CandidateMapper;
use PHPUnit\Framework\TestCase;

/**
 * Intake answers for follow-ups: waiting intake items reach the follow-up bundle as pending facts of
 * kind `patient_reported` (contract C5), bounded apart from lab values, newest intake form first,
 * with the form's upload date. Only intake item labels are ever sent - never a demographic field.
 */
final class PatientReportedFactsTest extends TestCase
{
    private const PATIENT = ['pid' => 42, 'uuid' => '3b9d2c1e-8f7a-4b6c-9d0e-1f2a3b4c5d6e'];
    private const NOTE = ['form_soap_id' => 1001, 'encounter' => 501, 'note_date' => '2026-06-10 09:30:00', 'plan' => 'Repeat HbA1c.'];

    /** @param list<array{0:string, 1:?string}> $items label, value */
    private static function document(FakeProcessingRepository $repo, int $documentId, string $docType, array $items, int &$nextId, ?string $received = null): void
    {
        $repo->records[$documentId] = ['document_id' => $documentId, 'pid' => 42, 'content_sha256' => str_repeat('a', 64), 'doc_type' => $docType, 'status' => 'extracted', 'prompt_version' => 'v', 'attempts' => 1, 'last_error_code' => null, 'identity_check' => 'match', 'extraction_json' => '{}', 'created_at' => '', 'updated_at' => '', 'received_at' => $received];
        foreach ($items as $index => [$label, $value]) {
            $repo->values[$documentId][] = [
                'id' => $nextId++, 'document_id' => $documentId, 'pid' => 42, 'status' => 'candidate', 'result_index' => $index,
                'test_name' => $label, 'value_text' => $value, 'unit' => $docType === 'lab_pdf' ? '%' : null, 'reference_range' => null,
                'abnormal_flag' => null, 'flag_source' => $docType === 'lab_pdf' ? 'unavailable' : null, 'collection_date' => $docType === 'lab_pdf' ? '2026-09-20' : null,
                'verification_status' => 'verified_exact', 'page' => 1, 'bbox' => null,
            ];
        }
    }

    /** @return array<string,mixed> */
    private static function sentBundle(FakeProcessingRepository $repo): array
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
            pendingFacts: $repo,
            schema: new FakeSchemaStatus(true),
        );
        $controller->handleForSession(['authUserID' => 1, 'authUser' => 'dr_smith', 'pid' => 42]);
        return $agent->posts[0]['bundle'];
    }

    public function testWaitingIntakeItemsArePatientReportedFactsBesideLabValues(): void
    {
        $repo = new FakeProcessingRepository();
        $id = 1;
        self::document($repo, 920, 'lab_pdf', [['Hemoglobin A1c', '9.1']], $id);
        self::document($repo, 930, 'intake_form', [[CandidateMapper::LABEL_MEDICATION, 'Metformin 1000 mg twice daily'], [CandidateMapper::LABEL_ALLERGY, 'Penicillin; reaction: rash']], $id, '2026-09-25 09:00:00');

        $facts = self::sentBundle($repo)['pending_document_facts'];
        self::assertSame(['copilot_extracted_value:1', 'copilot_extracted_value:2', 'copilot_extracted_value:3'], array_column($facts, 'fact_id'));
        self::assertArrayNotHasKey('kind', $facts[0], 'a lab value is sent as before');
        $med = $facts[1];
        self::assertSame('patient_reported', $med['kind']);
        self::assertSame(CandidateMapper::LABEL_MEDICATION, $med['test_name']);
        self::assertSame('Metformin 1000 mg twice daily', $med['value_text']);
        self::assertNull($med['unit']);
        self::assertNull($med['reference_range']);
        self::assertNull($med['abnormal_flag']);
        self::assertSame('unavailable', $med['flag_source']);
        self::assertNull($med['collection_date']);
        self::assertSame('2026-09-25', $med['received_at'], 'an intake form is dated by its upload date');
    }

    public function testOnlyIntakeItemLabelsAreEverSent(): void
    {
        $repo = new FakeProcessingRepository();
        $id = 1;
        self::document($repo, 930, 'intake_form', [['Patient name', 'Whitfield, Evelyn R.'], ['Date of birth', '1981-03-14'], [CandidateMapper::LABEL_CHIEF_CONCERN, 'Headaches for two weeks']], $id);
        $facts = self::sentBundle($repo)['pending_document_facts'];
        self::assertSame([CandidateMapper::LABEL_CHIEF_CONCERN], array_column($facts, 'test_name'));
        $text = json_encode($facts, JSON_THROW_ON_ERROR);
        self::assertStringNotContainsString('Whitfield', $text);
        self::assertStringNotContainsString('1981', $text);
    }

    public function testPatientReportedItemsAreBoundedApartAndNewestFormFirst(): void
    {
        $repo = new FakeProcessingRepository();
        $id = 1;
        $many = array_fill(0, BriefingTicketController::PATIENT_REPORTED_LIMIT + 5, [CandidateMapper::LABEL_MEDICATION, 'Aspirin 81 mg']);
        self::document($repo, 930, 'intake_form', $many, $id);
        self::document($repo, 940, 'intake_form', [[CandidateMapper::LABEL_CHIEF_CONCERN, 'Cough']], $id);
        self::document($repo, 920, 'lab_pdf', [['Hemoglobin A1c', '9.1']], $id);

        $facts = self::sentBundle($repo)['pending_document_facts'];
        $reported = array_values(array_filter($facts, static fn(array $f): bool => ($f['kind'] ?? null) === 'patient_reported'));
        self::assertCount(BriefingTicketController::PATIENT_REPORTED_LIMIT, $reported);
        self::assertSame(940, $reported[0]['document_id'], 'the newest intake form first');
        self::assertCount(1, array_filter($facts, static fn(array $f): bool => !isset($f['kind'])), 'lab values keep their own bound');
        self::assertLessThanOrEqual(500, count($facts));
    }

    public function testTheSqlReadsIntakeCandidatesNewestFirstAndBounded(): void
    {
        $source = (string) file_get_contents(__DIR__ . '/../src/Documents/SqlProcessingRepository.php');
        $start = strpos($source, 'public function listPatientReportedFacts');
        self::assertIsInt($start);
        $body = substr($source, $start, 2500);
        self::assertStringContainsString("d.doc_type = 'intake_form'", $body);
        self::assertStringContainsString("v.status = 'candidate'", $body);
        self::assertStringContainsString('ORDER BY v.document_id DESC, v.result_index ASC', $body);
        self::assertStringContainsString('od.date AS received_at', $body);
        self::assertStringContainsString('min($limit, 100)', $body);
    }
}
