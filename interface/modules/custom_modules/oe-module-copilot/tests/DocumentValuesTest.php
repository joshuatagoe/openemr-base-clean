<?php

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Tests;

use OpenEMR\Common\Http\HttpRestRequest;
use OpenEMR\Modules\Copilot\Authorization\CopilotAuthorizer;
use OpenEMR\Modules\Copilot\Controller\DocumentValuesController;
use OpenEMR\Modules\Copilot\Data\SourceUnavailableException;
use OpenEMR\Modules\Copilot\Support\SessionRelease;
use PHPUnit\Framework\TestCase;
use Symfony\Component\HttpFoundation\Session\Session;
use Symfony\Component\HttpFoundation\Session\Storage\MockArraySessionStorage;

/**
 * The panel's read of one document's candidate values (Wave 2 step 2):
 *   GET /api/copilot/documents/{document_id}/values
 * Same authorizer and binding as the file route: the session's patient, one 404
 * for a missing document and another patient's, core can_access() 403, audit
 * row with codes only.
 */
final class DocumentValuesTest extends TestCase
{
    private const PID = 42;
    private const OTHER_PID = 77;
    private const USER = 1;
    private const DOC = 501;
    private const OTHER_DOC = 600;
    private const READ_ACL = ['patients/demo' => true, 'encounters/auth_a' => true, 'encounters/notes' => true, 'patients/med' => true, 'patients/lab' => true, 'patients/appt' => true];

    private AuditCapture $audit;
    private CapturingLogger $logger;
    private FakeDocumentValues $values;
    private FakeFileSource $documents;

    protected function setUp(): void
    {
        $this->audit = new AuditCapture();
        $this->logger = new CapturingLogger();
        $this->documents = new FakeFileSource(docs: [
            self::DOC => ['pid' => self::PID, 'media_type' => 'application/pdf', 'size' => 10],
            self::OTHER_DOC => ['pid' => self::OTHER_PID, 'media_type' => 'application/pdf', 'size' => 10],
        ]);
        $this->values = new FakeDocumentValues();
        $this->values->records[self::DOC] = ['pid' => self::PID, 'status' => 'extracted', 'identity_check' => 'match', 'doc_type' => 'lab_pdf', 'last_error_code' => null];
        $this->values->records[self::OTHER_DOC] = ['pid' => self::OTHER_PID, 'status' => 'extracted', 'identity_check' => 'match', 'doc_type' => 'lab_pdf', 'last_error_code' => null];
        $this->values->rows[self::DOC] = [
            FakeDocumentValues::row(0, ['test_name' => 'Hemoglobin A1c', 'value_text' => '8.4', 'status' => 'candidate']),
            FakeDocumentValues::row(1, ['test_name' => 'Glucose', 'value_text' => null, 'verification_status' => 'unreadable', 'page' => null, 'bbox' => null]),
            FakeDocumentValues::row(2, ['status' => 'filed', 'procedure_result_id' => 26]),
        ];
        $this->values->rows[self::OTHER_DOC] = [FakeDocumentValues::row(0, ['test_name' => 'Secret other test'])];
    }

    private function controller(array $bases = [self::USER . ':' . self::PID => 'primary_provider'], bool $ready = true): DocumentValuesController
    {
        return new DocumentValuesController(
            new CopilotAuthorizer(new FakeAcl(self::READ_ACL), new FakeRelationships($bases)),
            new FakeSchemaStatus($ready),
            $this->documents,
            $this->values,
            $this->logger,
            $this->audit,
        );
    }

    private static function session(): array
    {
        return ['authUserID' => self::USER, 'authUser' => 'dr_smith', 'pid' => self::PID];
    }

    public function testReturnsEveryValueOfTheDocumentWithItsSourceAndStatus(): void
    {
        $r = $this->controller()->valuesForSession(self::session(), self::DOC);

        self::assertSame(200, $r['status']);
        $body = $r['body'];
        self::assertSame(self::DOC, $body['document_id']);
        self::assertSame('extracted', $body['status']);
        self::assertSame('match', $body['identity_check']);
        self::assertCount(3, $body['values']);
        $first = $body['values'][0];
        self::assertSame(
            ['result_index', 'test_name', 'value_text', 'unit', 'reference_range', 'abnormal_flag', 'flag_source', 'collection_date', 'verification_status', 'page', 'bbox', 'status', 'procedure_result_id'],
            array_keys($first)
        );
        self::assertSame(0, $first['result_index']);
        self::assertSame('Hemoglobin A1c', $first['test_name']);
        self::assertSame('8.4', $first['value_text']);
        self::assertSame([0.1, 0.2, 0.3, 0.25], $first['bbox'], 'the stored box comes back as a list of four numbers');
        self::assertSame(1, $first['page']);
        self::assertSame('candidate', $first['status']);
        self::assertNull($first['procedure_result_id']);
        self::assertNull($body['values'][1]['bbox'], 'no box is never turned into a guessed one');
        self::assertNull($body['values'][1]['value_text']);
        self::assertSame('unreadable', $body['values'][1]['verification_status']);
        self::assertSame('filed', $body['values'][2]['status']);
        self::assertSame(26, $body['values'][2]['procedure_result_id']);
    }

    public function testAnotherPatientsDocumentAndAMissingOneAreTheSame404WithoutReadingValues(): void
    {
        $other = $this->controller()->valuesForSession(self::session(), self::OTHER_DOC);
        $missing = $this->controller()->valuesForSession(self::session(), 99999);

        self::assertSame(404, $other['status']);
        self::assertSame(404, $missing['status']);
        self::assertSame($other['body']['detail']['code'], $missing['body']['detail']['code']);
        self::assertSame($other['body']['detail']['message'], $missing['body']['detail']['message']);
        self::assertSame([], $this->values->read, 'no candidate row is read for a document that is not the patient\'s');
        self::assertStringNotContainsString('Secret', json_encode($other['body']));
    }

    public function testAProcessingRecordUnderAnotherPatientIsNotReturned(): void
    {
        // The core document moved to this patient, but our record (and its values) still belong to the old one.
        $this->values->records[self::DOC]['pid'] = self::OTHER_PID;
        $r = $this->controller()->valuesForSession(self::session(), self::DOC);
        self::assertSame(200, $r['status']);
        self::assertNull($r['body']['status']);
        self::assertSame([], $r['body']['values']);
    }

    public function testCanAccessFalseIs403AndNoValuesAreRead(): void
    {
        $this->documents->denied = [self::DOC];
        $r = $this->controller()->valuesForSession(self::session(), self::DOC);
        self::assertSame(403, $r['status']);
        self::assertSame('document_access_denied', $r['body']['detail']['code']);
        self::assertSame([], $this->values->read);
    }

    public function testTheReadAuthorizerApplies(): void
    {
        self::assertSame(403, $this->controller([])->valuesForSession(self::session(), self::DOC)['status']);
        self::assertSame(409, $this->controller()->valuesForSession(['authUserID' => self::USER, 'authUser' => 'dr_smith'], self::DOC)['status']);
        self::assertSame(401, $this->controller()->valuesForSession([], self::DOC)['status']);
    }

    public function testAHeldDocumentsValuesAreWithheld(): void
    {
        $this->values->records[self::DOC] = ['status' => 'held_identity', 'identity_check' => 'mismatch'] + $this->values->records[self::DOC];
        $r = $this->controller()->valuesForSession(self::session(), self::DOC);
        self::assertSame(200, $r['status']);
        self::assertSame('held_identity', $r['body']['status']);
        self::assertSame('mismatch', $r['body']['identity_check']);
        self::assertSame([], $r['body']['values'], 'held facts are not shown until a clinician confirms the patient (ADR-012)');
    }

    public function testAnUnprocessedDocumentHasNoRecordAndNoValues(): void
    {
        unset($this->values->records[self::DOC]);
        $r = $this->controller()->valuesForSession(self::session(), self::DOC);
        self::assertSame(200, $r['status']);
        self::assertNull($r['body']['status']);
        self::assertNull($r['body']['identity_check']);
        self::assertSame([], $r['body']['values']);
    }

    public function testTablesNotInstalledIs503(): void
    {
        $r = $this->controller(ready: false)->valuesForSession(self::session(), self::DOC);
        self::assertSame(503, $r['status']);
        self::assertSame('copilot_tables_not_installed', $r['body']['detail']['code']);
    }

    public function testStoreFailureIs503WithNoExceptionText(): void
    {
        $this->documents->lookupFailure = new SourceUnavailableException('documents');
        $r = $this->controller()->valuesForSession(self::session(), self::DOC);
        self::assertSame(503, $r['status']);
        self::assertStringNotContainsString('Exception', json_encode($r['body']));
    }

    public function testOneAuditRowWithCodesAndCountsOnlyAndNoPhiInTheLog(): void
    {
        $this->controller()->valuesForSession(self::session(), self::DOC);

        self::assertCount(1, $this->audit->events);
        $row = $this->audit->events[0];
        self::assertSame(DocumentValuesController::AUDIT_EVENT, $row['event']);
        self::assertTrue($row['success']);
        self::assertStringContainsString('document_id=' . self::DOC, $row['comment']);
        self::assertStringContainsString('values=3', $row['comment']);
        foreach (['Hemoglobin', 'Glucose', '8.4', '0.1,0.2'] as $phi) {
            self::assertStringNotContainsString($phi, $row['comment']);
            self::assertStringNotContainsString($phi, $this->logger->dump());
        }
    }

    public function testTheRestHandlerReleasesTheSessionAndRejectsABadId(): void
    {
        $released = 0;
        SessionRelease::useCloser(static function () use (&$released): void {
            $released++;
        });
        try {
            $session = new Session(new MockArraySessionStorage());
            $session->set('authUserID', self::USER);
            $session->set('authUser', 'dr_smith');
            $session->set('pid', self::PID);
            $request = new HttpRestRequest([], [], [], [], [], ['REQUEST_METHOD' => 'GET'], '');
            $request->setSession($session);

            $ok = $this->controller()->handleRest((string) self::DOC, $request);
            self::assertSame(200, $ok->getStatusCode());
            self::assertSame(1, $released);
            self::assertSame('no-store, private', $ok->headers->get('Cache-Control'));

            $bad = $this->controller()->handleRest('1 OR 1=1', $request);
            self::assertSame(404, $bad->getStatusCode());
        } finally {
            SessionRelease::useCloser(null);
        }
    }
}
