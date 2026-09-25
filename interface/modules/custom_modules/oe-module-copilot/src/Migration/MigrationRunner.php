<?php

/**
 * The module's own schema migration runner (ADR-009 sections 6 and 7a).
 *
 * OpenEMR's `openemr:zfc-module` CLI cannot be used: it resolves module id 1.
 * This runner finds our `modules` row by directory and, under a named lock:
 *  - not yet installed (sql_run != 1 or no sql_version): applies sql/install.sql;
 *  - installed at an older version: applies sql/install.sql (guarded, so a no-op
 *    for tables that exist) and then each `X_Y_Z-to-A_B_C_upgrade.sql` whose
 *    from-version is >= the recorded version and < the code version, in version
 *    order (core's own selection rule). Every statement in every file must be
 *    guarded (#IfNotTable, #IfMissingColumn, ...);
 *  - recorded version >= code version: does nothing.
 * Files go through the store (OpenEMR's SQLUpgradeService in production), so the
 * `#IfNotTable`-style guards apply. The version is recorded only after every file
 * succeeded. Automatic migrations may not be destructive: a file containing
 * DROP TABLE / DROP COLUMN / DROP DATABASE / TRUNCATE / DELETE FROM outside a
 * comment line is refused before anything runs.
 *
 * `run()` never throws and returns one fixed outcome code; the logger receives
 * codes, versions and file names only, never SQL or exception messages.
 *
 * @package   OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Migration;

use Throwable;

final class MigrationRunner
{
    public const LOCK_NAME = 'openemr_copilot_module_migration';
    public const LOCK_TIMEOUT_SECONDS = 0;

    public const CODE_INSTALLED = 'installed';
    public const CODE_UPGRADED = 'upgraded';
    public const CODE_UP_TO_DATE = 'up_to_date';
    public const CODE_LOCK_BUSY = 'lock_busy';
    public const CODE_MODULE_NOT_REGISTERED = 'module_not_registered';
    public const CODE_NO_INSTALL_FILE = 'no_install_file';
    public const CODE_SQL_FAILED = 'sql_failed';
    public const CODE_DESTRUCTIVE_SQL_REFUSED = 'destructive_sql_refused';
    public const CODE_DB_UNAVAILABLE = 'db_unavailable';
    public const CODE_INTERNAL_ERROR = 'internal_error';

    private const UPGRADE_FILE = '/^(\d+)_(\d+)_(\d+)-to-(\d+)_(\d+)_(\d+)_upgrade\.sql$/';

    /** @var callable(string, array<string,mixed>): void */
    private $log;

    /**
     * @param callable(string, array<string,mixed>): void|null $log  receives a code and a code-only context
     */
    public function __construct(
        private readonly MigrationStoreInterface $store,
        private readonly string $moduleDirectory,
        private readonly string $sqlDirectory,
        private readonly string $codeVersion,
        ?callable $log = null,
    ) {
        $this->log = $log ?? static function (string $code, array $context): void {
        };
    }

    public function run(): string
    {
        try {
            $locked = $this->store->acquireLock(self::LOCK_NAME, self::LOCK_TIMEOUT_SECONDS);
        } catch (Throwable $e) {
            return $this->done(self::CODE_DB_UNAVAILABLE, ['type' => $e::class]);
        }
        if (!$locked) {
            return $this->done(self::CODE_LOCK_BUSY);
        }
        try {
            return $this->migrate();
        } catch (Throwable $e) {
            return $this->done(self::CODE_INTERNAL_ERROR, ['type' => $e::class]);
        } finally {
            try {
                $this->store->releaseLock(self::LOCK_NAME);
            } catch (Throwable) {
                // The lock is connection-scoped and ends with the process anyway.
            }
        }
    }

    /** True when the SQL contains a destructive statement outside comment lines. */
    public static function isDestructive(string $sql): bool
    {
        foreach (preg_split('/\R/', $sql) ?: [] as $line) {
            if (preg_match('/^\s*(--|#)/', $line)) {
                continue;
            }
            if (preg_match('/\b(DROP\s+(TABLE|COLUMN|DATABASE|SCHEMA)|TRUNCATE|DELETE\s+FROM)\b/i', $line)) {
                return true;
            }
        }
        return false;
    }

    private function migrate(): string
    {
        try {
            $module = $this->store->findModule($this->moduleDirectory);
        } catch (Throwable $e) {
            return $this->done(self::CODE_DB_UNAVAILABLE, ['type' => $e::class]);
        }
        if ($module === null) {
            return $this->done(self::CODE_MODULE_NOT_REGISTERED);
        }
        $recorded = trim($module['sql_version']);
        $installed = $module['sql_run'] === 1 && $recorded !== '';

        if (!$installed) {
            if (!is_file($this->sqlDirectory . '/install.sql')) {
                return $this->done(self::CODE_NO_INSTALL_FILE);
            }
            $files = ['install.sql'];
            $success = self::CODE_INSTALLED;
        } else {
            if (version_compare($recorded, $this->codeVersion, '>=')) {
                return $this->done(self::CODE_UP_TO_DATE, ['version' => $recorded]);
            }
            // install.sql first: core's Manage Modules "Install" records the version even when a
            // release shipped no SQL (0.1.0), so an older recorded version does not prove the
            // current tables exist. Every statement is guarded, so this is a no-op when they do.
            $files = is_file($this->sqlDirectory . '/install.sql') ? ['install.sql'] : [];
            $files = array_merge($files, $this->pendingUpgrades($recorded));
            $success = self::CODE_UPGRADED;
        }

        foreach ($files as $file) {
            if (self::isDestructive((string) file_get_contents($this->sqlDirectory . '/' . $file))) {
                return $this->done(self::CODE_DESTRUCTIVE_SQL_REFUSED, ['file' => $file]);
            }
        }
        foreach ($files as $file) {
            try {
                $this->store->applySqlFile($file, $this->sqlDirectory);
            } catch (Throwable $e) {
                return $this->done(self::CODE_SQL_FAILED, ['file' => $file, 'type' => $e::class]);
            }
        }
        $this->store->recordVersion($module['mod_id'], $this->codeVersion);
        return $this->done($success, ['from' => $recorded === '' ? null : $recorded, 'to' => $this->codeVersion, 'files' => count($files)]);
    }

    /** @return list<string> */
    private function pendingUpgrades(string $recorded): array
    {
        $byVersion = [];
        foreach (scandir($this->sqlDirectory) ?: [] as $name) {
            if (!preg_match(self::UPGRADE_FILE, $name, $m)) {
                continue;
            }
            $from = "{$m[1]}.{$m[2]}.{$m[3]}";
            if (version_compare($from, $recorded, '>=') && version_compare($from, $this->codeVersion, '<')) {
                $byVersion[$from] = $name;
            }
        }
        uksort($byVersion, version_compare(...));
        return array_values($byVersion);
    }

    /** @param array<string,mixed> $context */
    private function done(string $code, array $context = []): string
    {
        try {
            ($this->log)($code, $context);
        } catch (Throwable) {
            // Logging never changes the outcome.
        }
        return $code;
    }
}
