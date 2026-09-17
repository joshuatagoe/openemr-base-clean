<?php

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Tests;

use DateTimeZone;
use OpenEMR\Modules\Copilot\Authorization\CopilotAuthorizer;
use OpenEMR\Modules\Copilot\ContextBundleBuilder;
use OpenEMR\Modules\Copilot\Controller\ContextController;
use OpenEMR\Modules\Copilot\Data\SourceUnavailableException;
use PHPUnit\Framework\TestCase;

/**
 * Authorization path, as-of derivation, error mapping, response headers and
 * clinical-content hygiene of the context endpoint, with fakes for ACL,
 * relationships, reader, logger, audit and clock.
 */
final class ContextControllerTest extends TestCase
{
    private const PID = 42;
    private const OTHER_PID = 77;
    private const USER = 1;
    private const PLAN = 'Continue metformin. Repeat HbA1c in three months.';
    private const SERVER_NOW = '2026-09-17 10:00:00';

    private const FULL_ACL = ['patients/demo' => true, 'encounters/notes' => true, 'patients/lab' => true];

    /** @var list<array{form_soap_id:int, encounter:int, note_date:string, plan:string}> */
    private const NOTES = [
        ['form_soap_id' => 1001, 'encounter' => 501, 'note_date' => '2026-06-10 14:30:00', 'plan' => self::PLAN],
        ['form_soap_id' => 1002, 'encounter' => 502, 'note_date' => '2026-08-01 09:00:00', 'plan' => '   '], // newer, blank plan
        ['form_soap_id' => 1003, 'encounter' => 503, 'note_date' => '2026-09-17 09:30:00', 'plan' => 'Today plan.'], // current visit
    ];

    private CapturingLogger $logger;
    private AuditCapture $audit;

    protected function setUp(): void
    {
        $this->logger = new CapturingLogger();
        $this->audit = new AuditCapture();
    }

    /** @return array<string,mixed> */
    private function session(int|string|null $pid = self::PID, ?int $user = self::USER, ?string $username = 'drsmith', int|string|null $encounter = null): array
    {
        return ['authUserID' => $user, 'authUser' => $username, 'pid' => $pid, 'encounter' => $encounter];
    }

    /**
     * @param list<array{result_id:int, test_name:string, value:string, units:string, abnormal:string, result_status:string, observed_at:string}>|null $labs
     * @param list<array{form_soap_id:int, encounter:int, note_date:string, plan:string}> $notes
     * @param array<string,string> $encounterDates
     */
    private function reader(?array $labs = null, bool $notesFail = false, array $notes = self::NOTES, array $encounterDates = []): FakeReader
    {
        $labsByPid = [];
        if ($labs !== null) {
            $labsByPid[self::PID] = $labs;
        }
        return new FakeReader(
            patients: [
                self::PID => ['pid' => self::PID, 'uuid' => '3b9d2c1e-8f7a-4b6c-9d0e-1f2a3b4c5d6e'],
                self::OTHER_PID => ['pid' => self::OTHER_PID, 'uuid' => 'aaaaaaaa-0000-4000-8000-000000000077'],
            ],
            notes: [self::PID => $notes, self::OTHER_PID => $notes],
            labs: $labsByPid,
            notesFailure: $notesFail ? new SourceUnavailableException('soap_notes') : null,
            encounterDates: $encounterDates,
        );
    }

    /**
     * @param array<string,bool> $acl
     * @param array<string,string> $bases
     */
    private function controller(FakeReader $reader, array $acl = self::FULL_ACL, array $bases = [self::USER . ':' . self::PID => 'primary_provider'], bool $override = false): ContextController
    {
        return new ContextController(
            new CopilotAuthorizer(new FakeAcl($acl), new FakeRelationships($bases), $override),
            $reader,
            new ContextBundleBuilder(new DateTimeZone('UTC')),
            $this->logger,
            $this->audit,
            static fn(): string => self::SERVER_NOW,
        );
    }

    /**
     * @param array{status:int, body:array<string,mixed>, headers:array<string,string>} $result
     * @return array<string,mixed>
     */
    private static function detail(array $result): array
    {
        $detail = $result['body']['detail'] ?? null;
        self::assertIsArray($detail);
        /** @var array<string,mixed> $detail */
        return $detail;
    }

    /**
     * @param array{status:int, body:array<string,mixed>, headers:array<string,string>} $result
     * @return array{
     *   schema_version:string, correlation_id:string, patient_uuid:string,
     *   prior_note:array{note_id:string, encounter_id:string, note_date:string, plan_text:string},
     *   data_quality:array{sources_unavailable:list<string>, duplicates_collapsed:int},
     *   lab_results:list<array<string,mixed>>
     * }
     */
    private static function context(array $result): array
    {
        $context = $result['body']['context'] ?? null;
        self::assertIsArray($context);
        /** @var array{
         *   schema_version:string, correlation_id:string, patient_uuid:string,
         *   prior_note:array{note_id:string, encounter_id:string, note_date:string, plan_text:string},
         *   data_quality:array{sources_unavailable:list<string>, duplicates_collapsed:int},
         *   lab_results:list<array<string,mixed>>
         * } $context */
        return $context;
    }

    /** @return array{result_id:int, test_name:string, value:string, units:string, abnormal:string, result_status:string, observed_at:string} */
    private static function hba1c(): array
    {
        return ['result_id' => 9001, 'test_name' => 'Hemoglobin A1c', 'value' => '8.9', 'units' => '%', 'abnormal' => 'high', 'result_status' => 'final', 'observed_at' => '2026-09-12 09:15:00'];
    }

    // ------------------------------------------------------------------ //
    // Authorized path and baseline selection
    // ------------------------------------------------------------------ //

    public function testAuthorizedPatientProducesBundleWithLaterFinalHba1c(): void
    {
        // Tracer bullet: only the prior visit's note exists; its plan is the baseline and the later final HbA1c is carried.
        $result = $this->controller($this->reader([self::hba1c()], notes: [self::NOTES[0]]))->handleForSession($this->session());

        self::assertSame(200, $result['status']);
        $ctx = self::context($result);
        self::assertSame('3b9d2c1e-8f7a-4b6c-9d0e-1f2a3b4c5d6e', $ctx['patient_uuid']);
        self::assertSame(self::PLAN, $ctx['prior_note']['plan_text']);
        self::assertSame('form_soap:1001', $ctx['prior_note']['note_id']);
        self::assertCount(1, $ctx['lab_results']);
        self::assertSame('procedure_result:9001', $ctx['lab_results'][0]['result_id']);
        self::assertSame('final', $ctx['lab_results'][0]['status']);
        self::assertSame($ctx['correlation_id'], $result['headers'][ContextController::CORRELATION_HEADER]);
        self::assertMatchesRegularExpression('/^[0-9a-f-]{36}$/', $ctx['correlation_id']);

        self::assertCount(1, $this->audit->events);
        self::assertTrue($this->audit->events[0]['success']);
        self::assertSame(self::PID, $this->audit->events[0]['pid']);
        self::assertStringContainsString('basis=primary_provider', $this->audit->events[0]['comment']);
    }

    public function testLatestNonBlankPlanBeforeServerTimeIsSelectedWithoutLookbackLimit(): void
    {
        // Guards: a newer blank-plan note is skipped; the current-day note (before server time) is the latest non-blank plan.
        $reader = $this->reader([]);
        $result = $this->controller($reader)->handleForSession($this->session());
        self::assertSame(200, $result['status']);
        self::assertSame('form_soap:1003', self::context($result)['prior_note']['note_id']);
        self::assertSame([self::SERVER_NOW], $reader->asOfSeen);
        self::assertStringContainsString('as_of=server_time', $this->audit->events[0]['comment']);
    }

    public function testCurrentEncounterTimestampBoundsBaselineSelection(): void
    {
        // Guards: with the current visit open, its own note is excluded and the prior visit's plan is the baseline.
        $reader = $this->reader([], encounterDates: [self::PID . ':503' => '2026-09-17 09:00:00']);
        $result = $this->controller($reader)->handleForSession($this->session(encounter: 503));
        self::assertSame(200, $result['status']);
        self::assertSame('form_soap:1001', self::context($result)['prior_note']['note_id']);
        self::assertSame(['2026-09-17 09:00:00'], $reader->asOfSeen);
        self::assertStringContainsString('as_of=current_encounter', $this->audit->events[0]['comment']);
    }

    public function testEncounterOfAnotherPatientDoesNotSetAsOf(): void
    {
        // Guards: the session encounter must belong to the selected patient, otherwise server time is used.
        $reader = $this->reader([], encounterDates: [self::OTHER_PID . ':503' => '2026-09-17 09:00:00']);
        $result = $this->controller($reader)->handleForSession($this->session(encounter: 503));
        self::assertSame(200, $result['status']);
        self::assertSame([self::SERVER_NOW], $reader->asOfSeen);
        self::assertStringContainsString('as_of=server_time', $this->audit->events[0]['comment']);
    }

    public function testAsOfIsNeverTakenFromRequestValues(): void
    {
        // Guards: only session keys are read; extra request-shaped keys are ignored.
        $reader = $this->reader([]);
        $session = $this->session() + ['as_of' => '2020-01-01 00:00:00', 'pid_override' => self::OTHER_PID];
        $this->controller($reader)->handleForSession($session);
        self::assertSame([self::SERVER_NOW], $reader->asOfSeen);
        self::assertSame([self::PID], $reader->requestedPids);
    }

    public function testNoResultsYieldsEmptyAvailableList(): void
    {
        $result = $this->controller($this->reader([]))->handleForSession($this->session());
        self::assertSame(200, $result['status']);
        self::assertSame([], self::context($result)['lab_results']);
        self::assertSame([], self::context($result)['data_quality']['sources_unavailable']);
    }

    // ------------------------------------------------------------------ //
    // Response headers
    // ------------------------------------------------------------------ //

    public function testClinicalResponsesCarryNoStoreAndNosniffHeaders(): void
    {
        $ok = $this->controller($this->reader([self::hba1c()]))->handleForSession($this->session());
        $denied = $this->controller($this->reader([]), self::FULL_ACL, [])->handleForSession($this->session());
        foreach ([$ok, $denied] as $result) {
            self::assertSame('no-store, private', $result['headers']['Cache-Control']);
            self::assertSame('nosniff', $result['headers']['X-Content-Type-Options']);
            self::assertArrayHasKey(ContextController::CORRELATION_HEADER, $result['headers']);
        }
    }

    // ------------------------------------------------------------------ //
    // Authorization failures
    // ------------------------------------------------------------------ //

    public function testMissingPatientContextIsRejectedBeforeAnyRead(): void
    {
        foreach ([null, '', 0, 'abc'] as $pid) {
            $reader = $this->reader([self::hba1c()]);
            $result = $this->controller($reader)->handleForSession($this->session($pid));
            self::assertSame(409, $result['status'], var_export($pid, true));
            self::assertSame('no_patient_selected', self::detail($result)['code']);
            self::assertSame([], $reader->requestedPids);
        }
    }

    public function testUnauthenticatedIsRejectedBeforeAnyRead(): void
    {
        $reader = $this->reader([self::hba1c()]);
        $result = $this->controller($reader)->handleForSession($this->session(self::PID, null, null));
        self::assertSame(401, $result['status']);
        self::assertSame([], $reader->requestedPids);
        self::assertSame([], $this->audit->events);
    }

    public function testAclDeniedIsForbidden(): void
    {
        $reader = $this->reader([self::hba1c()]);
        $acl = self::FULL_ACL;
        $acl['patients/lab'] = false;
        $result = $this->controller($reader, $acl)->handleForSession($this->session());
        self::assertSame(403, $result['status']);
        self::assertSame('acl_denied', self::detail($result)['code']);
        self::assertSame([], $reader->requestedPids);
        self::assertFalse($this->audit->events[0]['success']);
    }

    public function testNoCareRelationshipIsForbiddenUnlessAuditedAdminOverride(): void
    {
        $reader = $this->reader([self::hba1c()]);
        $result = $this->controller($reader, self::FULL_ACL, [])->handleForSession($this->session());
        self::assertSame(403, $result['status']);
        self::assertSame('no_care_relationship', self::detail($result)['code']);
        self::assertSame([], $reader->requestedPids);

        // Override on but user is not admin/super -> still denied.
        $result = $this->controller($reader, self::FULL_ACL, [], true)->handleForSession($this->session());
        self::assertSame(403, $result['status']);

        // Override on and admin/super -> allowed, audited as admin_override.
        $acl = self::FULL_ACL + ['admin/super' => true];
        $result = $this->controller($this->reader([self::hba1c()]), $acl, [], true)->handleForSession($this->session());
        self::assertSame(200, $result['status']);
        $last = end($this->audit->events);
        self::assertNotFalse($last);
        self::assertStringContainsString('basis=admin_override', $last['comment']);
    }

    public function testCrossPatientAccessIsPreventedBySessionBinding(): void
    {
        // Guards: the only patient ever read is the session's; a relationship with patient A never yields patient B.
        $reader = $this->reader([self::hba1c()]);
        $bases = [self::USER . ':' . self::PID => 'encounter_provider']; // related to 42 only
        $result = $this->controller($reader, self::FULL_ACL, $bases)->handleForSession($this->session(self::OTHER_PID));
        self::assertSame(403, $result['status']);
        self::assertSame('no_care_relationship', self::detail($result)['code']);
        self::assertSame([], $reader->requestedPids);

        // With the session on the related patient, only that pid is read.
        $result = $this->controller($reader, self::FULL_ACL, $bases)->handleForSession($this->session(self::PID));
        self::assertSame(200, $result['status']);
        self::assertSame([self::PID], $reader->requestedPids);
    }

    // ------------------------------------------------------------------ //
    // Data failures
    // ------------------------------------------------------------------ //

    public function testNoPriorNoteIsExplicit(): void
    {
        $result = $this->controller($this->reader([self::hba1c()], notes: []))->handleForSession($this->session());
        self::assertSame(404, $result['status']);
        self::assertSame('no_prior_note', self::detail($result)['code']);
        self::assertStringContainsString('outcome=no_prior_note', $this->audit->events[0]['comment']);
    }

    public function testOnlyBlankPlansIsNoPriorNote(): void
    {
        $blank = [['form_soap_id' => 1, 'encounter' => 1, 'note_date' => '2026-06-10 14:30:00', 'plan' => '  ']];
        $result = $this->controller($this->reader([], notes: $blank))->handleForSession($this->session());
        self::assertSame(404, $result['status']);
    }

    public function testLabSourceFailureIsDeclaredNotEmptied(): void
    {
        $reader = new FakeReader(
            patients: [self::PID => ['pid' => self::PID, 'uuid' => '3b9d2c1e-8f7a-4b6c-9d0e-1f2a3b4c5d6e']],
            notes: [self::PID => self::NOTES],
            labs: [self::PID => new SourceUnavailableException('lab_results')],
        );
        $result = $this->controller($reader)->handleForSession($this->session());
        self::assertSame(200, $result['status']);
        self::assertSame(['lab_results'], self::context($result)['data_quality']['sources_unavailable']);
        self::assertSame([], self::context($result)['lab_results']);
    }

    public function testNoteSourceFailureIs503(): void
    {
        $result = $this->controller($this->reader([self::hba1c()], notesFail: true))->handleForSession($this->session());
        self::assertSame(503, $result['status']);
        self::assertSame('source_unavailable', self::detail($result)['code']);
    }

    // ------------------------------------------------------------------ //
    // Content hygiene
    // ------------------------------------------------------------------ //

    public function testNoClinicalContentInLogsAuditRowsOrErrorResponses(): void
    {
        // Operational logs and error bodies carry codes, counts and ids only.
        // The access audit row intentionally records user and patient identifiers - never clinical content.
        $labs = [self::hba1c()];
        $this->controller($this->reader($labs))->handleForSession($this->session());
        $denied = $this->controller($this->reader($labs), self::FULL_ACL, [])->handleForSession($this->session());
        $failed = $this->controller($this->reader($labs, notesFail: true))->handleForSession($this->session());

        $clinical = ['metformin', 'HbA1c', 'Hemoglobin', '8.9', '%', 'Today plan', '3b9d2c1e'];

        $logsAndErrors = $this->logger->dump()
            . json_encode($denied['body'], JSON_THROW_ON_ERROR)
            . json_encode($failed['body'], JSON_THROW_ON_ERROR);
        foreach ([...$clinical, 'drsmith'] as $needle) {
            self::assertStringNotContainsString($needle, $logsAndErrors, "found '{$needle}' in operational logs or error bodies");
        }

        $auditText = json_encode($this->audit->events, JSON_THROW_ON_ERROR);
        self::assertStringContainsString('drsmith', $auditText); // user identifier: by design
        self::assertStringContainsString('"pid":' . self::PID, $auditText); // patient identifier: by design
        foreach ($clinical as $needle) {
            self::assertStringNotContainsString($needle, $auditText, "found clinical content '{$needle}' in an audit comment");
        }

        foreach ([$denied, $failed] as $r) {
            self::assertSame(['code', 'message', 'correlation_id'], array_keys(self::detail($r)));
        }
    }
}
