<?php

/**
 * Patient dashboard (React), mode B: the Patient menu entry and the decision
 * behind the module's launch page (public/dashboard-launch.php).
 *
 * The launch page does not talk to the dashboard or read patient data. It sends
 * the browser to OpenEMR's own SMART EHR launch endpoint
 * (interface/smart/ehr-launch-client.php) with the dashboard's SMART client id
 * and the session's CSRF token; OpenEMR builds the launch token from the chart
 * that is open in the session and redirects to the client's launch URI (the
 * React app under public/dashboard/). The app then runs the SMART EHR launch.
 * Packaging only: no API route, no data access, no change to OpenEMR.
 *
 * @package   OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Dashboard;

use OpenEMR\Modules\Copilot\Bootstrap;
use stdClass;

final class DashboardLauncher
{
    public const MENU_ID = 'copilotdash0';
    public const LAUNCH_PAGE = '/public/dashboard-launch.php';
    /** Written after the SMART client is registered (dashboard/scripts/register-smart-client.mjs, or the start script). */
    public const CONFIG_FILE = '/public/dashboard.config.json';
    /** OpenEMR's EHR launch for the current chart (SmartLaunchController::redirectAndLaunchSmartApp). */
    public const EHR_LAUNCH = '/interface/smart/ehr-launch-client.php';
    /** SMARTLaunchToken::INTENT_MAIN_TAB: the app opens in a main tab, not the card's dialog. */
    public const INTENT = 'main.tab';

    private const CLIENT_ID = '/^[A-Za-z0-9_-]{8,128}$/';

    /**
     * Adds "Patient Dashboard (React)" to the Patient menu, right after the PHP
     * Dashboard entry. Shown only with a patient selected (requirement 1) and to
     * users with patients/demo, like the PHP dashboard entry.
     *
     * @param array<mixed> $menu top-level menu entries (MenuEvent::getMenu(): stdClass objects)
     * @param string $label already translated and HTML-safe (MenuRole translates with xlt before MENU_UPDATE)
     * @return array<mixed>
     */
    public function addMenuItem(array $menu, string $label): array
    {
        foreach ($menu as $top) {
            if (!$top instanceof stdClass || ($top->menu_id ?? null) !== 'patimg') {
                continue;
            }
            $children = is_array($top->children ?? null) ? $top->children : [];
            foreach ($children as $child) {
                if ($child instanceof stdClass && ($child->menu_id ?? null) === self::MENU_ID) {
                    return $menu;
                }
            }
            $item = new stdClass();
            $item->requirement = 1;
            $item->target = 'pat';
            $item->menu_id = self::MENU_ID;
            $item->label = $label;
            $item->url = Bootstrap::MODULE_PATH . self::LAUNCH_PAGE;
            $item->children = [];
            $item->acl_req = ['patients', 'demo'];
            $item->global_req = [];

            $at = count($children);
            foreach ($children as $i => $child) {
                if ($child instanceof stdClass && ($child->menu_id ?? null) === 'dem1') {
                    $at = $i + 1;
                    break;
                }
            }
            array_splice($children, $at, 0, [$item]);
            $top->children = $children;
            break;
        }
        return $menu;
    }

    /** The SMART client id from the config file, or null when missing or malformed. */
    public static function readClientId(string $path): ?string
    {
        if (!is_file($path)) {
            return null;
        }
        $raw = file_get_contents($path);
        if ($raw === false) {
            return null;
        }
        try {
            $data = json_decode($raw, true, 4, JSON_THROW_ON_ERROR);
        } catch (\JsonException) {
            return null;
        }
        $id = is_array($data) ? ($data['clientId'] ?? null) : null;
        return is_string($id) && preg_match(self::CLIENT_ID, $id) === 1 ? $id : null;
    }

    /**
     * @param int|null $pid the session's selected patient
     * @param bool $canReadDemographics patients/demo for the signed-in user
     * @param string $csrfToken CsrfUtils::collectCsrfToken for the session (default subject)
     * @param string $webroot OpenEMR's web root ('' in the Flex images)
     */
    public function decide(?int $pid, bool $canReadDemographics, ?string $clientId, string $csrfToken, string $webroot): LaunchDecision
    {
        if ($pid === null || $pid <= 0) {
            return LaunchDecision::refuse(400, 'Select a patient first, then open the dashboard from the Patient menu.');
        }
        if (!$canReadDemographics) {
            return LaunchDecision::refuse(403, 'You do not have permission to view patient demographics.');
        }
        if ($clientId === null || preg_match(self::CLIENT_ID, $clientId) !== 1) {
            return LaunchDecision::refuse(503, 'The patient dashboard is not configured: its SMART client is not registered. See dashboard/README.md.');
        }
        $query = http_build_query(['intent' => self::INTENT, 'client_id' => $clientId, 'csrf_token' => $csrfToken], '', '&', PHP_QUERY_RFC3986);
        return LaunchDecision::redirect(rtrim($webroot, '/') . self::EHR_LAUNCH . '?' . $query);
    }
}
