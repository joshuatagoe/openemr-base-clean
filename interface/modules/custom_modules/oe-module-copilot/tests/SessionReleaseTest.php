<?php

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Tests;

use DateTimeZone;
use OpenEMR\Common\Http\HttpRestRequest;
use OpenEMR\Modules\Copilot\Agent\AgentClientInterface;
use OpenEMR\Modules\Copilot\Agent\BundleAccepted;
use OpenEMR\Modules\Copilot\Authorization\CopilotAuthorizer;
use OpenEMR\Modules\Copilot\Config\CopilotConfig;
use OpenEMR\Modules\Copilot\ContextBundleBuilder;
use OpenEMR\Modules\Copilot\Controller\BriefingTicketController;
use OpenEMR\Modules\Copilot\Controller\DocumentBriefingController;
use OpenEMR\Modules\Copilot\Controller\DocumentFileController;
use OpenEMR\Modules\Copilot\Controller\DocumentsController;
use OpenEMR\Modules\Copilot\Controller\FilingController;
use OpenEMR\Modules\Copilot\Controller\ResultSourceController;
use OpenEMR\Modules\Copilot\Documents\DocumentProcessor;
use OpenEMR\Modules\Copilot\Filing\ValueFiler;
use OpenEMR\Modules\Copilot\Support\SessionRelease;
use PHPUnit\Framework\TestCase;
use Symfony\Component\HttpFoundation\Session\Session;
use Symfony\Component\HttpFoundation\Session\Storage\MockArraySessionStorage;

/**
 * Module routes read the session and release its lock before slow work: a
 * writable PHP session held during an agent call freezes the user's other
 * OpenEMR pages.
 */
final class SessionReleaseTest extends TestCase
{
    private const PID = 42;
    private const FULL_ACL = ['patients/demo' => true, 'encounters/auth_a' => true, 'encounters/notes' => true, 'patients/med' => true, 'patients/lab' => true, 'patients/appt' => true, 'patients/sign' => true];

    private Session $session;

    /** Shared with the observing agent: how many times the session lock was released so far. */
    private SessionReleaseCounter $released;

    protected function setUp(): void
    {
        $this->session = new Session(new MockArraySessionStorage());
        $this->session->set('authUserID', 1);
        $this->session->set('authUser', 'dr_smith');
        $this->session->set('pid', self::PID);
        $this->released = new SessionReleaseCounter();
        $counter = $this->released;
        SessionRelease::useCloser(static function () use ($counter): void {
            $counter->count++;
        });
    }

    protected function tearDown(): void
    {
        SessionRelease::useCloser(null);
    }

    private function request(string $method = 'GET', string $content = ''): HttpRestRequest
    {
        $request = new HttpRestRequest([], [], [], [], [], ['REQUEST_METHOD' => $method], $content);
        $request->setSession($this->session);
        return $request;
    }

    private static function authorizer(): CopilotAuthorizer
    {
        return new CopilotAuthorizer(new FakeAcl(self::FULL_ACL), new FakeRelationships(['1:' . self::PID => 'primary_provider']));
    }

    public function testProcessingReleasesTheSessionBeforeCallingTheAgent(): void
    {
        $agent = new SessionObservingAgent($this->released);
        $docs = new FakePatientDocuments([self::PID => [FakePatientDocuments::doc(25)]], [25 => 'lab-bytes']);
        $reader = new FakeReader(patients: [self::PID => ['pid' => self::PID, 'uuid' => '3b9d2c1e-8f7a-4b6c-9d0e-1f2a3b4c5d6e']]);
        $controller = new DocumentsController(
            self::authorizer(),
            $reader,
            new FakeSchemaStatus(true),
            new DocumentProcessor($docs, new FakeProcessingRepository(), $agent, new CapturingLogger()),
            new CapturingLogger(),
            new AuditCapture(),
        );

        $response = $controller->handleProcessRest($this->request('POST', '{}'));

        self::assertSame(200, $response->getStatusCode());
        self::assertSame([1], $agent->releasesAtCall, 'the session lock is released before the agent call');
    }

    public function testListReleasesTheSession(): void
    {
        $controller = new DocumentsController(
            self::authorizer(),
            new FakeReader(),
            new FakeSchemaStatus(true),
            new DocumentProcessor(new FakePatientDocuments(), new FakeProcessingRepository(), new SessionObservingAgent($this->released), new CapturingLogger()),
            new CapturingLogger(),
            new AuditCapture(),
        );
        $controller->handleListRest($this->request());
        self::assertSame(1, $this->released->count);
    }

    public function testFileRouteReleasesTheSession(): void
    {
        $source = new FakeFileSource(docs: [5 => ['pid' => self::PID, 'media_type' => 'application/pdf', 'size' => 3]], bytes: [5 => 'pdf']);
        $response = (new DocumentFileController(self::authorizer(), $source, new CapturingLogger(), new AuditCapture()))->handleRest('5', $this->request());
        self::assertSame(200, $response->getStatusCode());
        self::assertSame(1, $this->released->count);
    }

    public function testFilingRoutesReleaseTheSession(): void
    {
        $store = new FakeFilingStore();
        $controller = new FilingController(self::authorizer(), new FakeAcl(self::FULL_ACL), new FakeWriteAcl(['patients/lab' => true]), new FakeSchemaStatus(true), new FakeFileSource(), new ValueFiler($store), new CapturingLogger(), new AuditCapture());
        foreach (['handleFileRest', 'handleRejectRest', 'handleUnfileRest'] as $i => $method) {
            $controller->$method('5', '0', $this->request('POST', '{}'));
            self::assertSame($i + 1, $this->released->count, $method);
        }
    }

    public function testBriefingRoutesReleaseTheSession(): void
    {
        $patients = [self::PID => ['pid' => self::PID, 'uuid' => '3b9d2c1e-8f7a-4b6c-9d0e-1f2a3b4c5d6e', 'fname' => 'T', 'lname' => 'P', 'DOB' => '1960-01-01', 'sex' => 'Female']];
        $ticket = new BriefingTicketController(
            self::authorizer(),
            new FakeReader(patients: $patients, notes: [self::PID => [['form_soap_id' => 1, 'encounter' => 7, 'note_date' => '2026-09-01 09:00:00', 'plan' => 'x']]]),
            new ContextBundleBuilder(new DateTimeZone('UTC')),
            new FakeAgentClient(),
            new CopilotConfig('http://agent.test:8000', 'test-only-shared-secret-0123456789abcdef'),
            new CapturingLogger(),
            new AuditCapture(),
        );
        $ticket->handleRest($this->request('POST', '{}'));
        self::assertSame(1, $this->released->count, 'briefing-ticket');

        $briefing = new DocumentBriefingController(self::authorizer(), new FakeReader(patients: $patients), new FakeDocumentReader([]), new FakeAgentClient(), new CapturingLogger(), new AuditCapture());
        $briefing->handleRest($this->request('POST', '{}'));
        self::assertSame(2, $this->released->count, 'document-briefing');
    }

    public function testResultSourceReleasesTheSession(): void
    {
        (new ResultSourceController(self::authorizer(), new FakeFilingStore(), new FakeFileSource(), new CapturingLogger(), new AuditCapture()))->handleRest('5', $this->request());
        self::assertSame(1, $this->released->count);
    }
}

final class SessionReleaseCounter
{
    public int $count = 0;
}

/** Records how many session releases had happened each time the agent was called. */
final class SessionObservingAgent implements AgentClientInterface
{
    /** @var list<int> */
    public array $releasesAtCall = [];

    public function __construct(private readonly SessionReleaseCounter $released)
    {
    }

    public function postBundle(array $bundle, string $correlationId): BundleAccepted
    {
        throw new \LogicException('not used');
    }

    public function postDocumentBriefing(array $request, string $correlationId): array
    {
        throw new \LogicException('not used');
    }

    public function postDocumentExtraction(array $request, string $correlationId): array
    {
        $this->releasesAtCall[] = $this->released->count;
        return (new FakeAgentClient())->postDocumentExtraction($request, $correlationId);
    }
}
