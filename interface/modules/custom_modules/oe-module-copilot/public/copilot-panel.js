/**
 * Clinical Co-Pilot panel (ARCHITECTURE.md sections 4 and 5).
 *
 * Lifecycle: paint skeleton -> POST briefing-ticket (module, APICSRFTOKEN)
 * -> render deterministic sections -> open the briefing stream on the agent
 * with the ticket (fetch + ReadableStream so the ticket travels in an
 * Authorization header and the request can be aborted) -> render each
 * verified commitment -> on unload / patient change abort everything and
 * delete the bundle.
 *
 * Trust rules enforced here: every rendered string goes through textContent
 * (never innerHTML); any stream event whose correlation_id or patient_uuid
 * differs from the ticket response is dropped; values are rendered from the
 * cited records/sections, and the panel never invents a state.
 *
 * @package   OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */
(function () {
    'use strict';

    const STATE_LABELS = {
        matching_result_found: 'Matching result found',
        order_found_no_result: 'Order found, no result yet',
        matching_medication_record_found: 'Matching medication record found',
        no_matching_record_found: 'No matching record found in this system',
        ambiguous_match: 'Ambiguous match',
        conflicting_records: 'Conflicting records',
        verification_unavailable: 'Verification unavailable'
    };
    const STATE_BADGES = {
        matching_result_found: 'badge-success',
        order_found_no_result: 'badge-info',
        matching_medication_record_found: 'badge-success',
        no_matching_record_found: 'badge-warning',
        ambiguous_match: 'badge-warning',
        conflicting_records: 'badge-danger',
        verification_unavailable: 'badge-secondary'
    };

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) {
            node.className = className;
        }
        if (text !== undefined && text !== null) {
            node.textContent = String(text);
        }
        return node;
    }

    function fmtDate(iso) {
        if (typeof iso !== 'string' || iso === '') {
            return 'unknown date';
        }
        const d = new Date(iso);
        return isNaN(d.getTime()) ? iso : d.toLocaleString();
    }

    function fmtValue(result) {
        const value = result.value === null || result.value === undefined ? 'unknown' : String(result.value);
        const units = result.units ? ' ' + result.units : ' (units unknown)';
        return value + units;
    }

    class CopilotPanel {
        constructor(container) {
            this.container = container;
            this.pid = container.dataset.pid;
            this.csrf = container.dataset.csrf;
            this.ticketUrl = container.dataset.ticketUrl;
            this.status = container.querySelector('[data-role="status"]');
            this.body = container.querySelector('[data-role="body"]');
            this.abort = new AbortController();
            this.bound = null; // {correlation_id, patient_uuid, bundle_id, ticket, agent_url}
            this.closed = false;
            this.dropped = 0;
            this.refreshed = false;
            this.renderedCommitments = 0;
            this.commitmentLabels = {};

            const onLeave = () => this.close();
            window.addEventListener('pagehide', onLeave);
            window.addEventListener('beforeunload', onLeave);
        }

        setStatus(text, badge) {
            this.status.textContent = text;
            this.status.className = 'badge ' + (badge || 'badge-secondary');
        }

        clearBody() {
            while (this.body.firstChild) {
                this.body.removeChild(this.body.firstChild);
            }
        }

        async start() {
            this.setStatus('loading', 'badge-secondary');
            let response;
            try {
                response = await this.requestTicket();
            } catch {
                this.renderFailure('The briefing could not be requested.');
                return;
            }
            if (!response.ok) {
                this.renderError(response.detail);
                return;
            }
            const data = response.data;
            if (typeof data.patient_uuid !== 'string' || typeof data.correlation_id !== 'string') {
                this.renderFailure('The briefing response was incomplete.');
                return;
            }
            this.bound = {
                correlation_id: data.correlation_id,
                patient_uuid: data.patient_uuid,
                bundle_id: data.bundle_id,
                ticket: data.ticket,
                agent_url: data.agent_url
            };
            this.renderSections(data.sections || {});
            this.container.setAttribute('aria-busy', 'false');

            if (data.degraded || !data.ticket || !data.bundle_id || !data.agent_url) {
                const code = data.degraded && data.degraded.detail_code ? data.degraded.detail_code : 'no_ticket';
                this.renderPlanCheckUnavailable(code);
                return;
            }
            await this.streamBriefing();
        }

        async requestTicket() {
            const resp = await fetch(this.ticketUrl, {
                method: 'POST',
                credentials: 'same-origin',
                cache: 'no-store',
                signal: this.abort.signal,
                headers: { 'APICSRFTOKEN': this.csrf, 'Content-Type': 'application/json', 'Accept': 'application/json' },
                body: JSON.stringify({ pid: Number(this.pid) })
            });
            let json = null;
            try {
                json = await resp.json();
            } catch {
                json = null;
            }
            if (resp.status !== 200 || !json) {
                const detail = json && json.detail ? json.detail : { code: 'http_' + resp.status, message: 'The briefing could not be requested.' };
                return { ok: false, detail: detail };
            }
            return { ok: true, data: json };
        }

        renderError(detail) {
            this.clearBody();
            const code = detail && detail.code ? String(detail.code) : 'error';
            const message = detail && detail.message ? String(detail.message) : 'The briefing is unavailable.';
            this.setStatus(code === 'no_prior_note' ? 'no prior plan' : 'unavailable', code === 'no_prior_note' ? 'badge-info' : 'badge-danger');
            this.body.appendChild(el('p', 'mb-1', message));
            if (detail && detail.correlation_id) {
                this.body.appendChild(el('small', 'text-muted', 'Reference: ' + String(detail.correlation_id)));
            }
        }

        renderFailure(message) {
            this.clearBody();
            this.setStatus('unavailable', 'badge-danger');
            this.body.appendChild(el('p', 'mb-0', message));
        }

        renderSections(sections) {
            this.clearBody();
            const baseline = sections.baseline || {};
            const win = sections.window || {};
            const footer = sections.footer || {};

            const identity = sections.identity;
            if (identity) {
                const line = el('p', 'mb-1');
                line.appendChild(el('strong', null, String(identity.name || 'Patient')));
                const bits = [];
                if (identity.dob) { bits.push('DOB ' + String(identity.dob)); }
                if (identity.sex) { bits.push(String(identity.sex)); }
                if (identity.pubpid) { bits.push('ID ' + String(identity.pubpid)); }
                if (bits.length) { line.appendChild(el('span', 'text-muted', ' · ' + bits.join(' · '))); }
                this.body.appendChild(line);
            } else {
                this.body.appendChild(el('p', 'mb-1 text-muted', 'Identity could not be read.'));
            }

            const reasons = sections.reasons || {};
            const reasonList = el('ul', 'list-unstyled small mb-2');
            const reasonLine = (label, r) => {
                const li = el('li');
                li.appendChild(el('span', 'text-muted', label + ': '));
                li.appendChild(r && r.text ? el('span', 'font-italic', '“' + String(r.text) + '”') : el('span', 'text-muted', 'none recorded'));
                return li;
            };
            reasonList.appendChild(reasonLine('Scheduled reason (today)', reasons.scheduled));
            reasonList.appendChild(reasonLine('Reason on the baseline encounter', reasons.encounter));
            this.body.appendChild(reasonList);

            const head = el('p', 'mb-2');
            head.appendChild(el('strong', null, 'Plan check'));
            head.appendChild(document.createTextNode(' against the ' + fmtDate(baseline.note_date) + ' note'));
            head.appendChild(el('span', 'text-muted', ' (' + String(baseline.note_id || 'note') + ')'));
            this.body.appendChild(head);

            this.body.appendChild(el('p', 'text-muted small mb-2',
                'Evidence window: ' + fmtDate(win.start) + ' → ' + fmtDate(win.end) + (win.as_of_source === 'current_encounter' ? ' (current encounter)' : '')));

            const commitmentsTitle = el('h6', 'mt-3 mb-2', 'Commitments in the plan');
            this.body.appendChild(commitmentsTitle);
            this.commitments = el('ul', 'list-group mb-3');
            this.commitments.setAttribute('data-role', 'commitments');
            this.commitments.appendChild(el('li', 'list-group-item text-muted', 'Checking the plan…'));
            this.body.appendChild(this.commitments);

            const results = Array.isArray(sections.interval_results) ? sections.interval_results : [];
            this.body.appendChild(el('h6', 'mb-2', 'Results since the baseline note (' + results.length + ')'));
            const list = el('ul', 'list-group mb-3');
            list.setAttribute('data-role', 'interval-results');
            if (results.length === 0) {
                list.appendChild(el('li', 'list-group-item text-muted', 'No results recorded in this system in the window.'));
            }
            results.forEach((r) => {
                const item = el('li', 'list-group-item py-2');
                item.dataset.recordId = String(r.result_id || '');
                item.appendChild(el('strong', null, String(r.test_name || 'unknown test')));
                item.appendChild(document.createTextNode(' ' + fmtValue(r)));
                if (r.abnormal_flag) {
                    item.appendChild(document.createTextNode(' '));
                    item.appendChild(el('span', 'badge badge-warning', 'flagged ' + String(r.abnormal_flag) + ' (as recorded)'));
                }
                item.appendChild(el('div', 'small text-muted', String(r.status || 'status unknown') + ' · ' + fmtDate(r.observed_at) + ' · ' + String(r.record_id || r.result_id || '')));
                list.appendChild(item);
            });
            this.body.appendChild(list);

            const orders = Array.isArray(sections.interval_orders) ? sections.interval_orders : [];
            if (orders.length) {
                this.body.appendChild(el('h6', 'mb-2', 'Orders since the baseline note (' + orders.length + ')'));
                const olist = el('ul', 'list-group mb-3');
                olist.setAttribute('data-role', 'interval-orders');
                orders.forEach((o) => {
                    const item = el('li', 'list-group-item py-2');
                    item.dataset.recordId = String(o.order_id || '') + ':' + String(o.sequence || '');
                    item.appendChild(el('strong', null, String(o.test_name || 'unknown test')));
                    item.appendChild(el('span', 'badge badge-info ml-2', String(o.status || 'unknown')));
                    item.appendChild(el('div', 'small text-muted', 'ordered ' + fmtDate(o.ordered_at) + ' · ' + String(o.order_id || '') + ':' + String(o.sequence || '')));
                    olist.appendChild(item);
                });
                this.body.appendChild(olist);
            }

            const medChanges = Array.isArray(sections.medication_changes) ? sections.medication_changes : [];
            if (medChanges.length) {
                this.body.appendChild(el('h6', 'mb-2', 'Medication changes since the baseline note (' + medChanges.length + ')'));
                const mlist = el('ul', 'list-group mb-3');
                mlist.setAttribute('data-role', 'medication-changes');
                medChanges.forEach((m) => {
                    const item = el('li', 'list-group-item py-2');
                    item.dataset.recordId = String(m.record_id || '');
                    item.appendChild(el('strong', null, String(m.drug_name || 'unnamed')));
                    const status = m.active === true ? 'active' : (m.active === false ? 'inactive' : 'status indeterminate');
                    item.appendChild(el('span', 'badge badge-light border ml-2', status + ' (' + String(m.status_field || '') + ')'));
                    item.appendChild(el('span', 'badge badge-secondary ml-1', String(m.source_table || '')));
                    const detail = [];
                    if (m.dosage_text) { detail.push(String(m.dosage_text)); }
                    detail.push(String(m.timestamp_field || '') + ' ' + fmtDate(m.timestamp));
                    detail.push(String(m.record_id || ''));
                    item.appendChild(el('div', 'small text-muted', detail.join(' · ')));
                    mlist.appendChild(item);
                });
                this.body.appendChild(mlist);
            }

            const allergies = sections.allergies;
            this.body.appendChild(el('h6', 'mb-2', 'Allergies as recorded'));
            const alist = el('ul', 'list-group mb-3');
            alist.setAttribute('data-role', 'allergies');
            if (!allergies) {
                alist.appendChild(el('li', 'list-group-item text-muted', 'Allergy entries could not be read.'));
            } else if (!Array.isArray(allergies.entries) || allergies.entries.length === 0) {
                alist.appendChild(el('li', 'list-group-item text-muted', String(allergies.statement || 'no allergy entries on file (not confirmed NKA)')));
            } else {
                allergies.entries.forEach((a) => {
                    const item = el('li', 'list-group-item py-2' + (a.active ? '' : ' text-muted'));
                    item.appendChild(el('strong', null, String(a.title || 'unnamed entry')));
                    item.appendChild(el('span', 'badge badge-light border ml-2', a.coded ? 'coded' : 'as recorded (uncoded)'));
                    if (a.duplicate_count > 1) { item.appendChild(el('span', 'badge badge-secondary ml-1', '\u00d7' + String(a.duplicate_count))); }
                    if (!a.active) { item.appendChild(el('span', 'badge badge-secondary ml-1', 'inactive')); }
                    const detail = [];
                    if (a.reaction) { detail.push('reaction: ' + String(a.reaction)); }
                    if (a.severity) { detail.push('severity: ' + String(a.severity)); }
                    if (a.begdate) { detail.push('since ' + fmtDate(a.begdate)); }
                    detail.push(String(a.record_id || ''));
                    item.appendChild(el('div', 'small text-muted', detail.join(' · ')));
                    alist.appendChild(item);
                });
            }
            this.body.appendChild(alist);

            const foot = el('p', 'small text-muted mb-0');
            foot.setAttribute('data-role', 'footer');
            const unavailable = Array.isArray(footer.sources_unavailable) ? footer.sources_unavailable : [];
            foot.textContent = (unavailable.length
                ? 'Sources that could not be read: ' + unavailable.join(', ') + '. Affected commitments are shown as verification unavailable.'
                : 'All sources read. "No matching record found" means no evidence in this system, not that it was not done.')
                + (footer.duplicates_collapsed ? ' ' + String(footer.duplicates_collapsed) + ' duplicate record(s) collapsed.' : '');
            this.body.appendChild(foot);
        }

        renderPlanCheckUnavailable(code) {
            this.setStatus('plan check unavailable', 'badge-warning');
            if (!this.renderedCommitments) {
                while (this.commitments.firstChild) {
                    this.commitments.removeChild(this.commitments.firstChild);
                }
            }
            this.commitments.appendChild(el('li', 'list-group-item text-muted', 'Plan check unavailable (' + String(code) + ').' + (this.renderedCommitments ? ' Commitments shown above were verified before the failure.' : ' The sections above do not depend on it.')));
        }

        annotateInterval(annotations) {
            annotations.forEach((a) => {
                const item = this.body.querySelector('[data-record-id="' + String(a.record_id).replace(/"/g, '') + '"]');
                if (!item) {
                    return;
                }
                const tag = a.explained_by
                    ? el('span', 'badge badge-light border ml-2', 'explained by ' + (this.commitmentLabels[a.explained_by] || String(a.explained_by)))
                    : el('span', 'badge badge-warning ml-2', 'unexplained by the plan');
                tag.setAttribute('data-role', 'annotation');
                item.firstChild.after(tag);
            });
        }

        async streamBriefing() {
            this.setStatus('checking plan', 'badge-primary');
            let attempt = 0;
            while (!this.closed) {
                attempt += 1;
                let resp;
                try {
                    resp = await fetch(this.bound.agent_url + '/v1/briefings/' + encodeURIComponent(this.bound.bundle_id), {
                        method: 'GET',
                        cache: 'no-store',
                        signal: this.abort.signal,
                        headers: { 'Authorization': 'Bearer ' + this.bound.ticket, 'Accept': 'text/event-stream' }
                    });
                } catch {
                    if (this.closed) {
                        return;
                    }
                    this.renderPlanCheckUnavailable('agent_unreachable');
                    return;
                }
                if (resp.status === 401 && attempt === 1 && !this.refreshed) {
                    // Ticket expired between issue and use: one silent re-authorization.
                    this.refreshed = true;
                    const fresh = await this.requestTicket().catch(() => null);
                    if (fresh && fresh.ok && fresh.data.ticket && fresh.data.patient_uuid === this.bound.patient_uuid && fresh.data.bundle_id) {
                        this.bound.ticket = fresh.data.ticket;
                        this.bound.bundle_id = fresh.data.bundle_id;
                        this.bound.correlation_id = fresh.data.correlation_id;
                        continue;
                    }
                    this.renderPlanCheckUnavailable('ticket_expired');
                    return;
                }
                if (!resp.ok || !resp.body) {
                    this.renderPlanCheckUnavailable('agent_http_' + resp.status);
                    return;
                }
                await this.consumeStream(resp.body);
                return;
            }
        }

        async consumeStream(stream) {
            const reader = stream.getReader();
            const decoder = new TextDecoder('utf-8');
            let buffer = '';
            let first = true;
            try {
                for (;;) {
                    const chunk = await reader.read();
                    if (chunk.done) {
                        break;
                    }
                    buffer += decoder.decode(chunk.value, { stream: true });
                    let idx;
                    while ((idx = buffer.indexOf('\n\n')) >= 0) {
                        const frame = buffer.slice(0, idx);
                        buffer = buffer.slice(idx + 2);
                        const parsed = this.parseFrame(frame);
                        if (parsed) {
                            if (first) {
                                while (this.commitments.firstChild) {
                                    this.commitments.removeChild(this.commitments.firstChild);
                                }
                                first = false;
                            }
                            this.handleEvent(parsed.event, parsed.data);
                        }
                    }
                }
            } catch {
                if (!this.closed) {
                    this.renderPlanCheckUnavailable('stream_interrupted');
                }
            }
        }

        parseFrame(frame) {
            let event = 'message';
            const dataLines = [];
            frame.split('\n').forEach((line) => {
                if (line.startsWith('event:')) {
                    event = line.slice(6).trim();
                } else if (line.startsWith('data:')) {
                    dataLines.push(line.slice(5).trimStart());
                }
            });
            if (dataLines.length === 0) {
                return null;
            }
            try {
                return { event: event, data: JSON.parse(dataLines.join('\n')) };
            } catch {
                return null;
            }
        }

        isBound(data) {
            return data && data.correlation_id === this.bound.correlation_id && data.patient_uuid === this.bound.patient_uuid;
        }

        handleEvent(event, data) {
            if (!this.isBound(data)) {
                this.dropped += 1; // stale or foreign event: never rendered (ARCH-002)
                return;
            }
            if (event === 'commitment') {
                this.renderCommitment(data.match);
            } else if (event === 'interval_annotation') {
                this.annotateInterval(Array.isArray(data.annotations) ? data.annotations : []);
            } else if (event === 'complete') {
                if (data.commitments === 0) {
                    this.commitments.appendChild(el('li', 'list-group-item text-muted', 'No lab/test or medication commitments were found in the plan.'));
                }
                if (data.rejected_count > 0) {
                    this.commitments.appendChild(el('li', 'list-group-item small text-warning', String(data.rejected_count) + ' statement(s) withheld \u2014 could not be verified against the note.'));
                }
                const warnings = Array.isArray(data.warnings) ? data.warnings : [];
                warnings.forEach((w) => this.commitments.appendChild(el('li', 'list-group-item small text-muted', String(w))));
                this.setStatus('plan check complete', 'badge-success');
            } else if (event === 'degraded') {
                this.renderPlanCheckUnavailable(String(data.reason_code || data.stage || 'degraded'));
            }
        }

        renderCommitment(match) {
            if (!match || !match.commitment) {
                return;
            }
            const c = match.commitment;
            const unchecked = c.kind === 'other' || match.state === null || match.state === undefined;
            const state = unchecked ? 'not_checked' : String(match.state);
            this.renderedCommitments += 1;
            const label = '#' + this.renderedCommitments;
            if (c.commitment_id) { this.commitmentLabels[String(c.commitment_id)] = label; }
            const item = el('li', 'list-group-item py-2' + (unchecked ? ' text-muted' : ''));
            const header = el('div', 'd-flex justify-content-between align-items-start');
            const left = el('div');
            left.appendChild(el('span', 'badge badge-dark mr-1', label));
            left.appendChild(el('span', 'badge badge-light border mr-1', String(c.kind || '').replace('_', '/')));
            left.appendChild(el('span', 'font-italic', '“' + String(c.source_span || '') + '”'));
            header.appendChild(left);
            header.appendChild(el('span', 'badge ' + (unchecked ? 'badge-light border' : (STATE_BADGES[state] || 'badge-secondary')), unchecked ? 'Not checked' : (STATE_LABELS[state] || state)));
            item.appendChild(header);
            if (match.summary) {
                item.appendChild(el('div', 'small mt-1', String(match.summary)));
            }
            if (c.ambiguity_note) {
                item.appendChild(el('div', 'small font-italic', 'Wording note: ' + String(c.ambiguity_note)));
            }
            const cands = Array.isArray(match.candidates) ? match.candidates : [];
            if (cands.length) {
                item.appendChild(el('div', 'small text-muted', 'Also on file: ' + cands.map((x) => String(x.record_id) + ' (' + fmtDate(x.timestamp) + ')').join('; ')));
            }
            const cites = Array.isArray(match.citations) ? match.citations : [];
            if (cites.length) {
                item.appendChild(el('div', 'small text-muted', 'Sources: ' + cites.map((x) => String(x.record_id) + ' (' + fmtDate(x.timestamp) + ')').join('; ')));
            }
            this.commitments.appendChild(item);
        }

        close() {
            if (this.closed) {
                return;
            }
            this.closed = true;
            this.abort.abort();
            if (this.bound && this.bound.ticket && this.bound.bundle_id && this.bound.agent_url) {
                try {
                    fetch(this.bound.agent_url + '/v1/bundles/' + encodeURIComponent(this.bound.bundle_id), {
                        method: 'DELETE',
                        keepalive: true,
                        headers: { 'Authorization': 'Bearer ' + this.bound.ticket }
                    }).catch(() => undefined);
                } catch {
                    // Best effort: the agent's TTL removes the bundle regardless.
                }
            }
        }
    }

    function boot() {
        const container = document.getElementById('oe-copilot-panel');
        if (!container || container.dataset.booted === '1') {
            return;
        }
        container.dataset.booted = '1';
        const panel = new CopilotPanel(container);
        window.oeCopilotPanel = panel;
        panel.start();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', boot);
    } else {
        boot();
    }
})();
