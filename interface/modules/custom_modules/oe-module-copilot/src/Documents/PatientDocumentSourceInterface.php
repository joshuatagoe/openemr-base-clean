<?php

/**
 * The patient's documents in OpenEMR's own Documents feature, as the module
 * may see them. Production: Data\SqlDocumentReader.
 *
 * @package   OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Documents;

use OpenEMR\Modules\Copilot\Data\DocumentTooLargeException;
use OpenEMR\Modules\Copilot\Data\SourceUnavailableException;

interface PatientDocumentSourceInterface
{
    /**
     * Non-deleted documents filed to the patient that `$username` may access
     * (core `Document::can_access()`), newest first, at most `$limit`.
     * `media_type` is the agent media type, or null when the file type is not
     * supported. Category names are returned as stored.
     *
     * @return list<array{document_id:int, uploaded_at:string, media_type:?string, size:int, category_names:list<string>}>
     * @throws SourceUnavailableException
     */
    public function listForPatient(int $pid, string $username, int $limit): array;

    /**
     * The original bytes through core `Document::get_data()`; only for a
     * document filed to `$pid`.
     *
     * @throws DocumentTooLargeException
     * @throws SourceUnavailableException
     */
    public function readBytes(int $documentId, int $pid): string;
}
