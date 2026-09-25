<?php

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Tests;

use DateTimeZone;
use OpenEMR\Modules\Copilot\Authorization\CopilotAuthorizer;
use OpenEMR\Modules\Copilot\ContextBundleBuilder;
use OpenEMR\Modules\Copilot\Controller\DocumentBriefingController;
use PHPUnit\Framework\TestCase;

/**
 * ADR-008 §3: the briefing reader applies core `can_access()` like the
 * document list does - a document the user may not open is never sent to
 * the agent, in either the stored-extraction or the legacy path.
 */
final class BriefingDocumentAccessTest extends TestCase
{
    private const PID = 42;
    private const USER = 1;
    private const PUUID = '3b9d2c1e-8f7a-4b6c-9d0e-1f2a3b4c5d6e';
    private const FULL_ACL = ['patients/demo' => true, 'encounters/auth_a' => true, 'encounters/notes' => true, 'patients/med' => true, 'patients/lab' => true, 'patients/appt' => true];

    private function controller(FakeDocumentReader $documents, FakeAgentClient $agent, ?FakeProcessingRepository $repo, bool $ready): DocumentBriefingController
    {
        return new DocumentBriefingController(
            new CopilotAuthorizer(new FakeAcl(self::FULL_ACL), new FakeRelationships([self::USER . ':' . self::PID => 'primary_provider'])),
            new FakeReader(patients: [self::PID => ['pid' => self::PID, 'uuid' => self::PUUID]], labs: [self::PID => []]),
            $documents,
            $agent,
            new CapturingLogger(),
            new AuditCapture(),
            records: $repo ?? new FakeProcessingRepository(),
            schema: new FakeSchemaStatus($ready),
            builder: new ContextBundleBuilder(new DateTimeZone('UTC')),
        );
    }

    private static function session(): array
    {
        return ['authUserID' => self::USER, 'authUser' => 'dr_smith', 'pid' => self::PID];
    }

    private static function record(int $id): array
    {
        return ['document_id' => $id, 'pid' => self::PID, 'content_sha256' => hash('sha256', (string) $id), 'doc_type' => 'lab_pdf', 'status' => 'extracted', 'prompt_version' => 'v', 'attempts' => 1, 'last_error_code' => null, 'identity_check' => 'match', 'extraction_json' => '{"document_id":' . $id . ',"results":[]}', 'created_at' => '', 'updated_at' => ''];
    }

    public function testStoredExtractionOfAnInaccessibleDocumentIsNotSent(): void
    {
        $repo = new FakeProcessingRepository();
        $repo->records[5] = self::record(5);
        $repo->records[6] = self::record(6);
        $documents = new FakeDocumentReader([]);
        $documents->denied = [6];
        $agent = new FakeAgentClient();

        $this->controller($documents, $agent, $repo, true)->handleForSession(self::session());

        self::assertSame([5], array_column($agent->documentPosts[0]['request']['documents'], 'document_id'));
        self::assertContains('dr_smith', $documents->checkedFor);
    }

    public function testOnlyInaccessibleStoredExtractionsIsDegraded(): void
    {
        $repo = new FakeProcessingRepository();
        $repo->records[6] = self::record(6);
        $documents = new FakeDocumentReader([]);
        $documents->denied = [6];
        $agent = new FakeAgentClient();

        $result = $this->controller($documents, $agent, $repo, true)->handleForSession(self::session());

        self::assertSame('no_extracted_documents', $result['body']['degraded_reason']);
        self::assertSame([], $agent->documentPosts);
    }

    public function testLegacyPathChecksAccessForTheSessionUser(): void
    {
        $documents = new FakeDocumentReader([self::PID => ['document_id' => 9, 'media_type' => 'application/pdf', 'bytes' => 'b']]);
        $documents->denied = [9];
        $agent = new FakeAgentClient();

        $result = $this->controller($documents, $agent, null, false)->handleForSession(self::session());

        self::assertSame(['dr_smith'], $documents->checkedFor);
        self::assertSame('no_document_on_file', $result['body']['degraded_reason']);
        self::assertSame([], $agent->documentPosts);
    }
}
