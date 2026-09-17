<?php

/**
 * Clinical Co-Pilot module bootstrap.
 *
 * Registers:
 *  - `GET /api/copilot/context` on the standard REST route map. Called from the
 *    OpenEMR UI with the APICSRFTOKEN header it runs under the physician's own
 *    session (local API bridge) and returns the ContextBundle for the selected
 *    patient. No patient identifier is accepted from the request.
 *  - Module global under Administration > Globals > "Clinical Co-Pilot":
 *      copilot_admin_relationship_override (bool, default off) - allow
 *        admin/super users without a care relationship, audited as such
 *
 * Local inspection of the generated bundle: call the REST route from an
 * authenticated OpenEMR session with the APICSRFTOKEN header, or run the
 * module's PHPUnit suite.
 *
 * The listeners are lightweight (ARCHITECTURE.md section 4; AUDIT ARCH-005):
 * no data access happens at bootstrap or render time.
 *
 * @package   OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot;

use OpenEMR\Common\Http\HttpRestRequest;
use OpenEMR\Core\OEGlobalsBag;
use OpenEMR\Events\Globals\GlobalsInitializedEvent;
use OpenEMR\Events\RestApiExtend\RestApiCreateEvent;
use OpenEMR\Modules\Copilot\Authorization\AclMainChecker;
use OpenEMR\Modules\Copilot\Authorization\CopilotAuthorizer;
use OpenEMR\Modules\Copilot\Authorization\SqlRelationshipRepository;
use OpenEMR\Modules\Copilot\Controller\ContextController;
use OpenEMR\Modules\Copilot\Data\SqlClinicalReader;
use OpenEMR\Modules\Copilot\Support\UtcDate;
use OpenEMR\Services\Globals\GlobalSetting;
use Symfony\Component\EventDispatcher\EventDispatcherInterface;

final class Bootstrap
{
    public const MODULE_NAME = 'oe-module-copilot';
    public const GLOBALS_SECTION = 'Clinical Co-Pilot';
    public const GLOBAL_ADMIN_OVERRIDE = 'copilot_admin_relationship_override';
    public const ROUTE_CONTEXT = 'GET /api/copilot/context';

    public function __construct(private readonly EventDispatcherInterface $eventDispatcher)
    {
    }

    public function subscribeToEvents(): void
    {
        $this->eventDispatcher->addListener(RestApiCreateEvent::EVENT_HANDLE, $this->addRoutes(...));
        $this->eventDispatcher->addListener(GlobalsInitializedEvent::EVENT_HANDLE, $this->addGlobals(...));
    }

    public function addRoutes(RestApiCreateEvent $event): RestApiCreateEvent
    {
        $event->addToRouteMap(
            self::ROUTE_CONTEXT,
            static fn(HttpRestRequest $request) => self::createContextController()->handleRest($request)
        );
        return $event;
    }

    public function addGlobals(GlobalsInitializedEvent $event): void
    {
        $service = $event->getGlobalsService();
        $service->createSection(self::GLOBALS_SECTION);
        $service->appendToSection(
            self::GLOBALS_SECTION,
            self::GLOBAL_ADMIN_OVERRIDE,
            new GlobalSetting(
                xl('Allow admin/super users without a care relationship'),
                GlobalSetting::DATA_TYPE_BOOL,
                '0',
                xl('Off by default. When on, an admin/super user may read a selected patient context without a provider relationship; the read is audited with basis "admin_override".')
            )
        );
    }

    /**
     * Wire the controller with the real OpenEMR implementations.
     */
    public static function createContextController(): ContextController
    {
        $override = OEGlobalsBag::getInstance()->getBoolean(self::GLOBAL_ADMIN_OVERRIDE);
        return new ContextController(
            new CopilotAuthorizer(new AclMainChecker(), new SqlRelationshipRepository(), $override),
            new SqlClinicalReader(),
            new ContextBundleBuilder(UtcDate::serverZone()),
        );
    }
}
