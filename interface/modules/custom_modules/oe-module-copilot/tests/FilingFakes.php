<?php

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Tests;

use OpenEMR\Modules\Copilot\Data\SourceUnavailableException;
use OpenEMR\Modules\Copilot\Documents\DocumentFileSourceInterface;

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
