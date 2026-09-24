<?php

/**
 * The newest document on file is larger than the Co-Pilot will read.
 *
 * Raised by SqlDocumentReader before the file is base64-encoded and sent, so an
 * oversized upload is reported to the physician by name instead of surfacing as
 * an agent failure. The limit matches the agent's MAX_DOCUMENT_BYTES.
 *
 * @package   OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Data;

use RuntimeException;

final class DocumentTooLargeException extends RuntimeException
{
    public function __construct(private readonly int $documentId)
    {
        parent::__construct("document {$documentId} exceeds the size limit");
    }

    public function getDocumentId(): int
    {
        return $this->documentId;
    }
}
