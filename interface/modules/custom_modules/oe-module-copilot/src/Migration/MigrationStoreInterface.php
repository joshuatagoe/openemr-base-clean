<?php

/**
 * Database port for the migration runner, so its decisions are testable
 * without a database. Production: SqlMigrationStore.
 *
 * @package   OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Migration;

interface MigrationStoreInterface
{
    /** A named, connection-scoped lock (MariaDB GET_LOCK); false when another holder has it. */
    public function acquireLock(string $name, int $timeoutSeconds): bool;

    public function releaseLock(string $name): void;

    /**
     * Our row in core `modules`, found by directory (never by id).
     *
     * @return array{mod_id:int, sql_run:int, sql_version:string}|null
     */
    public function findModule(string $directory): ?array;

    /** Apply one guarded SQL file; throws on the first failing statement. */
    public function applySqlFile(string $fileName, string $directory): void;

    /** Record sql_run = 1 and sql_version for our row. */
    public function recordVersion(int $moduleId, string $version): void;
}
