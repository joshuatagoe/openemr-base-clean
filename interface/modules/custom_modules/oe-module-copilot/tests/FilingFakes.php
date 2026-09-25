<?php

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Tests;

use OpenEMR\Modules\Copilot\Authorization\AclWriteCheckerInterface;
use OpenEMR\Modules\Copilot\Data\SourceUnavailableException;
use OpenEMR\Modules\Copilot\Documents\DocumentFileSourceInterface;
use OpenEMR\Modules\Copilot\Filing\FilingStoreInterface;

/** In-memory core Documents for the file route and filing: one row per id, with its owner. */
final class FakeFileSource implements DocumentFileSourceInterface
{
    /** @var list<int> */
    public array $bytesRead = [];

    /** @var list<int> */
    public array $accessChecked = [];

    /**
     * @param array<int, array{pid:int, media_type:string, size:int}> $docs  rows that pass the lookup's filters
     * @param array<int, string|\RuntimeException> $bytes
     * @param list<int> $denied  documents core can_access() refuses
     */
    public function __construct(
        public array $docs = [],
        public array $bytes = [],
        public array $denied = [],
        public ?SourceUnavailableException $lookupFailure = null,
    ) {
    }

    public function findForPatient(int $documentId, int $pid): ?array
    {
        if ($this->lookupFailure !== null) {
            throw $this->lookupFailure;
        }
        $d = $this->docs[$documentId] ?? null;
        if ($d === null || $d['pid'] !== $pid) {
            return null;
        }
        return ['document_id' => $documentId, 'media_type' => $d['media_type'], 'size' => $d['size']];
    }

    public function canAccess(int $documentId, string $username): bool
    {
        $this->accessChecked[] = $documentId;
        return !in_array($documentId, $this->denied, true);
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
}

/** phpGACL write-level checks, keyed "section/value". */
final class FakeWriteAcl implements AclWriteCheckerInterface
{
    /** @param array<string,bool> $grants */
    public function __construct(private readonly array $grants = [])
    {
    }

    public function checkWrite(string $section, string $value, string $username): bool
    {
        return $this->grants["{$section}/{$value}"] ?? false;
    }
}

/**
 * In-memory filing store with the SQL store's transaction semantics: every
 * write outside transaction() is an error, and a throwing transaction leaves
 * no trace. `$failAt` names a write method that throws once reached.
 */
final class FakeFilingStore implements FilingStoreInterface
{
    /** @var array<int, array{pid:int, status:string, doc_type:string, extraction_json:?string}> */
    public array $documents = [];
    /** @var array<int, array<string,mixed>> candidate id -> row */
    public array $candidates = [];
    /** @var array<int, array<string,mixed>> */
    public array $orders = [];
    /** @var array<int, array<string,mixed>> */
    public array $reports = [];
    /** @var array<int, array<string,mixed>> */
    public array $results = [];
    /** @var array<int, string> */
    public array $labs = [];
    /** @var list<array{pid:int, test_name:string, code:string, date:string, value:string, status:string}> results already in the chart */
    public array $chart = [];

    public ?string $failAt = null;
    public int $transactions = 0;
    private bool $inTransaction = false;
    private int $nextId = 100;

    public function transaction(callable $work): mixed
    {
        $snapshot = [$this->documents, $this->candidates, $this->orders, $this->reports, $this->results, $this->labs, $this->nextId];
        $this->inTransaction = true;
        $this->transactions++;
        try {
            return $work();
        } catch (\Throwable $e) {
            [$this->documents, $this->candidates, $this->orders, $this->reports, $this->results, $this->labs, $this->nextId] = $snapshot;
            throw $e;
        } finally {
            $this->inTransaction = false;
        }
    }

    public function lockDocument(int $documentId): ?array
    {
        $this->mustBeInTransaction();
        return $this->documents[$documentId] ?? null;
    }

    public function lockCandidate(int $documentId, int $resultIndex): ?array
    {
        $this->mustBeInTransaction();
        foreach ($this->candidates as $c) {
            if ($c['document_id'] === $documentId && $c['result_index'] === $resultIndex) {
                return $c;
            }
        }
        return null;
    }

    public function findDocumentReport(int $documentId): ?int
    {
        foreach ($this->candidates as $c) {
            if ($c['document_id'] === $documentId && $c['procedure_result_id'] !== null) {
                return $this->results[$c['procedure_result_id']]['procedure_report_id'];
            }
        }
        return null;
    }

    public function findOrCreateOutsideLab(): int
    {
        $this->write('findOrCreateOutsideLab');
        $id = array_search(FilingStoreInterface::OUTSIDE_LAB_NAME, $this->labs, true);
        if ($id !== false) {
            return $id;
        }
        $id = $this->nextId++;
        $this->labs[$id] = FilingStoreInterface::OUTSIDE_LAB_NAME;
        return $id;
    }

    public function createOrderAndReport(int $pid, int $userId, int $labId, string $collectedAt): int
    {
        $this->write('createOrderAndReport');
        $orderId = $this->nextId++;
        $this->orders[$orderId] = ['pid' => $pid, 'provider_id' => $userId, 'lab_id' => $labId, 'date' => $collectedAt, 'codes' => [1]];
        $reportId = $this->nextId++;
        $this->reports[$reportId] = ['order_id' => $orderId, 'date_collected' => $collectedAt, 'date_report' => $collectedAt, 'review_status' => 'reviewed', 'source' => $userId];
        return $reportId;
    }

    public function insertResult(int $reportId, array $result): int
    {
        $this->write('insertResult');
        $id = $this->nextId++;
        $this->results[$id] = ['procedure_report_id' => $reportId] + $result;
        return $id;
    }

    public function markFiled(int $candidateId, int $userId, string $filedValue, int $resultId): void
    {
        $this->write('markFiled');
        $this->candidates[$candidateId] = ['status' => 'filed', 'filed_by' => $userId, 'filed_value' => $filedValue, 'procedure_result_id' => $resultId] + $this->candidates[$candidateId];
    }

    public function markRejected(int $candidateId): void
    {
        $this->write('markRejected');
        $this->candidates[$candidateId] = ['status' => 'rejected'] + $this->candidates[$candidateId];
    }

    public function sameResultInChart(int $pid, string $testName, string $code, string $collectionDate, string $value): bool
    {
        foreach ($this->chart as $r) {
            if ($r['pid'] === $pid && $r['date'] === $collectionDate && $r['value'] === $value && $r['status'] !== 'entered-in-error'
                && (strcasecmp($r['test_name'], $testName) === 0 || ($code !== '' && $r['code'] === $code))) {
                return true;
            }
        }
        return false;
    }

    /** @param array<string,mixed> $overrides */
    public function addCandidate(int $documentId, int $pid, int $resultIndex, array $overrides = []): int
    {
        $id = $this->nextId++;
        $this->candidates[$id] = $overrides + [
            'id' => $id, 'document_id' => $documentId, 'pid' => $pid, 'result_index' => $resultIndex,
            'test_name' => 'Hemoglobin A1c', 'value_text' => '7.1', 'unit' => '%', 'reference_range' => '4.0-5.6',
            'abnormal_flag' => 'H', 'flag_source' => 'extracted', 'collection_date' => '2026-09-01',
            'verification_status' => 'verified_exact', 'status' => 'candidate', 'filed_value' => null, 'procedure_result_id' => null,
        ];
        return $id;
    }

    private function mustBeInTransaction(): void
    {
        if (!$this->inTransaction) {
            throw new \LogicException('filing store used outside a transaction');
        }
    }

    private function write(string $method): void
    {
        $this->mustBeInTransaction();
        if ($this->failAt === $method) {
            throw new \RuntimeException('simulated deadlock');
        }
    }
}
