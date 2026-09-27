<?php

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Tests;

use PHPUnit\Framework\TestCase;

/**
 * ADR-009 section 6: bin/copilot-start.sh (the image's CMD) runs OpenEMR's setup with Apache
 * held back, runs the module migration as the web user, and then always execs
 * Apache - even when the migration fails or hangs. Driven with stand-in
 * commands through the script's override variables.
 */
final class StartScriptTest extends TestCase
{
    private string $dir;
    private string $script;

    protected function setUp(): void
    {
        $this->script = dirname(__DIR__) . '/bin/copilot-start.sh';
        self::assertFileExists($this->script);
        $this->dir = sys_get_temp_dir() . '/copilot-start-' . bin2hex(random_bytes(4));
        mkdir($this->dir);
    }

    protected function tearDown(): void
    {
        foreach (glob($this->dir . '/*') ?: [] as $f) {
            unlink($f);
        }
        rmdir($this->dir);
    }

    private function stub(string $name, string $body): string
    {
        $path = $this->dir . '/' . $name;
        file_put_contents($path, "#!/bin/sh\n" . $body . "\n");
        chmod($path, 0755);
        return $path;
    }

    /**
     * @param array<string, string> $extraEnv
     * @return array{int, string, string} exit code, stdout, the call trace
     */
    private function runScript(string $setupBody, string $migrateBody, int $timeout = 30, array $extraEnv = []): array
    {
        $trace = $this->dir . '/trace';
        file_put_contents($trace, '');
        $env = [
            'PATH' => (string) getenv('PATH'),
            'COPILOT_OPENEMR_SH' => $this->stub('openemr.sh', 'echo "setup skip_apache=$FLEX_SKIP_APACHE_EXEC" >> ' . $trace . "\n" . $setupBody),
            'COPILOT_MIGRATE_CMD' => $this->stub('migrate', 'echo migrate >> ' . $trace . "\n" . $migrateBody),
            'COPILOT_HTTPD_CMD' => $this->stub('httpd', 'echo "httpd $*" >> ' . $trace),
            'COPILOT_MIGRATE_TIMEOUT' => (string) $timeout,
            'COPILOT_DASHBOARD_CONFIG' => $this->dir . '/dashboard.config.json',
        ] + $extraEnv;
        $proc = proc_open(['/bin/sh', $this->script], [1 => ['pipe', 'w'], 2 => ['pipe', 'w']], $pipes, $this->dir, $env);
        self::assertIsResource($proc);
        $out = (string) stream_get_contents($pipes[1]);
        stream_get_contents($pipes[2]);
        fclose($pipes[1]);
        fclose($pipes[2]);
        $code = proc_close($proc);
        return [$code, $out, (string) file_get_contents($trace)];
    }

    public function testRunsSetupThenMigrationThenExecsApacheInTheForeground(): void
    {
        [$code, $out, $trace] = $this->runScript('exit 0', 'echo "copilot-migrate: installed"');
        self::assertSame(0, $code);
        self::assertSame("setup skip_apache=yes\nmigrate\nhttpd -D FOREGROUND\n", $trace);
        self::assertStringContainsString('copilot-migrate: installed', $out);
    }

    public function testApacheStartsEvenWhenTheMigrationFails(): void
    {
        [$code, $out, $trace] = $this->runScript('exit 0', 'exit 7');
        self::assertSame(0, $code);
        self::assertStringEndsWith("httpd -D FOREGROUND\n", $trace);
        self::assertStringContainsString('copilot-start: migrate_exit_7', $out);
    }

    public function testApacheStartsEvenWhenTheMigrationHangs(): void
    {
        [$code, $out, $trace] = $this->runScript('exit 0', 'sleep 30', 2);
        self::assertSame(0, $code);
        self::assertStringEndsWith("httpd -D FOREGROUND\n", $trace);
        self::assertStringContainsString('copilot-start: migrate_exit_', $out);
    }

    public function testWritesTheDashboardClientConfigFromTheEnvironment(): void
    {
        [$code, $out, $trace] = $this->runScript('exit 0', 'exit 0', 30, ['DASHBOARD_SMART_CLIENT_ID' => 'V9cjUfOKg7MB1YPgfnZGBntpRRNwPSZVB7jLiQjUVGQ']);
        self::assertSame(0, $code);
        self::assertStringEndsWith("httpd -D FOREGROUND\n", $trace);
        self::assertStringContainsString('copilot-start: dashboard_config_written', $out);
        self::assertStringNotContainsString('V9cj', $out, 'fixed codes only');
        self::assertSame('{"clientId":"V9cjUfOKg7MB1YPgfnZGBntpRRNwPSZVB7jLiQjUVGQ"}' . "\n", file_get_contents($this->dir . '/dashboard.config.json'));
    }

    public function testRefusesAMalformedDashboardClientIdAndStillStartsApache(): void
    {
        foreach (['x"},"evil":{"a', 'short', str_repeat('a', 129)] as $bad) {
            [$code, $out, $trace] = $this->runScript('exit 0', 'exit 0', 30, ['DASHBOARD_SMART_CLIENT_ID' => $bad]);
            self::assertSame(0, $code);
            self::assertStringEndsWith("httpd -D FOREGROUND\n", $trace);
            self::assertStringContainsString('copilot-start: dashboard_client_id_invalid', $out, $bad);
            self::assertFileDoesNotExist($this->dir . '/dashboard.config.json');
        }
    }

    public function testWithoutADashboardClientIdNoConfigIsWritten(): void
    {
        [, $out] = $this->runScript('exit 0', 'exit 0');
        self::assertStringNotContainsString('dashboard', $out);
        self::assertFileDoesNotExist($this->dir . '/dashboard.config.json');
    }

    public function testASetupFailureStopsTheContainerAsBefore(): void
    {
        [$code, $out, $trace] = $this->runScript('exit 3', 'exit 0');
        self::assertSame(3, $code);
        self::assertSame("setup skip_apache=yes\n", $trace);
        self::assertStringContainsString('copilot-start: openemr_setup_exit_3', $out);
    }
}
