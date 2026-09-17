<?php

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Tests;

use OpenEMR\Modules\Copilot\Agent\AgentClientInterface;
use OpenEMR\Modules\Copilot\Agent\AgentUnavailableException;
use OpenEMR\Modules\Copilot\Agent\BundleAccepted;
use OpenEMR\Modules\Copilot\Authorization\AclCheckerInterface;
use OpenEMR\Modules\Copilot\Authorization\RelationshipRepositoryInterface;
use OpenEMR\Modules\Copilot\Data\ClinicalReaderInterface;
use OpenEMR\Modules\Copilot\Data\SourceUnavailableException;
use Psr\Log\AbstractLogger;
use Stringable;

/**
 * @phpstan-type NoteRow array{form_soap_id:int, encounter:int, note_date:string, plan:string}
 * @phpstan-type LabRow array{result_id:int, order_id:int, test_name:string, code:string, value:string, units:string, range:string, abnormal:string, result_status:string, observed_at:string}
 */
final class FakeAcl implements AclCheckerInterface
{
    /** @param array<string,bool> $grants keyed "section/value" */
    public function __construct(private readonly array $grants = [])
    {
    }

    public function check(string $section, string $value, string $username): bool
    {
        return $this->grants["{$section}/{$value}"] ?? false;
    }
}

final class FakeRelationships implements RelationshipRepositoryInterface
{
    /** @var list<array{int,int}> */
    public array $calls = [];

    /** @param array<string,string> $bases keyed "userId:pid" -> basis */
    public function __construct(private readonly array $bases = [])
    {
    }

    public function findBasis(int $userId, int $pid): ?string
    {
        $this->calls[] = [$userId, $pid];
        return $this->bases["{$userId}:{$pid}"] ?? null;
    }
}

/**
 * @phpstan-import-type NoteRow from FakeAcl
 * @phpstan-import-type LabRow from FakeAcl
 */
final class FakeReader implements ClinicalReaderInterface
{
    /** @var list<int> */
    public array $requestedPids = [];

    /** @var list<string> as-of boundaries passed to findLatestSoapPlanBefore */
    public array $asOfSeen = [];

    /**
     * @param array<int,array{pid:int,uuid:string}> $patients
     * @param array<int,list<NoteRow>> $notes  any order; selection follows the reader contract
     * @param array<int,list<LabRow>|SourceUnavailableException> $labs
     * @param array<string,string> $encounterDates keyed "pid:encounter" -> local datetime
     * @param array<int,string> $userUuids keyed by user id
     */
    public function __construct(
        private readonly array $patients = [],
        private readonly array $notes = [],
        private readonly array $labs = [],
        private readonly ?SourceUnavailableException $notesFailure = null,
        private readonly array $encounterDates = [],
        private readonly array $userUuids = [],
    ) {
    }

    public function findPatient(int $pid): ?array
    {
        $this->requestedPids[] = $pid;
        return $this->patients[$pid] ?? null;
    }

    public function findUserUuid(int $userId): ?string
    {
        return $this->userUuids[$userId] ?? null;
    }

    public function findEncounterDate(int $pid, int $encounter): ?string
    {
        return $this->encounterDates["{$pid}:{$encounter}"] ?? null;
    }

    public function findLatestSoapPlanBefore(int $pid, string $asOfLocal): ?array
    {
        if ($this->notesFailure !== null) {
            throw $this->notesFailure;
        }
        $this->asOfSeen[] = $asOfLocal;
        $candidates = array_values(array_filter(
            $this->notes[$pid] ?? [],
            static fn(array $n): bool => trim($n['plan']) !== '' && $n['note_date'] < $asOfLocal
        ));
        usort($candidates, static fn(array $a, array $b): int => [$b['note_date'], $b['form_soap_id']] <=> [$a['note_date'], $a['form_soap_id']]);
        return $candidates[0] ?? null;
    }

    public function listLabResults(int $pid, string $sinceLocal, int $limit): array
    {
        $labs = $this->labs[$pid] ?? [];
        if ($labs instanceof SourceUnavailableException) {
            throw $labs;
        }
        return $labs;
    }

    /** @var list<array{order_id:int, seq:int, test_name:string, code:string, order_status:string, ordered_at:string}>|SourceUnavailableException */
    public array|SourceUnavailableException $orders = [];

    /** @var array{fname:string, lname:string, dob:string, sex:string, pubpid:string}|SourceUnavailableException|null */
    public array|SourceUnavailableException|null $identity = ['fname' => 'Evelyn', 'lname' => 'Demo', 'dob' => '1958-04-12', 'sex' => 'Female', 'pubpid' => 'P-7'];

    /** @var array{text:string, date:string, source:string}|SourceUnavailableException|null */
    public array|SourceUnavailableException|null $appointment = null;

    /** @var array{text:string, date:string, source:string}|SourceUnavailableException|null */
    public array|SourceUnavailableException|null $encounterReason = null;

    /** @var list<array{id:int, title:string, diagnosis:string, reaction:string, severity:string, begdate:string, enddate:string, activity:int}>|SourceUnavailableException */
    public array|SourceUnavailableException $allergies = [];

    /** @var list<string> local dates passed to findAppointmentReason */
    public array $appointmentDatesSeen = [];

    /** @var list<array{source_table:string, id:int, drug:string, rxnorm:string, dosage:string, active:int, begdate:string, enddate:string, date_added:string, date_modified:string}>|SourceUnavailableException */
    public array|SourceUnavailableException $medications = [];

    public function listMedications(int $pid, int $limit): array
    {
        if ($this->medications instanceof SourceUnavailableException) {
            throw $this->medications;
        }
        return $this->medications;
    }

    public function listLabOrders(int $pid, string $sinceLocal, int $limit): array
    {
        if ($this->orders instanceof SourceUnavailableException) {
            throw $this->orders;
        }
        return $this->orders;
    }

    public function findIdentity(int $pid): ?array
    {
        if ($this->identity instanceof SourceUnavailableException) {
            throw $this->identity;
        }
        return $this->identity;
    }

    public function findAppointmentReason(int $pid, string $localDate): ?array
    {
        $this->appointmentDatesSeen[] = $localDate;
        if ($this->appointment instanceof SourceUnavailableException) {
            throw $this->appointment;
        }
        return $this->appointment;
    }

    public function findEncounterReason(int $pid, int $encounter): ?array
    {
        if ($this->encounterReason instanceof SourceUnavailableException) {
            throw $this->encounterReason;
        }
        return $this->encounterReason;
    }

    public function listAllergies(int $pid): array
    {
        if ($this->allergies instanceof SourceUnavailableException) {
            throw $this->allergies;
        }
        return $this->allergies;
    }
}

final class CapturingLogger extends AbstractLogger
{
    /** @var list<array{level:string, message:string, context:array<mixed>}> */
    public array $records = [];

    /**
     * @param mixed $level
     * @param array<mixed> $context
     */
    public function log($level, string|Stringable $message, array $context = []): void
    {
        $levelText = is_scalar($level) || $level instanceof Stringable ? (string) $level : 'unknown';
        $this->records[] = ['level' => $levelText, 'message' => (string) $message, 'context' => $context];
    }

    public function dump(): string
    {
        return json_encode($this->records, JSON_THROW_ON_ERROR);
    }
}

final class AuditCapture
{
    /** @var list<array{event:string, user:string, success:bool, comment:string, pid:int}> */
    public array $events = [];

    public function __invoke(string $event, string $user, bool $success, string $comment, int $pid): void
    {
        $this->events[] = ['event' => $event, 'user' => $user, 'success' => $success, 'comment' => $comment, 'pid' => $pid];
    }
}

final class FakeAgentClient implements AgentClientInterface
{
    /** @var list<array{bundle:array<string,mixed>, cid:string}> */
    public array $posts = [];

    public function __construct(
        private readonly ?AgentUnavailableException $failure = null,
        private readonly string $bundleId = 'e6b2abe5-8664-4ebb-bf81-e3d5cca35df4',
    ) {
    }

    public function postBundle(array $bundle, string $correlationId): BundleAccepted
    {
        $this->posts[] = ['bundle' => $bundle, 'cid' => $correlationId];
        if ($this->failure !== null) {
            throw $this->failure;
        }
        $puuid = $bundle['patient_uuid'];
        assert(is_string($puuid));
        return new BundleAccepted($this->bundleId, $correlationId, $puuid, '2026-09-17T15:15:00Z');
    }
}
