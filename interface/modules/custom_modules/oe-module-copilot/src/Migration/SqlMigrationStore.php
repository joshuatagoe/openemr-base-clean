<?php

/**
 * Production MigrationStoreInterface over OpenEMR's own connection.
 *
 * SQL files are applied with core's SQLUpgradeService exactly as
 * InstModuleTable::installSQLWithUpgradeService() does (throw on error, no
 * screen output), so the `#IfNotTable` / `#IfNotRow2D` guards behave as they do
 * for Manage Modules > Install SQL. The lock is MariaDB GET_LOCK, scoped to this
 * connection, so it ends with the process even if release is never reached.
 *
 * @package   OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Migration;

use OpenEMR\Common\Database\QueryUtils;
use OpenEMR\Modules\Copilot\Support\Scalar;
use OpenEMR\Services\Utils\SQLUpgradeService;

final class SqlMigrationStore implements MigrationStoreInterface
{
    public function acquireLock(string $name, int $timeoutSeconds): bool
    {
        $rows = QueryUtils::fetchRecords('SELECT GET_LOCK(?, ?) AS got', [$name, $timeoutSeconds], true);
        return Scalar::int($rows[0]['got'] ?? null) === 1;
    }

    public function releaseLock(string $name): void
    {
        QueryUtils::fetchRecords('SELECT RELEASE_LOCK(?) AS released', [$name], true);
    }

    public function findModule(string $directory): ?array
    {
        $rows = QueryUtils::fetchRecords(
            'SELECT mod_id, sql_run, sql_version FROM modules WHERE mod_directory = ? ORDER BY mod_id ASC LIMIT 1',
            [$directory],
            true
        );
        $row = $rows[0] ?? null;
        if (!is_array($row)) {
            return null;
        }
        return [
            'mod_id' => Scalar::int($row['mod_id'] ?? null),
            'sql_run' => Scalar::int($row['sql_run'] ?? null),
            'sql_version' => Scalar::str($row['sql_version'] ?? null),
        ];
    }

    public function applySqlFile(string $fileName, string $directory): void
    {
        $service = new SQLUpgradeService();
        $service->setThrowExceptionOnError(true);
        $service->setRenderOutputToScreen(false);
        $service->upgradeFromSqlFile($fileName, $directory);
    }

    public function recordVersion(int $moduleId, string $version): void
    {
        QueryUtils::sqlStatementThrowException(
            'UPDATE modules SET sql_run = 1, sql_version = ?, date = NOW() WHERE mod_id = ?',
            [$version, $moduleId]
        );
    }
}
