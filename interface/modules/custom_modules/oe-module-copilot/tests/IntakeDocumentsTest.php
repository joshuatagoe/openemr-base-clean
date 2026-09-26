<?php

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Tests;

use DateTimeZone;
use OpenEMR\Modules\Copilot\Authorization\CopilotAuthorizer;
use OpenEMR\Modules\Copilot\ContextBundleBuilder;
use OpenEMR\Modules\Copilot\Controller\DocumentBriefingController;
use OpenEMR\Modules\Copilot\Documents\CandidateMapper;
use OpenEMR\Modules\Copilot\Documents\DocumentProcessor;
use PHPUnit\Framework\TestCase;

/**
 * Intake forms (ADR-010): stored like labs, shown as patient-reported candidate
 * rows that are never fileable, sent to the briefing; the written name and DOB
 * are compared in PHP and never stored (ADR-012).
 */
final class IntakeDocumentsTest extends TestCase
{
    private const PID = 42;
    private const USER = 1;
    private const PUUID = '3b9d2c1e-8f7a-4b6c-9d0e-1f2a3b4c5d6e';
    private const CID = '7f0e6b2a-3c4d-4e5f-9a1b-2c3d4e5f6a7b';
    private const CHART = ['lname' => 'Demo', 'dob' => '1958-04-12'];
    private const FULL_ACL = ['patients/demo' => true, 'encounters/auth_a' => true, 'encounters/notes' => true, 'patients/med' => true, 'patients/lab' => true, 'patients/appt' => true];

    /** @return array<string,mixed> */
    private static function cite(int $documentId, string $field, string $quote, ?int $page = 1, ?array $bbox = [0.1, 0.2, 0.5, 0.23]): array
    {
        return ['source_type' => 'document', 'source_id' => (string) $documentId, 'page_or_section' => 'p. 1', 'field_or_chunk_id' => $field, 'quote_or_value' => $quote, 'page' => $page, 'bbox' => $bbox];
    }

    /** @return array<string,mixed> */
    private static function field(int $documentId, string $field, ?string $value, string $status = 'verified_exact'): array
    {
        return ['value' => $value, 'verification_status' => $status, 'citation' => self::cite($documentId, $field, $value ?? '[illegible]')];
    }

    /** @return array<string,mixed> */
    private static function intakeExtraction(int $documentId): array
    {
        return [
            'document_id' => $documentId,
            'doc_type' => 'intake_form',
            'demographics' => [
                'name' => self::field($documentId, 'demographics.name', 'Demo, Evelyn'),
                'date_of_birth' => self::field($documentId, 'demographics.date_of_birth', '04/12/1958'),
                'sex' => self::field($documentId, 'demographics.sex', 'F'),
                'phone' => null,
            ],
            'chief_concern' => self::field($documentId, 'chief_concern', 'Tired and thirsty'),
            'current_medications' => [
                ['name' => 'Metformin', 'dose' => '500 mg', 'frequency' => 'twice daily', 'verification_status' => 'verified_exact', 'citation' => self::cite($documentId, 'current_medications[0]', 'Metformin 500 mg twice daily')],
                ['name' => null, 'dose' => '10 mg', 'frequency' => null, 'verification_status' => 'unreadable', 'citation' => self::cite($documentId, 'current_medications[1]', 'L#### 10 mg', null, null)],
            ],
            'medications_none_stated' => null,
            'allergies' => [
                ['substance' => 'Penicillin', 'reaction' => 'hives', 'verification_status' => 'model_says_verified', 'citation' => self::cite($documentId, 'allergies[0]', 'Penicillin - hives', 1, null)],
            ],
            'allergies_none_stated' => null,
            'family_history' => [
                ['relation' => 'Mother', 'condition' => 'type 2 diabetes', 'verification_status' => 'verified_fuzzy', 'citation' => self::cite($documentId, 'family_history[0]', 'Mother: type 2 diabetes')],
            ],
            'family_history_none_stated' => self::field($documentId, 'family_history_none_stated', 'None', 'unverified'),
            'extraction_metadata' => ['model_id' => 'm', 'prompt_version' => 'intake-v1', 'extracted_at' => '2026-09-25T10:00:00Z', 'page_count' => 1, 'verified_fraction' => 0.5, 'unreadable_count' => 1, 'unverified_count' => 2],
            'printed_identity' => null,
        ];
    }

    public function testIntakeItemsBecomeLabelledCandidateRowsInTheAgentsOrder(): void
    {
        $rows = CandidateMapper::fromIntake(self::intakeExtraction(9));
        self::assertNotNull($rows);
        self::assertSame(
            [
                [0, 'Chief concern', 'Tired and thirsty', 'verified_exact'],
                [1, 'Current medication', 'Metformin 500 mg twice daily', 'verified_exact'],
                [2, 'Current medication', '10 mg', 'unreadable'],
                [3, 'Allergy', 'Penicillin; reaction: hives', 'unverified'],
                [4, 'Family history', 'Mother: type 2 diabetes', 'verified_fuzzy'],
                [5, 'Family history (none reported)', 'None', 'unverified'],
            ],
            array_map(static fn(array $r): array => [$r['result_index'], $r['test_name'], $r['value_text'], $r['verification_status']], $rows)
        );
        self::assertSame(1, $rows[1]['page']);
        self::assertSame('0.1,0.2,0.5,0.23', $rows[1]['bbox']);
        self::assertNull($rows[2]['bbox'], 'an unreadable entry has no box');
        self::assertNull($rows[3]['bbox'], 'an unknown status is never promoted, and carries no box');
        foreach ($rows as $r) {
            self::assertNull($r['unit']);
            self::assertNull($r['reference_range']);
            self::assertNull($r['abnormal_flag']);
            self::assertNull($r['flag_source']);
            self::assertNull($r['collection_date']);
        }
        self::assertNull(CandidateMapper::fromIntake(['current_medications' => 'nope']));
    }

    public function testAnIntakeFormIsStoredWithoutTheWrittenNameAndDobAndItsItemsAsCandidates(): void
    {
        $docs = new FakePatientDocuments([self::PID => [FakePatientDocuments::doc(8, ['Intake Form'], 'image/jpeg')]], [8 => 'jpeg-bytes']);
        $repo = new FakeProcessingRepository();
        $agent = new FakeAgentClient();
        $agent->extractions[8] = [
            'status' => 'ok', 'degraded_reason' => null, 'prompt_version' => 'intake-v1', 'extraction_model' => 'm',
            'extraction' => self::intakeExtraction(8),
            'printed_identity' => ['name' => 'Demo, Evelyn', 'dob' => '1958-04-12'],
        ];
        $logger = new CapturingLogger();
        $processor = new DocumentProcessor($docs, $repo, $agent, $logger, static fn(): int => strtotime('2026-09-25 10:05:00'));

        $result = $processor->process(self::PID, self::PUUID, 'dr_smith', self::CHART, self::CID);

        self::assertSame([['document_id' => 8, 'outcome' => 'extracted']], $result['processed']);
        self::assertSame('intake_form', $agent->extractionPosts[0]['request']['doc_type']);
        $record = $repo->records[8];
        self::assertSame('match', $record['identity_check']);
        self::assertSame('intake-v1', $record['prompt_version']);
        $json = (string) $record['extraction_json'];
        self::assertStringNotContainsString('Evelyn', $json, 'the written name is never stored');
        self::assertStringNotContainsString('1958', $json, 'the written DOB is never stored');
        $stored = json_decode($json, true);
        self::assertIsArray($stored);
        self::assertNull($stored['demographics']['name']);
        self::assertNull($stored['demographics']['date_of_birth']);
        self::assertSame('F', $stored['demographics']['sex']['value']);
        self::assertCount(6, $repo->values[8]);
        self::assertSame('Current medication', $repo->values[8][1]['test_name']);
        self::assertStringNotContainsString('Metformin', implode("\n", array_map('json_encode', $logger->records)));
    }

    public function testAnIntakeFormCitingAnotherDocumentIsNeverStored(): void
    {
        $docs = new FakePatientDocuments([self::PID => [FakePatientDocuments::doc(8, ['Intake Form'])]], [8 => 'pdf']);
        $repo = new FakeProcessingRepository();
        $agent = new FakeAgentClient();
        $extraction = self::intakeExtraction(8);
        $extraction['allergies'][0]['citation']['source_id'] = '99';
        $agent->extractions[8] = ['status' => 'ok', 'prompt_version' => 'intake-v1', 'extraction' => $extraction, 'printed_identity' => null];

        $processor = new DocumentProcessor($docs, $repo, $agent, new CapturingLogger(), static fn(): int => strtotime('2026-09-25 10:05:00'));
        $result = $processor->process(self::PID, self::PUUID, 'dr_smith', self::CHART, self::CID);

        self::assertSame(DocumentProcessor::CODE_BAD_EXTRACTION, $result['processed'][0]['outcome']);
        self::assertArrayNotHasKey(8, $repo->values);
    }

    public function testTheBriefingSendsIntakeDocumentsWithTheirType(): void
    {
        $repo = new FakeProcessingRepository();
        $lab = ['document_id' => 5, 'pid' => self::PID, 'content_sha256' => 'a', 'doc_type' => 'lab_pdf', 'status' => 'extracted', 'prompt_version' => 'v', 'attempts' => 1, 'last_error_code' => null, 'identity_check' => 'match', 'extraction_json' => '{"document_id":5,"results":[]}', 'created_at' => '', 'updated_at' => ''];
        $repo->records[5] = $lab;
        $repo->records[8] = ['document_id' => 8, 'doc_type' => 'intake_form', 'extraction_json' => json_encode(self::intakeExtraction(8))] + $lab;
        $repo->waiting(5, 8);
        $agent = new FakeAgentClient();
        $controller = new DocumentBriefingController(
            new CopilotAuthorizer(new FakeAcl(self::FULL_ACL), new FakeRelationships([self::USER . ':' . self::PID => 'primary_provider'])),
            new FakeReader(patients: [self::PID => ['pid' => self::PID, 'uuid' => self::PUUID]], labs: [self::PID => []]),
            new FakeDocumentReader([]),
            $agent,
            new CapturingLogger(),
            new AuditCapture(),
            records: $repo,
            schema: new FakeSchemaStatus(true),
            builder: new ContextBundleBuilder(new DateTimeZone('UTC')),
        );

        $result = $controller->handleForSession(['authUserID' => self::USER, 'authUser' => 'dr_smith', 'pid' => self::PID], self::PID);

        self::assertSame(200, $result['status']);
        $documents = $agent->documentPosts[0]['request']['documents'];
        self::assertSame([[5, 'lab_pdf'], [8, 'intake_form']], array_map(static fn(array $d): array => [$d['document_id'], $d['doc_type']], $documents));
        self::assertSame('Metformin', $documents[1]['extraction']['current_medications'][0]['name']);
    }

    public function testIntakeItemsAreNotPendingLabFacts(): void
    {
        $repo = new FakeProcessingRepository();
        $repo->records[8] = ['document_id' => 8, 'pid' => self::PID, 'doc_type' => 'intake_form', 'status' => 'extracted'];
        $repo->values[8] = [['id' => 1, 'pid' => self::PID, 'status' => 'candidate', 'result_index' => 0, 'test_name' => 'Current medication', 'value_text' => 'Metformin', 'unit' => null, 'reference_range' => null, 'abnormal_flag' => null, 'flag_source' => null, 'collection_date' => null, 'verification_status' => 'verified_exact', 'page' => 1, 'bbox' => null]];
        self::assertSame([], $repo->listPendingFacts(self::PID, 10));
    }
}
