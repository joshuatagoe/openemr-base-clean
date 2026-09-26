<?php

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Tests;

use DateTimeZone;
use OpenEMR\Modules\Copilot\Authorization\CopilotAuthorizer;
use OpenEMR\Modules\Copilot\ContextBundleBuilder;
use OpenEMR\Modules\Copilot\Controller\DocumentBriefingController;
use OpenEMR\Modules\Copilot\Data\SourceUnavailableException;
use PHPUnit\Framework\TestCase;

/**
 * ADR-010 medication conflicts, module side: when an intake form is briefed, the
 * chart's current medications go to the agent as `chart_medications`, in the Week 1
 * bundle's medication shape, bounded, read-only. Lab-only briefings do not read them.
 */
final class MedicationBriefingTest extends TestCase
{
    private const PID = 42;
    private const USER = 1;
    private const PUUID = '3b9d2c1e-8f7a-4b6c-9d0e-1f2a3b4c5d6e';
    private const FULL_ACL = ['patients/demo' => true, 'encounters/auth_a' => true, 'encounters/notes' => true, 'patients/med' => true, 'patients/lab' => true, 'patients/appt' => true];

    /** The agent's MedicationRecord fields (test_intake_briefing.py pins the same list). */
    private const MEDICATION_KEYS = [
        'record_id', 'source_table', 'drug_name', 'rxnorm_code', 'dosage_text', 'active', 'status_field',
        'status_value', 'started_at', 'ended_at', 'modified_at', 'timestamp', 'timestamp_field',
    ];

    private CapturingLogger $logger;
    private AuditCapture $audit;

    protected function setUp(): void
    {
        $this->logger = new CapturingLogger();
        $this->audit = new AuditCapture();
    }

    /** @return array<string,mixed> */
    private static function record(int $id, string $docType, string $json): array
    {
        return ['document_id' => $id, 'pid' => self::PID, 'content_sha256' => hash('sha256', (string) $id), 'doc_type' => $docType, 'status' => 'extracted', 'prompt_version' => 'v', 'attempts' => 1, 'last_error_code' => null, 'identity_check' => 'match', 'extraction_json' => $json, 'created_at' => '', 'updated_at' => ''];
    }

    /** @return array<string,mixed> */
    private static function med(int $id, string $drug, int $active = 1, string $source = 'prescriptions', string $enddate = ''): array
    {
        return [
            'source_table' => $source, 'id' => $id, 'drug' => $drug, 'rxnorm' => '',
            'dosage' => $source === 'prescriptions' ? '1 tab twice daily' : '', 'active' => $active,
            'begdate' => '2026-01-10', 'enddate' => $enddate, 'date_added' => '2026-01-10 09:00:00', 'date_modified' => '',
        ];
    }

    private function controller(FakeProcessingRepository $repo, FakeAgentClient $agent, FakeReader $reader): DocumentBriefingController
    {
        return new DocumentBriefingController(
            new CopilotAuthorizer(new FakeAcl(self::FULL_ACL), new FakeRelationships([self::USER . ':' . self::PID => 'primary_provider'])),
            $reader,
            new FakeDocumentReader([]),
            $agent,
            $this->logger,
            $this->audit,
            records: $repo,
            schema: new FakeSchemaStatus(true),
            builder: new ContextBundleBuilder(new DateTimeZone('UTC')),
        );
    }

    private static function reader(): FakeReader
    {
        return new FakeReader(patients: [self::PID => ['pid' => self::PID, 'uuid' => self::PUUID]], labs: [self::PID => []]);
    }

    private static function intakeRepo(): FakeProcessingRepository
    {
        $repo = new FakeProcessingRepository();
        $repo->records[5] = self::record(5, 'lab_pdf', '{"document_id":5,"results":[]}');
        $repo->waiting(5);
        $repo->records[8] = self::record(8, 'intake_form', '{"document_id":8,"doc_type":"intake_form","current_medications":[{"name":"Zolpidemix"}]}');
        $repo->waiting(5, 8);
        return $repo;
    }

    /** @return array<string,mixed> */
    private function brief(FakeProcessingRepository $repo, FakeReader $reader): array
    {
        $agent = new FakeAgentClient();
        $result = $this->controller($repo, $agent, $reader)->handleForSession(['authUserID' => self::USER, 'authUser' => 'dr_smith', 'pid' => self::PID], self::PID);
        self::assertSame(200, $result['status']);
        return $agent->documentPosts[0]['request'];
    }

    public function testAnIntakeBriefingSendsTheCurrentChartMedicationsInTheBundleShape(): void
    {
        $reader = self::reader();
        $reader->medications = [
            self::med(11, 'Metformin 500 mg'),
            self::med(12, 'Lisinopril 10 mg', 1, 'lists'),
            self::med(13, 'Warfarin 5 mg', 0),                                     // inactive: not on the current list
            self::med(14, 'Metformin 500 mg'),                                     // exact duplicate of 11: collapsed like the bundle
            self::med(15, 'Atorvastatin 20 mg', 1, 'prescriptions', '2020-01-01'), // flag active, end passed: indeterminate, still sent
        ];

        $sent = $this->brief(self::intakeRepo(), $reader);

        self::assertSame(['prescriptions:11', 'lists:12', 'prescriptions:15'], array_column($sent['chart_medications'], 'record_id'));
        foreach ($sent['chart_medications'] as $m) {
            self::assertSame(self::MEDICATION_KEYS, array_keys($m));
        }
        self::assertTrue($sent['chart_medications'][0]['active']);
        self::assertNull($sent['chart_medications'][2]['active']);
        self::assertSame([DocumentBriefingController::MEDICATION_READ_LIMIT], $reader->medicationLimits);
        self::assertArrayNotHasKey('pid', $sent);
        $everything = implode("\n", array_column($this->audit->events, 'comment')) . $this->logger->dump();
        foreach (['Metformin', 'Lisinopril', 'Atorvastatin', 'Zolpidemix'] as $name) {
            self::assertStringNotContainsString($name, $everything);
        }
    }

    public function testChartMedicationsAreBoundedToTheNewestEntries(): void
    {
        $reader = self::reader();
        $max = DocumentBriefingController::MAX_CHART_MEDICATIONS;
        $rows = [];
        for ($i = 1; $i <= $max + 25; $i++) {
            $rows[] = self::med($i, "Drug{$i}");
        }
        $reader->medications = $rows;

        $sent = $this->brief(self::intakeRepo(), $reader);

        self::assertCount($max, $sent['chart_medications']);
        self::assertSame('prescriptions:26', $sent['chart_medications'][0]['record_id'], 'the oldest entries are the ones left out');
        self::assertSame('prescriptions:' . ($max + 25), $sent['chart_medications'][$max - 1]['record_id']);
    }

    public function testAnUnreadableMedicationSourceSendsNullSoTheAgentSaysNothingWasCompared(): void
    {
        $reader = self::reader();
        $reader->medications = new SourceUnavailableException('medications');

        $sent = $this->brief(self::intakeRepo(), $reader);

        self::assertArrayHasKey('chart_medications', $sent);
        self::assertNull($sent['chart_medications']);
        self::assertStringContainsString('medications', $this->logger->dump());
    }

    public function testALabOnlyBriefingDoesNotReadOrSendMedications(): void
    {
        $repo = new FakeProcessingRepository();
        $repo->records[5] = self::record(5, 'lab_pdf', '{"document_id":5,"results":[]}');
        $repo->waiting(5);
        $reader = self::reader();
        $reader->medications = [self::med(11, 'Metformin 500 mg')];

        $sent = $this->brief($repo, $reader);

        self::assertArrayNotHasKey('chart_medications', $sent);
        self::assertSame([], $reader->medicationLimits);
    }
}
