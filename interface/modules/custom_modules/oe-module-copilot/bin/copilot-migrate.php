<?php

/**
 * Apply the Co-Pilot module's schema migrations (ADR-009 sections 6 and 7a).
 *
 * Run as the web user at container start, after openemr.sh has finished setup:
 *   php interface/modules/custom_modules/oe-module-copilot/bin/copilot-migrate.php [--site=default]
 *
 * Prints exactly one line, `copilot-migrate: <code>`, and always exits 0 so a
 * failure can never stop Apache from starting; the module's schema check keeps
 * document processing disabled until the tables exist. Never prints SQL, error
 * messages or patient data.
 *
 * @package   OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

if (PHP_SAPI !== 'cli') {
    exit(0);
}

$site = 'default';
foreach (array_slice($argv, 1) as $arg) {
    if (preg_match('/^--site=([A-Za-z0-9_.-]+)$/', $arg, $m)) {
        $site = $m[1];
    }
}

$moduleDir = dirname(__DIR__);
$openemrRoot = dirname($moduleDir, 4);

$code = 'bootstrap_failed';
$reported = false;
// globals.php can exit() on a database error; still report one code line.
register_shutdown_function(static function () use (&$code, &$reported): void {
    if (!$reported) {
        while (ob_get_level() > 0) {
            ob_end_clean();
        }
        fwrite(STDOUT, "copilot-migrate: {$code}\n");
    }
});
try {
    $ignoreAuth = true;
    $sessionAllowWrite = true;
    $_GET['site'] = $site;
    ob_start();
    require_once $openemrRoot . '/interface/globals.php';
    ob_end_clean();

    (new \OpenEMR\Core\ModulesClassLoader($openemrRoot))
        ->registerNamespaceIfNotExists('OpenEMR\\Modules\\Copilot\\', $moduleDir . DIRECTORY_SEPARATOR . 'src');

    $version = \OpenEMR\Modules\Copilot\Migration\ModuleVersion::fromFile($moduleDir . '/version.php');
    if ($version === null) {
        $code = 'version_unreadable';
    } else {
        $runner = new \OpenEMR\Modules\Copilot\Migration\MigrationRunner(
            new \OpenEMR\Modules\Copilot\Migration\SqlMigrationStore(),
            basename($moduleDir),
            $moduleDir . '/sql',
            $version,
            static function (string $outcome, array $context): void {
                // Codes, versions and file names only (MigrationRunner never passes SQL or messages).
                fwrite(STDERR, 'copilot-migrate detail: ' . $outcome . ' ' . json_encode($context) . "\n");
            },
        );
        $code = $runner->run();
    }
} catch (\Throwable $e) {
    $code = 'bootstrap_failed:' . (new \ReflectionClass($e))->getShortName();
}

while (ob_get_level() > 0) {
    ob_end_clean();
}
fwrite(STDOUT, "copilot-migrate: {$code}\n");
$reported = true;
exit(0);
