<?php

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Tests;

use OpenEMR\Modules\Copilot\Data\SchemaStatusInterface;
use OpenEMR\Modules\Copilot\Data\SourceUnavailableException;
use OpenEMR\Modules\Copilot\Documents\DocumentValuesReaderInterface;
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
            // 'live' => false models a copy deleted or moved away in OpenEMR.
            if ($r['pid'] !== $pid && $r['content_sha256'] === $sha256 && ($r['live'] ?? true)) {
                return true;
            }
        }
        return false;
    }

    public function releaseMovedRecord(int $documentId, int $pid): bool
    {
        $r = $this->records[$documentId] ?? null;
        if ($r === null || $r['pid'] === $pid) {
            return true;
        }
        foreach ($this->values[$documentId] ?? [] as $v) {
            if (in_array($v['status'], ProcessingRepositoryInterface::FILED_STATUSES, true)) {
                return false;
            }
        }
        unset($this->records[$documentId], $this->values[$documentId]);
        return true;
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

    /** @var list<int> documents whose record changes between the read and the conditional update */
    public array $confirmRace = [];

    public function confirmHeldPatient(int $documentId, int $pid, string $resolutionCode): bool
    {
        $r = $this->records[$documentId] ?? null;
        if ($r === null || $r['pid'] !== $pid || $r['status'] !== 'held_identity' || in_array($documentId, $this->confirmRace, true)) {
            return false;
        }
        $this->records[$documentId] = ['status' => 'extracted', 'last_error_code' => $resolutionCode] + $r;
        return true;
    }

    public function listExtractions(int $pid, int $limit): array
    {
        // Like the SQL: extracted lab/intake documents with a value still waiting (a `candidate` row),
        // the newest `$limit` by document id, returned oldest first.
        $ids = [];
        foreach ($this->records as $id => $r) {
            if ($r['pid'] === $pid && $r['status'] === 'extracted' && is_string($r['extraction_json'] ?? null)
                && in_array($r['doc_type'], ['lab_pdf', 'intake_form'], true) && $this->hasCandidate($id)) {
                $ids[] = $id;
            }
        }
        rsort($ids);
        $ids = array_reverse(array_slice($ids, 0, max(1, min($limit, 50))));
        $out = [];
        foreach ($ids as $id) {
            $reviewed = [];
            foreach ($this->values[$id] ?? [] as $v) {
                if (in_array($v['status'], ['filed', 'rejected', 'unfiled'], true) && isset($v['result_index'])) {
                    $reviewed[] = (int) $v['result_index'];
                }
            }
            $out[] = ['document_id' => $id, 'doc_type' => $this->records[$id]['doc_type'], 'extraction_json' => $this->records[$id]['extraction_json'], 'reviewed_indices' => $reviewed, 'received_at' => $this->records[$id]['received_at'] ?? null];
        }
        return $out;
    }

    /** Test helper: give each document one value waiting for review (a `candidate` row at result 0). */
    public function waiting(int ...$documentIds): void
    {
        foreach ($documentIds as $id) {
            $this->values[$id][] = ['id' => $this->nextValueId++, 'document_id' => $id, 'pid' => $this->records[$id]['pid'], 'result_index' => 0, 'status' => 'candidate'];
        }
    }

    public function countExtractions(int $pid): array
    {
        $extracted = 0;
        $waiting = 0;
        foreach ($this->records as $id => $r) {
            if ($r['pid'] === $pid && $r['status'] === 'extracted' && is_string($r['extraction_json']) && in_array($r['doc_type'], ['lab_pdf', 'intake_form'], true)) {
                $extracted++;
                if ($this->hasCandidate($id)) {
                    $waiting++;
                }
            }
        }
        return ['extracted' => $extracted, 'waiting' => $waiting];
    }

    private function hasCandidate(int $documentId): bool
    {
        foreach ($this->values[$documentId] ?? [] as $v) {
            if ($v['status'] === 'candidate') {
                return true;
            }
        }
        return false;
    }

    public function listPendingFacts(int $pid, int $limit): array
    {
        $out = [];
        foreach ($this->values as $docId => $rows) {
            if (($this->records[$docId]['status'] ?? null) !== 'extracted' || ($this->records[$docId]['doc_type'] ?? 'lab_pdf') !== 'lab_pdf') {
                continue; // like the SQL: only lab candidates are pending lab facts (intake is patient-reported evidence)
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

/** In-memory processing records and candidate values for the values read route. */
final class FakeDocumentValues implements DocumentValuesReaderInterface
{
    /** @var array<int, array{pid:int, status:string, identity_check:?string, doc_type:string, last_error_code:?string}> */
    public array $records = [];

    /** @var array<int, list<array<string,mixed>>> document_id -> value rows (pid defaults to the record's) */
    public array $rows = [];

    /** @var list<int> documents whose values were read */
    public array $read = [];

    public function findRecord(int $documentId, int $pid): ?array
    {
        $r = $this->records[$documentId] ?? null;
        if ($r === null || $r['pid'] !== $pid) {
            return null;
        }
        return ['status' => $r['status'], 'identity_check' => $r['identity_check'], 'doc_type' => $r['doc_type'], 'last_error_code' => $r['last_error_code']];
    }

    public function listValues(int $documentId, int $pid): array
    {
        $this->read[] = $documentId;
        $owner = $this->records[$documentId]['pid'] ?? null;
        return $owner === $pid ? ($this->rows[$documentId] ?? []) : [];
    }

    /**
     * @param array<string,mixed> $overrides
     * @return array<string,mixed>
     */
    public static function row(int $resultIndex, array $overrides = []): array
    {
        return array_merge([
            'result_index' => $resultIndex, 'test_name' => 'Hemoglobin A1c', 'value_text' => '7.1', 'unit' => '%',
            'reference_range' => '4.0-5.6', 'abnormal_flag' => 'H', 'flag_source' => 'extracted', 'collection_date' => '2026-09-01',
            'verification_status' => 'verified_exact', 'page' => 1, 'bbox' => '0.1,0.2,0.3,0.25', 'status' => 'candidate',
            'procedure_result_id' => null,
        ], $overrides);
    }
}
