<?php

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Tests;

use OpenEMR\Menu\MenuEvent;
use OpenEMR\Modules\Copilot\Bootstrap;
use OpenEMR\Modules\Copilot\Dashboard\DashboardLauncher;
use PHPUnit\Framework\TestCase;
use stdClass;
use Symfony\Component\EventDispatcher\EventDispatcher;

/**
 * Patient dashboard (React), mode B: the Patient menu entry and the launch page
 * that starts OpenEMR's own SMART EHR launch for the chart's patient. Packaging
 * only: no data access, no API route, no change to OpenEMR.
 */
final class DashboardLaunchTest extends TestCase
{
    private string $dir;

    protected function setUp(): void
    {
        $this->dir = sys_get_temp_dir() . '/copilot-dash-' . bin2hex(random_bytes(4));
        mkdir($this->dir);
    }

    protected function tearDown(): void
    {
        foreach (glob($this->dir . '/*') ?: [] as $f) {
            unlink($f);
        }
        rmdir($this->dir);
    }

    /** @param list<stdClass> $children */
    private static function entry(string $menuId, string $label, array $children = []): stdClass
    {
        $e = new stdClass();
        $e->menu_id = $menuId;
        $e->label = $label;
        $e->children = $children;
        return $e;
    }

    /** @return list<stdClass> */
    private static function standardMenu(): array
    {
        return [
            self::entry('calimg', 'Calendar'),
            self::entry('patimg', 'Patient', [self::entry('new0', 'New/Search'), self::entry('dem1', 'Dashboard'), self::entry('', 'Visits')]),
            self::entry('misimg', 'Miscellaneous'),
        ];
    }

    /**
     * @param array<mixed> $menu
     * @return array<mixed>
     */
    private static function childrenOf(array $menu, int $index): array
    {
        $top = $menu[$index] ?? null;
        self::assertInstanceOf(stdClass::class, $top);
        self::assertIsArray($top->children);
        return $top->children;
    }

    /**
     * @param array<mixed> $entries
     * @return list<mixed>
     */
    private static function ids(array $entries): array
    {
        return array_values(array_map(static fn(mixed $e): mixed => $e instanceof stdClass ? $e->menu_id : null, $entries));
    }

    public function testMenuEntryGoesUnderPatientRightAfterTheDashboardAndNeedsAPatient(): void
    {
        $menu = (new DashboardLauncher())->addMenuItem(self::standardMenu(), 'Patient Dashboard (React)');

        $patient = self::childrenOf($menu, 1);
        self::assertSame(['new0', 'dem1', DashboardLauncher::MENU_ID, ''], self::ids($patient));
        $item = $patient[2];
        self::assertInstanceOf(stdClass::class, $item);
        self::assertSame('Patient Dashboard (React)', $item->label);
        self::assertSame(1, $item->requirement, 'only with a patient selected');
        self::assertSame('pat', $item->target);
        self::assertSame(Bootstrap::MODULE_PATH . '/public/dashboard-launch.php', $item->url);
        self::assertSame(['patients', 'demo'], $item->acl_req);
        self::assertSame([], $item->global_req);
        self::assertSame([], $item->children);
        // Other top-level menus are untouched.
        self::assertSame([], self::childrenOf($menu, 0));
        self::assertSame([], self::childrenOf($menu, 2));
    }

    public function testMenuEntryIsAppendedWhenThereIsNoDashboardItemAndNothingChangesWithoutAPatientMenu(): void
    {
        $launcher = new DashboardLauncher();
        $menu = $launcher->addMenuItem([self::entry('patimg', 'Patient', [self::entry('new0', 'New')])], 'X');
        self::assertSame(['new0', DashboardLauncher::MENU_ID], self::ids(self::childrenOf($menu, 0)));

        $noPatient = [self::entry('calimg', 'Calendar')];
        self::assertEquals($noPatient, $launcher->addMenuItem($noPatient, 'X'));
    }

    public function testMenuEntryIsAddedOnlyOnce(): void
    {
        $launcher = new DashboardLauncher();
        $menu = $launcher->addMenuItem($launcher->addMenuItem(self::standardMenu(), 'X'), 'X');
        self::assertCount(1, array_keys(self::ids(self::childrenOf($menu, 1)), DashboardLauncher::MENU_ID, true));
    }

    public function testBootstrapListensForTheMenuUpdate(): void
    {
        $dispatcher = new EventDispatcher();
        (new Bootstrap($dispatcher))->subscribeToEvents();
        self::assertTrue($dispatcher->hasListeners(MenuEvent::MENU_UPDATE));
    }

    public function testLaunchRedirectsToOpenEmrsEhrLaunchWithTheClientAndCsrfToken(): void
    {
        $d = (new DashboardLauncher())->decide(7, true, 'Client_ID-123456', 'csrf&"tok', '/openemr');

        self::assertSame(302, $d->status);
        self::assertNull($d->message);
        $url = (string) $d->location;
        self::assertStringStartsWith('/openemr/interface/smart/ehr-launch-client.php?', $url);
        parse_str((string) parse_url($url, PHP_URL_QUERY), $q);
        self::assertSame(['intent' => 'main.tab', 'client_id' => 'Client_ID-123456', 'csrf_token' => 'csrf&"tok'], $q);
        // The patient is not in the URL: OpenEMR takes it from the session's chart.
        self::assertStringNotContainsString('pid', $url);
    }

    public function testLaunchWithAnEmptyWebroot(): void
    {
        $d = (new DashboardLauncher())->decide(7, true, 'Client_ID-123456', 't', '');
        self::assertStringStartsWith('/interface/smart/ehr-launch-client.php?', (string) $d->location);
    }

    public function testLaunchRefusesWithoutAPatientWithoutPermissionOrWithoutAClient(): void
    {
        $launcher = new DashboardLauncher();

        $noPatient = $launcher->decide(null, true, 'Client_ID-123456', 't', '');
        self::assertSame(400, $noPatient->status);
        self::assertNull($noPatient->location);
        self::assertSame('Select a patient first, then open the dashboard from the Patient menu.', $noPatient->message);

        $denied = $launcher->decide(7, false, 'Client_ID-123456', 't', '');
        self::assertSame(403, $denied->status);
        self::assertNull($denied->location);

        $unconfigured = $launcher->decide(7, true, null, 't', '');
        self::assertSame(503, $unconfigured->status);
        self::assertNull($unconfigured->location);
        self::assertStringContainsString('not configured', (string) $unconfigured->message);

        self::assertSame(400, $launcher->decide(0, true, 'Client_ID-123456', 't', '')->status);
    }

    public function testClientIdComesFromTheConfigFileAndIsValidated(): void
    {
        $path = $this->dir . '/dashboard.config.json';
        self::assertNull(DashboardLauncher::readClientId($path), 'missing file');

        file_put_contents($path, '{"clientId":"V9cjUfOKg7MB1YPgfnZGBntpRRNwPSZVB7jLiQjUVGQ"}');
        self::assertSame('V9cjUfOKg7MB1YPgfnZGBntpRRNwPSZVB7jLiQjUVGQ', DashboardLauncher::readClientId($path));

        foreach (['not json', '[]', '{"clientId":5}', '{"clientId":"short"}', '{"clientId":"has space in it"}', '{"clientId":"a/b\\u0000cdefghij"}'] as $bad) {
            file_put_contents($path, $bad);
            self::assertNull(DashboardLauncher::readClientId($path), $bad);
        }
    }

    public function testLaunchPageRequiresAnOpenEmrLoginAndDelegatesToTheLauncher(): void
    {
        $page = (string) file_get_contents(dirname(__DIR__) . '/public/dashboard-launch.php');
        self::assertStringContainsString("require_once dirname(__DIR__, 4) . '/globals.php';", $page);
        self::assertStringNotContainsString('ignoreAuth', $page, 'the page must run behind the OpenEMR login');
        self::assertStringContainsString('DashboardLauncher', $page);
        self::assertStringContainsString("aclCheckCore('patients', 'demo')", $page);
        self::assertStringContainsString('collectCsrfToken', $page);
        // Messages are escaped; nothing else is echoed.
        self::assertStringContainsString('text($decision->message', $page);
    }
}
