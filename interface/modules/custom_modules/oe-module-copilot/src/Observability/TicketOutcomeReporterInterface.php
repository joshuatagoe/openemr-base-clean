<?php

/**
 * Reports the outcome of every briefing-ticket request to the observability
 * backend so requests that never reach the agent are counted (KEY_METRICS.md
 * section 3: the north-star denominator; section 11, first item).
 *
 * The report carries the correlation id and a fixed outcome code only - no
 * user, patient or clinical data - and must never delay or fail the request.
 *
 * @package   OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Observability;

interface TicketOutcomeReporterInterface
{
    /** Bundle accepted by the agent and a ticket minted. */
    public const OUTCOME_ISSUED = 'issued';
    /** An existing bundle re-ticketed for follow-up turns. */
    public const OUTCOME_TICKET_REFRESH = 'ticket_refresh';
    /** Eligible request, but the agent could not be reached or refused the bundle: the physician got sections without a plan check. */
    public const OUTCOME_AGENT_UNAVAILABLE = 'agent_unavailable';
    /** No prior note with plan text: nothing to check (not an eligible request). */
    public const OUTCOME_NO_PRIOR_NOTE = 'no_prior_note';
    /** Authorization, patient binding or session refusal (not an eligible request). */
    public const OUTCOME_REFUSED = 'refused';
    /** A clinical read failed; no partial output. */
    public const OUTCOME_SOURCE_UNAVAILABLE = 'source_unavailable';
    /** The module itself failed to build the bundle. */
    public const OUTCOME_INTERNAL_ERROR = 'internal_error';

    /**
     * @param string $correlationId  the request's cid; the report joins the agent's trace for the same cid
     * @param string $outcome        one of the OUTCOME_* constants
     * @param string|null $detail    a fixed code refining the outcome (refusal code, agent reason), never free text
     */
    public function report(string $correlationId, string $outcome, ?string $detail = null): void;
}
