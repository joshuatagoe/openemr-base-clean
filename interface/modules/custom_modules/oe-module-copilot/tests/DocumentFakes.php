<?php

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Tests;

use OpenEMR\Modules\Copilot\Data\SchemaStatusInterface;
use OpenEMR\Modules\Copilot\Data\SourceUnavailableException;
use OpenEMR\Modules\Copilot\Documents\PatientDocumentSourceInterface;
use OpenEMR\Modules\Copilot\Documents\ProcessingRepositoryInterface;
use RuntimeException;

/** In-memory core Documents: per pid, the documents a user may see, and their bytes. */
final class FakePatientDocuments implements PatientDocumentSourceInterface
{
    /** @var list<int> */
    public array $bytesRead = [];

    /** @var list<string> usernames passed to listForPatient */
    public array $listedFor = [];

    /**
     * @param array<int, list<array{document_id:int, uploaded_at:string, media_type:?string, size:int, category_names:list<string>}>> $byPid
     * @param array<int, string|\RuntimeException> $bytes keyed by document id
     */
    public function __construct(public array $byPid = [], public array $bytes = [], public ?SourceUnavailableException $listFailure = null)
    {
    }

    public function listForPatient(int $pid, string $username, int $limit): array
    {
        $this->listedFor[] = $username;
        if ($this->listFailure !== null) {
            throw $this->listFailure;
        }
        return array_slice($this->byPid[$pid] ?? [], 0, $limit);
    }

    public function readBytes(int $documentId, int $pid): string
    {
        $this->bytesRead[] = $documentId;
        $b = $this->bytes[$documentId] ?? new SourceUnavailableException('documents');
        if ($b instanceof \RuntimeException) {
            throw $b;
        }
        return $b;
    }

    /**
     * @param list<string> $categories
     * @return array{document_id:int, uploaded_at:string, media_type:?string, size:int, category_names:list<string>}
     */
    public static function doc(int $id, array $categories = ['Lab Report'], ?string $mediaType = 'application/pdf', int $size = 1000, string $uploadedAt = '2026-09-20 10:00:00'): array
    {
        return ['document_id' => $id, 'uploaded_at' => $uploadedAt, 'media_type' => $mediaType, 'size' => $size, 'category_names' => $categories];
    }
}

/** In-memory copilot_document / copilot_extracted_value with the SQL repository's semantics. */
final class FakeProcessingRepository implements ProcessingRepositoryInterface
{
    /** @var array<int, array<string,mixed>> document_id -> record */
    public array $records = [];

    /** @var array<int, list<array<string,mixed>>> document_id -> candidate rows (with id, status) */
    public array $values = [];

    public int $nextValueId = 1;

    public bool $failSave = false;

    /** @var list<int> documents whose claim is refused as if another request held it */
    public array $heldElsewhere = [];

    public function findRecords(int $pid, array $documentIds): array
    {
        $out = [];
        foreach ($documentIds as $id) {
            $r = $this->records[$id] ?? null;
            if ($r !== null && $r['pid'] === $pid) {
                $pending = count(array_filter($this->values[$id] ?? [], static fn(array $v): bool => $v['status'] === 'candidate'));
                $out[$id] = $r + ['pending_count' => $pending];
            }
        }
        return $out;
    }

    public function findSamePatientDuplicate(int $pid, string $sha256, int $excludingDocumentId): ?int
    {
        foreach ($this->records as $id => $r) {
            if ($id !== $excludingDocumentId && $r['pid'] === $pid && $r['content_sha256'] === $sha256 && in_array($r['status'], ['extracted', 'held_identity'], true)) {
                return $id;
            }
        }
        return null;
    }

    public function hashExistsForOtherPatient(int $pid, string $sha256): bool
    {
        foreach ($this->records as $r) {
            if ($r['pid'] !== $pid && $r['content_sha256'] === $sha256) {
                return true;
            }
        }
        return false;
    }

    public function claim(int $documentId, int $pid, string $sha256, string $docType, int $maxAttempts, int $staleSeconds): bool
    {
        if (in_array($documentId, $this->heldElsewhere, true)) {
            return false;
        }
        $r = $this->records[$documentId] ?? null;
        if ($r === null) {
            $this->records[$documentId] = [
                'document_id' => $documentId, 'pid' => $pid, 'content_sha256' => $sha256, 'doc_type' => $docType,
                'status' => 'processing', 'prompt_version' => null, 'attempts' => 1, 'last_error_code' => null,
                'identity_check' => null, 'extraction_json' => null, 'created_at' => '2026-09-25 10:00:00', 'updated_at' => '2026-09-25 10:00:00',
            ];
            return true;
        }
        $retryable = $r['status'] === 'queued' || ($r['status'] === 'failed' && $r['attempts'] < $maxAttempts) || ($r['status'] === 'processing' && ($r['stale'] ?? false));
        if ($r['pid'] !== $pid || !$retryable) {
            return false;
        }
        $this->records[$documentId] = ['status' => 'processing', 'attempts' => $r['attempts'] + 1, 'content_sha256' => $sha256, 'doc_type' => $docType, 'last_error_code' => null, 'stale' => false] + $r;
        return true;
    }

    public function saveExtraction(int $documentId, int $pid, string $status, string $identityCheck, ?string $promptVersion, string $extractionJson, array $candidates, ?string $errorCode): void
    {
        if ($this->failSave) {
            throw new RuntimeException('deadlock');
        }
        $this->records[$documentId] = ['status' => $status, 'identity_check' => $identityCheck, 'prompt_version' => $promptVersion, 'extraction_json' => $extractionJson, 'last_error_code' => $errorCode] + $this->records[$documentId];
        foreach ($candidates as $c) {
            $this->values[$documentId][] = ['id' => $this->nextValueId++, 'document_id' => $documentId, 'pid' => $pid, 'status' => 'candidate'] + $c;
        }
    }

    public function markStatus(int $documentId, string $status, ?string $errorCode): void
    {
        $this->records[$documentId] = ['status' => $status, 'last_error_code' => $errorCode] + $this->records[$documentId];
    }

    public function listExtractions(int $pid, int $limit): array
    {
        $out = [];
        foreach ($this->records as $id => $r) {
            if ($r['pid'] === $pid && $r['status'] === 'extracted' && is_string($r['extraction_json'])) {
                $out[] = ['document_id' => $id, 'doc_type' => $r['doc_type'], 'extraction_json' => $r['extraction_json']];
            }
        }
        return array_slice($out, 0, $limit);
    }

    public function listPendingFacts(int $pid, int $limit): array
    {
        $out = [];
        foreach ($this->values as $docId => $rows) {
            if (($this->records[$docId]['status'] ?? null) !== 'extracted') {
                continue;
            }
            foreach ($rows as $v) {
                if ($v['pid'] === $pid && $v['status'] === 'candidate') {
                    $out[] = [
                        'id' => $v['id'], 'document_id' => $docId, 'test_name' => $v['test_name'], 'value_text' => $v['value_text'],
                        'unit' => $v['unit'], 'reference_range' => $v['reference_range'], 'abnormal_flag' => $v['abnormal_flag'],
                        'flag_source' => $v['flag_source'], 'collection_date' => $v['collection_date'],
                        'verification_status' => $v['verification_status'], 'page' => $v['page'], 'bbox' => $v['bbox'],
                    ];
                }
            }
        }
        return array_slice($out, 0, $limit);
    }
}

final class FakeSchemaStatus implements SchemaStatusInterface
{
    public function __construct(private readonly bool $ready = true)
    {
    }

    public function isReady(): bool
    {
        return $this->ready;
    }
}
