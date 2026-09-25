<?php

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Tests;

use OpenEMR\Modules\Copilot\Migration\MigrationRunner;
use OpenEMR\Modules\Copilot\Migration\MigrationStoreInterface;
use OpenEMR\Modules\Copilot\Migration\ModuleVersion;
use PHPUnit\Framework\TestCase;
use RuntimeException;

/**
 * ADR-009 section 6/7a: the module's own migration runner. Resolves our `modules`
 * row by directory, applies install.sql (first time) or the versioned upgrade files
 * through the store (SQLUpgradeService in production), records sql_run/sql_version,
 * holds a named lock, logs codes only and never throws.
 */
final class MigrationRunnerTest extends TestCase
{
    private string $dir;

    protected function setUp(): void
    {
        $this->dir = sys_get_temp_dir() . '/copilot-mig-' . bin2hex(random_bytes(4));
        mkdir($this->dir);
        file_put_contents($this->dir . '/install.sql', "#IfNotTable t\nCREATE TABLE t (id INT);\n#EndIf\n");
    }

    protected function tearDown(): void
    {
        foreach (glob($this->dir . '/*') ?: [] as $f) {
            unlink($f);
        }
        rmdir($this->dir);
    }

    /** @param list<string> $logged */
    private function runner(FakeMigrationStore $store, string $version = '0.2.0', array &$logged = []): MigrationRunner
    {
        return new MigrationRunner($store, 'oe-module-copilot', $this->dir, $version, static function (string $code, array $ctx) use (&$logged): void {
            $logged[] = $code . ' ' . json_encode($ctx);
        });
    }

    public function testFreshModuleRunsInstallSqlOnceAndRecordsVersion(): void
    {
        $store = new FakeMigrationStore(['mod_id' => 6, 'sql_run' => 0, 'sql_version' => '']);
        self::assertSame(MigrationRunner::CODE_INSTALLED, $this->runner($store)->run());
        self::assertSame(['install.sql'], $store->applied);
        self::assertSame([[6, '0.2.0']], $store->recorded);
        self::assertSame([MigrationRunner::LOCK_NAME], $store->released);
    }

    public function testRerunAtTheSameVersionIsANoOp(): void
    {
        $store = new FakeMigrationStore(['mod_id' => 6, 'sql_run' => 1, 'sql_version' => '0.2.0']);
        self::assertSame(MigrationRunner::CODE_UP_TO_DATE, $this->runner($store)->run());
        self::assertSame([], $store->applied);
        self::assertSame([], $store->recorded);
    }

    public function testRecordedVersionNewerThanCodeIsLeftAlone(): void
    {
        $store = new FakeMigrationStore(['mod_id' => 6, 'sql_run' => 1, 'sql_version' => '0.3.0']);
        self::assertSame(MigrationRunner::CODE_UP_TO_DATE, $this->runner($store)->run());
        self::assertSame([], $store->applied);
    }

    public function testUpgradeAppliesOnlyPendingFilesInVersionOrder(): void
    {
        file_put_contents($this->dir . '/0_1_0-to-0_2_0_upgrade.sql', "SELECT 1;\n");
        file_put_contents($this->dir . '/0_3_0-to-0_4_0_upgrade.sql', "SELECT 1;\n");
        file_put_contents($this->dir . '/0_2_0-to-0_3_0_upgrade.sql', "SELECT 1;\n");
        file_put_contents($this->dir . '/0_10_0-to-0_11_0_upgrade.sql', "SELECT 1;\n");
        $store = new FakeMigrationStore(['mod_id' => 6, 'sql_run' => 1, 'sql_version' => '0.2.0']);
        self::assertSame(MigrationRunner::CODE_UPGRADED, $this->runner($store, '0.4.0')->run());
        // install.sql (the whole current schema, every statement guarded) first, then the steps.
        self::assertSame(['install.sql', '0_2_0-to-0_3_0_upgrade.sql', '0_3_0-to-0_4_0_upgrade.sql'], $store->applied);
        self::assertSame([[6, '0.4.0']], $store->recorded);
    }

    public function testModuleInstalledAtAnOlderVersionWithoutSqlGetsTheInstallSchema(): void
    {
        // Core's Manage Modules "Install" records sql_run=1 and the then-current version even
        // when the module shipped no SQL (Week 1, 0.1.0): the tables must still be created.
        $store = new FakeMigrationStore(['mod_id' => 6, 'sql_run' => 1, 'sql_version' => '0.1.0']);
        self::assertSame(MigrationRunner::CODE_UPGRADED, $this->runner($store)->run());
        self::assertSame(['install.sql'], $store->applied);
        self::assertSame([[6, '0.2.0']], $store->recorded);
    }

    public function testUnregisteredModuleSkipsWithoutApplyingAnything(): void
    {
        $store = new FakeMigrationStore(null);
        self::assertSame(MigrationRunner::CODE_MODULE_NOT_REGISTERED, $this->runner($store)->run());
        self::assertSame([], $store->applied);
        self::assertSame([MigrationRunner::LOCK_NAME], $store->released);
    }

    public function testBusyLockSkipsAndNeverReleasesAnotherHoldersLock(): void
    {
        $store = new FakeMigrationStore(['mod_id' => 6, 'sql_run' => 0, 'sql_version' => ''], lockAvailable: false);
        self::assertSame(MigrationRunner::CODE_LOCK_BUSY, $this->runner($store)->run());
        self::assertSame([], $store->applied);
        self::assertSame([], $store->released);
    }

    public function testFailedSqlDoesNotRecordTheVersionReleasesTheLockAndDoesNotThrow(): void
    {
        $store = new FakeMigrationStore(['mod_id' => 6, 'sql_run' => 0, 'sql_version' => ''], failOn: 'install.sql');
        $logged = [];
        self::assertSame(MigrationRunner::CODE_SQL_FAILED, $this->runner($store, '0.2.0', $logged)->run());
        self::assertSame([], $store->recorded);
        self::assertSame([MigrationRunner::LOCK_NAME], $store->released);
        // Codes only: the exception message (which can carry a statement) is never logged.
        self::assertStringNotContainsString('secret statement', implode("\n", $logged));
    }

    public function testStoreFailureBeforeTheLockIsACodeNotAnException(): void
    {
        $store = new FakeMigrationStore(null, lockThrows: true);
        self::assertSame(MigrationRunner::CODE_DB_UNAVAILABLE, $this->runner($store)->run());
    }

    public function testDestructiveUpgradeFileIsRefusedBeforeAnythingRuns(): void
    {
        file_put_contents($this->dir . '/0_2_0-to-0_3_0_upgrade.sql', "ALTER TABLE t ADD c INT;\n");
        file_put_contents($this->dir . '/0_3_0-to-0_4_0_upgrade.sql', "DROP TABLE copilot_document;\n");
        $store = new FakeMigrationStore(['mod_id' => 6, 'sql_run' => 1, 'sql_version' => '0.2.0']);
        self::assertSame(MigrationRunner::CODE_DESTRUCTIVE_SQL_REFUSED, $this->runner($store, '0.4.0')->run());
        self::assertSame([], $store->applied);
        self::assertSame([], $store->recorded);
    }

    public function testCommentedDestructiveWordsAreNotRefused(): void
    {
        file_put_contents($this->dir . '/install.sql', "-- no DROP TABLE here, and no DELETE FROM either\nCREATE TABLE t (id INT);\n");
        $store = new FakeMigrationStore(['mod_id' => 6, 'sql_run' => 0, 'sql_version' => '']);
        self::assertSame(MigrationRunner::CODE_INSTALLED, $this->runner($store)->run());
    }

    public function testMissingInstallFileIsACode(): void
    {
        unlink($this->dir . '/install.sql');
        $store = new FakeMigrationStore(['mod_id' => 6, 'sql_run' => 0, 'sql_version' => '']);
        self::assertSame(MigrationRunner::CODE_NO_INSTALL_FILE, $this->runner($store)->run());
    }

    public function testShippedInstallSqlIsGuardedAndNonDestructive(): void
    {
        $sql = (string) file_get_contents(__DIR__ . '/../sql/install.sql');
        self::assertStringContainsString('#IfNotTable copilot_document', $sql);
        self::assertStringContainsString('#IfNotTable copilot_extracted_value', $sql);
        self::assertStringContainsString('`extraction_json` MEDIUMTEXT NULL', $sql);
        self::assertStringContainsString('#IfNotRow2D list_options list_id proc_res_status option_id entered-in-error', $sql);
        self::assertFalse(MigrationRunner::isDestructive($sql));
        // SQLUpgradeService joins lines with spaces: an inline "--" comment would swallow the statement.
        foreach (explode("\n", $sql) as $line) {
            if (!preg_match('/^\s*(--|#)/', $line)) {
                self::assertStringNotContainsString('--', $line, $line);
            }
        }
    }

    public function testModuleVersionIsReadFromVersionPhp(): void
    {
        self::assertSame('0.2.0', ModuleVersion::fromFile(__DIR__ . '/../version.php'));
        self::assertNull(ModuleVersion::fromFile($this->dir . '/missing.php'));
    }
}

final class FakeMigrationStore implements MigrationStoreInterface
{
    /** @var list<string> */
    public array $applied = [];
    /** @var list<array{int,string}> */
    public array $recorded = [];
    /** @var list<string> */
    public array $released = [];

    /** @param array{mod_id:int, sql_run:int, sql_version:string}|null $module */
    public function __construct(
        private readonly ?array $module,
        private readonly bool $lockAvailable = true,
        private readonly ?string $failOn = null,
        private readonly bool $lockThrows = false,
    ) {
    }

    public function acquireLock(string $name, int $timeoutSeconds): bool
    {
        if ($this->lockThrows) {
            throw new RuntimeException('connection refused');
        }
        return $this->lockAvailable;
    }

    public function releaseLock(string $name): void
    {
        $this->released[] = $name;
    }

    public function findModule(string $directory): ?array
    {
        return $directory === 'oe-module-copilot' ? $this->module : null;
    }

    public function applySqlFile(string $fileName, string $directory): void
    {
        if ($fileName === $this->failOn) {
            throw new RuntimeException('secret statement failed');
        }
        $this->applied[] = $fileName;
    }

    public function recordVersion(int $moduleId, string $version): void
    {
        $this->recorded[] = [$moduleId, $version];
    }
}
