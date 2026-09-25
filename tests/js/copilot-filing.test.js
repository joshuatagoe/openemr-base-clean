/**
 * @jest-environment jsdom
 */

/**
 * Clinical Co-Pilot panel - Verify and file / Reject / Un-file (ADR-003, ADR-009
 * §2, §7, §7b, §7c): the rules the panel applies before a request and how it
 * reads each answer of the filing routes.
 *
 * Run with: npx jest tests/js/copilot-filing.test.js
 *
 * @package   OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

const lib = require('../../interface/modules/custom_modules/oe-module-copilot/public/copilot-panel.js');

const { buildFileBody, classifyFileResponse, DocumentsSection } = lib;

function value(over) {
    return Object.assign({
        result_index: 0, test_name: 'Hemoglobin A1c', value_text: '8.4', unit: '%', reference_range: '4.0-5.6',
        abnormal_flag: 'H', flag_source: 'extracted', collection_date: '2026-09-01', verification_status: 'verified_exact',
        page: 1, bbox: [0.1, 0.2, 0.3, 0.25], status: 'candidate', procedure_result_id: null
    }, over || {});
}

const blank = { typed: '', confirmUnverified: false, date: '', confirmDate: false, reason: '' };

describe('buildFileBody', () => {
    test('a verified value as read: no overrides sent', () => {
        expect(buildFileBody(value(), Object.assign({}, blank, { typed: '8.4' }))).toEqual({ ok: true, body: { filed_value: null, confirm_unverified: false } });
    });

    test('a corrected value is sent as the filed value', () => {
        expect(buildFileBody(value(), Object.assign({}, blank, { typed: ' 8.1 ' })).body.filed_value).toBe('8.1');
    });

    test('unverified needs the extra confirmation', () => {
        const refused = buildFileBody(value({ verification_status: 'unverified', bbox: null }), Object.assign({}, blank, { typed: '8.4' }));
        expect(refused.ok).toBe(false);
        expect(refused.code).toBe('confirmation_required');
        const ok = buildFileBody(value({ verification_status: 'unverified', bbox: null }), Object.assign({}, blank, { typed: '8.4', confirmUnverified: true }));
        expect(ok.body.confirm_unverified).toBe(true);
    });

    test('unreadable needs a typed value', () => {
        const v = value({ verification_status: 'unreadable', value_text: null, bbox: null });
        expect(buildFileBody(v, Object.assign({}, blank, { typed: '  ' })).code).toBe('value_required');
        expect(buildFileBody(v, Object.assign({}, blank, { typed: '7.9' })).body.filed_value).toBe('7.9');
    });

    test('a missing collection date needs the clinician’s verified date; never the upload date', () => {
        const v = value({ collection_date: null });
        expect(buildFileBody(v, Object.assign({}, blank, { typed: '8.4' })).code).toBe('collection_date_required');
        expect(buildFileBody(v, Object.assign({}, blank, { typed: '8.4', date: '2026-08-30' })).body.collection_date).toBe('2026-08-30');
    });

    test('a date correction needs confirmation and a reason', () => {
        const input = Object.assign({}, blank, { typed: '8.4', date: '2026-08-30', confirmDate: true, reason: '' });
        expect(buildFileBody(value(), input).code).toBe('reason_required');
        const ok = buildFileBody(value(), Object.assign(input, { reason: 'Misread: the report says 30 Aug' }));
        expect(ok.body).toEqual({ filed_value: null, confirm_unverified: false, collection_date: '2026-08-30', confirm_date_override: true, override_reason: 'Misread: the report says 30 Aug' });
        expect(buildFileBody(value(), Object.assign({}, input, { reason: 'x'.repeat(501) })).code).toBe('reason_too_long');
    });
});

describe('classifyFileResponse', () => {
    test('filed, with the duplicate warning', () => {
        expect(classifyFileResponse(200, { status: 'filed', already_filed: false, warning: 'same_result_already_in_chart' }))
            .toEqual(expect.objectContaining({ kind: 'filed', warning: expect.stringMatching(/already in the chart/) }));
    });

    test('already filed (double click) is a success, not an error', () => {
        expect(classifyFileResponse(200, { status: 'filed', already_filed: true, warning: null }).kind).toBe('already_filed');
    });

    test('date conflict carries both dates', () => {
        const r = classifyFileResponse(409, { detail: { code: 'collection_date_conflict', message: 'm', extracted_collection_date: '2026-09-01', entered_collection_date: '2026-08-30' } });
        expect(r).toEqual(expect.objectContaining({ kind: 'date_conflict', extracted: '2026-09-01', entered: '2026-08-30' }));
    });

    test.each([
        [422, 'collection_date_required', 'date_required'],
        [422, 'confirmation_required', 'confirm_unverified'],
        [422, 'value_required', 'value_required'],
        [409, 'value_rejected', 'closed'],
        [409, 'value_unfiled', 'closed'],
        [409, 'value_already_filed', 'closed'],
        [403, 'filing_not_permitted', 'error'],
        [500, 'filing_failed', 'error']
    ])('%i %s -> %s', (status, code, kind) => {
        const r = classifyFileResponse(status, { detail: { code: code, message: 'Server says ' + code } });
        expect(r.kind).toBe(kind);
        expect(r.message).toContain('Server says ' + code);
    });

    test('no body (network) is an error', () => {
        expect(classifyFileResponse(0, null).kind).toBe('error');
    });
});

// ------------------------------------------------------------------ DOM flow

function json(status, body) {
    return { ok: status >= 200 && status < 300, status: status, headers: { get: () => 'application/json' }, json: async () => body };
}

function pdfResponse() {
    return { ok: true, status: 200, headers: { get: () => 'application/pdf' }, arrayBuffer: async () => new Uint8Array([1]).buffer };
}

function fakePdfjs() {
    return {
        GlobalWorkerOptions: {},
        getDocument: () => ({
            promise: Promise.resolve({
                numPages: 1,
                getPage: async () => ({ rotate: 0, getViewport: ({ scale }) => ({ width: 612 * scale, height: 792 * scale, rotation: 0 }), render: () => ({ promise: Promise.resolve() }) })
            }),
            destroy: () => undefined
        })
    };
}

async function settle() {
    for (let i = 0; i < 6; i += 1) {
        await new Promise((r) => setTimeout(r, 0));
    }
}

/** A tiny module: one document, its values, and the filing routes. */
function server(values) {
    const state = { values: values, posts: [] };
    global.fetch = jest.fn(async (url, init) => {
        if (url.endsWith('/documents/process') || /\/documents\?pid=/.test(url)) {
            const pending = state.values.filter((v) => v.status === 'candidate').length;
            return json(200, { status: 'ok', processed: [], remaining: 0, documents: [{ document_id: 5, doc_type: 'lab_pdf', uploaded_at: '2026-09-20 10:00:00', status: 'extracted', error_code: null, identity_check: 'match', pending_count: pending }] });
        }
        if (url.endsWith('/documents/5/values')) {
            return json(200, { document_id: 5, status: 'extracted', identity_check: 'match', values: state.values });
        }
        if (url.endsWith('/document-file/5')) {
            return pdfResponse();
        }
        const m = url.match(/\/documents\/5\/values\/(\d+)\/(file|reject|unfile)$/);
        if (m) {
            const body = init.body ? JSON.parse(init.body) : null;
            state.posts.push({ action: m[2], index: Number(m[1]), body: body, headers: init.headers });
            return state.respond(m[2], Number(m[1]), body);
        }
        throw new Error('unexpected ' + url);
    });
    return state;
}

async function openPanel(state) {
    Element.prototype.scrollIntoView = jest.fn();
    HTMLCanvasElement.prototype.getContext = jest.fn(() => ({}));
    const c = document.createElement('div');
    c.dataset.pid = '7';
    c.dataset.csrf = 'csrf-token';
    c.dataset.ticketUrl = '/openemr/apis/default/api/copilot/briefing-ticket';
    document.body.appendChild(c);
    const section = new DocumentsSection(c, { loadPdfjs: async () => fakePdfjs() });
    await section.start();
    return { c: c, section: section, state: state };
}

async function review(c, index) {
    c.querySelector('[data-role="value-row"][data-result-index="' + index + '"] [data-action="review"]').click();
    await settle();
    return c.querySelector('[data-role="value-panel"]');
}

afterEach(() => {
    document.body.innerHTML = '';
    delete global.fetch;
});

describe('Verify and file - in the viewer, beside the outlined value', () => {
    test('files a verified value, shows Filed ✓ with the duplicate warning, and offers Un-file', async () => {
        const state = server([value()]);
        state.respond = (action, index) => {
            state.values[index] = Object.assign({}, state.values[index], { status: 'filed', procedure_result_id: 26 });
            return json(200, { status: 'filed', already_filed: false, warning: 'same_result_already_in_chart', procedure_result_id: 26 });
        };
        const { c } = await openPanel(state);
        const panel = await review(c, 0);
        expect(c.querySelector('[data-role="bbox-overlay"]')).not.toBeNull();
        panel.querySelector('[data-action="file"]').click();
        await settle();

        expect(state.posts).toHaveLength(1);
        expect(state.posts[0].headers.APICSRFTOKEN).toBe('csrf-token');
        expect(state.posts[0].body).toEqual({ filed_value: null, confirm_unverified: false });
        const after = c.querySelector('[data-role="value-panel"]');
        expect(after.textContent).toContain('Filed ✓');
        expect(after.textContent).toMatch(/already in the chart/);
        expect(after.querySelector('[data-action="file"]')).toBeNull();
        expect(after.querySelector('[data-action="unfile"]')).not.toBeNull();
        const row = c.querySelector('[data-role="value-row"][data-result-index="0"]');
        expect(row.textContent).toContain('Filed ✓');
    });

    test('unverified: the button stays disabled until the clinician confirms checking it', async () => {
        const state = server([value({ verification_status: 'unverified', bbox: null, page: null })]);
        state.respond = () => json(200, { status: 'filed', already_filed: false, warning: null, procedure_result_id: 27 });
        const { c } = await openPanel(state);
        const panel = await review(c, 0);
        expect(c.querySelector('[data-role="viewer-notice"]').textContent).toBe('Could not locate this value on the page (unverified)');
        const file = panel.querySelector('[data-action="file"]');
        expect(file.disabled).toBe(true);
        const confirm = panel.querySelector('[data-role="confirm-unverified"]');
        confirm.checked = true;
        confirm.dispatchEvent(new Event('change'));
        expect(file.disabled).toBe(false);
        file.click();
        await settle();
        expect(state.posts[0].body.confirm_unverified).toBe(true);
    });

    test('unreadable: a typed value is required and sent', async () => {
        const state = server([value({ verification_status: 'unreadable', value_text: null, bbox: null, page: null })]);
        state.respond = () => json(200, { status: 'filed', already_filed: false, warning: null, procedure_result_id: 28 });
        const { c } = await openPanel(state);
        const panel = await review(c, 0);
        const input = panel.querySelector('[data-role="filed-value"]');
        expect(input.value).toBe('');
        panel.querySelector('[data-action="file"]').click();
        await settle();
        expect(state.posts).toHaveLength(0);
        expect(panel.querySelector('[data-role="filing-message"]').textContent).toMatch(/Type the value/);
        input.value = '7.9';
        panel.querySelector('[data-action="file"]').click();
        await settle();
        expect(state.posts[0].body.filed_value).toBe('7.9');
    });

    test('missing date: a verified collection date is asked for, never guessed', async () => {
        const state = server([value({ collection_date: null })]);
        state.respond = () => json(200, { status: 'filed', already_filed: false, warning: null, procedure_result_id: 29 });
        const { c } = await openPanel(state);
        const panel = await review(c, 0);
        const date = panel.querySelector('[data-role="collection-date"]');
        expect(date.hidden).toBe(false);
        expect(date.value).toBe('');
        panel.querySelector('[data-action="file"]').click();
        await settle();
        expect(state.posts).toHaveLength(0);
        date.value = '2026-08-30';
        panel.querySelector('[data-action="file"]').click();
        await settle();
        expect(state.posts[0].body.collection_date).toBe('2026-08-30');
    });

    test('date conflict: both dates side by side, then confirmation and a reason, then filed', async () => {
        const state = server([value()]);
        state.respond = (action, index, body) => {
            if (!body.confirm_date_override) {
                return json(409, { detail: { code: 'collection_date_conflict', message: 'The document states a different collection date; confirm the correction and give a reason to file this date.', extracted_collection_date: '2026-09-01', entered_collection_date: body.collection_date } });
            }
            state.values[index] = Object.assign({}, state.values[index], { status: 'filed', procedure_result_id: 30 });
            return json(200, { status: 'filed', already_filed: false, warning: null, procedure_result_id: 30 });
        };
        const { c } = await openPanel(state);
        const panel = await review(c, 0);
        panel.querySelector('[data-action="correct-date"]').click();
        const date = panel.querySelector('[data-role="collection-date"]');
        expect(date.hidden).toBe(false);
        date.value = '2026-08-30';
        panel.querySelector('[data-action="file"]').click();
        await settle();

        const conflict = panel.querySelector('[data-role="date-conflict"]');
        expect(conflict.hidden).toBe(false);
        expect(conflict.textContent).toContain('2026-09-01');
        expect(conflict.textContent).toContain('2026-08-30');
        const file = panel.querySelector('[data-action="file"]');
        expect(file.disabled).toBe(true);
        const confirm = conflict.querySelector('[data-role="confirm-date"]');
        const reason = conflict.querySelector('[data-role="override-reason"]');
        confirm.checked = true;
        confirm.dispatchEvent(new Event('change'));
        expect(file.disabled).toBe(true);
        reason.value = 'Report header shows 30 Aug; the extraction read the print date';
        reason.dispatchEvent(new Event('input'));
        expect(file.disabled).toBe(false);
        file.click();
        await settle();

        expect(state.posts).toHaveLength(2);
        expect(state.posts[1].body).toEqual(expect.objectContaining({ collection_date: '2026-08-30', confirm_date_override: true, override_reason: 'Report header shows 30 Aug; the extraction read the print date' }));
        expect(c.querySelector('[data-role="value-panel"]').textContent).toContain('Filed ✓');
    });

    test('already filed (second click elsewhere) reads as filed', async () => {
        const state = server([value()]);
        state.respond = (action, index) => {
            state.values[index] = Object.assign({}, state.values[index], { status: 'filed', procedure_result_id: 26 });
            return json(200, { status: 'filed', already_filed: true, warning: null, procedure_result_id: 26 });
        };
        const { c } = await openPanel(state);
        const panel = await review(c, 0);
        panel.querySelector('[data-action="file"]').click();
        await settle();
        const after = c.querySelector('[data-role="value-panel"]');
        expect(after.textContent).toContain('Filed ✓');
        expect(after.textContent).toMatch(/already filed/);
    });

    test('a value rejected meanwhile: the server’s refusal is shown and the list refreshed', async () => {
        const state = server([value()]);
        state.respond = (action, index) => {
            state.values[index] = Object.assign({}, state.values[index], { status: 'rejected' });
            return json(409, { detail: { code: 'value_rejected', message: 'This value was rejected and cannot be filed.' } });
        };
        const { c } = await openPanel(state);
        const panel = await review(c, 0);
        panel.querySelector('[data-action="file"]').click();
        await settle();
        const after = c.querySelector('[data-role="value-panel"]');
        expect(after.textContent).toContain('This value was rejected and cannot be filed.');
        expect(after.textContent).toContain('Rejected');
        expect(after.querySelector('[data-action="file"]')).toBeNull();
    });
});

describe('Reject and Un-file - two-step', () => {
    test('reject asks once more, then rejects', async () => {
        const state = server([value()]);
        state.respond = (action, index) => {
            state.values[index] = Object.assign({}, state.values[index], { status: 'rejected' });
            return json(200, { status: 'rejected', already_rejected: false });
        };
        const { c } = await openPanel(state);
        const panel = await review(c, 0);
        const reject = panel.querySelector('[data-action="reject"]');
        reject.click();
        await settle();
        expect(state.posts).toHaveLength(0);
        expect(reject.textContent).toMatch(/Confirm reject/);
        reject.click();
        await settle();
        expect(state.posts.map((p) => p.action)).toEqual(['reject']);
        expect(c.querySelector('[data-role="value-panel"]').textContent).toContain('Rejected');
    });

    test('un-file from the list asks once more, then marks it entered in error', async () => {
        const state = server([value({ status: 'filed', procedure_result_id: 26 })]);
        state.respond = (action, index) => {
            state.values[index] = Object.assign({}, state.values[index], { status: 'unfiled' });
            return json(200, { status: 'unfiled', already_unfiled: false, result_status: 'entered-in-error' });
        };
        const { c } = await openPanel(state);
        const unfile = c.querySelector('[data-role="value-row"] [data-action="unfile"]');
        unfile.click();
        await settle();
        expect(state.posts).toHaveLength(0);
        c.querySelector('[data-role="value-row"] [data-action="unfile"]').click();
        await settle();
        expect(state.posts.map((p) => p.action)).toEqual(['unfile']);
        expect(c.querySelector('[data-role="value-row"]').textContent).toContain('Un-filed (entered in error)');
    });
});

describe('Filed chart results link back to their source', () => {
    test('a Week 1 result row filed from a document gets a source link through the result-source route', async () => {
        const state = server([value({ status: 'filed', procedure_result_id: 26 })]);
        const { c, section } = await openPanel(state);
        const week1 = document.createElement('ul');
        const li = document.createElement('li');
        li.dataset.recordId = 'procedure_result:26';
        li.appendChild(document.createElement('strong'));
        const other = document.createElement('li');
        other.dataset.recordId = 'procedure_result:99';
        other.appendChild(document.createElement('strong'));
        week1.appendChild(li);
        week1.appendChild(other);
        c.insertBefore(week1, c.firstChild);
        const base = global.fetch;
        global.fetch = jest.fn(async (url, init) => (url.endsWith('/results/26/source')
            ? json(200, { procedure_result_id: 26, document_id: 5, result_index: 0, page: 1, bbox: [0.1, 0.2, 0.3, 0.25], status: 'filed' })
            : base(url, init)));
        section.annotateFiledResults();
        section.annotateFiledResults();
        const links = c.querySelectorAll('[data-action="result-source"]');
        expect(links).toHaveLength(1);
        expect(other.querySelector('[data-action="result-source"]')).toBeNull();
        links[0].click();
        await settle();
        expect(global.fetch.mock.calls.some((call) => call[0] === '/openemr/apis/default/api/copilot/results/26/source')).toBe(true);
        expect(c.querySelector('[data-role="bbox-overlay"]')).not.toBeNull();
    });
});
