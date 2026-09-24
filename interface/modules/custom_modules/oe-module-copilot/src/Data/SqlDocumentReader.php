<?php

/**
 * Reads the selected patient's most recent lab document from OpenEMR's own
 * Documents feature (core `documents` table, `foreign_id = pid`). No upload
 * code of our own: the file is whatever the clinic filed through core.
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
use OpenEMR\Modules\Copilot\Support\Scalar;
use Throwable;

class SqlDocumentReader
{
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
                "SELECT id, mimetype FROM documents"
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
        try {
            $bytes = (new Document((string) $id))->get_data();
        } catch (Throwable $e) {
            throw new SourceUnavailableException('documents', $e);
        }
        if (!is_string($bytes) || $bytes === '') {
            throw new SourceUnavailableException('documents');
        }
        return ['document_id' => $id, 'media_type' => $mediaType, 'bytes' => $bytes];
    }
}
