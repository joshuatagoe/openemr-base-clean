<?php

/**
 * Reads the selected patient's documents from OpenEMR's own Documents feature
 * (core `documents` table, `foreign_id = pid`). No upload code of our own: the
 * file is whatever the clinic filed through core. listForPatient()/readBytes()
 * serve per-document processing (ADR-012) and apply core `can_access()`;
 * findLatestDocument() is the legacy single-document path.
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
use OpenEMR\Modules\Copilot\Documents\PatientDocumentSourceInterface;
use OpenEMR\Modules\Copilot\Support\Scalar;
use Throwable;

class SqlDocumentReader implements PatientDocumentSourceInterface
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

    /**
     * @return array{document_id:int, media_type:string, bytes:string}|null  null when nothing is on file
     * @throws SourceUnavailableException
     */
    public function findLatestDocument(int $pid): ?array
    {
        $types = array_keys(self::MEDIA_TYPES);
        try {
            $rows = QueryUtils::fetchRecords(
                "SELECT id, mimetype, size FROM documents"
                . " WHERE foreign_id = ? AND deleted = 0"
                . " AND date_expires IS NULL"
                . " AND LOWER(mimetype) IN (" . implode(',', array_fill(0, count($types), '?')) . ")"
                . " ORDER BY `date` DESC, id DESC LIMIT 1",
                array_merge([$pid], $types)
            );
        } catch (Throwable $e) {
            throw new SourceUnavailableException('documents', $e);
        }
        $row = $rows[0] ?? null;
        if (!is_array($row)) {
            return null;
        }
        $id = Scalar::int($row['id'] ?? null);
        $mediaType = self::MEDIA_TYPES[strtolower(Scalar::str($row['mimetype'] ?? null))] ?? null;
        if ($id <= 0 || $mediaType === null) {
            return null;
        }
        // Checked before reading so an oversized file is never loaded; `size` can be
        // NULL on older rows, so the byte length is checked again after reading.
        if (Scalar::int($row['size'] ?? null) > self::MAX_DOCUMENT_BYTES) {
            throw new DocumentTooLargeException($id);
        }
        try {
            $bytes = (new Document((string) $id))->get_data();
        } catch (Throwable $e) {
            throw new SourceUnavailableException('documents', $e);
        }
        if (!is_string($bytes) || $bytes === '') {
            throw new SourceUnavailableException('documents');
        }
        if (strlen($bytes) > self::MAX_DOCUMENT_BYTES) {
            throw new DocumentTooLargeException($id);
        }
        return ['document_id' => $id, 'media_type' => $mediaType, 'bytes' => $bytes];
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
            try {
                if (!(new Document((string) $id))->can_access($username)) {
                    continue;
                }
            } catch (Throwable $e) {
                throw new SourceUnavailableException('documents', $e);
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
