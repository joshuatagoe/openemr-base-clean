<?php

/**
 * `POST /api/copilot/briefing-ticket`: authorizes the caller for the patient
 * selected in their session, builds the ContextBundle, hands it to the agent,
 * and returns a short-lived patient-bound ticket plus the deterministic
 * sections (ARCHITECTURE.md section 5, steps 2-4).
 *
 * Identity, patient and current encounter come only from the authenticated
 * OpenEMR session (`authUserID`, `authUser`, `pid`, `encounter`). The panel may
 * send the `pid` it was rendered with; it is never used as the binding - if it
 * disagrees with the session it means the panel is stale and the request is
 * refused (AUDIT ARCH-002 / SEC-002).
 *
 * As-of timestamp (server-derived): the local timestamp of the session's
 * current encounter when that encounter exists and belongs to the selected
 * patient; otherwise the server's current local time. The baseline note is the
 * latest non-blank SOAP plan strictly before as-of. Lab results are windowed
 * from the baseline note onward.
 *
 * The bundle (plan text, results, orders) goes only to the agent, server to
 * server. The response to the browser carries the ticket and the deterministic
 * sections (identity, both reasons verbatim with provenance, interval results,
 * allergies as recorded, footer), never the plan text. Identity never reaches
 * the agent. If the agent cannot be reached the response still carries the
 * sections, with `degraded` set and no ticket. A panel-only source that fails
 * to read is named in the footer, never rendered as empty.
 *
 * Responses are `Cache-Control: no-store, private`, `X-Content-Type-Options:
 * nosniff`, and carry an X-Correlation-Id. Log lines carry the correlation id,
 * outcome codes and counts only. The audit row (`log` table, event
 * `copilot-briefing`) records user and patient - its purpose - but never
 * clinical content.
 *
 * @package   OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Controller;

use Closure;
use InvalidArgumentException;
use OpenEMR\BC\ServiceContainer;
use OpenEMR\Common\Http\HttpRestRequest;
use OpenEMR\Common\Logging\EventAuditLogger;
use OpenEMR\Modules\Copilot\Agent\AgentClientInterface;
use OpenEMR\Modules\Copilot\Agent\AgentUnavailableException;
use OpenEMR\Modules\Copilot\Authorization\CopilotAuthorizer;
use OpenEMR\Modules\Copilot\Config\CopilotConfig;
use OpenEMR\Modules\Copilot\ContextBundleBuilder;
use OpenEMR\Modules\Copilot\Data\ClinicalReaderInterface;
use OpenEMR\Modules\Copilot\Data\SourceUnavailableException;
use OpenEMR\Modules\Copilot\Support\Scalar;
use OpenEMR\Modules\Copilot\Support\UtcDate;
use OpenEMR\Modules\Copilot\Ticket\TicketSigner;
use Psr\Log\LoggerInterface;
use Ramsey\Uuid\Uuid;
use RuntimeException;
use Symfony\Component\HttpFoundation\JsonResponse;

final class BriefingTicketController
{
    public const SCHEMA_VERSION = '1.0';
    public const CORRELATION_HEADER = 'X-Correlation-Id';
    public const AUDIT_EVENT = 'copilot-briefing';

    public const LAB_RESULT_LIMIT = 200;

    public const AS_OF_SOURCE_ENCOUNTER = 'current_encounter';
    public const AS_OF_SOURCE_SERVER = 'server_time';

    public const DEGRADED_AGENT_UNAVAILABLE = 'agent_unavailable';

    /** Headers set on every response from this controller. */
    public const RESPONSE_HEADERS = [
        'Cache-Control' => 'no-store, private',
        'X-Content-Type-Options' => 'nosniff',
    ];

    private const ERRORS = [
        CopilotAuthorizer::CODE_NOT_AUTHENTICATED => [401, 'Authentication is required.'],
        CopilotAuthorizer::CODE_NO_PATIENT_SELECTED => [409, 'No patient is selected in this session.'],
        CopilotAuthorizer::CODE_ACL_DENIED => [403, 'Your role does not permit reading this patient context.'],
        CopilotAuthorizer::CODE_NO_CARE_RELATIONSHIP => [403, 'No care relationship with the selected patient was found.'],
        'patient_mismatch' => [409, 'The selected patient changed; reload the patient summary.'],
        'invalid_bundle_id' => [400, 'The bundle id to refresh is not valid.'],
        'agent_not_configured' => [503, 'The Co-Pilot agent is not configured.'],
        'patient_not_found' => [404, 'The selected patient record could not be found.'],
        'no_prior_note' => [404, 'No prior note with plan text is on file for the selected patient.'],
        'source_unavailable' => [503, 'A required clinical source could not be read.'],
        'internal_error' => [500, 'The briefing could not be prepared.'],
    ];

    /** @var callable(string, string, bool, string, int): void */
    private $auditWriter;

    private readonly LoggerInterface $logger;

    /** @var Closure(): string  local server time as 'Y-m-d H:i:s' */
    private readonly Closure $serverTime;

    /** @var Closure(): int  unix seconds */
    private readonly Closure $unixTime;

    public function __construct(
        private readonly CopilotAuthorizer $authorizer,
        private readonly ClinicalReaderInterface $reader,
        private readonly ContextBundleBuilder $builder,
        private readonly AgentClientInterface $agent,
        private readonly CopilotConfig $config,
        ?LoggerInterface $logger = null,
        ?callable $auditWriter = null,
        ?Closure $serverTime = null,
        ?Closure $unixTime = null,
    ) {
        $this->logger = $logger ?? ServiceContainer::getLogger();
        $this->auditWriter = $auditWriter ?? static function (string $event, string $user, bool $success, string $comment, int $pid): void {
            EventAuditLogger::getInstance()->newEvent($event, $user, 'copilot', $success ? 1 : 0, $comment, $pid);
        };
        $this->serverTime = $serverTime ?? static fn(): string => date('Y-m-d H:i:s');
        $this->unixTime = $unixTime ?? static fn(): int => time();
    }

    /**
     * Framework-neutral entry point used by the REST route and by tests.
     *
     * With `$refreshBundleId` the call re-authorizes and mints a new ticket for
     * an existing agent bundle (follow-up turns after the previous ticket
     * expired) without re-reading the chart or re-posting a bundle. The
     * bundle id is not trusted: the agent enforces that it belongs to the
     * ticket's patient and user.
     *
     * @param array{authUserID?:mixed, authUser?:mixed, pid?:mixed, encounter?:mixed} $session
     * @param int|null $requestedPid  the pid the panel was rendered with, if it sent one
     * @param string|null $refreshBundleId  an existing bundle id to re-ticket, or null for a new briefing
     * @param string|null $refreshCorrelationId  that bundle's original correlation id (the agent requires it to match)
     * @return array{status:int, body:array<string,mixed>, headers:array<string,string>}
     */
    public function handleForSession(array $session, ?int $requestedPid = null, ?string $refreshBundleId = null, ?string $refreshCorrelationId = null): array
    {
        $correlationId = Uuid::uuid4()->toString();
        $headers = self::RESPONSE_HEADERS + [self::CORRELATION_HEADER => $correlationId];

        $userId = Scalar::positiveIntOrNull($session['authUserID'] ?? null);
        $usernameRaw = Scalar::str($session['authUser'] ?? null);
        $username = $usernameRaw === '' ? null : $usernameRaw;
        $pid = Scalar::positiveIntOrNull($session['pid'] ?? null);
        $encounter = Scalar::positiveIntOrNull($session['encounter'] ?? null);

        $decision = $this->authorizer->authorize($userId, $username, $pid);
        if (!$decision['allowed']) {
            $this->logger->info('copilot briefing denied', ['cid' => $correlationId, 'code' => $decision['code']]);
            if ($pid !== null && $username !== null && $decision['code'] !== CopilotAuthorizer::CODE_NOT_AUTHENTICATED) {
                ($this->auditWriter)(self::AUDIT_EVENT, $username, false, "cid={$correlationId}; code={$decision['code']}", $pid);
            }
            return $this->error($decision['code'], $correlationId, $headers);
        }
        assert($userId !== null && $pid !== null && $username !== null);

        if ($requestedPid !== null && $requestedPid !== $pid) {
            $this->logger->info('copilot briefing denied', ['cid' => $correlationId, 'code' => 'patient_mismatch']);
            ($this->auditWriter)(self::AUDIT_EVENT, $username, false, "cid={$correlationId}; code=patient_mismatch", $pid);
            return $this->error('patient_mismatch', $correlationId, $headers);
        }

        if ($refreshBundleId !== null) {
            return $this->refreshTicket($refreshBundleId, $refreshCorrelationId ?? '', $correlationId, $headers, $userId, $username, $pid, $decision['basis']);
        }

        try {
            $patient = $this->reader->findPatient($pid);
            if ($patient === null) {
                return $this->error('patient_not_found', $correlationId, $headers);
            }
            $userUuid = $this->reader->findUserUuid($userId);

            [$asOf, $asOfSource] = $this->resolveAsOf($pid, $encounter);

            $note = $this->reader->findLatestSoapPlanBefore($pid, $asOf);
            if ($note === null) {
                ($this->auditWriter)(
                    self::AUDIT_EVENT,
                    $username,
                    true,
                    "cid={$correlationId}; basis={$decision['basis']}; as_of={$asOfSource}; outcome=no_prior_note",
                    $pid
                );
                return $this->error('no_prior_note', $correlationId, $headers);
            }

            $sectionSourcesUnavailable = [];
            $labResults = $this->readOptional($correlationId, 'lab_results', $sectionSourcesUnavailable, fn(): array => $this->reader->listLabResults($pid, $note['note_date'], self::LAB_RESULT_LIMIT));
            $labOrders = $this->readOptional($correlationId, 'lab_orders', $sectionSourcesUnavailable, fn(): array => $this->reader->listLabOrders($pid, $note['note_date'], self::LAB_RESULT_LIMIT));
            $identity = $this->readOptional($correlationId, 'identity', $sectionSourcesUnavailable, fn(): ?array => $this->reader->findIdentity($pid));
            $scheduled = $this->readOptional($correlationId, 'appointment', $sectionSourcesUnavailable, fn(): ?array => $this->reader->findAppointmentReason($pid, substr($asOf, 0, 10)));
            $encounterReason = $this->readOptional($correlationId, 'encounters', $sectionSourcesUnavailable, fn(): ?array => $this->reader->findEncounterReason($pid, $note['encounter']));
            $allergies = $this->readOptional($correlationId, 'allergies', $sectionSourcesUnavailable, fn(): array => $this->reader->listAllergies($pid));
            $medications = $this->readOptional($correlationId, 'medications', $sectionSourcesUnavailable, fn(): array => $this->reader->listMedications($pid, self::LAB_RESULT_LIMIT));

            $bundle = $this->builder->build($correlationId, $patient, $note, $labResults, $userUuid, $labOrders, $medications, $asOf, $allergies);
            $asOfUtc = UtcDate::toIso($asOf, $this->builder->getLocalZone());
            $sections = $this->sections($bundle, $asOfUtc, $asOfSource, $identity, $scheduled, $encounterReason, $allergies, $sectionSourcesUnavailable);
        } catch (SourceUnavailableException $e) {
            $this->logger->error('copilot source unavailable', ['cid' => $correlationId, 'source' => $e->getSource()]);
            return $this->error('source_unavailable', $correlationId, $headers);
        } catch (InvalidArgumentException | RuntimeException $e) {
            // Bad stored timestamp, uuid conversion failure, or a helper's runtime error: never partial output.
            $this->logger->error('copilot briefing build failed', ['cid' => $correlationId, 'type' => $e::class]);
            return $this->error('internal_error', $correlationId, $headers);
        }

        // Hand-off. Failure degrades explicitly: sections without a ticket.
        $ticket = null;
        $bundleId = null;
        $ticketExpiresAt = null;
        $degraded = null;
        $agentOutcome = 'accepted';
        try {
            $accepted = $this->agent->postBundle($bundle, $correlationId);
            $bundleId = $accepted->bundleId;
            $issuedAt = $this->issuedAt();
            $secret = $this->config->ticketSecret;
            if ($secret === null) {
                throw new AgentUnavailableException(AgentUnavailableException::REASON_NOT_CONFIGURED);
            }
            $ticket = (new TicketSigner($secret))->mint(
                $userUuid ?? 'user:' . $userId,
                $bundle['patient_uuid'],
                $bundleId,
                $correlationId,
                Uuid::uuid4()->toString(),
                $issuedAt,
                $this->config->ticketTtlSeconds,
            );
            $ticketExpiresAt = gmdate(UtcDate::FORMAT, $issuedAt + $this->config->ticketTtlSeconds);
        } catch (AgentUnavailableException $e) {
            $agentOutcome = $e->getReason();
            $this->logger->warning('copilot agent hand-off failed', [
                'cid' => $correlationId,
                'reason' => $e->getReason(),
                'http_status' => $e->getHttpStatus(),
            ]);
            $degraded = ['reason_code' => self::DEGRADED_AGENT_UNAVAILABLE, 'detail_code' => $e->getReason()];
            $bundleId = null;
        }

        ($this->auditWriter)(
            self::AUDIT_EVENT,
            $username,
            true,
            "cid={$correlationId}; basis={$decision['basis']}; as_of={$asOfSource}; results=" . count($bundle['lab_results'])
                . "; unavailable=" . count($bundle['data_quality']['sources_unavailable'])
                . "; agent={$agentOutcome}",
            $pid
        );
        $this->logger->info('copilot briefing ticket issued', [
            'cid' => $correlationId,
            'basis' => $decision['basis'],
            'as_of_source' => $asOfSource,
            'results' => count($bundle['lab_results']),
            'sources_unavailable' => $bundle['data_quality']['sources_unavailable'],
            'omitted' => $this->builder->getOmittedCounts(),
            'agent' => $agentOutcome,
            'ticket' => $ticket !== null,
        ]);

        $body = [
            'schema_version' => self::SCHEMA_VERSION,
            'correlation_id' => $correlationId,
            'patient_uuid' => $bundle['patient_uuid'],
            'agent_url' => $this->config->agentBaseUrl(),
            'bundle_id' => $bundleId,
            'ticket' => $ticket,
            'ticket_expires_at' => $ticketExpiresAt,
            'sections' => $sections,
            'degraded' => $degraded,
            'warnings' => [],
        ];
        return ['status' => 200, 'body' => $body, 'headers' => $headers];
    }

    /** REST route adapter: `POST /api/copilot/briefing-ticket` under the local API bridge. */
    public function handleRest(HttpRestRequest $request): JsonResponse
    {
        $session = $request->getSession();
        $requestedPid = null;
        $refreshBundleId = null;
        $refreshCorrelationId = null;
        $json = json_decode($request->getContent(), true, 4);
        if (is_array($json)) {
            $requestedPid = Scalar::positiveIntOrNull($json['pid'] ?? null);
            $raw = $json['refresh_bundle_id'] ?? null;
            $refreshBundleId = is_string($raw) && $raw !== '' ? $raw : null;
            $rawCid = $json['refresh_correlation_id'] ?? null;
            $refreshCorrelationId = is_string($rawCid) && $rawCid !== '' ? $rawCid : null;
        }
        $result = $this->handleForSession([
            'authUserID' => $session->get('authUserID'),
            'authUser' => $session->get('authUser'),
            'pid' => $session->get('pid'),
            'encounter' => $session->get('encounter'),
        ], $requestedPid, $refreshBundleId, $refreshCorrelationId);
        if (!headers_sent()) {
            // PHP's session cache limiter pre-sets a weaker Cache-Control; ensure no-store is the only one sent.
            header_remove('Cache-Control');
        }
        return new JsonResponse($result['body'], $result['status'], $result['headers']);
    }

    /**
     * Mint a fresh ticket for an existing bundle (authorization already passed).
     *
     * @param array<string,string> $headers
     * @return array{status:int, body:array<string,mixed>, headers:array<string,string>}
     */
    private function refreshTicket(string $bundleId, string $bundleCid, string $correlationId, array $headers, int $userId, string $username, int $pid, ?string $basis): array
    {
        if (!preg_match('/^[0-9a-f-]{36}$/', $bundleId) || !preg_match('/^[0-9a-f-]{36}$/', $bundleCid)) {
            return $this->error('invalid_bundle_id', $correlationId, $headers);
        }
        $secret = $this->config->ticketSecret;
        if ($secret === null) {
            return $this->error('agent_not_configured', $correlationId, $headers);
        }
        try {
            $patient = $this->reader->findPatient($pid);
            if ($patient === null) {
                return $this->error('patient_not_found', $correlationId, $headers);
            }
            $userUuid = $this->reader->findUserUuid($userId);
        } catch (SourceUnavailableException $e) {
            $this->logger->error('copilot source unavailable', ['cid' => $correlationId, 'source' => $e->getSource()]);
            return $this->error('source_unavailable', $correlationId, $headers);
        }
        $issuedAt = $this->issuedAt();
        // The ticket carries the bundle's original cid (supplied by the panel); the agent binds tickets by
        // bundle + patient + user and checks that cid against the stored bundle, so a wrong pair fails there.
        $ticket = (new TicketSigner($secret))->mint(
            $userUuid ?? 'user:' . $userId,
            $patient['uuid'],
            $bundleId,
            $bundleCid,
            Uuid::uuid4()->toString(),
            $issuedAt,
            $this->config->ticketTtlSeconds,
        );
        ($this->auditWriter)(self::AUDIT_EVENT, $username, true, "cid={$correlationId}; basis={$basis}; outcome=ticket_refresh; bundle_cid={$bundleCid}", $pid);
        $this->logger->info('copilot ticket refreshed', ['cid' => $correlationId, 'basis' => $basis]);
        return ['status' => 200, 'body' => [
            'schema_version' => self::SCHEMA_VERSION,
            'correlation_id' => $bundleCid,
            'patient_uuid' => $patient['uuid'],
            'agent_url' => $this->config->agentBaseUrl(),
            'bundle_id' => $bundleId,
            'ticket' => $ticket,
            'ticket_expires_at' => gmdate(UtcDate::FORMAT, $issuedAt + $this->config->ticketTtlSeconds),
            'sections' => null,
            'degraded' => null,
            'warnings' => [],
        ], 'headers' => $headers];
    }

    /**
     * Run one optional read. A failure is recorded by source name and the
     * value becomes null - never an empty list - so the panel and the agent
     * can tell "could not read" from "nothing there".
     *
     * @template T of array|null
     * @param list<string> $unavailable  appended to on failure
     * @param callable(): T $read
     * @return T|null
     */
    private function readOptional(string $correlationId, string $source, array &$unavailable, callable $read): ?array
    {
        try {
            return $read();
        } catch (SourceUnavailableException $e) {
            $this->logger->warning('copilot source unavailable', ['cid' => $correlationId, 'source' => $e->getSource()]);
            $unavailable[] = $source;
            return null;
        }
    }

    /**
     * Deterministic sections rendered by the panel before (and regardless of)
     * the agent (ARCHITECTURE.md sections 2 and 6): identity, both reasons
     * verbatim with provenance, the checked window, interval results and
     * orders, allergies as recorded, and what could not be read. Never
     * includes the plan text.
     *
     * @param array<string,mixed> $bundle
     * @param array{fname:string, lname:string, dob:string, sex:string, pubpid:string}|null $identity
     * @param array{text:string, date:string, source:string}|null $scheduled
     * @param array{text:string, date:string, source:string}|null $encounterReason
     * @param list<array{id:int, title:string, diagnosis:string, reaction:string, severity:string, begdate:string, enddate:string, activity:int}>|null $allergies
     * @param list<string> $sectionSourcesUnavailable
     * @return array<string,mixed>
     */
    private function sections(
        array $bundle,
        string $asOfUtc,
        string $asOfSource,
        ?array $identity,
        ?array $scheduled,
        ?array $encounterReason,
        ?array $allergies,
        array $sectionSourcesUnavailable,
    ): array {
        $priorNote = $bundle['prior_note'];
        $labResults = $bundle['lab_results'];
        $labOrders = $bundle['lab_orders'];
        $medications = $bundle['medications'];
        assert(is_array($priorNote) && is_array($labResults) && is_array($labOrders) && is_array($medications));
        // Medication changes since the baseline: rows whose window timestamp is after the note (unannotated here).
        $noteDate = $priorNote['note_date'];
        $medicationChanges = [];
        foreach ($medications as $m) {
            if (is_array($m) && is_string($m['timestamp'] ?? null) && is_string($noteDate) && $m['timestamp'] > $noteDate) {
                $medicationChanges[] = $m;
            }
        }

        // Allergies as recorded come from the bundle (collapsed there, DATA-004); null when the source failed.
        $allergyRows = $allergies === null ? null : $bundle['allergies'];
        assert($allergyRows === null || is_array($allergyRows));
        $dataQuality = $bundle['data_quality'];
        assert(is_array($dataQuality));

        return [
            'identity' => $identity === null ? null : [
                'name' => trim($identity['fname'] . ' ' . $identity['lname']),
                'dob' => $identity['dob'] === '' ? null : $identity['dob'],
                'sex' => $identity['sex'] === '' ? null : $identity['sex'],
                'pubpid' => $identity['pubpid'] === '' ? null : $identity['pubpid'],
            ],
            'reasons' => [
                'scheduled' => $scheduled === null || $scheduled['text'] === '' ? null : ['text' => $scheduled['text'], 'provenance' => 'scheduled', 'date' => $scheduled['date']],
                'encounter' => $encounterReason === null || $encounterReason['text'] === '' ? null : ['text' => $encounterReason['text'], 'provenance' => 'baseline_encounter', 'date' => $encounterReason['date']],
            ],
            'baseline' => [
                'note_id' => $priorNote['note_id'],
                'encounter_id' => $priorNote['encounter_id'],
                'note_date' => $priorNote['note_date'],
            ],
            'window' => [
                'start' => $priorNote['note_date'],
                'end' => $asOfUtc,
                'as_of_source' => $asOfSource,
            ],
            'interval_results' => array_values($labResults),
            'interval_orders' => array_values($labOrders),
            'medication_changes' => $medicationChanges,
            'allergies' => $allergyRows === null ? null : [
                'entries' => $allergyRows,
                'statement' => $allergyRows === [] ? 'no allergy entries on file (not confirmed NKA)' : count($allergyRows) . ' allergy entr' . (count($allergyRows) === 1 ? 'y' : 'ies') . ' as recorded',
            ],
            'footer' => [
                'sources_unavailable' => array_values(array_unique($sectionSourcesUnavailable)),
                'results_in_window' => count($labResults),
                'orders_in_window' => count($labOrders),
                'medication_changes_in_window' => count($medicationChanges),
                'medications_on_file' => count($medications),
                'duplicates_collapsed' => Scalar::int($dataQuality['duplicates_collapsed'] ?? 0),
                'omitted' => $this->builder->getOmittedCounts(),
            ],
        ];
    }

    private function issuedAt(): int
    {
        $now = ($this->unixTime)();
        if (!is_int($now) || $now <= 0) {
            throw new RuntimeException('clock unavailable');
        }
        return $now;
    }

    /**
     * Server-derived as-of boundary: the current encounter's timestamp when it
     * exists and belongs to this patient, else server time.
     *
     * @return array{string, string}  [local 'Y-m-d H:i:s', source]
     * @throws SourceUnavailableException
     */
    private function resolveAsOf(int $pid, ?int $encounter): array
    {
        if ($encounter !== null) {
            $encounterDate = $this->reader->findEncounterDate($pid, $encounter);
            if ($encounterDate !== null) {
                return [$encounterDate, self::AS_OF_SOURCE_ENCOUNTER];
            }
        }
        $now = Scalar::str(($this->serverTime)());
        if ($now === '') {
            throw new RuntimeException('server time unavailable');
        }
        return [$now, self::AS_OF_SOURCE_SERVER];
    }

    /**
     * @param array<string,string> $headers
     * @return array{status:int, body:array<string,mixed>, headers:array<string,string>}
     */
    private function error(string $code, string $correlationId, array $headers): array
    {
        [$status, $message] = self::ERRORS[$code] ?? self::ERRORS['internal_error'];
        return [
            'status' => $status,
            'body' => ['detail' => ['code' => $code, 'message' => $message, 'correlation_id' => $correlationId]],
            'headers' => $headers,
        ];
    }
}
