<?php

/**
 * Clinical Co-Pilot module bootstrap.
 *
 * Registers:
 *  - The panel mount point on the Patient Summary (RenderEvent
 *    EVENT_SECTION_LIST_RENDER_TOP): a container plus one external script
 *    tag; no data access at render time (AUDIT ARCH-005).
 *  - `POST /api/copilot/briefing-ticket` on the standard REST route map. Called
 *    from the panel with the APICSRFTOKEN header it runs under the physician's
 *    own session (local API bridge), authorizes, builds the ContextBundle,
 *    hands it to the agent and returns a patient-bound ticket plus the
 *    deterministic sections. The patient binding is the session's selected
 *    patient; a `pid` in the body is only checked for staleness.
 *  - `GET /api/copilot/document-file/:did` - the source file for the preview
 *    (ADR-008 §3), same authorizer; see Controller\DocumentFileController.
 *  - `POST /api/copilot/documents/:did/values/:idx/file`, `.../reject`, `.../unfile` -
 *    Verify and file / reject one extracted value (ADR-009); see
 *    Controller\FilingController.
 *  - `GET /api/copilot/documents/:did/values` - one document's candidate values
 *    for the panel's value list and viewer (read-only); see
 *    Controller\DocumentValuesController.
 *  - `GET /api/copilot/results/:rid/source` - the source document of a filed
 *    chart result (ADR-009 7b); see Controller\ResultSourceController.
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

use OpenEMR\BC\ServiceContainer;
use OpenEMR\Common\Csrf\CsrfUtils;
use OpenEMR\Common\Http\HttpRestRequest;
use OpenEMR\Common\Session\SessionWrapperFactory;
use OpenEMR\Core\OEGlobalsBag;
use OpenEMR\Events\Globals\GlobalsInitializedEvent;
use OpenEMR\Events\PatientDemographics\RenderEvent;
use OpenEMR\Events\RestApiExtend\RestApiCreateEvent;
use OpenEMR\Modules\Copilot\Agent\GuzzleAgentClient;
use OpenEMR\Modules\Copilot\Authorization\AclMainChecker;
use OpenEMR\Modules\Copilot\Authorization\CopilotAuthorizer;
use OpenEMR\Modules\Copilot\Authorization\SqlRelationshipRepository;
use OpenEMR\Modules\Copilot\Config\CopilotConfig;
use OpenEMR\Modules\Copilot\Controller\BriefingTicketController;
use OpenEMR\Modules\Copilot\Controller\DocumentBriefingController;
use OpenEMR\Modules\Copilot\Controller\DocumentFileController;
use OpenEMR\Modules\Copilot\Controller\DocumentValuesController;
use OpenEMR\Modules\Copilot\Controller\FilingController;
use OpenEMR\Modules\Copilot\Controller\ResultSourceController;
use OpenEMR\Modules\Copilot\Controller\DocumentsController;
use OpenEMR\Modules\Copilot\Data\SqlClinicalReader;
use OpenEMR\Modules\Copilot\Data\SqlDocumentReader;
use OpenEMR\Modules\Copilot\Data\SqlSchemaStatus;
use OpenEMR\Modules\Copilot\Documents\DocumentProcessor;
use OpenEMR\Modules\Copilot\Documents\SqlDocumentValuesReader;
use OpenEMR\Modules\Copilot\Documents\SqlProcessingRepository;
use OpenEMR\Modules\Copilot\Filing\SqlFilingStore;
use OpenEMR\Modules\Copilot\Filing\ValueFiler;
use OpenEMR\Modules\Copilot\Observability\LangfuseTicketOutcomeReporter;
use OpenEMR\Modules\Copilot\Observability\NullTicketOutcomeReporter;
use OpenEMR\Modules\Copilot\Panel\PanelRenderer;
use OpenEMR\Modules\Copilot\Support\UtcDate;
use OpenEMR\Services\Globals\GlobalSetting;
use Symfony\Component\EventDispatcher\EventDispatcherInterface;

final class Bootstrap
{
    public const MODULE_NAME = 'oe-module-copilot';
    public const GLOBALS_SECTION = 'Clinical Co-Pilot';
    public const GLOBAL_ADMIN_OVERRIDE = 'copilot_admin_relationship_override';
    public const ROUTE_BRIEFING_TICKET = 'POST /api/copilot/briefing-ticket';
    public const ROUTE_DOCUMENT_BRIEFING = 'POST /api/copilot/document-briefing';
    public const ROUTE_DOCUMENTS_PROCESS = 'POST /api/copilot/documents/process';
    public const ROUTE_DOCUMENTS_LIST = 'GET /api/copilot/documents';
    public const ROUTE_DOCUMENT_FILE = 'GET /api/copilot/document-file/:did';
    public const ROUTE_DOCUMENT_VALUES = 'GET /api/copilot/documents/:did/values';
    public const ROUTE_VALUE_FILE = 'POST /api/copilot/documents/:did/values/:idx/file';
    public const ROUTE_VALUE_REJECT = 'POST /api/copilot/documents/:did/values/:idx/reject';
    public const ROUTE_VALUE_UNFILE = 'POST /api/copilot/documents/:did/values/:idx/unfile';
    public const ROUTE_RESULT_SOURCE = 'GET /api/copilot/results/:rid/source';
    public const MODULE_PATH = '/interface/modules/custom_modules/oe-module-copilot';
    public const PANEL_SCRIPT = '/public/copilot-panel.js';

    public function __construct(private readonly EventDispatcherInterface $eventDispatcher)
    {
    }

    public function subscribeToEvents(): void
    {
        $this->eventDispatcher->addListener(RestApiCreateEvent::EVENT_HANDLE, $this->addRoutes(...));
        $this->eventDispatcher->addListener(GlobalsInitializedEvent::EVENT_HANDLE, $this->addGlobals(...));
        $this->eventDispatcher->addListener(RenderEvent::EVENT_SECTION_LIST_RENDER_TOP, $this->renderPanel(...));
    }

    /**
     * Emit the panel mount point. Only rendering: the pid is passed to the
     * panel for a staleness check, never used for authorization here.
     */
    public function renderPanel(RenderEvent $event): void
    {
        $pid = $event->getPid();
        if (!is_int($pid) || $pid <= 0) {
            return;
        }
        $session = SessionWrapperFactory::getInstance()->getActiveSession();
        $siteId = $session->get('site_id');
        if (!is_string($siteId) || $siteId === '') {
            $siteId = 'default';
        }
        $webroot = OEGlobalsBag::getInstance()->getWebRoot();
        echo (new PanelRenderer())->render(
            $pid,
            CsrfUtils::collectCsrfToken($session, 'api'),
            $webroot . '/apis/' . rawurlencode($siteId) . '/api/copilot/briefing-ticket',
            $webroot . self::MODULE_PATH . self::PANEL_SCRIPT . '?v=' . self::panelScriptVersion(),
        );
    }

    /**
     * Cache-busting token for the panel script: the file's content hash, so a deploy
     * invalidates every physician's cached copy without a manual hard reload.
     */
    public static function panelScriptVersion(): string
    {
        $path = __DIR__ . '/..' . self::PANEL_SCRIPT;
        $hash = is_file($path) ? hash_file('crc32b', $path) : false;
        return $hash === false ? '0' : $hash;
    }

    public function addRoutes(RestApiCreateEvent $event): RestApiCreateEvent
    {
        $event->addToRouteMap(
            self::ROUTE_BRIEFING_TICKET,
            static fn(HttpRestRequest $request) => self::createBriefingTicketController()->handleRest($request)
        );
        $event->addToRouteMap(
            self::ROUTE_DOCUMENT_BRIEFING,
            static fn(HttpRestRequest $request) => self::createDocumentBriefingController()->handleRest($request)
        );
        $event->addToRouteMap(
            self::ROUTE_DOCUMENTS_PROCESS,
            static fn(HttpRestRequest $request) => self::createDocumentsController()->handleProcessRest($request)
        );
        $event->addToRouteMap(
            self::ROUTE_DOCUMENTS_LIST,
            static fn(HttpRestRequest $request) => self::createDocumentsController()->handleListRest($request)
        );
        $event->addToRouteMap(
            self::ROUTE_DOCUMENT_FILE,
            static fn(string $did, HttpRestRequest $request) => self::createDocumentFileController()->handleRest($did, $request)
        );
        $event->addToRouteMap(
            self::ROUTE_DOCUMENT_VALUES,
            static fn(string $did, HttpRestRequest $request) => self::createDocumentValuesController()->handleRest($did, $request)
        );
        $event->addToRouteMap(
            self::ROUTE_VALUE_FILE,
            static fn(string $did, string $idx, HttpRestRequest $request) => self::createFilingController()->handleFileRest($did, $idx, $request)
        );
        $event->addToRouteMap(
            self::ROUTE_VALUE_REJECT,
            static fn(string $did, string $idx, HttpRestRequest $request) => self::createFilingController()->handleRejectRest($did, $idx, $request)
        );
        $event->addToRouteMap(
            self::ROUTE_VALUE_UNFILE,
            static fn(string $did, string $idx, HttpRestRequest $request) => self::createFilingController()->handleUnfileRest($did, $idx, $request)
        );
        $event->addToRouteMap(
            self::ROUTE_RESULT_SOURCE,
            static fn(string $rid, HttpRestRequest $request) => self::createResultSourceController()->handleRest($rid, $request)
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
        $reporter = $config->hasLangfuse()
            ? new LangfuseTicketOutcomeReporter((string) $config->langfuseBaseUrl, (string) $config->langfusePublicKey, (string) $config->langfuseSecretKey, $config->environment, logger: ServiceContainer::getLogger())
            : new NullTicketOutcomeReporter();
        return new BriefingTicketController(
            new CopilotAuthorizer(new AclMainChecker(), new SqlRelationshipRepository(), $override),
            new SqlClinicalReader(),
            new ContextBundleBuilder(UtcDate::serverZone()),
            new GuzzleAgentClient($config),
            $config,
            reporter: $reporter,
            pendingFacts: new SqlProcessingRepository(),
            schema: new SqlSchemaStatus(),
            documentAccess: new SqlDocumentReader(),
        );
    }

    /** Chart-open processing and the document list (ADR-012), same authorizer wiring. */
    public static function createDocumentsController(): DocumentsController
    {
        $override = OEGlobalsBag::getInstance()->getBoolean(self::GLOBAL_ADMIN_OVERRIDE);
        $logger = ServiceContainer::getLogger();
        return new DocumentsController(
            new CopilotAuthorizer(new AclMainChecker(), new SqlRelationshipRepository(), $override),
            new SqlClinicalReader(),
            new SqlSchemaStatus(),
            new DocumentProcessor(new SqlDocumentReader(), new SqlProcessingRepository(), new GuzzleAgentClient(CopilotConfig::fromEnvironment()), $logger),
            $logger,
        );
    }

    /** The source-file route for the preview (ADR-008), same authorizer wiring. */
    public static function createDocumentFileController(): DocumentFileController
    {
        $override = OEGlobalsBag::getInstance()->getBoolean(self::GLOBAL_ADMIN_OVERRIDE);
        return new DocumentFileController(
            new CopilotAuthorizer(new AclMainChecker(), new SqlRelationshipRepository(), $override),
            new SqlDocumentReader(),
        );
    }

    /** One document's candidate values for the panel (read-only), same authorizer wiring. */
    public static function createDocumentValuesController(): DocumentValuesController
    {
        $override = OEGlobalsBag::getInstance()->getBoolean(self::GLOBAL_ADMIN_OVERRIDE);
        return new DocumentValuesController(
            new CopilotAuthorizer(new AclMainChecker(), new SqlRelationshipRepository(), $override),
            new SqlSchemaStatus(),
            new SqlDocumentReader(),
            new SqlDocumentValuesReader(),
        );
    }

    /** Verify and file / reject (ADR-009): read authorizer plus lab-write and sign checks. */
    public static function createFilingController(): FilingController
    {
        $override = OEGlobalsBag::getInstance()->getBoolean(self::GLOBAL_ADMIN_OVERRIDE);
        $acl = new AclMainChecker();
        return new FilingController(
            new CopilotAuthorizer($acl, new SqlRelationshipRepository(), $override),
            $acl,
            $acl,
            new SqlSchemaStatus(),
            new SqlDocumentReader(),
            new ValueFiler(new SqlFilingStore()),
        );
    }

    /** Filed chart result -> its source document (ADR-009 7b), same authorizer wiring. */
    public static function createResultSourceController(): ResultSourceController
    {
        $override = OEGlobalsBag::getInstance()->getBoolean(self::GLOBAL_ADMIN_OVERRIDE);
        return new ResultSourceController(
            new CopilotAuthorizer(new AclMainChecker(), new SqlRelationshipRepository(), $override),
            new SqlFilingStore(),
            new SqlDocumentReader(),
        );
    }

    /** Same authorizer wiring as the ticket route (Week 2 document briefing). */
    public static function createDocumentBriefingController(): DocumentBriefingController
    {
        $override = OEGlobalsBag::getInstance()->getBoolean(self::GLOBAL_ADMIN_OVERRIDE);
        return new DocumentBriefingController(
            new CopilotAuthorizer(new AclMainChecker(), new SqlRelationshipRepository(), $override),
            new SqlClinicalReader(),
            new SqlDocumentReader(),
            new GuzzleAgentClient(CopilotConfig::fromEnvironment()),
            records: new SqlProcessingRepository(),
            schema: new SqlSchemaStatus(),
            builder: new ContextBundleBuilder(UtcDate::serverZone()),
        );
    }
}
