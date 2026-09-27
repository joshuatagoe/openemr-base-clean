<?php

/**
 * Patient dashboard (React), mode B: opened by the Patient menu entry
 * "Patient Dashboard (React)". Starts OpenEMR's own SMART EHR launch for the
 * chart that is open in this session; OpenEMR then redirects to the React app
 * (public/dashboard/), which finishes the launch. With OpenEMR's "EHR launch
 * authorization flow skip" enabled for the client, no second login is asked.
 * See Dashboard\DashboardLauncher. No data access here.
 *
 * @package   OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

require_once dirname(__DIR__, 4) . '/globals.php';

use OpenEMR\Common\Acl\AclMain;
use OpenEMR\Common\Csrf\CsrfUtils;
use OpenEMR\Common\Session\SessionWrapperFactory;
use OpenEMR\Core\OEGlobalsBag;
use OpenEMR\Modules\Copilot\Dashboard\DashboardLauncher;

$session = SessionWrapperFactory::getInstance()->getActiveSession();
$pid = $session->get('pid');
$decision = (new DashboardLauncher())->decide(
    is_numeric($pid) ? (int) $pid : null,
    AclMain::aclCheckCore('patients', 'demo'),
    DashboardLauncher::readClientId(dirname(__DIR__) . DashboardLauncher::CONFIG_FILE),
    CsrfUtils::collectCsrfToken($session),
    OEGlobalsBag::getInstance()->getWebRoot(),
);

if ($decision->location !== null) {
    header('Location: ' . $decision->location, true, 302);
    exit;
}

http_response_code($decision->status);
header('Content-Type: text/html; charset=utf-8');
?>
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <title><?php echo xlt('Patient Dashboard (React)'); ?></title>
</head>
<body>
    <p><?php echo text($decision->message ?? ''); ?></p>
</body>
</html>
