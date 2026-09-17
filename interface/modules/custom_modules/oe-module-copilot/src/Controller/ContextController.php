<?php

/**
 * Builds the ContextBundle for the patient selected in the caller's session.
 *
 * Identity, patient and current encounter come only from the authenticated
 * OpenEMR session (`authUserID`, `authUser`, `pid`, `encounter`); query
 * parameters and request bodies are ignored, so a client cannot name a patient
 * or a time boundary it has not selected through the UI (AUDIT ARCH-002 /
 * SEC-002).
 *
 * As-of timestamp (server-derived, never client-supplied): the local timestamp
 * of the session's current encounter when that encounter exists and belongs to
 * the selected patient; otherwise the server's current local time. The
 * baseline note is the latest non-blank SOAP plan strictly before as-of. Lab
 * results are windowed from the baseline note onward and are not capped at
 * as-of, so results filed during today's visit remain visible.
 *
 * Responses carrying clinical context are marked `Cache-Control: no-store,
 * private` and `X-Content-Type-Options: nosniff`, plus an X-Correlation-Id.
 *
 * Logging: operational log lines carry the correlation id, outcome codes and
 * counts only. The OpenEMR access audit row (`log` table, event
 * `copilot-context`) intentionally records the user and patient identifiers
 * - that is its purpose - but never plan text, test names, values, units or
 * any other clinical content. Error bodies are fixed codes and messages.
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
use OpenEMR\Modules\Copilot\Authorization\CopilotAuthorizer;
use OpenEMR\Modules\Copilot\ContextBundleBuilder;
use OpenEMR\Modules\Copilot\Data\ClinicalReaderInterface;
use OpenEMR\Modules\Copilot\Data\SourceUnavailableException;
use OpenEMR\Modules\Copilot\Support\Scalar;
use Psr\Log\LoggerInterface;
use Ramsey\Uuid\Uuid;
use RuntimeException;
use Symfony\Component\HttpFoundation\JsonResponse;

final class ContextController
{
    public const CORRELATION_HEADER = 'X-Correlation-Id';
    public const AUDIT_EVENT = 'copilot-context';

    public const LAB_RESULT_LIMIT = 200;

    public const AS_OF_SOURCE_ENCOUNTER = 'current_encounter';
    public const AS_OF_SOURCE_SERVER = 'server_time';

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
        'patient_not_found' => [404, 'The selected patient record could not be found.'],
        'no_prior_note' => [404, 'No prior note with plan text is on file for the selected patient.'],
        'source_unavailable' => [503, 'A required clinical source could not be read.'],
        'internal_error' => [500, 'The patient context could not be built.'],
    ];

    /** @var callable(string, string, bool, string, int): void */
    private $auditWriter;

    private readonly LoggerInterface $logger;

    /** @var Closure(): string  local server time as 'Y-m-d H:i:s' */
    private readonly Closure $serverTime;

    public function __construct(
        private readonly CopilotAuthorizer $authorizer,
        private readonly ClinicalReaderInterface $reader,
        private readonly ContextBundleBuilder $builder,
        ?LoggerInterface $logger = null,
        ?callable $auditWriter = null,
        ?Closure $serverTime = null,
    ) {
        $this->logger = $logger ?? ServiceContainer::getLogger();
        $this->auditWriter = $auditWriter ?? static function (string $event, string $user, bool $success, string $comment, int $pid): void {
            EventAuditLogger::getInstance()->newEvent($event, $user, 'copilot', $success ? 1 : 0, $comment, $pid);
        };
        $this->serverTime = $serverTime ?? static fn(): string => date('Y-m-d H:i:s');
    }

    /**
     * Framework-neutral entry point used by the REST route and by tests.
     *
     * @param array{authUserID?:mixed, authUser?:mixed, pid?:mixed, encounter?:mixed} $session
     * @return array{status:int, body:array<string,mixed>, headers:array<string,string>}
     */
    public function handleForSession(array $session): array
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
            $this->logger->info('copilot context denied', ['cid' => $correlationId, 'code' => $decision['code']]);
            if ($pid !== null && $username !== null && $decision['code'] !== CopilotAuthorizer::CODE_NOT_AUTHENTICATED) {
                ($this->auditWriter)(self::AUDIT_EVENT, $username, false, "cid={$correlationId}; code={$decision['code']}", $pid);
            }
            return $this->error($decision['code'], $correlationId, $headers);
        }
        assert($pid !== null && $username !== null);

        try {
            $patient = $this->reader->findPatient($pid);
            if ($patient === null) {
                return $this->error('patient_not_found', $correlationId, $headers);
            }

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

            try {
                $labResults = $this->reader->listLabResults($pid, $note['note_date'], self::LAB_RESULT_LIMIT);
            } catch (SourceUnavailableException $e) {
                $this->logger->warning('copilot lab source unavailable', ['cid' => $correlationId, 'source' => $e->getSource()]);
                $labResults = null; // declared in data_quality.sources_unavailable, never an empty list
            }

            $bundle = $this->builder->build($correlationId, $patient, $note, $labResults);
        } catch (SourceUnavailableException $e) {
            $this->logger->error('copilot source unavailable', ['cid' => $correlationId, 'source' => $e->getSource()]);
            return $this->error('source_unavailable', $correlationId, $headers);
        } catch (InvalidArgumentException | RuntimeException $e) {
            // Bad stored timestamp, uuid conversion failure, or a helper's runtime error: never partial output.
            $this->logger->error('copilot context build failed', ['cid' => $correlationId, 'type' => $e::class]);
            return $this->error('internal_error', $correlationId, $headers);
        }

        ($this->auditWriter)(
            self::AUDIT_EVENT,
            $username,
            true,
            "cid={$correlationId}; basis={$decision['basis']}; as_of={$asOfSource}; results=" . count($bundle['lab_results'])
                . "; unavailable=" . count($bundle['data_quality']['sources_unavailable']),
            $pid
        );
        $this->logger->info('copilot context built', [
            'cid' => $correlationId,
            'basis' => $decision['basis'],
            'as_of_source' => $asOfSource,
            'results' => count($bundle['lab_results']),
            'sources_unavailable' => $bundle['data_quality']['sources_unavailable'],
            'omitted' => $this->builder->getOmittedCounts(),
        ]);

        return ['status' => 200, 'body' => ['context' => $bundle], 'headers' => $headers];
    }

    /** REST route adapter: `GET /api/copilot/context` under the local API bridge. */
    public function handleRest(HttpRestRequest $request): JsonResponse
    {
        $session = $request->getSession();
        $result = $this->handleForSession([
            'authUserID' => $session->get('authUserID'),
            'authUser' => $session->get('authUser'),
            'pid' => $session->get('pid'),
            'encounter' => $session->get('encounter'),
        ]);
        if (!headers_sent()) {
            // PHP's session cache limiter pre-sets a weaker Cache-Control; ensure no-store is the only one sent.
            header_remove('Cache-Control');
        }
        return new JsonResponse($result['body'], $result['status'], $result['headers']);
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
