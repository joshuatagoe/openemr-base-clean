<?php

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Tests;

use OpenEMR\Common\Http\HttpRestRequest;
use OpenEMR\Modules\Copilot\Authorization\CopilotAuthorizer;
use OpenEMR\Modules\Copilot\Controller\DocumentPatientConfirmController;
use OpenEMR\Modules\Copilot\Documents\DocumentProcessor;
use OpenEMR\Modules\Copilot\Support\SessionRelease;
use PHPUnit\Framework\TestCase;
use Symfony\Component\HttpFoundation\Session\Session;
use Symfony\Component\HttpFoundation\Session\Storage\MockArraySessionStorage;

/**
 * "This is the right patient" (ADR-012 §4a):
 *   POST /api/copilot/documents/{document_id}/confirm-patient  body {"confirm": true}
 * resolves a document held for an identity check: it becomes `extracted`, the
 * identity result stays as history, the resolution is recorded with a fixed
 * code, and its candidates become pending facts.
 */
final class DocumentPatientConfirmTest extends TestCase
{
    private const PID = 42;
    private const OTHER_PID = 77;
    private const USER = 1;
    private const HELD = 601;
    private const HELD_OTHER_CHART = 602;
    private const READ = 603;
    private const NO_RECORD = 604;
    private const OTHER_PATIENTS = 605;
    private const READ_ACL = ['patients/demo' => true, 'encounters/auth_a' => true, 'encounters/notes' => true, 'patients/med' => true, 'patients/lab' => true, 'patients/appt' => true];

    private CapturingLogger $logger;
    private AuditCapture $audit;
    private FakeProcessingRepository $records;
    private FakeFileSource $documents;

    protected function setUp(): void
    {
        $this->logger = new CapturingLogger();
        $this->audit = new AuditCapture();
        $this->records = new FakeProcessingRepository();
        $this->seed(self::HELD, self::PID, 'held_identity', 'mismatch', null);
        $this->seed(self::HELD_OTHER_CHART, self::PID, 'held_identity', 'missing', DocumentProcessor::CODE_OTHER_CHART);
        $this->seed(self::READ, self::PID, 'extracted', 'match', null);
        $this->seed(self::OTHER_PATIENTS, self::OTHER_PID, 'held_identity', 'mismatch', null);
        $docs = [];
        foreach ([self::HELD, self::HELD_OTHER_CHART, self::READ, self::NO_RECORD] as $id) {
            $docs[$id] = ['pid' => self::PID, 'media_type' => 'application/pdf', 'size' => 1000];
        }
        $docs[self::OTHER_PATIENTS] = ['pid' => self::OTHER_PID, 'media_type' => 'application/pdf', 'size' => 1000];
        $this->documents = new FakeFileSource(docs: $docs);
    }

    private function seed(int $documentId, int $pid, string $status, string $identity, ?string $code): void
    {
        $this->records->records[$documentId] = [
            'document_id' => $documentId, 'pid' => $pid, 'content_sha256' => str_repeat('a', 63) . ($documentId % 10), 'doc_type' => 'lab_pdf',
            'status' => $status, 'prompt_version' => 'lab-v3', 'attempts' => 1, 'last_error_code' => $code,
            'identity_check' => $identity, 'extraction_json' => '{"document_id":' . $documentId . ',"results":[{"test_name":"Hemoglobin A1c"}]}',
            'created_at' => '2026-09-25 10:00:00', 'updated_at' => '2026-09-25 10:00:00',
        ];
        foreach ([0, 1] as $i) {
            $this->records->values[$documentId][] = [
                'id' => $this->records->nextValueId++, 'document_id' => $documentId, 'pid' => $pid, 'status' => 'candidate',
                'result_index' => $i, 'test_name' => $i === 0 ? 'Hemoglobin A1c' : 'Glucose', 'value_text' => $i === 0 ? '7.1' : '142',
                'unit' => $i === 0 ? '%' : 'mg/dL', 'reference_range' => null, 'abnormal_flag' => null, 'flag_source' => null,
                'collection_date' => '2026-09-01', 'verification_status' => 'verified_exact', 'page' => 1, 'bbox' => '0.1,0.2,0.3,0.25',
            ];
        }
    }

    /**
     * @param array<string,bool> $acl
     * @param array<string,bool> $write
     * @param array<string,string> $bases
     */
    private function controller(array $acl = self::READ_ACL + ['patients/sign' => true], array $write = ['patients/lab' => true], bool $ready = true, array $bases = [self::USER . ':' . self::PID => 'primary_provider']): DocumentPatientConfirmController
    {
        $read = new FakeAcl($acl);
        return new DocumentPatientConfirmController(
            new CopilotAuthorizer($read, new FakeRelationships($bases)),
            $read,
            new FakeWriteAcl($write),
            new FakeSchemaStatus($ready),
            $this->documents,
            $this->records,
            $this->logger,
            $this->audit,
        );
    }

    /**
     * @param array<mixed>|null $body
     * @return array{status:int, body:array<string,mixed>, headers:array<string,string>}
     */
    private function confirm(int $documentId = self::HELD, ?array $body = ['confirm' => true], ?DocumentPatientConfirmController $c = null): array
    {
        return ($c ?? $this->controller())->confirmForSession(['authUserID' => self::USER, 'authUser' => 'dr_smith', 'pid' => self::PID], $documentId, $body);
    }

    private function recordStatus(int $documentId): string
    {
        return $this->records->records[$documentId]['status'];
    }

    private function assertNoPhi(): void
    {
        $all = json_encode($this->audit->events, JSON_THROW_ON_ERROR) . $this->logger->dump();
        foreach (['Hemoglobin', 'Glucose', '7.1', '142', 'mg/dL'] as $needle) {
            self::assertStringNotContainsString($needle, $all);
        }
    }

    public function testConfirmingAHeldDocumentMakesItReadAndItsValuesPendingFacts(): void
    {
        $heldFacts = fn(): array => array_values(array_filter($this->records->listPendingFacts(self::PID, 100), static fn(array $f): bool => $f['document_id'] === self::HELD));
        self::assertSame([], $heldFacts(), 'held facts are not pending facts');

        $result = $this->confirm();

        self::assertSame(200, $result['status'], (string) json_encode($result['body']));
        $body = $result['body'];
        self::assertSame(self::HELD, $body['document_id']);
        self::assertSame('extracted', $body['status']);
        self::assertSame('mismatch', $body['identity_check'], 'the identity result stays as history');
        self::assertSame(DocumentPatientConfirmController::CODE_CONFIRMED, $body['error_code']);
        self::assertSame(2, $body['pending_count']);
        self::assertArrayHasKey('correlation_id', $body);

        $record = $this->records->records[self::HELD];
        self::assertSame('extracted', $record['status']);
        self::assertSame('mismatch', $record['identity_check']);
        self::assertSame('identity_confirmed_by_clinician', $record['last_error_code']);

        self::assertCount(2, $heldFacts(), 'its candidates are pending facts for the briefing and follow-ups');
        $extractions = array_column($this->records->listExtractions(self::PID, 50), 'document_id');
        self::assertContains(self::HELD, $extractions, 'its stored extraction joins the briefing');

        self::assertCount(1, $this->audit->events);
        $event = $this->audit->events[0];
        self::assertSame(DocumentPatientConfirmController::AUDIT_EVENT, $event['event']);
        self::assertSame('copilot-document-patient-confirmed', $event['event']);
        self::assertSame('dr_smith', $event['user']);
        self::assertSame(self::PID, $event['pid']);
        self::assertTrue($event['success']);
        self::assertStringContainsString('document_id=' . self::HELD, $event['comment']);
        self::assertStringContainsString('outcome=identity_confirmed_by_clinician', $event['comment']);
        self::assertStringContainsString('identity_check=mismatch', $event['comment']);
        self::assertStringContainsString('hold_code=none', $event['comment']);
        self::assertStringContainsString('basis=primary_provider', $event['comment']);
        $this->assertNoPhi();
    }

    public function testTheListShowsTheConfirmedDocumentAsReadWithItsPendingCount(): void
    {
        $processor = new DocumentProcessor(
            new FakePatientDocuments([self::PID => [FakePatientDocuments::doc(self::HELD)]]),
            $this->records,
            new FakeAgentClient(),
            $this->logger,
        );
        $before = $processor->listDocuments(self::PID, 'dr_smith')[0];
        self::assertSame('held_identity', $before['status']);
        self::assertSame(0, $before['pending_count']);

        self::assertSame(200, $this->confirm()['status']);

        $after = $processor->listDocuments(self::PID, 'dr_smith')[0];
        self::assertSame('extracted', $after['status']);
        self::assertSame('mismatch', $after['identity_check']);
        self::assertSame('identity_confirmed_by_clinician', $after['error_code']);
        self::assertSame(2, $after['pending_count']);
    }

    public function testTheHoldCodeIsRecordedInTheAuditRow(): void
    {
        $result = $this->confirm(self::HELD_OTHER_CHART);

        self::assertSame(200, $result['status']);
        self::assertSame('missing', $this->records->records[self::HELD_OTHER_CHART]['identity_check']);
        $comment = $this->audit->events[0]['comment'];
        self::assertStringContainsString('hold_code=same_file_in_other_chart', $comment);
        self::assertStringContainsString('identity_check=missing', $comment);
    }

    public function testAnotherPatientsDocumentAndAMissingOneAnswerTheSame404(): void
    {
        $other = $this->confirm(self::OTHER_PATIENTS);
        $missing = $this->confirm(999);
        $invalid = $this->confirm(0);

        foreach ([$other, $missing, $invalid] as $r) {
            self::assertSame(404, $r['status']);
            self::assertSame('document_not_found', $r['body']['detail']['code']);
        }
        self::assertSame($other['body']['detail']['message'], $missing['body']['detail']['message']);
        self::assertSame('held_identity', $this->recordStatus(self::OTHER_PATIENTS), 'another patient\'s record is never touched');
        self::assertSame([], $this->documents->accessChecked, 'the lookup comes before can_access');
        foreach ($this->audit->events as $e) {
            self::assertFalse($e['success']);
            self::assertStringContainsString('outcome=document_not_found', $e['comment']);
        }
    }

    public function testCoreCanAccessDenialIs403AndChangesNothing(): void
    {
        $this->documents->denied = [self::HELD];

        $result = $this->confirm();

        self::assertSame(403, $result['status']);
        self::assertSame('document_access_denied', $result['body']['detail']['code']);
        self::assertSame('held_identity', $this->recordStatus(self::HELD));
    }

    public function testADocumentThatIsNotHeldIs409NotHeld(): void
    {
        $read = $this->confirm(self::READ);
        $noRecord = $this->confirm(self::NO_RECORD);

        foreach ([$read, $noRecord] as $r) {
            self::assertSame(409, $r['status']);
            self::assertSame('not_held', $r['body']['detail']['code']);
        }
        self::assertNull($this->records->records[self::READ]['last_error_code'], 'an extracted document is not re-marked');
        self::assertArrayNotHasKey(self::NO_RECORD, $this->records->records);
    }

    public function testASecondConfirmIs409AndLeavesTheRecordAsConfirmed(): void
    {
        self::assertSame(200, $this->confirm()['status']);

        $again = $this->confirm();

        self::assertSame(409, $again['status']);
        self::assertSame('not_held', $again['body']['detail']['code']);
        self::assertSame('identity_confirmed_by_clinician', $this->records->records[self::HELD]['last_error_code']);
        self::assertCount(2, $this->audit->events);
        self::assertFalse($this->audit->events[1]['success']);
    }

    public function testAConcurrentChangeBetweenReadAndUpdateIs409(): void
    {
        $this->records->confirmRace = [self::HELD];

        $result = $this->confirm();

        self::assertSame(409, $result['status']);
        self::assertSame('not_held', $result['body']['detail']['code']);
    }

    public function testLabWriteAndSignAreBothRequired(): void
    {
        $noWrite = $this->confirm(c: $this->controller(write: []));
        $noSign = $this->confirm(c: $this->controller(acl: self::READ_ACL));

        foreach ([$noWrite, $noSign] as $r) {
            self::assertSame(403, $r['status']);
            self::assertSame('confirm_not_permitted', $r['body']['detail']['code']);
        }
        self::assertSame('held_identity', $this->recordStatus(self::HELD));
        self::assertCount(2, $this->audit->events);
        foreach ($this->audit->events as $e) {
            self::assertSame(DocumentPatientConfirmController::AUDIT_EVENT, $e['event']);
            self::assertFalse($e['success']);
        }
    }

    public function testTheReadAuthorizerStillApplies(): void
    {
        $result = $this->confirm(c: $this->controller(bases: []));

        self::assertSame(403, $result['status']);
        self::assertSame(CopilotAuthorizer::CODE_NO_CARE_RELATIONSHIP, $result['body']['detail']['code']);
        self::assertSame('held_identity', $this->recordStatus(self::HELD));
    }

    public function testAnExplicitConfirmationIsRequired(): void
    {
        foreach ([null, [], ['confirm' => false], ['confirm' => 'true'], ['confirm' => 1]] as $body) {
            $r = $this->confirm(body: $body);
            self::assertSame(422, $r['status'], (string) json_encode($body));
            self::assertSame('confirmation_required', $r['body']['detail']['code']);
        }
        self::assertSame('held_identity', $this->recordStatus(self::HELD));
    }

    public function testTablesNotInstalledIs503(): void
    {
        $result = $this->confirm(c: $this->controller(ready: false));

        self::assertSame(503, $result['status']);
        self::assertSame('copilot_tables_not_installed', $result['body']['detail']['code']);
    }

    public function testTheRestHandlerReleasesTheSessionAndParsesTheBody(): void
    {
        $session = new Session(new MockArraySessionStorage());
        $session->set('authUserID', self::USER);
        $session->set('authUser', 'dr_smith');
        $session->set('pid', self::PID);
        $released = 0;
        SessionRelease::useCloser(static function () use (&$released): void {
            $released++;
        });
        try {
            $request = new HttpRestRequest([], [], [], [], [], ['REQUEST_METHOD' => 'POST'], '{"confirm":true}');
            $request->setSession($session);

            $response = $this->controller()->handleRest((string) self::HELD, $request);
        } finally {
            SessionRelease::useCloser(null);
        }

        self::assertSame(1, $released);
        self::assertSame(200, $response->getStatusCode(), (string) $response->getContent());
        self::assertSame('extracted', $this->recordStatus(self::HELD));
        self::assertSame('no-store, private', $response->headers->get('Cache-Control'));
    }
}
