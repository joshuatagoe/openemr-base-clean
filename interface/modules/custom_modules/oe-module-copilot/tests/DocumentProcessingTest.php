<?php

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Tests;

use OpenEMR\Modules\Copilot\Agent\AgentUnavailableException;
use OpenEMR\Modules\Copilot\Authorization\CopilotAuthorizer;
use OpenEMR\Modules\Copilot\Controller\DocumentsController;
use OpenEMR\Modules\Copilot\Data\DocumentTooLargeException;
use OpenEMR\Modules\Copilot\Data\SourceUnavailableException;
use OpenEMR\Modules\Copilot\Documents\CandidateMapper;
use OpenEMR\Modules\Copilot\Documents\DocType;
use OpenEMR\Modules\Copilot\Documents\DocumentProcessor;
use OpenEMR\Modules\Copilot\Documents\IdentityComparator;
use PHPUnit\Framework\TestCase;

/**
 * ADR-012 per-document processing on chart open, ADR-009 dedup layer (b), the
 * PHP identity comparison, and the documents routes.
 */
final class DocumentProcessingTest extends TestCase
{
    private const PID = 42;
    private const OTHER_PID = 77;
    private const USER = 1;
    private const PUUID = '3b9d2c1e-8f7a-4b6c-9d0e-1f2a3b4c5d6e';
    private const CID = '7f0e6b2a-3c4d-4e5f-9a1b-2c3d4e5f6a7b';
    private const CHART = ['lname' => 'Demo', 'dob' => '1958-04-12'];
    private const FULL_ACL = ['patients/demo' => true, 'encounters/auth_a' => true, 'encounters/notes' => true, 'patients/med' => true, 'patients/lab' => true, 'patients/appt' => true];

    private CapturingLogger $logger;
    private AuditCapture $audit;

    protected function setUp(): void
    {
        $this->logger = new CapturingLogger();
        $this->audit = new AuditCapture();
    }

    /** @return array<string,mixed> */
    private static function labExtraction(int $documentId): array
    {
        return [
            'document_id' => $documentId,
            'doc_type' => 'lab_pdf',
            'collection_date' => '2026-09-10',
            'ordering_provider' => null,
            'printed_identity' => ['name' => 'DEMO, EVELYN', 'dob' => '1958-04-12'],
            'results' => [
                [
                    'test_name' => 'Hemoglobin A1c', 'value' => '7.9', 'unit' => '%', 'reference_range' => '4.0-5.6',
                    'collection_date' => null, 'abnormal_flag' => 'H', 'abnormal_flag_source' => 'extracted',
                    'verification_status' => 'verified_exact', 'loinc_code' => null,
                    'citation' => ['source_type' => 'document', 'source_id' => (string) $documentId, 'page_or_section' => 'p. 1', 'field_or_chunk_id' => 'results[0]', 'quote_or_value' => 'Hemoglobin A1c 7.9 %', 'page' => 1, 'bbox' => [0.1, 0.2, 0.55, 0.23]],
                ],
                [
                    'test_name' => 'Glucose', 'value' => null, 'unit' => null, 'reference_range' => '70-99',
                    'collection_date' => '2026-09-11', 'abnormal_flag' => null, 'abnormal_flag_source' => 'unavailable',
                    'verification_status' => 'unreadable', 'loinc_code' => null,
                    'citation' => ['source_type' => 'document', 'source_id' => (string) $documentId, 'page_or_section' => 'p. 2', 'field_or_chunk_id' => 'results[1]', 'quote_or_value' => 'Glucose', 'page' => 2, 'bbox' => null],
                ],
            ],
            'extraction_metadata' => ['model_id' => 'm', 'prompt_version' => 'lab-v3', 'extracted_at' => '2026-09-25T10:00:00Z', 'page_count' => 2, 'verified_fraction' => 0.5, 'unreadable_count' => 1, 'unverified_count' => 0],
        ];
    }

    /**
     * @param array<string,mixed> $overrides
     * @return array<string,mixed>
     */
    private static function ok(int $documentId, array $overrides = []): array
    {
        return $overrides + [
            'status' => 'ok',
            'degraded_reason' => null,
            'prompt_version' => 'lab-v3',
            'extraction_model' => 'fake',
            'extraction' => self::labExtraction($documentId),
            'printed_identity' => ['name' => 'Evelyn Demo', 'dob' => '1958-04-12'],
        ];
    }

    private function processor(FakePatientDocuments $docs, FakeProcessingRepository $repo, FakeAgentClient $agent): DocumentProcessor
    {
        return new DocumentProcessor($docs, $repo, $agent, $this->logger, static fn(): int => strtotime('2026-09-25 10:05:00'));
    }

    // ------------------------------------------------------------------ //
    // Pure pieces
    // ------------------------------------------------------------------ //

    public function testDocTypeComesFromTheCategoryNameNotItsId(): void
    {
        self::assertSame(DocType::LAB_PDF, DocType::fromCategoryNames(['Lab Report']));
        self::assertSame(DocType::LAB_PDF, DocType::fromCategoryNames([' lab report ']));
        self::assertSame(DocType::INTAKE_FORM, DocType::fromCategoryNames(['Intake Form']));
        self::assertSame(DocType::UNSUPPORTED, DocType::fromCategoryNames([]));
        self::assertSame(DocType::UNSUPPORTED, DocType::fromCategoryNames(['Medical Record']));
        self::assertSame(DocType::UNSUPPORTED, DocType::fromCategoryNames(['Lab Report', 'Intake Form']), 'ambiguous is never guessed');
    }

    public function testIdentityRule(): void
    {
        self::assertSame('match', IdentityComparator::compare('DEMO, EVELYN', '1958-04-12', 'Demo', '1958-04-12'));
        self::assertSame('match', IdentityComparator::compare('Dr. Evelyn A. Démo', '1958-04-12', 'Demo', '1958-04-12'));
        self::assertSame('match', IdentityComparator::compare('Ann SMITH JONES', '1970-01-02', 'Smith-Jones', '1970-01-02'));
        self::assertSame('match', IdentityComparator::compare("Pat O'BRIEN", '1970-01-02', 'OBrien', '1970-01-02'));
        self::assertSame('mismatch', IdentityComparator::compare('Evelyn Demos', '1958-04-12', 'Demo', '1958-04-12'));
        self::assertSame('mismatch', IdentityComparator::compare('Evelyn Demo', '1958-04-13', 'Demo', '1958-04-12'));
        self::assertSame('mismatch', IdentityComparator::compare(null, '1958-04-13', 'Demo', '1958-04-12'), 'a disagreement wins over an absence');
        self::assertSame('missing', IdentityComparator::compare(null, '1958-04-12', 'Demo', '1958-04-12'));
        self::assertSame('missing', IdentityComparator::compare('Evelyn Demo', null, 'Demo', '1958-04-12'));
        self::assertSame('missing', IdentityComparator::compare('Evelyn Demo', '1958-04-12', 'Demo', '0000-00-00'));
        self::assertSame('missing', IdentityComparator::compare('  ', 'not a date', 'Demo', '1958-04-12'));
    }

    public function testCandidatesKeepTheirResultIndexAndClosedVocabularies(): void
    {
        $extraction = self::labExtraction(5);
        $extraction['results'][] = ['test_name' => '', 'value' => '1'];
        $extraction['results'][] = ['test_name' => 'Sodium', 'value' => 140, 'unit' => 'mmol/L', 'abnormal_flag' => 'high', 'abnormal_flag_source' => 'guessed', 'verification_status' => 'verified_by_model', 'citation' => ['page' => 0, 'bbox' => [0.5, 0.5, 0.4, 0.6]]];
        $rows = CandidateMapper::fromExtraction($extraction);
        self::assertNotNull($rows);
        self::assertSame([0, 1, 3], array_column($rows, 'result_index'));
        self::assertSame(['result_index' => 0, 'test_name' => 'Hemoglobin A1c', 'value_text' => '7.9', 'unit' => '%', 'reference_range' => '4.0-5.6', 'abnormal_flag' => 'H', 'flag_source' => 'extracted', 'collection_date' => '2026-09-10', 'verification_status' => 'verified_exact', 'page' => 1, 'bbox' => '0.1,0.2,0.55,0.23'], $rows[0]);
        self::assertNull($rows[1]['value_text']);
        self::assertSame('2026-09-11', $rows[1]['collection_date'], 'a per-result date wins');
        self::assertSame('unreadable', $rows[1]['verification_status']);
        self::assertSame('140', $rows[2]['value_text']);
        self::assertNull($rows[2]['abnormal_flag']);
        self::assertNull($rows[2]['flag_source']);
        self::assertSame('unverified', $rows[2]['verification_status'], 'unknown statuses are never promoted');
        self::assertNull($rows[2]['page']);
        self::assertNull($rows[2]['bbox'], 'an invalid box is dropped, never clamped');
        self::assertNull(CandidateMapper::fromExtraction(['results' => 'nope']));
        self::assertSame([0.1, 0.2, 0.55, 0.23], CandidateMapper::bboxToList('0.1,0.2,0.55,0.23'));
    }

    // ------------------------------------------------------------------ //
    // Processing
    // ------------------------------------------------------------------ //

    public function testProcessesAtMostTwoDocumentsPerCallAndStoresExtractionAndCandidates(): void
    {
        $docs = new FakePatientDocuments([self::PID => [FakePatientDocuments::doc(3), FakePatientDocuments::doc(2), FakePatientDocuments::doc(1)]], [3 => 'pdf-three', 2 => 'pdf-two', 1 => 'pdf-one']);
        $repo = new FakeProcessingRepository();
        $agent = new FakeAgentClient();
        foreach ([1, 2, 3] as $id) {
            $agent->extractions[$id] = self::ok($id);
        }

        $result = $this->processor($docs, $repo, $agent)->process(self::PID, self::PUUID, 'dr_smith', self::CHART, self::CID);

        self::assertSame([['document_id' => 3, 'outcome' => 'extracted'], ['document_id' => 2, 'outcome' => 'extracted']], $result['processed']);
        self::assertSame(1, $result['remaining']);
        self::assertCount(2, $agent->extractionPosts);
        $sent = $agent->extractionPosts[0]['request'];
        self::assertSame(['correlation_id' => self::CID, 'patient_uuid' => self::PUUID, 'document_id' => 3, 'doc_type' => 'lab_pdf', 'media_type' => 'application/pdf', 'document_base64' => base64_encode('pdf-three')], $sent);
        self::assertArrayNotHasKey('pid', $sent);

        $record = $repo->records[3];
        self::assertSame('extracted', $record['status']);
        self::assertSame('match', $record['identity_check']);
        self::assertSame('lab-v3', $record['prompt_version']);
        self::assertSame(hash('sha256', 'pdf-three'), $record['content_sha256']);
        self::assertSame(1, $record['attempts']);
        $stored = json_decode((string) $record['extraction_json'], true);
        self::assertIsArray($stored);
        self::assertSame(self::labExtraction(3)['results'], $stored['results'], 'the full validated extraction is stored');
        self::assertArrayNotHasKey('printed_identity', $stored, 'the printed identity is never stored');
        self::assertCount(2, $repo->values[3]);

        // Next chart open: the third document; then nothing is left and nothing is re-extracted.
        $again = $this->processor($docs, $repo, $agent)->process(self::PID, self::PUUID, 'dr_smith', self::CHART, self::CID);
        self::assertSame([['document_id' => 1, 'outcome' => 'extracted']], $again['processed']);
        $last = $this->processor($docs, $repo, $agent)->process(self::PID, self::PUUID, 'dr_smith', self::CHART, self::CID);
        self::assertSame([], $last['processed']);
        self::assertCount(3, $agent->extractionPosts);
    }

    public function testIdentityMismatchIsHeldAndOnlyTheComparisonIsKept(): void
    {
        $docs = new FakePatientDocuments([self::PID => [FakePatientDocuments::doc(9)]], [9 => 'pdf']);
        $repo = new FakeProcessingRepository();
        $agent = new FakeAgentClient();
        $agent->extractions[9] = self::ok(9, ['printed_identity' => ['name' => 'Someone Else', 'dob' => '1958-04-12']]);

        $result = $this->processor($docs, $repo, $agent)->process(self::PID, self::PUUID, 'dr_smith', self::CHART, self::CID);

        self::assertSame('held_identity', $result['processed'][0]['outcome']);
        self::assertSame('held_identity', $repo->records[9]['status']);
        self::assertSame('mismatch', $repo->records[9]['identity_check']);
        self::assertSame([], $repo->listPendingFacts(self::PID, 50), 'held facts are not pending chart updates');
        self::assertStringNotContainsString('Someone Else', (string) $repo->records[9]['extraction_json']);
        self::assertStringNotContainsString('Someone', $this->logger->dump());
    }

    public function testMissingPrintedIdentityIsExtractedAsMissing(): void
    {
        $docs = new FakePatientDocuments([self::PID => [FakePatientDocuments::doc(9)]], [9 => 'pdf']);
        $repo = new FakeProcessingRepository();
        $agent = new FakeAgentClient();
        $extraction = self::labExtraction(9);
        unset($extraction['printed_identity']);
        $agent->extractions[9] = self::ok(9, ['printed_identity' => null, 'extraction' => $extraction]);

        $this->processor($docs, $repo, $agent)->process(self::PID, self::PUUID, 'dr_smith', self::CHART, self::CID);
        self::assertSame('extracted', $repo->records[9]['status']);
        self::assertSame('missing', $repo->records[9]['identity_check']);
    }

    public function testSamePatientReuploadIsSkippedAsDuplicateWithoutAnAgentCall(): void
    {
        $docs = new FakePatientDocuments([self::PID => [FakePatientDocuments::doc(11), FakePatientDocuments::doc(10)]], [10 => 'same-bytes', 11 => 'same-bytes']);
        $repo = new FakeProcessingRepository();
        $repo->records[10] = ['document_id' => 10, 'pid' => self::PID, 'content_sha256' => hash('sha256', 'same-bytes'), 'doc_type' => 'lab_pdf', 'status' => 'extracted', 'prompt_version' => 'lab-v3', 'attempts' => 1, 'last_error_code' => null, 'identity_check' => 'match', 'extraction_json' => '{}', 'created_at' => '2026-09-24 09:00:00', 'updated_at' => '2026-09-24 09:00:00'];
        $agent = new FakeAgentClient();

        $result = $this->processor($docs, $repo, $agent)->process(self::PID, self::PUUID, 'dr_smith', self::CHART, self::CID);

        self::assertSame([['document_id' => 11, 'outcome' => 'skipped_duplicate']], $result['processed']);
        self::assertSame('skipped_duplicate', $repo->records[11]['status']);
        self::assertSame(DocumentProcessor::CODE_DUPLICATE, $repo->records[11]['last_error_code']);
        self::assertSame([], $agent->extractionPosts);
    }

    public function testSameFileInAnotherPatientsChartIsHeldWithACodeAndNeverMerged(): void
    {
        $docs = new FakePatientDocuments([self::PID => [FakePatientDocuments::doc(21)]], [21 => 'shared-bytes']);
        $repo = new FakeProcessingRepository();
        $repo->records[20] = ['document_id' => 20, 'pid' => self::OTHER_PID, 'content_sha256' => hash('sha256', 'shared-bytes'), 'doc_type' => 'lab_pdf', 'status' => 'extracted', 'prompt_version' => 'lab-v3', 'attempts' => 1, 'last_error_code' => null, 'identity_check' => 'match', 'extraction_json' => '{}', 'created_at' => '2026-09-24 09:00:00', 'updated_at' => '2026-09-24 09:00:00'];
        $agent = new FakeAgentClient();
        $agent->extractions[21] = self::ok(21);

        $this->processor($docs, $repo, $agent)->process(self::PID, self::PUUID, 'dr_smith', self::CHART, self::CID);

        self::assertSame('held_identity', $repo->records[21]['status']);
        self::assertSame(DocumentProcessor::CODE_OTHER_CHART, $repo->records[21]['last_error_code']);
        self::assertSame('extracted', $repo->records[20]['status'], 'the other chart is untouched');
        self::assertSame([], $repo->listPendingFacts(self::PID, 50));
    }

    public function testOnlyLabAndIntakeCategoriesWithSupportedFilesAreProcessed(): void
    {
        $docs = new FakePatientDocuments([self::PID => [
            FakePatientDocuments::doc(31, ['Medical Record']),
            FakePatientDocuments::doc(32, ['Lab Report'], null),
            FakePatientDocuments::doc(33, ['Lab Report'], 'application/pdf', 11 * 1024 * 1024),
            FakePatientDocuments::doc(34, ['Intake Form'], 'image/jpeg'),
        ]], [34 => 'jpeg']);
        $repo = new FakeProcessingRepository();
        $agent = new FakeAgentClient();
        $agent->extractions[34] = ['status' => 'degraded', 'degraded_reason' => 'doc_type_not_supported_yet', 'extraction' => null];

        $processor = $this->processor($docs, $repo, $agent);
        $result = $processor->process(self::PID, self::PUUID, 'dr_smith', self::CHART, self::CID);

        self::assertSame([['document_id' => 34, 'outcome' => 'doc_type_not_supported_yet']], $result['processed']);
        self::assertSame('intake_form', $agent->extractionPosts[0]['request']['doc_type']);
        self::assertSame('unsupported', $repo->records[34]['status'], 'terminal: not retried on every chart open');
        self::assertSame([34], $docs->bytesRead);

        $list = $processor->listDocuments(self::PID, 'dr_smith');
        self::assertSame(
            [
                [31, 'unsupported', 'unsupported', DocumentProcessor::CODE_NEEDS_CATEGORY],
                [32, 'lab_pdf', 'unsupported', DocumentProcessor::CODE_UNSUPPORTED_MEDIA],
                [33, 'lab_pdf', 'unsupported', DocumentProcessor::CODE_TOO_LARGE],
                [34, 'intake_form', 'unsupported', 'doc_type_not_supported_yet'],
            ],
            array_map(static fn(array $d): array => [$d['document_id'], $d['doc_type'], $d['status'], $d['error_code']], $list)
        );
    }

    public function testAgentFailureIsAFixedCodeAndRetriedUpToTheLimit(): void
    {
        $docs = new FakePatientDocuments([self::PID => [FakePatientDocuments::doc(41)]], [41 => 'pdf']);
        $repo = new FakeProcessingRepository();
        $agent = new FakeAgentClient();
        $agent->extractions[41] = new AgentUnavailableException(AgentUnavailableException::REASON_TIMEOUT);
        $processor = $this->processor($docs, $repo, $agent);

        for ($i = 0; $i < 5; $i++) {
            $processor->process(self::PID, self::PUUID, 'dr_smith', self::CHART, self::CID);
        }
        self::assertCount(DocumentProcessor::MAX_ATTEMPTS, $agent->extractionPosts);
        self::assertSame('failed', $repo->records[41]['status']);
        self::assertSame('agent_timeout', $repo->records[41]['last_error_code']);
        self::assertSame(DocumentProcessor::MAX_ATTEMPTS, $repo->records[41]['attempts']);
    }

    public function testFreeTextDegradedReasonsAreNeverStored(): void
    {
        $docs = new FakePatientDocuments([self::PID => [FakePatientDocuments::doc(42)]], [42 => 'pdf']);
        $repo = new FakeProcessingRepository();
        $agent = new FakeAgentClient();
        $agent->extractions[42] = ['status' => 'degraded', 'degraded_reason' => 'Patient Evelyn Demo: value unreadable', 'extraction' => null];
        $this->processor($docs, $repo, $agent)->process(self::PID, self::PUUID, 'dr_smith', self::CHART, self::CID);
        self::assertSame('agent_degraded', $repo->records[42]['last_error_code']);
    }

    public function testMalformedExtractionAndStoreFailureAreFailedCodes(): void
    {
        $docs = new FakePatientDocuments([self::PID => [FakePatientDocuments::doc(51), FakePatientDocuments::doc(52)]], [51 => 'a', 52 => 'b']);
        $repo = new FakeProcessingRepository();
        $agent = new FakeAgentClient();
        $agent->extractions[51] = self::ok(51, ['extraction' => ['results' => 'not-a-list']]);
        $agent->extractions[52] = self::ok(52);
        $repo->failSave = true;
        $result = $this->processor($docs, $repo, $agent)->process(self::PID, self::PUUID, 'dr_smith', self::CHART, self::CID);
        self::assertSame(['bad_extraction', 'store_failed'], array_column($result['processed'], 'outcome'));
        self::assertSame('failed', $repo->records[51]['status']);
        self::assertSame('failed', $repo->records[52]['status']);
    }

    public function testAnExtractionAttributedToAnotherDocumentIsNeverStored(): void
    {
        // The briefing contract rejects a stored extraction whose document_id or citation
        // source_id names another document, which would break every later briefing.
        $docs = new FakePatientDocuments([self::PID => [FakePatientDocuments::doc(53), FakePatientDocuments::doc(54)]], [53 => 'a', 54 => 'b']);
        $repo = new FakeProcessingRepository();
        $agent = new FakeAgentClient();
        $agent->extractions[53] = self::ok(53, ['extraction' => self::labExtraction(999)]);
        $wrongCitation = self::labExtraction(54);
        $wrongCitation['results'][1]['citation']['source_id'] = '999';
        $agent->extractions[54] = self::ok(54, ['extraction' => $wrongCitation]);
        $result = $this->processor($docs, $repo, $agent)->process(self::PID, self::PUUID, 'dr_smith', self::CHART, self::CID);
        self::assertSame(['bad_extraction', 'bad_extraction'], array_column($result['processed'], 'outcome'));
        self::assertSame('failed', $repo->records[53]['status']);
        self::assertNull($repo->records[54]['extraction_json'] ?? null);
        self::assertSame([], $repo->values[54] ?? []);
    }

    public function testADocumentHeldByAnotherRequestIsNotProcessedTwice(): void
    {
        $docs = new FakePatientDocuments([self::PID => [FakePatientDocuments::doc(61)]], [61 => 'pdf']);
        $repo = new FakeProcessingRepository();
        $repo->heldElsewhere = [61];
        $agent = new FakeAgentClient();
        $result = $this->processor($docs, $repo, $agent)->process(self::PID, self::PUUID, 'dr_smith', self::CHART, self::CID);
        self::assertSame([['document_id' => 61, 'outcome' => DocumentProcessor::CODE_BUSY]], $result['processed']);
        self::assertSame([], $agent->extractionPosts);
    }

    public function testStuckProcessingRecordIsReclaimedOnlyWhenStale(): void
    {
        $docs = new FakePatientDocuments([self::PID => [FakePatientDocuments::doc(71)]], [71 => 'pdf']);
        $repo = new FakeProcessingRepository();
        $repo->records[71] = ['document_id' => 71, 'pid' => self::PID, 'content_sha256' => hash('sha256', 'pdf'), 'doc_type' => 'lab_pdf', 'status' => 'processing', 'prompt_version' => null, 'attempts' => 1, 'last_error_code' => null, 'identity_check' => null, 'extraction_json' => null, 'created_at' => '2026-09-25 10:04:00', 'updated_at' => '2026-09-25 10:04:00'];
        $agent = new FakeAgentClient();
        $agent->extractions[71] = self::ok(71);
        $processor = $this->processor($docs, $repo, $agent);

        self::assertSame([], $processor->process(self::PID, self::PUUID, 'dr_smith', self::CHART, self::CID)['processed'], 'fresh claim: another request is working on it');
        $repo->records[71]['updated_at'] = '2026-09-25 09:00:00';
        $repo->records[71]['stale'] = true;
        self::assertSame('extracted', $processor->process(self::PID, self::PUUID, 'dr_smith', self::CHART, self::CID)['processed'][0]['outcome']);
        self::assertSame(2, $repo->records[71]['attempts']);
    }

    public function testUnreadableOrOversizedBytesAreReportedWithoutARecord(): void
    {
        $docs = new FakePatientDocuments([self::PID => [FakePatientDocuments::doc(81), FakePatientDocuments::doc(82)]], [81 => new SourceUnavailableException('documents'), 82 => new DocumentTooLargeException(82)]);
        $repo = new FakeProcessingRepository();
        $result = $this->processor($docs, $repo, new FakeAgentClient())->process(self::PID, self::PUUID, 'dr_smith', self::CHART, self::CID);
        self::assertSame(['document_unavailable', 'document_too_large'], array_column($result['processed'], 'outcome'));
        self::assertSame([], $repo->records);
    }

    public function testListShowsPendingCountsOnlyForExtractedDocuments(): void
    {
        $docs = new FakePatientDocuments([self::PID => [FakePatientDocuments::doc(91), FakePatientDocuments::doc(92)]], [91 => 'a', 92 => 'b']);
        $repo = new FakeProcessingRepository();
        $agent = new FakeAgentClient();
        $agent->extractions[91] = self::ok(91);
        $agent->extractions[92] = self::ok(92, ['printed_identity' => ['name' => 'Other Person', 'dob' => '1958-04-12']]);
        $processor = $this->processor($docs, $repo, $agent);
        $processor->process(self::PID, self::PUUID, 'dr_smith', self::CHART, self::CID);

        $list = $processor->listDocuments(self::PID, 'dr_smith');
        self::assertSame(['document_id' => 91, 'doc_type' => 'lab_pdf', 'uploaded_at' => '2026-09-20 10:00:00', 'status' => 'extracted', 'error_code' => null, 'identity_check' => 'match', 'pending_count' => 2], $list[0]);
        self::assertSame('held_identity', $list[1]['status']);
        self::assertSame(0, $list[1]['pending_count']);
        self::assertSame(['dr_smith'], array_values(array_unique($docs->listedFor)), 'listing is filtered for the requesting user');
    }

    // ------------------------------------------------------------------ //
    // Routes
    // ------------------------------------------------------------------ //

    /** @param array<string,string> $bases */
    private function controller(FakePatientDocuments $docs, FakeProcessingRepository $repo, FakeAgentClient $agent, bool $schemaReady = true, array $bases = [self::USER . ':' . self::PID => 'primary_provider']): DocumentsController
    {
        $reader = new FakeReader(patients: [self::PID => ['pid' => self::PID, 'uuid' => self::PUUID]]);
        $reader->identity = ['fname' => 'Evelyn', 'lname' => 'Demo', 'dob' => '1958-04-12', 'sex' => 'Female', 'pubpid' => 'P-7'];
        return new DocumentsController(
            new CopilotAuthorizer(new FakeAcl(self::FULL_ACL), new FakeRelationships($bases), false),
            $reader,
            new FakeSchemaStatus($schemaReady),
            $this->processor($docs, $repo, $agent),
            $this->logger,
            $this->audit,
        );
    }

    /** @return array{authUserID:int, authUser:string, pid:int} */
    private static function session(): array
    {
        return ['authUserID' => self::USER, 'authUser' => 'dr_smith', 'pid' => self::PID];
    }

    public function testProcessRouteProcessesComparesIdentityAndReturnsTheList(): void
    {
        $docs = new FakePatientDocuments([self::PID => [FakePatientDocuments::doc(101)]], [101 => 'pdf']);
        $repo = new FakeProcessingRepository();
        $agent = new FakeAgentClient();
        $agent->extractions[101] = self::ok(101, ['printed_identity' => ['name' => 'Evelyn Demo', 'dob' => '1960-01-01']]);

        $result = $this->controller($docs, $repo, $agent)->processForSession(self::session(), self::PID);

        self::assertSame(200, $result['status']);
        self::assertSame('ok', $result['body']['status']);
        self::assertSame([['document_id' => 101, 'outcome' => 'held_identity']], $result['body']['processed']);
        self::assertSame('held_identity', $result['body']['documents'][0]['status']);
        self::assertSame('mismatch', $result['body']['documents'][0]['identity_check']);
        self::assertSame('no-store, private', $result['headers']['Cache-Control']);
        self::assertCount(1, $this->audit->events);
        self::assertSame(DocumentsController::AUDIT_PROCESS, $this->audit->events[0]['event']);
        $everything = $this->audit->events[0]['comment'] . $this->logger->dump();
        self::assertStringNotContainsString('1960-01-01', $everything);
        self::assertStringNotContainsString('Evelyn', $everything);
        self::assertStringNotContainsString(self::PUUID, $everything);
        self::assertStringNotContainsString('Hemoglobin', $everything);
    }

    public function testRoutesAreDisabledUntilTheTablesExist(): void
    {
        $docs = new FakePatientDocuments([self::PID => [FakePatientDocuments::doc(111)]], [111 => 'pdf']);
        $agent = new FakeAgentClient();
        $controller = $this->controller($docs, new FakeProcessingRepository(), $agent, false);
        foreach ([$controller->processForSession(self::session()), $controller->listForSession(self::session())] as $result) {
            self::assertSame(200, $result['status']);
            self::assertSame('degraded', $result['body']['status']);
            self::assertSame('copilot_tables_not_installed', $result['body']['degraded_reason']);
        }
        self::assertSame([], $docs->bytesRead);
        self::assertSame([], $agent->extractionPosts);
    }

    public function testListRouteDoesNotProcess(): void
    {
        $docs = new FakePatientDocuments([self::PID => [FakePatientDocuments::doc(121)]], [121 => 'pdf']);
        $agent = new FakeAgentClient();
        $result = $this->controller($docs, new FakeProcessingRepository(), $agent)->listForSession(self::session());
        self::assertSame('queued', $result['body']['documents'][0]['status']);
        self::assertSame([], $agent->extractionPosts);
        self::assertSame(DocumentsController::AUDIT_LIST, $this->audit->events[0]['event']);
    }

    public function testStalePidAndNoRelationshipAreRefusedBeforeAnyRead(): void
    {
        $docs = new FakePatientDocuments([self::PID => [FakePatientDocuments::doc(131)]], [131 => 'pdf']);
        $stale = $this->controller($docs, new FakeProcessingRepository(), new FakeAgentClient())->processForSession(self::session(), self::OTHER_PID);
        self::assertSame(409, $stale['status']);
        $denied = $this->controller($docs, new FakeProcessingRepository(), new FakeAgentClient(), true, [])->processForSession(self::session());
        self::assertSame(403, $denied['status']);
        self::assertSame([], $docs->listedFor);
    }

    public function testUnreadableDocumentListIsDegraded(): void
    {
        $docs = new FakePatientDocuments([], [], new SourceUnavailableException('documents'));
        $result = $this->controller($docs, new FakeProcessingRepository(), new FakeAgentClient())->processForSession(self::session());
        self::assertSame(200, $result['status']);
        self::assertSame('documents_unavailable', $result['body']['degraded_reason']);
    }
}
