<?php

/**
 * Raised when a clinical source could not be read. Carries only the source
 * name; the underlying database error is never included in the message.
 *
 * @package   OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Data;

use RuntimeException;
use Throwable;

final class SourceUnavailableException extends RuntimeException
{
    public function __construct(private readonly string $source, ?Throwable $previous = null)
    {
        parent::__construct("clinical source unavailable: {$source}", 0, $previous);
    }

    public function getSource(): string
    {
        return $this->source;
    }
}
