<?php

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Tests;

use DateTimeZone;
use OpenEMR\Modules\Copilot\Agent\AgentUnavailableException;
use OpenEMR\Modules\Copilot\Authorization\CopilotAuthorizer;
use OpenEMR\Modules\Copilot\Config\CopilotConfig;
use OpenEMR\Modules\Copilot\ContextBundleBuilder;
use OpenEMR\Modules\Copilot\Controller\BriefingTicketController;
use OpenEMR\Modules\Copilot\Data\SourceUnavailableException;
use PHPUnit\Framework\TestCase;

/**
 * Authorization path, as-of derivation, agent hand-off, ticket minting,
 * degradation, error mapping, response headers and clinical-content hygiene
 * of the briefing-ticket endpoint, with fakes for ACL, relationships, reader,
 * agent, logger, audit and clocks.
 */
final class BriefingTicketControllerTest extends TestCase
{
    private const PID = 42;
    private const OTHER_PID = 77;
    private const USER = 1;
    private const USER_UUID = '9c3f0a2b-1d4e-4f5a-8b6c-7d8e9f0a1b2c';
    private const PUUID = '3b9d2c1e-8f7a-4b6c-9d0e-1f2a3b4c5d6e';
    private const PLAN = 'Continue metformin. Repeat HbA1c in three months.';
    private const SERVER_NOW = '2026-09-17 10:00:00';
    private const UNIX_NOW = 1758100000;
    private const SECRET = 'test-only-shared-secret-0123456789abcdef';
    private const AGENT_URL = 'http://agent.test:8000/';

    private const FULL_ACL = ['patients/demo' => true, 'encounters/auth_a' => true, 'encounters/notes' => true, 'patients/med' => true, 'patients/lab' => true, 'patients/appt' => true];

    /** @var list<array{form_soap_id:int, encounter:int, note_date:string, plan:string}> */
    private const NOTES = [
        ['form_soap_id' => 1001, 'encounter' => 501, 'note_date' => '2026-06-10 14:30:00', 'plan' => self::PLAN],
        ['form_soap_id' => 1002, 'encounter' => 502, 'note_date' => '2026-08-01 09:00:00', 'plan' => '   '], // newer, blank plan
        ['form_soap_id' => 1003, 'encounter' => 503, 'note_date' => '2026-09-17 09:30:00', 'plan' => 'Today plan.'], // current visit
    ];

    private CapturingLogger $logger;
    private AuditCapture $audit;
    private FakeAgentClient $agent;

    protected function setUp(): void
    {
        $this->logger = new CapturingLogger();
        $this->audit = new AuditCapture();
        $this->agent = new FakeAgentClient();
    }

    /** @return array<string,mixed> */
    private function session(int|string|null $pid = self::PID, ?int $user = self::USER, ?string $username = 'drsmith', int|string|null $encounter = null): array
    {
        return ['authUserID' => $user, 'authUser' => $username, 'pid' => $pid, 'encounter' => $encounter];
    }

    /**
     * @param list<array{result_id:int, order_id:int, test_name:string, code:string, value:string, units:string, range:string, abnormal:string, result_status:string, observed_at:string}>|null $labs
     * @param list<array{form_soap_id:int, encounter:int, note_date:string, plan:string}> $notes
     * @param array<string,string> $encounterDates
     * @param array<int,string> $userUuids
     */
    private function reader(?array $labs = null, bool $notesFail = false, array $notes = self::NOTES, array $encounterDates = [], array $userUuids = [self::USER => self::USER_UUID]): FakeReader
    {
        $labsByPid = [];
        if ($labs !== null) {
            $labsByPid[self::PID] = $labs;
        }
        return new FakeReader(
            patients: [
                self::PID => ['pid' => self::PID, 'uuid' => self::PUUID],
                self::OTHER_PID => ['pid' => self::OTHER_PID, 'uuid' => 'aaaaaaaa-0000-4000-8000-000000000077'],
            ],
            notes: [self::PID => $notes, self::OTHER_PID => $notes],
            labs: $labsByPid,
            notesFailure: $notesFail ? new SourceUnavailableException('soap_notes') : null,
            encounterDates: $encounterDates,
            userUuids: $userUuids,
        );
    }

    /**
     * @param array<string,bool> $acl
     * @param array<string,string> $bases
     */
    private function controller(
        FakeReader $reader,
        array $acl = self::FULL_ACL,
        array $bases = [self::USER . ':' . self::PID => 'primary_provider'],
        bool $override = false,
        ?FakeAgentClient $agent = null,
        ?CopilotConfig $config = null,
    ): BriefingTicketController {
        return new BriefingTicketController(
            new CopilotAuthorizer(new FakeAcl($acl), new FakeRelationships($bases), $override),
            $reader,
            new ContextBundleBuilder(new DateTimeZone('UTC')),
            $agent ?? $this->agent,
            $config ?? new CopilotConfig(self::AGENT_URL, self::SECRET),
            $this->logger,
            $this->audit,
            static fn(): string => self::SERVER_NOW,
            static fn(): int => self::UNIX_NOW,
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
     * @return array<string,mixed>
     */
    private static function sections(array $result): array
    {
        $sections = $result['body']['sections'] ?? null;
        self::assertIsArray($sections);
        /** @var array<string,mixed> $sections */
        return $sections;
    }

    /**
     * One named section as an array.
     *
     * @param array{status:int, body:array<string,mixed>, headers:array<string,string>} $result
     * @return array<string,mixed>
     */
    private static function section(array $result, string $name): array
    {
        $section = self::sections($result)[$name] ?? null;
        self::assertIsArray($section, "section '{$name}'");
        /** @var array<string,mixed> $section */
        return $section;
    }

    /**
     * @param array{status:int, body:array<string,mixed>, headers:array<string,string>} $result
     * @return list<array<string,mixed>>
     */
    private static function intervalResults(array $result): array
    {
        $rows = [];
        foreach (self::section($result, 'interval_results') as $row) {
            self::assertIsArray($row);
            /** @var array<string,mixed> $row */
            $rows[] = $row;
        }
        return $rows;
    }

    /**
     * @param array{status:int, body:array<string,mixed>, headers:array<string,string>} $result
     * @return list<array<string,mixed>>
     */
    private static function intervalOrders(array $result): array
    {
        $rows = [];
        foreach (self::section($result, 'interval_orders') as $row) {
            self::assertIsArray($row);
            /** @var array<string,mixed> $row */
            $rows[] = $row;
        }
        return $rows;
    }

    /**
     * Decode and verify a ticket the way the agent does (HS256 over header.payload).
     *
     * @return array<string,mixed>
     */
    private static function claims(string $ticket): array
    {
        $parts = explode('.', $ticket);
        self::assertCount(3, $parts);
        $expected = rtrim(strtr(base64_encode(hash_hmac('sha256', $parts[0] . '.' . $parts[1], self::SECRET, true)), '+/', '-_'), '=');
        self::assertTrue(hash_equals($expected, $parts[2]), 'ticket signature must verify with the shared secret');
        $payload = base64_decode(strtr($parts[1], '-_', '+/') . str_repeat('=', (4 - strlen($parts[1]) % 4) % 4), true);
        self::assertIsString($payload);
        $claims = json_decode($payload, true, 4, JSON_THROW_ON_ERROR);
        self::assertIsArray($claims);
        /** @var array<string,mixed> $claims */
        return $claims;
    }

    /** @return array{result_id:int, order_id:int, test_name:string, code:string, value:string, units:string, range:string, abnormal:string, result_status:string, observed_at:string} */
    private static function hba1c(): array
    {
        return ['result_id' => 9001, 'order_id' => 0, 'test_name' => 'Hemoglobin A1c', 'code' => '', 'value' => '8.9', 'units' => '%', 'range' => '', 'abnormal' => 'high', 'result_status' => 'final', 'observed_at' => '2026-09-12 09:15:00'];
    }

    // ------------------------------------------------------------------ //
    // Authorized path: bundle to agent, ticket to panel
    // ------------------------------------------------------------------ //

    public function testAuthorizedPatientHandsBundleToAgentAndReturnsBoundTicket(): void
    {
        // Tracer bullet: the prior note's plan and the later final HbA1c go to the agent; the panel gets a ticket and sections.
        $result = $this->controller($this->reader([self::hba1c()], notes: [self::NOTES[0]]))->handleForSession($this->session());

        self::assertSame(200, $result['status']);
        $body = $result['body'];
        self::assertSame('1.0', $body['schema_version']);
        self::assertSame(self::PUUID, $body['patient_uuid']);
        self::assertSame('http://agent.test:8000', $body['agent_url']);
        self::assertSame('e6b2abe5-8664-4ebb-bf81-e3d5cca35df4', $body['bundle_id']);
        self::assertNull($body['degraded']);
        $cid = $body['correlation_id'];
        self::assertIsString($cid);
        self::assertMatchesRegularExpression('/^[0-9a-f-]{36}$/', $cid);
        self::assertSame($cid, $result['headers'][BriefingTicketController::CORRELATION_HEADER]);

        // The agent received the full bundle, bound to the same cid and user.
        self::assertCount(1, $this->agent->posts);
        $bundle = $this->agent->posts[0]['bundle'];
        self::assertSame($cid, $this->agent->posts[0]['cid']);
        self::assertSame($cid, $bundle['correlation_id']);
        self::assertSame(self::PUUID, $bundle['patient_uuid']);
        self::assertSame(self::USER_UUID, $bundle['user_uuid']);
        self::assertIsArray($bundle['prior_note']);
        self::assertSame(self::PLAN, $bundle['prior_note']['plan_text']);
        self::assertSame('form_soap:1001', $bundle['prior_note']['note_id']);
        self::assertIsArray($bundle['lab_results']);
        self::assertCount(1, $bundle['lab_results']);

        // The ticket binds user, patient, bundle and cid, single-use, 120 s.
        $ticket = $body['ticket'];
        self::assertIsString($ticket);
        $claims = self::claims($ticket);
        self::assertSame(self::USER_UUID, $claims['sub']);
        self::assertSame(self::PUUID, $claims['puuid']);
        self::assertSame('e6b2abe5-8664-4ebb-bf81-e3d5cca35df4', $claims['bundle_id']);
        self::assertSame($cid, $claims['cid']);
        self::assertIsString($claims['jti']);
        self::assertMatchesRegularExpression('/^[0-9a-f-]{36}$/', $claims['jti']);
        self::assertSame(self::UNIX_NOW, $claims['iat']);
        self::assertSame(self::UNIX_NOW + 120, $claims['exp']);
        self::assertSame(['sub', 'puuid', 'bundle_id', 'cid', 'jti', 'iat', 'exp'], array_keys($claims));
        self::assertSame(gmdate('Y-m-d\TH:i:s\Z', self::UNIX_NOW + 120), $body['ticket_expires_at']);

        // Deterministic sections: identity, reasons, baseline, window, interval results/orders, allergies, footer - no plan text.
        $sections = self::sections($result);
        self::assertSame(['identity', 'reasons', 'baseline', 'window', 'interval_results', 'interval_orders', 'medication_changes', 'allergies', 'footer'], array_keys($sections));
        self::assertSame([], $sections['medication_changes']);
        self::assertSame([], $bundle['medications']);
        self::assertSame([], $bundle['allergies']);
        self::assertSame(['name' => 'Evelyn Demo', 'dob' => '1958-04-12', 'sex' => 'Female', 'pubpid' => 'P-7'], $sections['identity']);
        self::assertSame(['scheduled' => null, 'encounter' => null], $sections['reasons']);
        self::assertSame([], self::intervalOrders($result));
        self::assertSame(['entries' => [], 'statement' => 'no allergy entries on file (not confirmed NKA)'], $sections['allergies']);
        self::assertSame(['note_id' => 'form_soap:1001', 'encounter_id' => 'form_encounter:501', 'note_date' => '2026-06-10T14:30:00Z'], $sections['baseline']);
        self::assertSame(['start' => '2026-06-10T14:30:00Z', 'end' => '2026-09-17T10:00:00Z', 'as_of_source' => 'server_time'], $sections['window']);
        self::assertCount(1, self::intervalResults($result));
        self::assertSame('procedure_result:9001', self::intervalResults($result)[0]['result_id']);
        self::assertSame(['sources_unavailable' => [], 'results_in_window' => 1, 'orders_in_window' => 0, 'medication_changes_in_window' => 0, 'medications_on_file' => 0, 'duplicates_collapsed' => 0, 'omitted' => ['empty_test_name' => 0, 'non_numeric_value' => 0, 'unmapped_status' => 0, 'unmapped_abnormal_flag' => 0, 'bad_timestamp' => 0, 'orders_omitted' => 0, 'medications_omitted' => 0]], $sections['footer']);
        self::assertStringNotContainsString('metformin', json_encode($body, JSON_THROW_ON_ERROR));
        // Identity is panel-only: the bundle sent to the agent never carries it.
        self::assertStringNotContainsString('Evelyn', json_encode($bundle, JSON_THROW_ON_ERROR));
        self::assertStringNotContainsString('1958', json_encode($bundle, JSON_THROW_ON_ERROR));

        self::assertCount(1, $this->audit->events);
        self::assertTrue($this->audit->events[0]['success']);
        self::assertSame(self::PID, $this->audit->events[0]['pid']);
        self::assertStringContainsString('basis=primary_provider', $this->audit->events[0]['comment']);
        self::assertStringContainsString('agent=accepted', $this->audit->events[0]['comment']);
    }

    public function testTicketsAreSingleUseByConstruction(): void
    {
        // Two briefings for the same patient mint distinct jti values.
        $a = $this->controller($this->reader([]))->handleForSession($this->session());
        $b = $this->controller($this->reader([]))->handleForSession($this->session());
        $ticketA = $a['body']['ticket'];
        $ticketB = $b['body']['ticket'];
        self::assertIsString($ticketA);
        self::assertIsString($ticketB);
        self::assertNotSame(self::claims($ticketA)['jti'], self::claims($ticketB)['jti']);
        self::assertNotSame($a['body']['correlation_id'], $b['body']['correlation_id']);
    }

    public function testUserWithoutUuidStillGetsATicketWithOpaqueSubjectAndNoUserBindingInBundle(): void
    {
        $result = $this->controller($this->reader([], userUuids: []))->handleForSession($this->session());
        self::assertSame(200, $result['status']);
        $ticket = $result['body']['ticket'];
        self::assertIsString($ticket);
        self::assertSame('user:1', self::claims($ticket)['sub']);
        self::assertArrayNotHasKey('user_uuid', $this->agent->posts[0]['bundle']);
    }

    // ------------------------------------------------------------------ //
    // Degradation: agent unavailable
    // ------------------------------------------------------------------ //

    public function testAgentUnavailableDegradesExplicitlyWithSectionsAndNoTicket(): void
    {
        $agent = new FakeAgentClient(new AgentUnavailableException(AgentUnavailableException::REASON_TIMEOUT));
        $result = $this->controller($this->reader([self::hba1c()]), agent: $agent)->handleForSession($this->session());

        self::assertSame(200, $result['status']);
        $body = $result['body'];
        self::assertNull($body['ticket']);
        self::assertNull($body['bundle_id']);
        self::assertNull($body['ticket_expires_at']);
        self::assertSame(['reason_code' => 'agent_unavailable', 'detail_code' => 'agent_timeout'], $body['degraded']);
        self::assertIsArray($body['sections']);
        self::assertCount(1, self::intervalResults($result));
        self::assertStringContainsString('agent=agent_timeout', $this->audit->events[0]['comment']);
        self::assertStringContainsString('agent hand-off failed', $this->logger->dump());
    }

    public function testUnconfiguredModuleDegradesWithoutContactingTheAgent(): void
    {
        $agent = new FakeAgentClient(new AgentUnavailableException(AgentUnavailableException::REASON_NOT_CONFIGURED));
        $config = new CopilotConfig(null, null);
        $result = $this->controller($this->reader([]), agent: $agent, config: $config)->handleForSession($this->session());
        self::assertSame(200, $result['status']);
        self::assertNull($result['body']['ticket']);
        self::assertNull($result['body']['agent_url']);
        self::assertSame('agent_not_configured', self::degraded($result)['detail_code']);
    }

    /**
     * @param array{status:int, body:array<string,mixed>, headers:array<string,string>} $result
     * @return array<string,mixed>
     */
    private static function degraded(array $result): array
    {
        $degraded = $result['body']['degraded'] ?? null;
        self::assertIsArray($degraded);
        /** @var array<string,mixed> $degraded */
        return $degraded;
    }

    // ------------------------------------------------------------------ //
    // Baseline selection and as-of
    // ------------------------------------------------------------------ //

    public function testLatestNonBlankPlanBeforeServerTimeIsSelectedWithoutLookbackLimit(): void
    {
        // Guards: a newer blank-plan note is skipped; the current-day note (before server time) is the latest non-blank plan.
        $reader = $this->reader([]);
        $result = $this->controller($reader)->handleForSession($this->session());
        self::assertSame(200, $result['status']);
        self::assertSame('form_soap:1003', self::section($result, 'baseline')['note_id']);
        self::assertSame([self::SERVER_NOW], $reader->asOfSeen);
        self::assertStringContainsString('as_of=server_time', $this->audit->events[0]['comment']);
    }

    public function testCurrentEncounterTimestampBoundsBaselineSelection(): void
    {
        // Guards: with the current visit open, its own note is excluded and the prior visit's plan is the baseline.
        $reader = $this->reader([], encounterDates: [self::PID . ':503' => '2026-09-17 09:00:00']);
        $result = $this->controller($reader)->handleForSession($this->session(encounter: 503));
        self::assertSame(200, $result['status']);
        self::assertSame('form_soap:1001', self::section($result, 'baseline')['note_id']);
        self::assertSame('2026-09-17T09:00:00Z', self::section($result, 'window')['end']);
        self::assertSame('current_encounter', self::section($result, 'window')['as_of_source']);
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
        self::assertSame([], self::intervalResults($result));
        self::assertSame([], self::section($result, 'footer')['sources_unavailable']);
        self::assertSame([], $this->agent->posts[0]['bundle']['lab_results']);
    }

    // ------------------------------------------------------------------ //
    // Panel staleness guard
    // ------------------------------------------------------------------ //

    public function testRequestedPidMustMatchSessionPatient(): void
    {
        // The panel's pid is a staleness check, never a binding: a mismatch is refused and audited; no read happens.
        $reader = $this->reader([self::hba1c()]);
        $result = $this->controller($reader)->handleForSession($this->session(), self::OTHER_PID);
        self::assertSame(409, $result['status']);
        self::assertSame('patient_mismatch', self::detail($result)['code']);
        self::assertSame([], $reader->requestedPids);
        self::assertSame([], $this->agent->posts);
        self::assertFalse($this->audit->events[0]['success']);
        self::assertStringContainsString('code=patient_mismatch', $this->audit->events[0]['comment']);

        $result = $this->controller($this->reader([self::hba1c()]))->handleForSession($this->session(), self::PID);
        self::assertSame(200, $result['status']);
    }

    // ------------------------------------------------------------------ //
    // Response headers
    // ------------------------------------------------------------------ //

    public function testResponsesCarryNoStoreAndNosniffHeaders(): void
    {
        $ok = $this->controller($this->reader([self::hba1c()]))->handleForSession($this->session());
        $denied = $this->controller($this->reader([]), self::FULL_ACL, [])->handleForSession($this->session());
        foreach ([$ok, $denied] as $result) {
            self::assertSame('no-store, private', $result['headers']['Cache-Control']);
            self::assertSame('nosniff', $result['headers']['X-Content-Type-Options']);
            self::assertArrayHasKey(BriefingTicketController::CORRELATION_HEADER, $result['headers']);
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
        self::assertSame([], $this->agent->posts);
    }

    public function testUnauthenticatedIsRejectedBeforeAnyRead(): void
    {
        $reader = $this->reader([self::hba1c()]);
        $result = $this->controller($reader)->handleForSession($this->session(self::PID, null, null));
        self::assertSame(401, $result['status']);
        self::assertSame([], $reader->requestedPids);
        self::assertSame([], $this->audit->events);
        self::assertSame([], $this->agent->posts);
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
        self::assertSame([], $this->agent->posts);
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
        self::assertSame([], $this->agent->posts);

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
        self::assertSame([], $this->agent->posts);

        // With the session on the related patient, only that pid is read and only that patient's uuid is bound.
        $result = $this->controller($reader, self::FULL_ACL, $bases)->handleForSession($this->session(self::PID));
        self::assertSame(200, $result['status']);
        self::assertSame([self::PID], $reader->requestedPids);
        $ticket = $result['body']['ticket'];
        self::assertIsString($ticket);
        self::assertSame(self::PUUID, self::claims($ticket)['puuid']);
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
        self::assertSame([], $this->agent->posts);
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
            patients: [self::PID => ['pid' => self::PID, 'uuid' => self::PUUID]],
            notes: [self::PID => self::NOTES],
            labs: [self::PID => new SourceUnavailableException('lab_results')],
        );
        $result = $this->controller($reader)->handleForSession($this->session());
        self::assertSame(200, $result['status']);
        self::assertSame(['lab_results'], self::section($result, 'footer')['sources_unavailable']);
        self::assertSame([], self::intervalResults($result));
        $quality = $this->agent->posts[0]['bundle']['data_quality'];
        self::assertIsArray($quality);
        self::assertSame(['lab_results'], $quality['sources_unavailable']);
    }

    public function testNoteSourceFailureIs503(): void
    {
        $result = $this->controller($this->reader([self::hba1c()], notesFail: true))->handleForSession($this->session());
        self::assertSame(503, $result['status']);
        self::assertSame('source_unavailable', self::detail($result)['code']);
        self::assertSame([], $this->agent->posts);
    }

    // ------------------------------------------------------------------ //
    // Deterministic sections: reasons, orders, allergies, panel-only source failures
    // ------------------------------------------------------------------ //

    public function testReasonsAreVerbatimWithProvenanceAndAppointmentIsLookedUpForTheAsOfDate(): void
    {
        $reader = $this->reader([]);
        $reader->appointment = ['text' => 'Diabetes f/u; review A1c', 'date' => '2026-09-17 10:30:00', 'source' => 'openemr_postcalendar_events'];
        $reader->encounterReason = ['text' => 'Diabetes follow-up', 'date' => '2026-06-10 14:30:00', 'source' => 'form_encounter'];
        $result = $this->controller($reader)->handleForSession($this->session());
        self::assertSame(200, $result['status']);
        self::assertSame([
            'scheduled' => ['text' => 'Diabetes f/u; review A1c', 'provenance' => 'scheduled', 'date' => '2026-09-17 10:30:00'],
            'encounter' => ['text' => 'Diabetes follow-up', 'provenance' => 'baseline_encounter', 'date' => '2026-06-10 14:30:00'],
        ], self::section($result, 'reasons'));
        self::assertSame(['2026-09-17'], $reader->appointmentDatesSeen);
        // Reasons are panel-only in this phase: not in the bundle.
        self::assertStringNotContainsString('review A1c', json_encode($this->agent->posts[0]['bundle'], JSON_THROW_ON_ERROR));
    }

    public function testOrdersAreCarriedInBundleAndSections(): void
    {
        $reader = $this->reader([]);
        $reader->orders = [['order_id' => 12, 'seq' => 1, 'test_name' => 'Lipid Panel', 'code' => '24331-1', 'order_status' => 'pending', 'ordered_at' => '2026-09-14 08:00:00']];
        $result = $this->controller($reader)->handleForSession($this->session());
        self::assertSame(200, $result['status']);
        $expected = ['order_id' => 'procedure_order:12', 'sequence' => 1, 'test_name' => 'Lipid Panel', 'code' => '24331-1', 'status' => 'pending', 'ordered_at' => '2026-09-14T08:00:00Z'];
        self::assertSame([$expected], self::intervalOrders($result));
        self::assertSame([$expected], $this->agent->posts[0]['bundle']['lab_orders']);
        self::assertSame(1, self::section($result, 'footer')['orders_in_window']);
    }

    public function testAllergiesAreRenderedAsRecordedWithCodedFlag(): void
    {
        $reader = $this->reader([]);
        $reader->allergies = [
            ['id' => 5, 'title' => 'Penicillin', 'diagnosis' => '', 'reaction' => 'rash', 'severity' => 'moderate', 'begdate' => '2020-01-01 00:00:00', 'enddate' => '', 'activity' => 1],
            ['id' => 6, 'title' => 'Sulfa', 'diagnosis' => 'RXNORM:10831', 'reaction' => '', 'severity' => '', 'begdate' => '', 'enddate' => '2024-01-01 00:00:00', 'activity' => 0],
        ];
        $result = $this->controller($reader)->handleForSession($this->session());
        $allergies = self::section($result, 'allergies');
        self::assertSame('2 allergy entries as recorded', $allergies['statement']);
        self::assertSame([
            ['record_id' => 'lists:5', 'title' => 'Penicillin', 'coded' => false, 'code' => null, 'reaction' => 'rash', 'severity' => 'moderate', 'active' => true, 'begdate' => '2020-01-01T00:00:00Z', 'enddate' => null, 'duplicate_count' => 1],
            ['record_id' => 'lists:6', 'title' => 'Sulfa', 'coded' => true, 'code' => 'RXNORM:10831', 'reaction' => null, 'severity' => null, 'active' => false, 'begdate' => null, 'enddate' => '2024-01-01T00:00:00Z', 'duplicate_count' => 1],
        ], $allergies['entries']);
        // The same rows go to the agent so follow-up questions can cite them (UC-04 list_allergies).
        self::assertSame($allergies['entries'], $this->agent->posts[0]['bundle']['allergies']);
    }

    public function testMedicationsGoToTheAgentAndOnlyChangesSinceBaselineAreASection(): void
    {
        $reader = $this->reader([], notes: [self::NOTES[0]]); // baseline 2026-06-10
        $reader->medications = [
            ['source_table' => 'prescriptions', 'id' => 31, 'drug' => 'Metformin 500 mg', 'rxnorm' => '861007', 'dosage' => '1 tab BID', 'active' => 1, 'begdate' => '2026-01-15', 'enddate' => '', 'date_added' => '2026-01-15 09:00:00', 'date_modified' => ''],
            ['source_table' => 'prescriptions', 'id' => 50, 'drug' => 'Atorvastatin 20 mg', 'rxnorm' => '', 'dosage' => 'nightly', 'active' => 1, 'begdate' => '2026-06-11', 'enddate' => '', 'date_added' => '2026-06-11 09:00:00', 'date_modified' => ''],
        ];
        $result = $this->controller($reader)->handleForSession($this->session());
        self::assertSame(200, $result['status']);
        $bundleMeds = $this->agent->posts[0]['bundle']['medications'];
        self::assertIsArray($bundleMeds);
        self::assertCount(2, $bundleMeds);
        $changes = array_values(self::section($result, 'medication_changes'));
        self::assertCount(1, $changes);
        $change = $changes[0];
        self::assertIsArray($change);
        self::assertSame('prescriptions:50', $change['record_id']);
        self::assertSame(1, self::section($result, 'footer')['medication_changes_in_window']);
        self::assertSame(2, self::section($result, 'footer')['medications_on_file']);
    }

    public function testDuplicateAllergyEntriesCollapseWithACountAndAreReportedInTheFooter(): void
    {
        // AUDIT DATA-004: same title (case-insensitive) and begdate collapse; a different begdate stays separate.
        $reader = $this->reader([]);
        $reader->allergies = [
            ['id' => 5, 'title' => 'Penicillin', 'diagnosis' => '', 'reaction' => 'rash', 'severity' => '', 'begdate' => '2020-01-01 00:00:00', 'enddate' => '', 'activity' => 1],
            ['id' => 7, 'title' => 'PENICILLIN', 'diagnosis' => '', 'reaction' => 'rash', 'severity' => '', 'begdate' => '2020-01-01 00:00:00', 'enddate' => '', 'activity' => 1],
            ['id' => 8, 'title' => 'Penicillin', 'diagnosis' => '', 'reaction' => '', 'severity' => '', 'begdate' => '2021-05-05 00:00:00', 'enddate' => '', 'activity' => 1],
        ];
        $result = $this->controller($reader)->handleForSession($this->session());
        $entries = self::section($result, 'allergies')['entries'];
        self::assertIsArray($entries);
        self::assertCount(2, $entries);
        self::assertIsArray($entries[0]);
        self::assertSame('lists:5', $entries[0]['record_id']);
        self::assertSame(2, $entries[0]['duplicate_count']);
        self::assertSame(1, self::section($result, 'footer')['duplicates_collapsed']);
    }

    public function testPanelOnlySourceFailuresAreNamedNotEmptied(): void
    {
        $reader = $this->reader([self::hba1c()]);
        $reader->allergies = new SourceUnavailableException('allergies');
        $reader->identity = new SourceUnavailableException('identity');
        $reader->orders = new SourceUnavailableException('lab_orders');
        $reader->medications = new SourceUnavailableException('medications');
        $result = $this->controller($reader)->handleForSession($this->session());
        self::assertSame(200, $result['status']);
        $sections = self::sections($result);
        self::assertNull($sections['allergies']);
        self::assertNull($sections['identity']);
        self::assertSame([], $sections['interval_orders']);
        self::assertSame(['lab_orders', 'identity', 'allergies', 'medications'], self::section($result, 'footer')['sources_unavailable']);
        // The agent is told about every failed evidence source so tools and the matcher never assert absence for them.
        $quality = $this->agent->posts[0]['bundle']['data_quality'];
        self::assertIsArray($quality);
        self::assertSame(['lab_orders', 'medications', 'allergies'], $quality['sources_unavailable']);
        self::assertCount(1, self::intervalResults($result));
    }

    // ------------------------------------------------------------------ //
    // Ticket refresh for follow-up turns
    // ------------------------------------------------------------------ //

    public function testRefreshMintsATicketForTheGivenBundleWithoutReadingTheChartOrPostingABundle(): void
    {
        $reader = $this->reader([self::hba1c()]);
        $bundleId = 'e6b2abe5-8664-4ebb-bf81-e3d5cca35df4';
        $bundleCid = '7f0e6b2a-3c4d-4e5f-9a1b-2c3d4e5f6a7b';
        $result = $this->controller($reader)->handleForSession($this->session(), self::PID, $bundleId, $bundleCid);
        self::assertSame(200, $result['status']);
        $body = $result['body'];
        self::assertSame($bundleId, $body['bundle_id']);
        self::assertSame($bundleCid, $body['correlation_id']);
        self::assertNull($body['sections']);
        $ticket = $body['ticket'];
        self::assertIsString($ticket);
        $claims = self::claims($ticket);
        self::assertSame($bundleId, $claims['bundle_id']);
        self::assertSame($bundleCid, $claims['cid']);
        self::assertSame(self::PUUID, $claims['puuid']);
        self::assertSame(self::USER_UUID, $claims['sub']);
        self::assertSame([], $this->agent->posts); // no bundle rebuilt or re-posted
        self::assertSame([], $reader->asOfSeen); // no note or evidence read
        self::assertStringContainsString('outcome=ticket_refresh', $this->audit->events[0]['comment']);
    }

    public function testRefreshStillRequiresAuthorizationAndValidIds(): void
    {
        $bundleId = 'e6b2abe5-8664-4ebb-bf81-e3d5cca35df4';
        $cid = '7f0e6b2a-3c4d-4e5f-9a1b-2c3d4e5f6a7b';
        $denied = $this->controller($this->reader([]), self::FULL_ACL, [])->handleForSession($this->session(), self::PID, $bundleId, $cid);
        self::assertSame(403, $denied['status']);
        $bad = $this->controller($this->reader([]))->handleForSession($this->session(), self::PID, 'not-a-uuid', $cid);
        self::assertSame(400, $bad['status']);
        self::assertSame('invalid_bundle_id', self::detail($bad)['code']);
        $noCid = $this->controller($this->reader([]))->handleForSession($this->session(), self::PID, $bundleId, null);
        self::assertSame(400, $noCid['status']);
        $stale = $this->controller($this->reader([]))->handleForSession($this->session(), self::OTHER_PID, $bundleId, $cid);
        self::assertSame(409, $stale['status']);
    }

    // ------------------------------------------------------------------ //
    // Content hygiene
    // ------------------------------------------------------------------ //

    public function testNoClinicalContentInLogsAuditRowsErrorResponsesOrTicketResponsePlanText(): void
    {
        // Operational logs and error bodies carry codes, counts and ids only. The access audit row
        // intentionally records user and patient identifiers - never clinical content. The success
        // body carries the deterministic sections (results) but never the plan text.
        $labs = [self::hba1c()];
        $ok = $this->controller($this->reader($labs))->handleForSession($this->session());
        $denied = $this->controller($this->reader($labs), self::FULL_ACL, [])->handleForSession($this->session());
        $failed = $this->controller($this->reader($labs, notesFail: true))->handleForSession($this->session());

        $clinical = ['metformin', 'HbA1c', 'Hemoglobin', '8.9', '%', 'Today plan', '3b9d2c1e'];

        $logsAndErrors = $this->logger->dump()
            . json_encode($denied['body'], JSON_THROW_ON_ERROR)
            . json_encode($failed['body'], JSON_THROW_ON_ERROR);
        foreach ([...$clinical, 'drsmith', self::SECRET] as $needle) {
            self::assertStringNotContainsString($needle, $logsAndErrors, "found '{$needle}' in operational logs or error bodies");
        }

        $auditText = json_encode($this->audit->events, JSON_THROW_ON_ERROR);
        self::assertStringContainsString('drsmith', $auditText); // user identifier: by design
        self::assertStringContainsString('"pid":' . self::PID, $auditText); // patient identifier: by design
        foreach ($clinical as $needle) {
            self::assertStringNotContainsString($needle, $auditText, "found clinical content '{$needle}' in an audit comment");
        }

        $okText = json_encode($ok['body'], JSON_THROW_ON_ERROR);
        foreach (['metformin', 'Today plan', 'plan_text', self::SECRET] as $needle) {
            self::assertStringNotContainsString($needle, $okText, "found '{$needle}' in the ticket response");
        }

        foreach ([$denied, $failed] as $r) {
            self::assertSame(['code', 'message', 'correlation_id'], array_keys(self::detail($r)));
        }
    }
}
