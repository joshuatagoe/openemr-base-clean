<?php

/**
 * Clinical Co-Pilot module bootstrap.
 *
 * Registers:
 *  - `POST /api/copilot/briefing-ticket` on the standard REST route map. Called
 *    from the panel with the APICSRFTOKEN header it runs under the physician's
 *    own session (local API bridge), authorizes, builds the ContextBundle,
 *    hands it to the agent and returns a patient-bound ticket plus the
 *    deterministic sections. The patient binding is the session's selected
 *    patient; a `pid` in the body is only checked for staleness.
 *  - Module global under Administration > Globals > "Clinical Co-Pilot":
 *      copilot_admin_relationship_override (bool, default off) - allow
 *        admin/super users without a care relationship, audited as such
 *
 * Configuration comes from the environment (COPILOT_AGENT_URL,
 * COPILOT_TICKET_SECRET; see Config\CopilotConfig), never from globals.
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
use OpenEMR\Modules\Copilot\Agent\GuzzleAgentClient;
use OpenEMR\Modules\Copilot\Authorization\AclMainChecker;
use OpenEMR\Modules\Copilot\Authorization\CopilotAuthorizer;
use OpenEMR\Modules\Copilot\Authorization\SqlRelationshipRepository;
use OpenEMR\Modules\Copilot\Config\CopilotConfig;
use OpenEMR\Modules\Copilot\Controller\BriefingTicketController;
use OpenEMR\Modules\Copilot\Data\SqlClinicalReader;
use OpenEMR\Modules\Copilot\Support\UtcDate;
use OpenEMR\Services\Globals\GlobalSetting;
use Symfony\Component\EventDispatcher\EventDispatcherInterface;

final class Bootstrap
{
    public const MODULE_NAME = 'oe-module-copilot';
    public const GLOBALS_SECTION = 'Clinical Co-Pilot';
    public const GLOBAL_ADMIN_OVERRIDE = 'copilot_admin_relationship_override';
    public const ROUTE_BRIEFING_TICKET = 'POST /api/copilot/briefing-ticket';

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
            self::ROUTE_BRIEFING_TICKET,
            static fn(HttpRestRequest $request) => self::createBriefingTicketController()->handleRest($request)
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
    public static function createBriefingTicketController(): BriefingTicketController
    {
        $override = OEGlobalsBag::getInstance()->getBoolean(self::GLOBAL_ADMIN_OVERRIDE);
        $config = CopilotConfig::fromEnvironment();
        return new BriefingTicketController(
            new CopilotAuthorizer(new AclMainChecker(), new SqlRelationshipRepository(), $override),
            new SqlClinicalReader(),
            new ContextBundleBuilder(UtcDate::serverZone()),
            new GuzzleAgentClient($config),
            $config,
        );
    }
}
