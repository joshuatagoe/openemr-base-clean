<?php

/**
 * Server-to-server hand-off of a ContextBundle to the copilot agent
 * (ARCHITECTURE.md section 5, step 4). Implementations sign the body, bound
 * the wait, and raise AgentUnavailableException on any failure so the caller
 * can degrade explicitly.
 *
 * @package   OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Agent;

interface AgentClientInterface
{
    /**
     * @param array<string,mixed> $bundle  a ContextBundle (schema 1.0) as an array
     * @throws AgentUnavailableException
     */
    public function postBundle(array $bundle, string $correlationId): BundleAccepted;

    /**
     * Week 2: `POST /v1/documents/briefing` (copilot-agent/app/document_briefing.py).
     * Returns the agent's DocumentBriefingResponse, decoded, after checking it
     * echoes this request's correlation id and patient uuid.
     *
     * @param array<string,mixed> $request  a DocumentBriefingRequest as an array
     * @return array<string,mixed>
     * @throws AgentUnavailableException
     */
    public function postDocumentBriefing(array $request, string $correlationId): array;

    /**
     * Week 2 Final: `POST /v1/documents/extract` (contract C4). Returns the
     * decoded response after checking it echoes this request's correlation id,
     * patient uuid and document id. `printed_identity` in the response is for
     * the module's identity comparison only and must never be logged or stored.
     *
     * @param array<string,mixed> $request  correlation_id, patient_uuid, document_id, doc_type, media_type, document_base64
     * @return array<string,mixed>
     * @throws AgentUnavailableException
     */
    public function postDocumentExtraction(array $request, string $correlationId): array;
}
