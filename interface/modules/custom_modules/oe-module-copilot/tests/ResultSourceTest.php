<?php

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Tests;

use OpenEMR\Modules\Copilot\Authorization\CopilotAuthorizer;
use OpenEMR\Modules\Copilot\Controller\ResultSourceController;
use PHPUnit\Framework\TestCase;

/**
 * ADR-009 7b: filed results carry no core document link, so the panel finds a
 * filed chart result's source document through the Co-Pilot candidate row:
 *   GET /api/copilot/results/{procedure_result_id}/source
 */
final class ResultSourceTest extends TestCase
{
    private const PID = 42;
    private const OTHER_PID = 77;
    private const USER = 1;
    private const DOC = 501;
    private const READ_ACL = ['patients/demo' => true, 'encounters/auth_a' => true, 'encounters/notes' => true, 'patients/med' => true, 'patients/lab' => true, 'patients/appt' => true];

    private AuditCapture $audit;
    private FakeFilingStore $store;
    private FakeFileSource $documents;

    protected function setUp(): void
    {
        $this->audit = new AuditCapture();
        $this->store = new FakeFilingStore();
        $this->documents = new FakeFileSource(docs: [self::DOC => ['pid' => self::PID, 'media_type' => 'application/pdf', 'size' => 10], 600 => ['pid' => self::OTHER_PID, 'media_type' => 'application/pdf', 'size' => 10]]);
        $this->store->addCandidate(self::DOC, self::PID, 3, ['status' => 'filed', 'procedure_result_id' => 26, 'page' => 2]);
        $this->store->addCandidate(600, self::OTHER_PID, 0, ['status' => 'filed', 'procedure_result_id' => 90]);
    }

    private function controller(array $bases = [self::USER . ':' . self::PID => 'primary_provider']): ResultSourceController
    {
        return new ResultSourceController(
            new CopilotAuthorizer(new FakeAcl(self::READ_ACL), new FakeRelationships($bases)),
            $this->store,
            $this->documents,
            new CapturingLogger(),
            $this->audit,
        );
    }

    private static function session(): array
    {
        return ['authUserID' => self::USER, 'authUser' => 'dr_smith', 'pid' => self::PID];
    }

    public function testAFiledResultLeadsToItsSourceDocumentAndBox(): void
    {
        $r = $this->controller()->sourceForSession(self::session(), 26);

        self::assertSame(200, $r['status']);
        self::assertSame(self::DOC, $r['body']['document_id']);
        self::assertSame(3, $r['body']['result_index']);
        self::assertSame(2, $r['body']['page']);
        self::assertSame([0.1, 0.2, 0.3, 0.25], $r['body']['bbox']);
        self::assertSame('filed', $r['body']['status']);
        self::assertSame(26, $r['body']['procedure_result_id']);
        self::assertSame(ResultSourceController::AUDIT_EVENT, $this->audit->events[0]['event']);
    }

    public function testAnotherPatientsResultAndAnUnknownResultAreTheSame404(): void
    {
        $other = $this->controller()->sourceForSession(self::session(), 90);
        $unknown = $this->controller()->sourceForSession(self::session(), 12345);
        self::assertSame(404, $other['status']);
        self::assertSame(404, $unknown['status']);
        self::assertSame($other['body']['detail']['message'], $unknown['body']['detail']['message']);
    }

    public function testADocumentNoLongerFiledToThePatientIs404(): void
    {
        unset($this->documents->docs[self::DOC]);
        self::assertSame(404, $this->controller()->sourceForSession(self::session(), 26)['status']);
    }

    public function testCanAccessFalseIs403(): void
    {
        $this->documents->denied = [self::DOC];
        self::assertSame(403, $this->controller()->sourceForSession(self::session(), 26)['status']);
    }

    public function testTheReadAuthorizerApplies(): void
    {
        self::assertSame(403, $this->controller([])->sourceForSession(self::session(), 26)['status']);
    }
}
