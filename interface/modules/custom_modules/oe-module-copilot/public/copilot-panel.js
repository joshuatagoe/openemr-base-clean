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
 * (never innerHTML); any stream event or turn answer whose correlation_id or
 * patient_uuid differs from the ticket response is dropped; values are
 * rendered from the cited records/sections, and the panel never invents a
 * state. Follow-up questions (UC-04) go to the agent with the same ticket;
 * on 401 the panel silently re-requests a ticket for the same bundle from the
 * module (which re-authorizes) and retries once.
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
    const KIND_BADGES = {
        fact: 'badge-success',
        no_record_found: 'badge-warning',
        clarification: 'badge-info',
        refusal: 'badge-secondary'
    };
    // One per record source the agent can read (results, orders, medications, allergies, plan check).
    const EXAMPLE_QUESTIONS = [
        'What was the last A1c?',
        'When was the last potassium?',
        'Was a lipid panel ordered?',
        'Is there a metformin prescription on file?',
        'Any allergies on file?',
        'Which plan items have no evidence yet?'
    ];
    const STATE_BADGES = {
        matching_result_found: 'badge-success',
        order_found_no_result: 'badge-info',
        matching_medication_record_found: 'badge-success',
        no_matching_record_found: 'badge-warning',
        ambiguous_match: 'badge-warning',
        conflicting_records: 'badge-danger',
        verification_unavailable: 'badge-secondary'
    };

    // Hover text for every label the panel shows. The same definitions, as a
    // table, are in COPILOT_GLOSSARY.md at the repository root - keep them in step.
    const HELP = {
        // Week 1 - plan check (one row per commitment from the last plan)
        commitment_number: 'Commitment number, in the order it appears in the last plan. Other sections refer back to it.',
        commitment_lab_test: 'Commitment kind: a lab test the last plan said to order or recheck.',
        commitment_medication: 'Commitment kind: a medication the last plan said to start, stop, change or continue.',
        commitment_other: 'Commitment kind: anything else in the plan (referral, counselling, follow-up). Listed for completeness; not checked against the record.',
        state_matching_result_found: 'A result for this test exists in the record after the plan was written. Cited below.',
        state_order_found_no_result: 'The test was ordered after the plan, but no result has been filed yet.',
        state_matching_medication_record_found: 'A prescription or medication-list entry for this drug exists. Cited below. It shows the record exists, not that the patient took it.',
        state_no_matching_record_found: 'Nothing matching was found in the sources this system searched. This does NOT mean it was not done - it may be recorded elsewhere or outside this EHR.',
        state_ambiguous_match: 'More than one record could match; the system will not guess which one the plan meant.',
        state_conflicting_records: 'Records disagree (for example, active in one table, stopped in another). Both are shown; the conflict is not resolved for you.',
        state_verification_unavailable: 'The source could not be checked at all (it failed to load). Different from "no matching record found".',
        state_not_checked: 'This kind of commitment is not checked against the record; it is listed so nothing in the plan is silently dropped.',
        explained_by: 'This change in the record since the last visit matches a commitment in the plan.',
        unexplained: 'This change in the record since the last visit does not match anything in the plan - worth a look.',
        flag_as_recorded: 'The abnormal flag exactly as stored in the chart. The Co-Pilot did not compute it.',
        medication_status: 'Whether the medication record is active, and which field of the record says so.',
        source_table: 'The OpenEMR table this record came from.',
        allergy_coded: 'Recorded with a standard code.',
        allergy_uncoded: 'Recorded as free text, shown exactly as entered.',
        allergy_inactive: 'Marked inactive in the chart.',
        // Week 1 - follow-up answers
        kind_fact: 'A statement taken from a record in this chart, with its citation.',
        kind_no_record_found: 'The system searched and found nothing. Scoped to what it searched - not proof of absence.',
        kind_clarification: 'The question was ambiguous; the Co-Pilot asks what you meant rather than guessing.',
        kind_refusal: 'Outside what the Co-Pilot does (for example dosing advice, or another patient). A fixed sentence, never model wording.',
        // Week 2 - document briefing
        tier_document_stated: 'Printed in the uploaded document, quoted exactly. Not yet confirmed by a clinician.',
        tier_chart_fact: 'Already recorded in this patient\u2019s chart.',
        tier_computed: 'Worked out by this system with a fixed rule (for example, value above the printed reference range). The lab did not print this; the inputs and the rule are shown.',
        tier_patient_reported: 'Stated by the patient on an intake form. An observation, not a clinical finding.',
        tier_guideline_supported: 'What a published guideline says, quoted and attributed, with why it bears on this patient. It describes the guidance; it is not a recommendation or an order.',
        not_yet_in_chart: 'Read from the document but not filed into the chart. Nothing is filed without a clinician checking it against the source.',
        flag_printed: 'The abnormal flag (H/L) exactly as the lab printed it on the report.',
        computed_rule: 'Computed here, not printed by the lab. It is never shown as if the lab had flagged it.',
        guideline_citation: 'Publisher, year and the population the guidance was written for. Tier A: guidance for general US adults in primary care; can support a consideration. Tier B: care-process material only; never the sole support for a threshold or target.',
        uncertainty: 'What the evidence does not settle for this patient - stated rather than hidden.',
        route: 'The supervisor\u2019s handoffs in order: which worker ran next, and why. intake-extractor reads the document; evidence-retriever finds guideline passages; answer proposes considerations, which are screened before display.',
        provenance: 'Everything that produced this briefing: both models, the reranker (fake-lexical = local deterministic ranking, not a learned model) and the exact guideline corpus version.',
        // Week 2 - documents in this chart (list, values, source viewer, filing)
        doc_status_queued: 'Not read yet. Documents are read when the chart is opened, two at a time.',
        doc_status_processing: 'Being read now.',
        doc_status_extracted: 'Read. Its values are listed below as candidates: nothing is in the chart until a clinician verifies and files it.',
        doc_status_failed: 'Could not be read this time. The reason is shown; most failures are tried again the next time the chart is opened (up to three attempts).',
        doc_status_skipped_duplicate: 'The same file was already read in this chart; its values are listed under that document. Nothing is read twice.',
        doc_status_unsupported: 'Not read: it needs a Co-Pilot category (Lab Report), or it is a kind of file the Co-Pilot does not read.',
        doc_status_held_identity: 'Held: the patient printed on the document may not be this patient. Its values are not shown or used until a clinician confirms the patient.',
        pending_count: 'Values read from this document that are waiting for a clinician to verify and file or reject.',
        verification_verified_exact: 'The system found this exact value on the page; the box shows where. Found is not the same as correct - check it.',
        verification_verified_fuzzy: 'The system found a close match on the page (for example different spacing); the box shows where. Check it.',
        verification_unverified: 'The system could not find this value on the page, so there is no box. Filing it needs an extra confirmation that you checked it yourself.',
        verification_unreadable: 'The value could not be read. It can only be filed with a value you type from the document.',
        value_candidate: 'Read from the document, not in the chart. Waiting for a clinician to verify and file or reject.',
        value_filed: 'Verified and filed into the chart by a clinician (an outside-lab result).',
        value_rejected: 'Rejected by a clinician; never filed. It cannot be filed later.',
        value_unfiled: 'Was filed, then withdrawn: the chart result is kept and marked entered-in-error. It cannot be filed again.',
        verify_and_file: 'Files this value into the chart as an outside-lab result, after you have compared it with the highlighted source. Filing is signing: it needs lab-write and sign permissions.',
        reject_value: 'Marks this value as not to be filed (for example misread or not a result). It cannot be filed afterwards.',
        unfile_value: 'Withdraws a filed value: the chart result is kept for history and marked entered-in-error, so it no longer counts as chart data.',
        bbox_overlay: 'Where on the page the system found this value. It shows where the system read, not that it read correctly.',
        bbox_missing: 'The system could not locate this value on the page, so no box is drawn. It never guesses a position.',
        collection_date_conflict: 'The date you entered differs from the collection date printed on the document. Filing your date needs your confirmation and a reason; both dates and the reason go to the EHR audit log.',
        filed_result_source: 'This chart result was filed from an uploaded document. Opens the document at the page and box it came from.'
    };

    function explain(node, key) {
        const text = HELP[key];
        if (text) {
            node.title = text;
            node.style.cursor = 'help';
        }
        return node;
    }

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

    // ------------------------------------------------------------------ //
    // Source viewer geometry (ADR-008). The agent's box is [x0, y0, x1, y1],
    // 0-1, top-left origin, relative to the page CROPBOX in the displayed
    // (rotation-applied) frame - for photos, the EXIF-oriented image. pdf.js
    // viewports are built from the cropbox at the page's own rotation, and
    // browsers show photos EXIF-oriented, so normally only scaling applies;
    // `rotation` is any extra clockwise rotation the renderer adds on top.
    // A missing or malformed box gives null: the viewer then shows a notice,
    // never a guessed box.
    // ------------------------------------------------------------------ //
    function validBox(bbox) {
        if (!Array.isArray(bbox) || bbox.length !== 4) {
            return false;
        }
        if (!bbox.every((v) => typeof v === 'number' && Number.isFinite(v) && v >= 0 && v <= 1)) {
            return false;
        }
        return bbox[0] < bbox[2] && bbox[1] < bbox[3];
    }

    function rotatePoint(x, y, rotation) {
        switch (rotation) {
            case 90: return [1 - y, x];
            case 180: return [1 - x, 1 - y];
            case 270: return [y, 1 - x];
            default: return [x, y];
        }
    }

    /**
     * @param {number[]|null} bbox  [x0, y0, x1, y1], 0-1, top-left, displayed frame
     * @param {{width:number, height:number, rotation?:number}|null} frame  rendered size in CSS px
     * @returns {{left:number, top:number, width:number, height:number}|null}
     */
    function overlayRect(bbox, frame) {
        if (!validBox(bbox) || !frame || !(frame.width > 0) || !(frame.height > 0)) {
            return null;
        }
        const rotation = frame.rotation || 0;
        if ([0, 90, 180, 270].indexOf(rotation) < 0) {
            return null;
        }
        const a = rotatePoint(bbox[0], bbox[1], rotation);
        const b = rotatePoint(bbox[2], bbox[3], rotation);
        const x0 = Math.min(a[0], b[0]);
        const x1 = Math.max(a[0], b[0]);
        const y0 = Math.min(a[1], b[1]);
        const y1 = Math.max(a[1], b[1]);
        return { left: x0 * frame.width, top: y0 * frame.height, width: (x1 - x0) * frame.width, height: (y1 - y0) * frame.height };
    }

    /** Frame of a pdf.js viewport; extra rotation = viewport rotation minus the page's own /Rotate. */
    function pdfFrame(viewport, pageRotate) {
        const extra = ((((viewport.rotation || 0) - (pageRotate || 0)) % 360) + 360) % 360;
        return { width: viewport.width, height: viewport.height, rotation: extra };
    }

    /** Frame of a displayed <img>: its rendered size (the browser has already applied EXIF orientation). */
    function imageFrame(img) {
        return { width: img.clientWidth, height: img.clientHeight, rotation: 0 };
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

        async requestTicket(refresh) {
            const payload = { pid: Number(this.pid) };
            if (refresh && this.bound && this.bound.bundle_id) {
                payload.refresh_bundle_id = this.bound.bundle_id;
                payload.refresh_correlation_id = this.bound.correlation_id;
            }
            const resp = await fetch(this.ticketUrl, {
                method: 'POST',
                credentials: 'same-origin',
                cache: 'no-store',
                signal: this.abort.signal,
                headers: { 'APICSRFTOKEN': this.csrf, 'Content-Type': 'application/json', 'Accept': 'application/json' },
                body: JSON.stringify(payload)
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
                    item.appendChild(explain(el('span', 'badge badge-warning', 'flagged ' + String(r.abnormal_flag) + ' (as recorded)'), 'flag_as_recorded'));
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
                    item.appendChild(explain(el('span', 'badge badge-light border ml-2', status + ' (' + String(m.status_field || '') + ')'), 'medication_status'));
                    item.appendChild(explain(el('span', 'badge badge-secondary ml-1', String(m.source_table || '')), 'source_table'));
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
                    item.appendChild(explain(el('span', 'badge badge-light border ml-2', a.coded ? 'coded' : 'as recorded (uncoded)'), a.coded ? 'allergy_coded' : 'allergy_uncoded'));
                    if (a.duplicate_count > 1) { item.appendChild(el('span', 'badge badge-secondary ml-1', '\u00d7' + String(a.duplicate_count))); }
                    if (!a.active) { item.appendChild(explain(el('span', 'badge badge-secondary ml-1', 'inactive'), 'allergy_inactive')); }
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
            if (typeof this.onSectionsRendered === 'function') {
                this.onSectionsRendered();
            }
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
                    ? explain(el('span', 'badge badge-light border ml-2', 'explained by ' + (this.commitmentLabels[a.explained_by] || String(a.explained_by))), 'explained_by')
                    : explain(el('span', 'badge badge-warning ml-2', 'unexplained by the plan'), 'unexplained');
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
                    // Ticket expired between issue and use: one silent re-authorization for the same bundle.
                    this.refreshed = true;
                    if (await this.refreshTicket()) {
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

        async refreshTicket() {
            const fresh = await this.requestTicket(true).catch(() => null);
            if (fresh && fresh.ok && fresh.data.ticket && fresh.data.patient_uuid === this.bound.patient_uuid
                && fresh.data.bundle_id === this.bound.bundle_id && fresh.data.correlation_id === this.bound.correlation_id) {
                this.bound.ticket = fresh.data.ticket;
                return true;
            }
            return false;
        }

        renderQuestionBox() {
            if (!this.bound || !this.bound.ticket || !this.bound.bundle_id) {
                return;
            }
            const box = el('div', 'mt-3');
            box.setAttribute('data-role', 'followup');
            box.appendChild(el('h6', 'mb-1', 'Ask about this patient\u2019s record'));
            // The scope is the contract (USER.md UC-04): say it before the physician types.
            const scope = el('div', 'small text-muted mb-2', 'Answers come only from this patient\u2019s results, orders, medications, allergies and the last plan; every statement cites a record. ');
            const examplesToggle = el('a', 'small', 'Examples');
            examplesToggle.href = '#';
            examplesToggle.setAttribute('role', 'button');
            examplesToggle.setAttribute('aria-expanded', 'false');
            scope.appendChild(examplesToggle);
            box.appendChild(scope);
            const examples = el('div', 'mb-2');
            examples.setAttribute('data-role', 'examples');
            examples.hidden = true;
            const form = el('form', 'form-inline mb-2');
            const input = el('input', 'form-control mr-2 flex-grow-1');
            input.type = 'text';
            input.maxLength = 1000;
            input.placeholder = 'e.g. ' + EXAMPLE_QUESTIONS[0];
            input.setAttribute('aria-label', 'Question about this patient\u2019s record');
            EXAMPLE_QUESTIONS.forEach((q) => {
                const chip = el('button', 'btn btn-outline-secondary btn-sm mr-1 mb-1', q);
                chip.type = 'button';
                chip.addEventListener('click', () => { input.value = q; input.focus(); });
                examples.appendChild(chip);
            });
            examplesToggle.addEventListener('click', (ev) => {
                ev.preventDefault();
                examples.hidden = !examples.hidden;
                examplesToggle.setAttribute('aria-expanded', examples.hidden ? 'false' : 'true');
            });
            box.appendChild(examples);
            const button = el('button', 'btn btn-primary btn-sm', 'Ask');
            button.type = 'submit';
            form.appendChild(input);
            form.appendChild(button);
            const answers = el('ul', 'list-group');
            answers.setAttribute('data-role', 'answers');
            form.addEventListener('submit', (ev) => {
                ev.preventDefault();
                const q = input.value.trim();
                if (!q || button.disabled) {
                    return;
                }
                button.disabled = true;
                this.askQuestion(q, answers).finally(() => { button.disabled = false; input.value = ''; });
            });
            box.appendChild(form);
            box.appendChild(answers);
            this.body.appendChild(box);
        }

        async askQuestion(question, answers) {
            const item = el('li', 'list-group-item py-2');
            item.appendChild(el('div', 'font-italic small mb-1', 'Q: ' + question));
            const pending = el('div', 'small text-muted', 'Checking the record\u2026');
            item.appendChild(pending);
            answers.insertBefore(item, answers.firstChild);  // newest question on top: no scrolling in the 90 seconds
            let body = null;
            for (let attempt = 1; attempt <= 2; attempt += 1) {
                let resp;
                try {
                    resp = await fetch(this.bound.agent_url + '/v1/conversations/' + encodeURIComponent(this.bound.bundle_id) + '/turns', {
                        method: 'POST',
                        cache: 'no-store',
                        signal: this.abort.signal,
                        headers: { 'Authorization': 'Bearer ' + this.bound.ticket, 'Content-Type': 'application/json', 'Accept': 'application/json' },
                        body: JSON.stringify({ question: question })
                    });
                } catch {
                    pending.textContent = 'The agent could not be reached.';
                    return;
                }
                if (resp.status === 401 && attempt === 1 && await this.refreshTicket()) {
                    continue;
                }
                if (!resp.ok) {
                    pending.textContent = 'The question could not be answered (agent_http_' + resp.status + ').';
                    return;
                }
                try {
                    body = await resp.json();
                } catch {
                    body = null;
                }
                break;
            }
            item.removeChild(pending);
            if (!this.isBound(body)) {
                this.dropped += 1;
                item.appendChild(el('div', 'small text-muted', 'The answer did not match this patient and was discarded.'));
                return;
            }
            this.renderTurn(body, item);
        }

        renderTurn(turn, item) {
            if (turn.degraded) {
                item.appendChild(el('div', 'small text-warning', 'The answer could not be produced (' + String(turn.degraded.reason_code || 'degraded') + ').'));
                return;
            }
            const statements = Array.isArray(turn.statements) ? turn.statements : [];
            if (statements.length === 0) {
                item.appendChild(el('div', 'small text-muted', 'No verified statements could be made for this question.'));
            }
            statements.forEach((s) => {
                const line = el('div', 'mb-1');
                line.appendChild(explain(el('span', 'badge ' + (KIND_BADGES[s.kind] || 'badge-secondary') + ' mr-1', String(s.kind || '').replace('_', ' ')), 'kind_' + String(s.kind)));
                line.appendChild(document.createTextNode(String(s.text || '')));
                const cites = Array.isArray(s.citations) ? s.citations : [];
                if (cites.length) {
                    line.appendChild(el('div', 'small text-muted', 'Sources: ' + cites.map((c) => String(c.record_id) + ' (' + fmtDate(c.timestamp) + ')').join('; ')));
                }
                item.appendChild(line);
            });
            if (turn.rejected_count > 0) {
                item.appendChild(el('div', 'small text-warning', String(turn.rejected_count) + ' statement(s) withheld \u2014 could not be verified.'));
            }
            const tools = Array.isArray(turn.tool_calls) ? turn.tool_calls : [];
            if (tools.length) {
                item.appendChild(el('div', 'small text-muted', 'Searched: ' + tools.map((t) => String(t.tool) + (t.error ? ' (' + String(t.error) + ')' : ' (' + String(t.records) + ')')).join(', ')));
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
                this.renderQuestionBox();
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
            left.appendChild(explain(el('span', 'badge badge-dark mr-1', label), 'commitment_number'));
            left.appendChild(explain(el('span', 'badge badge-light border mr-1', String(c.kind || '').replace('_', '/')), 'commitment_' + String(c.kind)));
            left.appendChild(el('span', 'font-italic', '“' + String(c.source_span || '') + '”'));
            header.appendChild(left);
            header.appendChild(explain(el('span', 'badge ' + (unchecked ? 'badge-light border' : (STATE_BADGES[state] || 'badge-secondary')), unchecked ? 'Not checked' : (STATE_LABELS[state] || state)), 'state_' + state));
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

    // ------------------------------------------------------------------ //
    // Week 2: brief from the patient's latest lab document on file.
    // Independent of the Week 1 flow above: its own node after the card body
    // (so a Week 1 error that clears the body cannot remove it), its own
    // request to POST /api/copilot/document-briefing (same session, same
    // APICSRFTOKEN), plain text only via textContent.
    // ------------------------------------------------------------------ //
    const DOC_DEGRADED_MESSAGES = {
        no_document_on_file: 'No lab document (PDF, PNG or JPEG) is on file in this patient’s Documents.',
        document_unavailable: 'The latest document on file could not be read.',
        document_too_large: 'The latest document on file is over 10 MB, the largest the Co-Pilot reads.',
        agent_unavailable: 'The Co-Pilot agent could not be reached.'
    };

    function docValue(value, unit) {
        return String(value) + (unit ? ' ' + String(unit) : '');
    }

    function docCitation(c) {
        if (!c) {
            return null;
        }
        if (c.source_type === 'document') {
            return 'Source: document ' + String(c.source_id || '') + (c.page_or_section ? ', ' + String(c.page_or_section) : '')
                + (c.quote_or_value ? ', as printed: “' + String(c.quote_or_value) + '”' : '');
        }
        return 'Source: chart ' + String(c.record_type || '') + ' ' + String(c.record_id || c.source_id || '') + (c.timestamp ? ' (' + fmtDate(c.timestamp) + ')' : '');
    }

    function tierBadge(tier) {
        return explain(el('span', 'badge badge-light border mr-1', String(tier || 'unknown tier')), 'tier_' + String(tier));
    }

    function notInChartBadge() {
        return explain(el('span', 'badge badge-info ml-1', 'not yet in the chart'), 'not_yet_in_chart');
    }

    class DocumentBriefingSection {
        constructor(container) {
            this.pid = container.dataset.pid;
            this.csrf = container.dataset.csrf;
            this.url = String(container.dataset.ticketUrl || '').replace(/\/briefing-ticket$/, '/document-briefing');
            this.busy = false;
            this.root = el('div', 'card-body border-top');
            this.root.dataset.role = 'document-briefing';
            this.button = el('button', 'btn btn-outline-primary btn-sm', 'Brief from latest lab document');
            this.button.setAttribute('type', 'button');
            this.button.addEventListener('click', (e) => {
                if (e && e.preventDefault) {
                    e.preventDefault();
                }
                this.run();
            });
            this.output = el('div', 'mt-2');
            this.root.appendChild(this.button);
            this.root.appendChild(this.output);
            container.appendChild(this.root);
        }

        clear() {
            while (this.output.firstChild) {
                this.output.removeChild(this.output.firstChild);
            }
        }

        async run() {
            if (this.busy) {
                return;
            }
            this.busy = true;
            this.button.disabled = true;
            this.clear();
            this.output.appendChild(el('p', 'text-muted small mb-0', 'Reading the latest lab document… this can take up to a minute.'));
            let data = null;
            let failure = null;
            try {
                const resp = await fetch(this.url, {
                    method: 'POST',
                    credentials: 'same-origin',
                    cache: 'no-store',
                    headers: { 'APICSRFTOKEN': this.csrf, 'Content-Type': 'application/json', 'Accept': 'application/json' },
                    body: JSON.stringify({ pid: Number(this.pid) })
                });
                try {
                    data = await resp.json();
                } catch {
                    data = null;
                }
                if (resp.status !== 200 || !data) {
                    const detail = data && data.detail ? data.detail : {};
                    failure = String(detail.message || 'The document briefing could not be requested.') + ' (' + String(detail.code || 'http_' + resp.status) + ')';
                }
            } catch {
                failure = 'The document briefing could not be requested.';
            }
            this.clear();
            if (failure !== null) {
                this.output.appendChild(el('p', 'text-danger small mb-0', failure));
            } else {
                this.render(data);
            }
            this.busy = false;
            this.button.disabled = false;
        }

        render(data) {
            if (data.status !== 'ok' || !data.briefing) {
                const reason = String(data.degraded_reason || 'unknown');
                this.output.appendChild(el('p', 'text-muted small mb-0',
                    (DOC_DEGRADED_MESSAGES[reason] || 'The document briefing is unavailable.') + ' (reason: ' + reason + ')'));
                this.renderProvenance(data.provenance, data.routing);
                return;
            }
            const b = data.briefing;
            if (b.refusal) {
                this.output.appendChild(el('p', 'small font-italic mb-2', String(b.refusal)));
            }
            this.renderLines('What changed', b.what_changed);
            this.renderLines('Needs attention', b.needs_attention);
            this.renderConsiderations('What to consider', b.what_to_consider);

            const limitations = Array.isArray(b.limitations) ? b.limitations : [];
            if (limitations.length) {
                this.output.appendChild(el('h6', 'mt-3 mb-1', 'Limitations'));
                const ul = el('ul', 'small mb-2');
                limitations.forEach((l) => ul.appendChild(el('li', null, String(l))));
                this.output.appendChild(ul);
            }
            const dropped = Array.isArray(b.dropped) ? b.dropped : [];
            if (dropped.length) {
                // Say WHY each was withheld. A single generic sentence misstated the
                // reason: a statement dropped for being phrased as an instruction was
                // reported as "could not be verified against its sources".
                const reasons = new Map();
                dropped.forEach((d) => {
                    const why = d && typeof d.detail === 'string' && d.detail
                        ? d.detail
                        : String((d && d.reason) || 'reason not given');
                    reasons.set(why, (reasons.get(why) || 0) + 1);
                });
                this.output.appendChild(el('p', 'small text-warning mb-1', String(dropped.length) + ' statement(s) withheld before display:'));
                const why = el('ul', 'small text-warning mb-2');
                reasons.forEach((n, reason) => why.appendChild(el('li', null, String(n) + ' × ' + reason)));
                this.output.appendChild(why);
            }
            this.renderProvenance(data.provenance, data.routing);
        }

        renderLines(heading, lines) {
            this.output.appendChild(el('h6', 'mt-3 mb-1', heading));
            const list = el('ul', 'list-group mb-2');
            const rows = Array.isArray(lines) ? lines : [];
            if (!rows.length) {
                list.appendChild(el('li', 'list-group-item small text-muted', 'Nothing to report from the supplied records.'));
            }
            rows.forEach((line) => list.appendChild(this.lineItem(line)));
            this.output.appendChild(list);
        }

        lineItem(line) {
            const item = el('li', 'list-group-item py-2 small');
            item.dataset.tier = String(line.tier || '');
            const head = el('div');
            head.appendChild(tierBadge(line.tier));
            head.appendChild(el('span', null, String(line.text || '')));
            if (line.not_yet_in_chart) {
                head.appendChild(notInChartBadge());
            }
            item.appendChild(head);
            const c = line.computed;
            if (line.tier === 'computed' && c) {
                // Derived here, not printed by the lab: show the inputs and the rule, never an H/L flag badge.
                item.appendChild(explain(el('div', 'text-muted font-italic',
                    'computed by this system: ' + docValue(c.value, c.unit) + ' is ' + String(c.direction) + ' the printed reference range ' + String(c.reference_range)), 'computed_rule'));
                item.appendChild(el('div', 'text-muted font-italic', 'rule: ' + String(c.rule || '')));
            } else if (line.abnormal_flag_source === 'extracted' && line.abnormal_flag) {
                item.appendChild(explain(el('span', 'badge badge-warning', 'flag printed on the report: ' + String(line.abnormal_flag)), 'flag_printed'));
            }
            [docCitation(line.document_citation), docCitation(line.record_citation)]
                .filter((t) => t)
                .forEach((t) => item.appendChild(el('div', 'text-muted', t)));
            return item;
        }

        renderConsiderations(heading, considerations) {
            this.output.appendChild(el('h6', 'mt-3 mb-1', heading));
            const list = el('ul', 'list-group mb-2');
            const rows = Array.isArray(considerations) ? considerations : [];
            if (!rows.length) {
                list.appendChild(el('li', 'list-group-item small text-muted', 'Nothing to report from the supplied records.'));
            }
            rows.forEach((c) => {
                const item = el('li', 'list-group-item py-2 small');
                item.dataset.tier = String(c.tier || 'guideline_supported');
                const head = el('div');
                head.appendChild(tierBadge(c.tier || 'guideline_supported'));
                head.appendChild(el('strong', null, String(c.topic || '')));
                item.appendChild(head);
                item.appendChild(el('div', 'mt-1', String(c.text || '')));
                item.appendChild(el('div', 'mt-1', 'Why this patient: ' + String(c.relevance || '')));
                (Array.isArray(c.facts) ? c.facts : []).forEach((f) => {
                    const fact = el('div', 'text-muted mt-1');
                    fact.appendChild(el('span', null, 'Patient fact: '));
                    fact.appendChild(tierBadge(f.tier));
                    fact.appendChild(el('span', null, String(f.text || '')));
                    if (f.not_yet_in_chart) {
                        fact.appendChild(notInChartBadge());
                    }
                    item.appendChild(fact);
                });
                (Array.isArray(c.citations) ? c.citations : []).forEach((g) => {
                    const cite = el('div', 'mt-1 pl-2 border-left');
                    cite.appendChild(explain(el('div', null,
                        'Guideline: ' + String(g.publisher || 'unknown publisher') + ', ' + (g.publication_year ? String(g.publication_year) : 'undated')
                        + ' · population: ' + String(g.population_scope || 'not stated')
                        + ' · evidence Tier ' + String(g.evidence_tier || '?')), 'guideline_citation'));
                    if (g.page_or_section) {
                        cite.appendChild(el('div', 'text-muted', String(g.page_or_section)));
                    }
                    if (g.quote_or_value) {
                        cite.appendChild(el('div', 'text-muted font-italic', '“' + String(g.quote_or_value) + '”'));
                    }
                    item.appendChild(cite);
                });
                if (c.uncertainty) {
                    item.appendChild(explain(el('div', 'mt-1 font-italic', 'Uncertainty: ' + String(c.uncertainty)), 'uncertainty'));
                }
                list.appendChild(item);
            });
            this.output.appendChild(list);
        }

        renderProvenance(p, routing) {
            // The supervisor's handoffs, in order (CR4). Fixed codes only.
            if (Array.isArray(routing) && routing.length) {
                this.output.appendChild(explain(el('p', 'small text-muted mt-2 mb-0',
                    'Route: supervisor → ' + routing.map(function (d) {
                        return String(d.target) + ' (' + String(d.reason_code) + ')';
                    }).join(' → ')), 'route'));
            }
            if (!p) {
                return;
            }
            this.output.appendChild(explain(el('p', 'small text-muted mt-2 mb-0',
                'Extraction model: ' + String(p.extraction_model || 'unknown')
                + ' · Answer model: ' + String(p.answer_model || 'unknown')
                + ' · Reranker: ' + String(p.reranker || 'unknown')
                + ' · Corpus: ' + String(p.corpus_version || 'unknown')
                + (p.evidence_status ? ' · Evidence retrieval: ' + String(p.evidence_status) : '')), 'provenance'));
        }
    }

    // ------------------------------------------------------------------ //
    // Week 2 Final: documents in this chart (ADR-012), their values, the
    // source viewer (ADR-008) and Verify and file (ADR-003, ADR-009).
    // Every request goes to the module under the local API bridge with the
    // APICSRFTOKEN header; the module binds it to the session's patient.
    // Everything is rendered with textContent; nothing is logged.
    // ------------------------------------------------------------------ //
    const DOC_TYPE_LABELS = {
        lab_pdf: 'Lab report',
        intake_form: 'Intake form',
        unsupported: 'No Co-Pilot category'
    };
    const DOC_STATUS = {
        queued: ['Waiting to be read', 'badge-secondary'],
        processing: ['Being read', 'badge-info'],
        extracted: ['Read', 'badge-success'],
        failed: ['Could not be read', 'badge-danger'],
        skipped_duplicate: ['Already read (same file)', 'badge-light border'],
        unsupported: ['Not read', 'badge-light border'],
        held_identity: ['Held: identity check', 'badge-warning']
    };
    const DOC_CODE_TEXT = {
        needs_category: 'Needs a category: file it under “Lab Report” in this patient’s Documents to have it read.',
        unsupported_media_type: 'Only PDF, PNG and JPEG files are read.',
        document_too_large: 'Over 10 MB, the largest file the Co-Pilot reads.',
        doc_type_not_supported_yet: 'Intake forms are not read yet.',
        same_content_as_other_document: 'The same file was already read in this chart; its values are listed under that document.',
        same_file_in_other_chart: 'The same file is also filed in another patient’s chart, and the name and date of birth printed on it do not confirm this patient: it may be misfiled.',
        same_file_also_in_other_chart: 'The same file is also filed in another patient’s chart. The name and date of birth printed on it match this chart, so the other copy is the likely misfiling.',
        moved_after_filing: 'This document was moved here from another chart after a value from it had been filed there; it is not read again automatically. A clinician needs to review it.',
        bad_extraction: 'The reading came back malformed. It will be tried again the next time the chart is opened.',
        store_failed: 'The reading could not be saved. It will be tried again the next time the chart is opened.',
        document_unavailable: 'The file could not be read from OpenEMR.',
        already_in_progress: 'Another request is reading it right now.',
        agent_not_configured: 'The Co-Pilot agent is not configured on this server.',
        agent_unreachable: 'The Co-Pilot agent could not be reached. It will be tried again the next time the chart is opened.',
        agent_timeout: 'The Co-Pilot agent took too long. It will be tried again the next time the chart is opened.',
        agent_rejected: 'The Co-Pilot agent refused the request. It will be tried again the next time the chart is opened.',
        agent_bad_response: 'The Co-Pilot agent’s answer was not usable. It will be tried again the next time the chart is opened.',
        agent_degraded: 'The Co-Pilot agent could not read it this time. It will be tried again the next time the chart is opened.'
    };
    const HELD_TEXT = 'The name or date of birth printed on this document does not match this chart. Its values are not shown or used until the patient is confirmed. Open the document to check it; if it belongs to another patient, move it to the right chart in Documents.';
    const VERIFICATION_LABELS = {
        verified_exact: ['Found on the page', 'badge-success'],
        verified_fuzzy: ['Found on the page (close match)', 'badge-success'],
        unverified: ['Not found on the page', 'badge-warning'],
        unreadable: ['Unreadable', 'badge-danger']
    };
    const VALUE_STATUS = {
        candidate: ['Waiting for review', 'badge-info'],
        filed: ['Filed ✓', 'badge-success'],
        rejected: ['Rejected', 'badge-secondary'],
        unfiled: ['Un-filed (entered in error)', 'badge-secondary']
    };
    const MAX_PROCESS_CALLS = 10;
    const MAX_VALUE_LISTS = 20;

    /**
     * Plain-language description of one row of GET /api/copilot/documents.
     * Pure; unknown codes are shown as codes, never guessed.
     */
    function describeDocument(doc) {
        const status = String(doc.status || '');
        const code = doc.error_code ? String(doc.error_code) : null;
        let label = (DOC_STATUS[status] || [status || 'unknown', 'badge-secondary'])[0];
        let badge = (DOC_STATUS[status] || [null, 'badge-secondary'])[1];
        if (status === 'unsupported' && code === 'needs_category') {
            label = 'Needs a category';
            badge = 'badge-warning';
        }
        const notes = [];
        if (status === 'held_identity') {
            notes.push(HELD_TEXT);
        }
        if (code) {
            notes.push(DOC_CODE_TEXT[code] || ('Reason code: ' + code + '.'));
        }
        if (status === 'extracted') {
            const n = Number(doc.pending_count) || 0;
            notes.push(n === 0 ? 'No values waiting for review.' : (n === 1 ? '1 value waiting for review.' : n + ' values waiting for review.'));
            if (doc.identity_check === 'missing') {
                notes.push('The name and date of birth printed on it could not be compared with this chart; check the document is this patient’s before filing.');
            }
        }
        return {
            typeLabel: DOC_TYPE_LABELS[doc.doc_type] || String(doc.doc_type || 'Document'),
            statusLabel: label,
            badge: badge,
            helpKey: 'doc_status_' + status,
            notes: notes,
            showValues: status === 'extracted'
        };
    }

    /** Module routes under the local API bridge, bound to the session's patient. */
    class CopilotApi {
        constructor(container) {
            this.csrf = container.dataset.csrf;
            this.pid = Number(container.dataset.pid);
            const ticketUrl = String(container.dataset.ticketUrl || '');
            this.base = ticketUrl.replace(/\/briefing-ticket$/, '');
            this.webroot = ticketUrl.split('/apis/')[0];
            this.abort = new AbortController();
        }

        headers(json) {
            const h = { 'APICSRFTOKEN': this.csrf, 'Accept': json ? 'application/json' : '*/*' };
            if (json) {
                h['Content-Type'] = 'application/json';
            }
            return h;
        }

        /** @returns {Promise<{status:number, data:object|null}>} status 0 when the request could not be sent */
        async json(method, path, body) {
            let resp;
            try {
                resp = await fetch(this.base + path, {
                    method: method,
                    credentials: 'same-origin',
                    cache: 'no-store',
                    signal: this.abort.signal,
                    headers: this.headers(true),
                    body: body === undefined ? undefined : JSON.stringify(body)
                });
            } catch {
                return { status: 0, data: null };
            }
            let data = null;
            try {
                data = await resp.json();
            } catch {
                data = null;
            }
            return { status: resp.status, data: data };
        }

        /** The stored bytes of one document (ADR-008 file route). */
        file(documentId) {
            return fetch(this.base + '/document-file/' + encodeURIComponent(String(documentId)), {
                method: 'GET',
                credentials: 'same-origin',
                cache: 'no-store',
                signal: this.abort.signal,
                headers: this.headers(false)
            });
        }
    }

    function problemText(result, fallback) {
        const detail = result && result.data && result.data.detail ? result.data.detail : null;
        if (detail && detail.message) {
            return String(detail.message) + ' (' + String(detail.code || 'http_' + result.status) + ')';
        }
        return fallback + ' (' + (result && result.status ? 'http_' + result.status : 'network') + ')';
    }

    function valueText(v) {
        if (v.value_text === null || v.value_text === undefined || v.value_text === '') {
            return 'unreadable';
        }
        return String(v.value_text) + (v.unit ? ' ' + String(v.unit) : '');
    }

    class DocumentsSection {
        constructor(container) {
            this.api = new CopilotApi(container);
            this.docs = [];
            this.values = {}; // document_id -> values response
            this.filedResults = {}; // procedure_result_id -> true
            this.root = el('div', 'card-body border-top');
            this.root.dataset.role = 'documents';
            this.root.setAttribute('aria-label', 'Documents in this chart');
            this.root.appendChild(el('h6', 'mb-1', 'Documents in this chart'));
            this.status = el('p', 'small text-muted mb-2', 'Checking for new documents…');
            this.status.dataset.role = 'documents-status';
            this.status.setAttribute('role', 'status');
            this.status.setAttribute('aria-live', 'polite');
            this.list = el('ul', 'list-group');
            this.list.dataset.role = 'document-list';
            this.viewerHost = el('div', 'mt-2');
            this.viewerHost.dataset.role = 'viewer-host';
            this.root.appendChild(this.status);
            this.root.appendChild(this.list);
            this.root.appendChild(this.viewerHost);
            container.appendChild(this.root);
            const onLeave = () => this.api.abort.abort();
            window.addEventListener('pagehide', onLeave);
        }

        /** Chart open: process unread documents (two per call) until none remain, then list them. */
        async start() {
            let calls = 0;
            let last = null;
            for (;;) {
                calls += 1;
                const result = await this.api.json('POST', '/documents/process', { pid: this.api.pid });
                if (result.status !== 200 || !result.data) {
                    if (last === null) {
                        this.status.textContent = problemText(result, 'The documents could not be checked.');
                        return;
                    }
                    break;
                }
                last = result.data;
                if (last.status !== 'ok') {
                    this.status.textContent = 'Documents are unavailable (' + String(last.degraded_reason || 'degraded') + ').';
                    return;
                }
                const remaining = Number(last.remaining) || 0;
                const processed = Array.isArray(last.processed) ? last.processed.length : 0;
                this.renderList(Array.isArray(last.documents) ? last.documents : []);
                if (remaining <= 0) {
                    this.status.textContent = this.summary();
                    break;
                }
                if (processed === 0 || calls >= MAX_PROCESS_CALLS) {
                    this.status.textContent = remaining + ' document(s) are still being read or could not be read now; they are tried again the next time the chart is opened.';
                    break;
                }
                this.status.textContent = 'Reading new documents… ' + remaining + ' left.';
            }
            await this.loadAllValues();
        }

        summary() {
            const pending = this.docs.reduce((n, d) => n + (d.status === 'extracted' ? (Number(d.pending_count) || 0) : 0), 0);
            if (!this.docs.length) {
                return 'No documents on file.';
            }
            return this.docs.length + ' document(s) on file; ' + pending + ' value(s) waiting for review.';
        }

        async refreshList() {
            const result = await this.api.json('GET', '/documents?pid=' + encodeURIComponent(String(this.api.pid)));
            if (result.status === 200 && result.data && result.data.status === 'ok') {
                this.renderList(Array.isArray(result.data.documents) ? result.data.documents : []);
                this.status.textContent = this.summary();
                await this.loadAllValues();
            }
        }

        renderList(docs) {
            this.docs = docs;
            while (this.list.firstChild) {
                this.list.removeChild(this.list.firstChild);
            }
            if (!docs.length) {
                this.list.appendChild(el('li', 'list-group-item small text-muted', 'No documents on file for this patient.'));
                return;
            }
            docs.forEach((doc) => {
                const d = describeDocument(doc);
                const item = el('li', 'list-group-item py-2 small');
                item.dataset.role = 'document-item';
                item.dataset.documentId = String(doc.document_id);
                const head = el('div', 'd-flex flex-wrap align-items-center');
                head.appendChild(el('strong', 'mr-2', d.typeLabel));
                head.appendChild(el('span', 'text-muted mr-2', 'uploaded ' + fmtDate(doc.uploaded_at) + ' · document ' + String(doc.document_id)));
                head.appendChild(explain(el('span', 'badge ' + d.badge + ' mr-1', d.statusLabel), d.helpKey));
                if (doc.status === 'extracted' && Number(doc.pending_count) > 0) {
                    head.appendChild(explain(el('span', 'badge badge-info mr-1', String(doc.pending_count) + ' to review'), 'pending_count'));
                }
                const view = el('button', 'btn btn-link btn-sm p-0 ml-auto', 'View document');
                view.type = 'button';
                view.dataset.action = 'view-document';
                view.addEventListener('click', () => this.openSource(doc.document_id, null, null));
                if (!doc.error_code || ['unsupported_media_type', 'document_too_large'].indexOf(String(doc.error_code)) < 0) {
                    head.appendChild(view);
                }
                item.appendChild(head);
                d.notes.forEach((n) => item.appendChild(el('div', doc.status === 'held_identity' || doc.status === 'failed' ? 'text-warning' : 'text-muted', n)));
                const holder = el('div', 'mt-1');
                holder.dataset.role = 'value-list';
                item.appendChild(holder);
                if (this.values[doc.document_id] && d.showValues) {
                    this.renderValues(doc, this.values[doc.document_id], holder);
                }
                this.list.appendChild(item);
            });
        }

        async loadAllValues() {
            const wanted = this.docs.filter((d) => describeDocument(d).showValues).slice(0, MAX_VALUE_LISTS);
            for (const doc of wanted) {
                await this.loadValues(doc);
            }
        }

        async loadValues(doc) {
            const holder = this.holderFor(doc.document_id);
            const result = await this.api.json('GET', '/documents/' + encodeURIComponent(String(doc.document_id)) + '/values');
            if (!holder) {
                return;
            }
            if (result.status !== 200 || !result.data) {
                while (holder.firstChild) {
                    holder.removeChild(holder.firstChild);
                }
                holder.appendChild(el('div', 'text-danger', problemText(result, 'The values could not be read.')));
                return;
            }
            this.values[doc.document_id] = result.data;
            (Array.isArray(result.data.values) ? result.data.values : []).forEach((v) => {
                if (v.procedure_result_id && (v.status === 'filed' || v.status === 'unfiled')) {
                    this.filedResults[String(v.procedure_result_id)] = true;
                }
            });
            this.renderValues(doc, result.data, holder);
            this.annotateFiledResults();
        }

        holderFor(documentId) {
            const item = this.list.querySelector('[data-role="document-item"][data-document-id="' + String(Number(documentId)) + '"]');
            return item ? item.querySelector('[data-role="value-list"]') : null;
        }

        valueFor(documentId, resultIndex) {
            const data = this.values[documentId];
            const values = data && Array.isArray(data.values) ? data.values : [];
            return values.find((v) => v.result_index === resultIndex) || null;
        }

        renderValues(doc, data, holder) {
            while (holder.firstChild) {
                holder.removeChild(holder.firstChild);
            }
            const values = Array.isArray(data.values) ? data.values : [];
            if (!values.length) {
                holder.appendChild(el('div', 'text-muted', 'No values were read from this document.'));
                return;
            }
            const list = el('ul', 'list-unstyled mb-0 pl-2 border-left');
            values.forEach((v) => list.appendChild(this.valueRow(doc, v)));
            holder.appendChild(list);
        }

        valueRow(doc, v) {
            const row = el('li', 'py-1');
            row.dataset.role = 'value-row';
            row.dataset.resultIndex = String(v.result_index);
            const line = el('div', 'd-flex flex-wrap align-items-center');
            line.appendChild(el('strong', 'mr-1', String(v.test_name || 'unnamed test')));
            line.appendChild(el('span', 'mr-2', valueText(v)));
            const ver = VERIFICATION_LABELS[v.verification_status] || [String(v.verification_status || 'unknown'), 'badge-secondary'];
            line.appendChild(explain(el('span', 'badge ' + ver[1] + ' mr-1', ver[0]), 'verification_' + String(v.verification_status)));
            const st = VALUE_STATUS[v.status] || [String(v.status || 'unknown'), 'badge-secondary'];
            line.appendChild(explain(el('span', 'badge ' + st[1] + ' mr-1', st[0]), 'value_' + String(v.status)));
            if (v.status === 'candidate') {
                const review = el('button', 'btn btn-outline-primary btn-sm py-0 ml-1', 'Review source');
                review.type = 'button';
                review.dataset.action = 'review';
                review.setAttribute('aria-label', 'Review the source of ' + String(v.test_name || 'this value') + ' before filing');
                review.addEventListener('click', () => this.openSource(doc.document_id, v.result_index, null));
                line.appendChild(review);
            } else if (v.status === 'filed' || v.status === 'unfiled') {
                const view = el('button', 'btn btn-link btn-sm py-0', 'View source');
                view.type = 'button';
                view.dataset.action = 'view';
                view.addEventListener('click', () => this.openSource(doc.document_id, v.result_index, null));
                line.appendChild(view);
                if (v.status === 'filed') {
                    line.appendChild(this.unfileButton(doc.document_id, v));
                }
            }
            row.appendChild(line);
            const detail = [];
            if (v.reference_range) {
                detail.push('range ' + String(v.reference_range));
            }
            detail.push(v.collection_date ? 'collected ' + String(v.collection_date) : 'no collection date on the document');
            if (v.page) {
                detail.push('page ' + String(v.page));
            }
            const info = el('div', 'text-muted', detail.join(' · '));
            if (v.flag_source === 'extracted' && v.abnormal_flag) {
                info.appendChild(document.createTextNode(' '));
                info.appendChild(explain(el('span', 'badge badge-warning', 'flag printed on the report: ' + String(v.abnormal_flag)), 'flag_printed'));
            }
            row.appendChild(info);
            return row;
        }

        // Replaced by the source viewer and filing controls (below).
        openSource() {
            return undefined;
        }

        unfileButton() {
            return el('span');
        }

        annotateFiledResults() {
            return undefined;
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
        const documents = new DocumentsSection(container);
        window.oeCopilotDocuments = documents;
        panel.onSectionsRendered = () => documents.annotateFiledResults();
        documents.start();
        window.oeCopilotDocumentBriefing = new DocumentBriefingSection(container);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', boot);
    } else {
        boot();
    }

    // Pure helpers, exported for the jest tests only (browsers have no `module`).
    if (typeof module === 'object' && module && module.exports) {
        module.exports = {
            overlayRect: overlayRect,
            pdfFrame: pdfFrame,
            imageFrame: imageFrame,
            describeDocument: describeDocument,
            DocumentsSection: DocumentsSection
        };
    }
})();
