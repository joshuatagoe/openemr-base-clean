<?php

/**
 * `GET /api/copilot/document-file/{document_id}` (ADR-008 §3): the original
 * bytes of one of the selected patient's documents, for the source preview.
 *
 * Authorization is the briefing routes', unchanged (CopilotAuthorizer on the
 * session's selected patient; CSRF by the local API bridge). Then one lookup
 * that requires the document to be filed to that patient, not deleted, with no
 * expiry and an allow-listed type - a missing document and another patient's
 * document get the same 404, and nothing is read for either. Core
 * `can_access()` refuses with 403 before any byte is read. The stored size and
 * then the read length are checked against the 10 MB limit (413). Bytes come
 * from core `Document::get_data()` (decrypted in memory, no temporary files).
 *
 * The response is served inline without a filename, with the allow-listed
 * Content-Type, `nosniff`, `no-store` and a sandbox CSP so the file can never
 * run script in the OpenEMR origin. One audit row (`copilot-document-view`)
 * per authorized call, codes and ids only; no bytes, names or file names are
 * logged.
 *
 * @package   OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Controller;

use OpenEMR\BC\ServiceContainer;
use OpenEMR\Common\Http\HttpRestRequest;
use OpenEMR\Common\Logging\EventAuditLogger;
use OpenEMR\Modules\Copilot\Authorization\CopilotAuthorizer;
use OpenEMR\Modules\Copilot\Data\DocumentTooLargeException;
use OpenEMR\Modules\Copilot\Data\SourceUnavailableException;
use OpenEMR\Modules\Copilot\Data\SqlDocumentReader;
use OpenEMR\Modules\Copilot\Documents\DocumentFileSourceInterface;
use OpenEMR\Modules\Copilot\Support\Scalar;
use Psr\Log\LoggerInterface;
use Ramsey\Uuid\Uuid;
use Symfony\Component\HttpFoundation\JsonResponse;
use Symfony\Component\HttpFoundation\Response;
use Throwable;

final class DocumentFileController
{
    public const AUDIT_EVENT = 'copilot-document-view';

    public const CODE_NOT_FOUND = 'document_not_found';
    public const CODE_ACCESS_DENIED = 'document_access_denied';
    public const CODE_TOO_LARGE = 'document_too_large';
    public const CODE_UNAVAILABLE = 'document_unavailable';

    public const FILE_HEADERS = [
        'Content-Disposition' => 'inline',
        'X-Content-Type-Options' => 'nosniff',
        'Cache-Control' => 'no-store, private',
        'Content-Security-Policy' => "sandbox; default-src 'none'",
    ];

    private const ERRORS = [
        CopilotAuthorizer::CODE_NOT_AUTHENTICATED => [401, 'Authentication is required.'],
        CopilotAuthorizer::CODE_NO_PATIENT_SELECTED => [409, 'No patient is selected in this session.'],
        CopilotAuthorizer::CODE_ACL_DENIED => [403, 'Your role does not permit reading this patient context.'],
        CopilotAuthorizer::CODE_NO_CARE_RELATIONSHIP => [403, 'No care relationship with the selected patient was found.'],
        self::CODE_NOT_FOUND => [404, 'The document was not found.'],
        self::CODE_ACCESS_DENIED => [403, 'Your role does not permit viewing this document.'],
        self::CODE_TOO_LARGE => [413, 'The document is too large to preview.'],
        self::CODE_UNAVAILABLE => [503, 'The document store could not be read.'],
        'internal_error' => [500, 'The document could not be served.'],
    ];

    /** @var callable(string, string, bool, string, int): void */
    private $auditWriter;

    private readonly LoggerInterface $logger;

    public function __construct(
        private readonly CopilotAuthorizer $authorizer,
        private readonly DocumentFileSourceInterface $documents,
        ?LoggerInterface $logger = null,
        ?callable $auditWriter = null,
    ) {
        $this->logger = $logger ?? ServiceContainer::getLogger();
        $this->auditWriter = $auditWriter ?? static function (string $event, string $user, bool $success, string $comment, int $pid): void {
            EventAuditLogger::getInstance()->newEvent($event, $user, 'copilot', $success ? 1 : 0, $comment, $pid);
        };
    }

    /**
     * @param array{authUserID?:mixed, authUser?:mixed, pid?:mixed} $session
     * @return array{status:int, body:string|array<string,mixed>, headers:array<string,string>}
     */
    public function viewForSession(array $session, int $documentId): array
    {
        $correlationId = Uuid::uuid4()->toString();
        $headers = BriefingTicketController::RESPONSE_HEADERS + [BriefingTicketController::CORRELATION_HEADER => $correlationId];

        $userId = Scalar::positiveIntOrNull($session['authUserID'] ?? null);
        $usernameRaw = Scalar::str($session['authUser'] ?? null);
        $username = $usernameRaw === '' ? null : $usernameRaw;
        $pid = Scalar::positiveIntOrNull($session['pid'] ?? null);

        $decision = $this->authorizer->authorize($userId, $username, $pid);
        if (!$decision['allowed']) {
            $this->logger->info('copilot document view denied', ['cid' => $correlationId, 'code' => $decision['code']]);
            if ($pid !== null && $username !== null && $decision['code'] !== CopilotAuthorizer::CODE_NOT_AUTHENTICATED) {
                ($this->auditWriter)(self::AUDIT_EVENT, $username, false, "cid={$correlationId}; code={$decision['code']}", $pid);
            }
            return $this->error($decision['code'], $correlationId, $headers);
        }
        assert($pid !== null && $username !== null);

        $outcome = 'ok';
        $bytes = null;
        $mediaType = null;
        try {
            $document = $documentId > 0 ? $this->documents->findForPatient($documentId, $pid) : null;
            if ($document === null) {
                $outcome = self::CODE_NOT_FOUND;
            } elseif (!$this->documents->canAccess($documentId, $username)) {
                $outcome = self::CODE_ACCESS_DENIED;
            } elseif ($document['size'] > SqlDocumentReader::MAX_DOCUMENT_BYTES) {
                $outcome = self::CODE_TOO_LARGE;
            } else {
                $bytes = $this->documents->readBytes($documentId, $pid);
                if (strlen($bytes) > SqlDocumentReader::MAX_DOCUMENT_BYTES) {
                    $bytes = null;
                    $outcome = self::CODE_TOO_LARGE;
                }
                $mediaType = $document['media_type'];
            }
        } catch (DocumentTooLargeException) {
            $outcome = self::CODE_TOO_LARGE;
        } catch (SourceUnavailableException $e) {
            $this->logger->error('copilot source unavailable', ['cid' => $correlationId, 'source' => $e->getSource()]);
            $outcome = self::CODE_UNAVAILABLE;
        } catch (Throwable $e) {
            $this->logger->error('copilot document view failed', ['cid' => $correlationId, 'type' => $e::class]);
            $outcome = 'internal_error';
        }

        $documentRef = $documentId > 0 ? (string) $documentId : 'invalid';
        ($this->auditWriter)(
            self::AUDIT_EVENT,
            $username,
            $outcome === 'ok',
            "cid={$correlationId}; basis={$decision['basis']}; document_id={$documentRef}; outcome={$outcome}",
            $pid
        );
        $this->logger->info('copilot document view', ['cid' => $correlationId, 'outcome' => $outcome]);

        if ($bytes === null || $mediaType === null) {
            return $this->error($outcome, $correlationId, $headers);
        }
        return [
            'status' => 200,
            'body' => $bytes,
            'headers' => ['Content-Type' => $mediaType] + self::FILE_HEADERS + [BriefingTicketController::CORRELATION_HEADER => $correlationId],
        ];
    }

    /** REST route adapter: `GET /api/copilot/document-file/:did` under the local API bridge. */
    public function handleRest(string $documentId, HttpRestRequest $request): Response
    {
        $id = preg_match('/^[1-9]\d{0,18}$/', $documentId) === 1 ? (int) $documentId : 0;
        $session = $request->getSession();
        $result = $this->viewForSession([
            'authUserID' => $session->get('authUserID'),
            'authUser' => $session->get('authUser'),
            'pid' => $session->get('pid'),
        ], $id);
        if (!headers_sent()) {
            header_remove('Cache-Control');
        }
        if (is_string($result['body'])) {
            return new Response($result['body'], $result['status'], $result['headers']);
        }
        return new JsonResponse($result['body'], $result['status'], $result['headers']);
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
