<?php

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Tests;

use OpenEMR\Modules\Copilot\Authorization\CopilotAuthorizer;
use OpenEMR\Modules\Copilot\Controller\DocumentFileController;
use OpenEMR\Modules\Copilot\Data\DocumentTooLargeException;
use OpenEMR\Modules\Copilot\Data\SourceUnavailableException;
use OpenEMR\Modules\Copilot\Data\SqlDocumentReader;
use PHPUnit\Framework\Attributes\DataProvider;
use PHPUnit\Framework\TestCase;

/**
 * `GET /api/copilot/document-file/{document_id}` (ADR-008 §3, §7): the source
 * file for the preview, under the briefing routes' authorizer.
 */
final class DocumentFileRouteTest extends TestCase
{
    private const PID = 42;
    private const OTHER_PID = 77;
    private const USER = 1;
    private const DOC = 501;
    private const OTHER_DOC = 502;
    private const BYTES = "%PDF-1.4 synthetic \x00\x01\xff bytes";
    private const FULL_ACL = ['patients/demo' => true, 'encounters/auth_a' => true, 'encounters/notes' => true, 'patients/med' => true, 'patients/lab' => true, 'patients/appt' => true];

    private CapturingLogger $logger;
    private AuditCapture $audit;

    protected function setUp(): void
    {
        $this->logger = new CapturingLogger();
        $this->audit = new AuditCapture();
    }

    private static function session(?int $pid = self::PID, ?int $user = self::USER): array
    {
        return ['authUserID' => $user, 'authUser' => $user === null ? null : 'dr_smith', 'pid' => $pid];
    }

    private static function source(string $mediaType = 'application/pdf', int $size = 1000): FakeFileSource
    {
        return new FakeFileSource(
            docs: [
                self::DOC => ['pid' => self::PID, 'media_type' => $mediaType, 'size' => $size],
                self::OTHER_DOC => ['pid' => self::OTHER_PID, 'media_type' => 'application/pdf', 'size' => 1000],
            ],
            bytes: [self::DOC => self::BYTES, self::OTHER_DOC => 'other patient bytes'],
        );
    }

    /** @param array<string,string> $bases */
    private function controller(FakeFileSource $source, array $bases = [self::USER . ':' . self::PID => 'primary_provider'], array $acl = self::FULL_ACL): DocumentFileController
    {
        return new DocumentFileController(
            new CopilotAuthorizer(new FakeAcl($acl), new FakeRelationships($bases)),
            $source,
            $this->logger,
            $this->audit,
        );
    }

    /** @return array<string, array{string}> */
    public static function mediaTypes(): array
    {
        return ['pdf' => ['application/pdf'], 'png' => ['image/png'], 'jpeg' => ['image/jpeg']];
    }

    #[DataProvider('mediaTypes')]
    public function testOwnPatientDocumentReturnsExactBytesAndHeaders(string $mediaType): void
    {
        $source = self::source($mediaType);
        $result = $this->controller($source)->viewForSession(self::session(), self::DOC);

        self::assertSame(200, $result['status']);
        self::assertSame(self::BYTES, $result['body']);
        $h = $result['headers'];
        self::assertSame($mediaType, $h['Content-Type']);
        self::assertSame('inline', $h['Content-Disposition']);
        self::assertSame('nosniff', $h['X-Content-Type-Options']);
        self::assertSame('no-store, private', $h['Cache-Control']);
        self::assertSame("sandbox; default-src 'none'", $h['Content-Security-Policy']);
        self::assertSame([self::DOC], $source->accessChecked);

        self::assertCount(1, $this->audit->events);
        $event = $this->audit->events[0];
        self::assertSame(DocumentFileController::AUDIT_EVENT, $event['event']);
        self::assertTrue($event['success']);
        self::assertSame(self::PID, $event['pid']);
        self::assertStringContainsString('document_id=' . self::DOC, $event['comment']);
        self::assertStringContainsString('outcome=ok', $event['comment']);
        self::assertStringNotContainsString('synthetic', $event['comment'] . $this->logger->dump());
    }

    public function testAnotherPatientsDocumentIs404WithoutReadingTheStore(): void
    {
        $source = self::source();
        $result = $this->controller($source)->viewForSession(self::session(), self::OTHER_DOC);

        self::assertSame(404, $result['status']);
        self::assertSame([], $source->bytesRead);
        self::assertSame([], $source->accessChecked);
        self::assertSame('document_not_found', $result['body']['detail']['code']);
    }

    public function testMissingAndOtherPatientGiveTheSameResponse(): void
    {
        $other = $this->controller(self::source())->viewForSession(self::session(), self::OTHER_DOC);
        $missing = $this->controller(self::source())->viewForSession(self::session(), 99999);
        $zero = $this->controller(self::source())->viewForSession(self::session(), 0);

        $normalised = [];
        foreach ([$other, $missing, $zero] as $r) {
            self::assertSame(404, $r['status']);
            $body = $r['body'];
            unset($body['detail']['correlation_id']);
            $normalised[] = [$body, array_keys($r['headers'])];
        }
        self::assertSame($normalised[0], $normalised[1]);
        self::assertSame($normalised[0], $normalised[2]);
    }

    public function testCanAccessFalseIs403WithoutReadingBytes(): void
    {
        $source = self::source();
        $source->denied = [self::DOC];
        $result = $this->controller($source)->viewForSession(self::session(), self::DOC);

        self::assertSame(403, $result['status']);
        self::assertSame('document_access_denied', $result['body']['detail']['code']);
        self::assertSame([], $source->bytesRead);
        self::assertFalse($this->audit->events[0]['success']);
    }

    public function testNoCareRelationshipIs403AndNothingIsLookedUp(): void
    {
        $source = self::source();
        $result = $this->controller($source, bases: [])->viewForSession(self::session(), self::DOC);

        self::assertSame(403, $result['status']);
        self::assertSame(CopilotAuthorizer::CODE_NO_CARE_RELATIONSHIP, $result['body']['detail']['code']);
        self::assertSame([], $source->bytesRead);
        self::assertSame([], $source->accessChecked);
    }

    public function testAclDeniedIs403(): void
    {
        $result = $this->controller(self::source(), acl: ['patients/demo' => true])->viewForSession(self::session(), self::DOC);
        self::assertSame(403, $result['status']);
        self::assertSame(CopilotAuthorizer::CODE_ACL_DENIED, $result['body']['detail']['code']);
    }

    public function testUnauthenticatedIs401WithoutAudit(): void
    {
        $result = $this->controller(self::source())->viewForSession(self::session(user: null), self::DOC);
        self::assertSame(401, $result['status']);
        self::assertSame([], $this->audit->events);
    }

    public function testNoPatientSelectedIs409(): void
    {
        $result = $this->controller(self::source())->viewForSession(self::session(pid: null), self::DOC);
        self::assertSame(409, $result['status']);
        self::assertSame(CopilotAuthorizer::CODE_NO_PATIENT_SELECTED, $result['body']['detail']['code']);
    }

    public function testOversizeByStoredSizeIsRefusedWithoutReading(): void
    {
        $source = self::source(size: SqlDocumentReader::MAX_DOCUMENT_BYTES + 1);
        $result = $this->controller($source)->viewForSession(self::session(), self::DOC);

        self::assertSame(413, $result['status']);
        self::assertSame('document_too_large', $result['body']['detail']['code']);
        self::assertSame([], $source->bytesRead);
    }

    public function testOversizeFoundWhileReadingIsRefused(): void
    {
        $source = self::source(size: 0);
        $source->bytes[self::DOC] = new DocumentTooLargeException(self::DOC);
        $result = $this->controller($source)->viewForSession(self::session(), self::DOC);
        self::assertSame(413, $result['status']);
    }

    public function testStoreFailureIs503WithNoExceptionText(): void
    {
        $source = self::source();
        $source->bytes[self::DOC] = new SourceUnavailableException('documents', new \RuntimeException('disk /var/secret/path.pdf unreadable'));
        $result = $this->controller($source)->viewForSession(self::session(), self::DOC);

        self::assertSame(503, $result['status']);
        self::assertSame('document_unavailable', $result['body']['detail']['code']);
        $all = json_encode($result['body'], JSON_THROW_ON_ERROR) . $this->logger->dump() . json_encode($this->audit->events, JSON_THROW_ON_ERROR);
        self::assertStringNotContainsString('secret', $all);
        self::assertStringNotContainsString('unreadable', $all);
    }

    public function testLookupFailureIs503(): void
    {
        $source = self::source();
        $source->lookupFailure = new SourceUnavailableException('documents');
        $result = $this->controller($source)->viewForSession(self::session(), self::DOC);
        self::assertSame(503, $result['status']);
    }

    public function testErrorResponsesAreJsonAndNotCached(): void
    {
        $result = $this->controller(self::source())->viewForSession(self::session(), self::OTHER_DOC);
        self::assertSame('no-store, private', $result['headers']['Cache-Control']);
        self::assertSame('nosniff', $result['headers']['X-Content-Type-Options']);
        self::assertIsArray($result['body']);
    }

    public function testOneAuditRowPerAuthorizedCallWithCodesOnly(): void
    {
        $this->controller(self::source())->viewForSession(self::session(), self::OTHER_DOC);
        self::assertCount(1, $this->audit->events);
        self::assertFalse($this->audit->events[0]['success']);
        self::assertStringContainsString('outcome=document_not_found', $this->audit->events[0]['comment']);
    }
}
