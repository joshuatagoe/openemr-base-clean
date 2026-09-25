<?php

/**
 * Reads the selected patient's documents from OpenEMR's own Documents feature
 * (core `documents` table, `foreign_id = pid`). No upload code of our own: the
 * file is whatever the clinic filed through core. listForPatient()/readBytes()
 * serve per-document processing (ADR-012); findForPatient()/canAccess() serve
 * the source-file route and filing (ADR-008/009); findLatestDocument() is the
 * legacy single-document path. Every path applies core `can_access()` for the
 * session user before a document's bytes are read.
 *
 * The row is chosen here (newest `date`, then highest `id`; PDF/PNG/JPEG only;
 * not deleted, no expiry set); the bytes are read through core `Document::get_data()`
 * so file-system vs CouchDB storage and at-rest encryption are handled exactly
 * as core handles them.
 *
 * Only rows with no `date_expires` are considered: core `Document::has_expired()`
 * in this checkout returns true for an expiry date in the *future*, so
 * `get_data()` would refuse such a row.
 *
 * @package   OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Data;

use Document;
use OpenEMR\Common\Database\QueryUtils;
use OpenEMR\Modules\Copilot\Documents\DocumentFileSourceInterface;
use OpenEMR\Modules\Copilot\Documents\PatientDocumentSourceInterface;
use OpenEMR\Modules\Copilot\Support\Scalar;
use Throwable;

class SqlDocumentReader implements PatientDocumentSourceInterface, DocumentFileSourceInterface
{
    /** Matches the agent's MAX_DOCUMENT_BYTES (app/document_briefing.py). */
    public const MAX_DOCUMENT_BYTES = 10 * 1024 * 1024;

    /** Stored mimetype -> media type the agent accepts. */
    public const MEDIA_TYPES = [
        'application/pdf' => 'application/pdf',
        'image/png' => 'image/png',
        'image/jpeg' => 'image/jpeg',
        'image/jpg' => 'image/jpeg',
        'image/pjpeg' => 'image/jpeg',
    ];

    /** Documents looked at for the newest one `$username` may access (legacy path). */
    public const LATEST_CANDIDATES = 20;

    /**
     * The newest supported document of the patient that `$username` may access
     * (core `can_access()`); documents it may not are skipped, never read.
     *
     * @return array{document_id:int, media_type:string, bytes:string}|null  null when nothing is on file
     * @throws SourceUnavailableException
     */
    public function findLatestDocument(int $pid, string $username): ?array
    {
        $types = array_keys(self::MEDIA_TYPES);
        try {
            $rows = QueryUtils::fetchRecords(
                "SELECT id, mimetype, size FROM documents"
                . " WHERE foreign_id = ? AND deleted = 0"
                . " AND date_expires IS NULL"
                . " AND LOWER(mimetype) IN (" . implode(',', array_fill(0, count($types), '?')) . ")"
                . " ORDER BY `date` DESC, id DESC LIMIT " . self::LATEST_CANDIDATES,
                array_merge([$pid], $types)
            );
        } catch (Throwable $e) {
            throw new SourceUnavailableException('documents', $e);
        }
        foreach ($rows as $row) {
            $id = Scalar::int($row['id'] ?? null);
            $mediaType = self::MEDIA_TYPES[strtolower(Scalar::str($row['mimetype'] ?? null))] ?? null;
            if ($id <= 0 || $mediaType === null || !$this->canAccess($id, $username)) {
                continue;
            }
            // Checked before reading so an oversized file is never loaded; `size` can be
            // NULL on older rows, so the byte length is checked again after reading.
            if (Scalar::int($row['size'] ?? null) > self::MAX_DOCUMENT_BYTES) {
                throw new DocumentTooLargeException($id);
            }
            return ['document_id' => $id, 'media_type' => $mediaType, 'bytes' => $this->readBytes($id, $pid)];
        }
        return null;
    }

    public function findForPatient(int $documentId, int $pid): ?array
    {
        $types = array_keys(self::MEDIA_TYPES);
        try {
            $rows = QueryUtils::fetchRecords(
                "SELECT id, mimetype, size FROM documents"
                . " WHERE id = ? AND foreign_id = ? AND deleted = 0 AND date_expires IS NULL"
                . " AND LOWER(mimetype) IN (" . implode(',', array_fill(0, count($types), '?')) . ")"
                . " LIMIT 1",
                array_merge([$documentId, $pid], $types)
            );
        } catch (Throwable $e) {
            throw new SourceUnavailableException('documents', $e);
        }
        $row = $rows[0] ?? null;
        if (!is_array($row)) {
            return null;
        }
        $mediaType = self::MEDIA_TYPES[strtolower(Scalar::str($row['mimetype'] ?? null))] ?? null;
        if ($mediaType === null) {
            return null;
        }
        return ['document_id' => $documentId, 'media_type' => $mediaType, 'size' => Scalar::int($row['size'] ?? null)];
    }

    public function canAccess(int $documentId, string $username): bool
    {
        try {
            return (new Document((string) $documentId))->can_access($username);
        } catch (Throwable $e) {
            throw new SourceUnavailableException('documents', $e);
        }
    }

    public function listForPatient(int $pid, string $username, int $limit): array
    {
        try {
            $rows = QueryUtils::fetchRecords(
                "SELECT d.id, d.`date` AS uploaded_at, d.mimetype, d.size,"
                . " GROUP_CONCAT(c.name ORDER BY c.id SEPARATOR '\\n') AS category_names"
                . " FROM documents d"
                . " LEFT JOIN categories_to_documents ctd ON ctd.document_id = d.id"
                . " LEFT JOIN categories c ON c.id = ctd.category_id"
                . " WHERE d.foreign_id = ? AND d.deleted = 0 AND d.date_expires IS NULL"
                . " GROUP BY d.id, d.`date`, d.mimetype, d.size"
                . " ORDER BY d.`date` DESC, d.id DESC"
                . " LIMIT " . max(1, min($limit, 200)),
                [$pid]
            );
        } catch (Throwable $e) {
            throw new SourceUnavailableException('documents', $e);
        }
        $out = [];
        foreach ($rows as $row) {
            $id = Scalar::int($row['id'] ?? null);
            if ($id <= 0) {
                continue;
            }
            if (!$this->canAccess($id, $username)) {
                continue;
            }
            $names = Scalar::str($row['category_names'] ?? null);
            $out[] = [
                'document_id' => $id,
                'uploaded_at' => Scalar::str($row['uploaded_at'] ?? null),
                'media_type' => self::MEDIA_TYPES[strtolower(Scalar::str($row['mimetype'] ?? null))] ?? null,
                'size' => Scalar::int($row['size'] ?? null),
                'category_names' => $names === '' ? [] : explode("\n", $names),
            ];
        }
        return $out;
    }

    public function readBytes(int $documentId, int $pid): string
    {
        try {
            $rows = QueryUtils::fetchRecords(
                "SELECT size FROM documents WHERE id = ? AND foreign_id = ? AND deleted = 0 AND date_expires IS NULL LIMIT 1",
                [$documentId, $pid]
            );
        } catch (Throwable $e) {
            throw new SourceUnavailableException('documents', $e);
        }
        $row = $rows[0] ?? null;
        if (!is_array($row)) {
            throw new SourceUnavailableException('documents');
        }
        if (Scalar::int($row['size'] ?? null) > self::MAX_DOCUMENT_BYTES) {
            throw new DocumentTooLargeException($documentId);
        }
        try {
            $bytes = (new Document((string) $documentId))->get_data();
        } catch (Throwable $e) {
            throw new SourceUnavailableException('documents', $e);
        }
        if (!is_string($bytes) || $bytes === '') {
            throw new SourceUnavailableException('documents');
        }
        if (strlen($bytes) > self::MAX_DOCUMENT_BYTES) {
            throw new DocumentTooLargeException($documentId);
        }
        return $bytes;
    }
}
