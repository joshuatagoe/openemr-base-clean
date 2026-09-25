<?php

/**
 * SchemaStatusInterface over OpenEMR's connection; the answer is cached for the
 * request once the tables are found (they are never dropped automatically).
 *
 * @package   OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Data;

use OpenEMR\Common\Database\QueryUtils;
use Throwable;

final class SqlSchemaStatus implements SchemaStatusInterface
{
    private bool $ready = false;

    public function isReady(): bool
    {
        if ($this->ready) {
            return true;
        }
        try {
            foreach (self::TABLES as $table) {
                if (!QueryUtils::existsTable($table)) {
                    return false;
                }
            }
        } catch (Throwable) {
            return false;
        }
        return $this->ready = true;
    }
}
