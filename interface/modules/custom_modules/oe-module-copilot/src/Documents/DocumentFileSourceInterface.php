<?php

/**
 * One patient document as the source-file route and filing may see it
 * (ADR-008 §3, ADR-009). Production: Data\SqlDocumentReader.
 *
 * @package   OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Documents;

use OpenEMR\Modules\Copilot\Data\DocumentTooLargeException;
use OpenEMR\Modules\Copilot\Data\SourceUnavailableException;

interface DocumentFileSourceInterface
{
    /**
     * One lookup: the document with this id filed to `$pid`, not deleted, no
     * expiry set, of an allow-listed type. Null otherwise - a missing document
     * and another patient's document are indistinguishable to the caller.
     * `media_type` is the allow-listed media type; `size` the stored size (0
     * when unknown).
     *
     * @return array{document_id:int, media_type:string, size:int}|null
     * @throws SourceUnavailableException
     */
    public function findForPatient(int $documentId, int $pid): ?array;

    /**
     * Core `Document::can_access()`: may `$username` access every category the
     * document is filed under.
     *
     * @throws SourceUnavailableException
     */
    public function canAccess(int $documentId, string $username): bool;

    /**
     * The original bytes through core `Document::get_data()` (decrypted in
     * memory, no temporary files); only for a document filed to `$pid`.
     *
     * @throws DocumentTooLargeException
     * @throws SourceUnavailableException
     */
    public function readBytes(int $documentId, int $pid): string;
}
