/**
 * @jest-environment jsdom
 */

/**
 * Clinical Co-Pilot panel - intake forms (ADR-010): patient-reported evidence
 * with its source box, never Verify and file or Reject.
 *
 * @package   OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

const lib = require('../../interface/modules/custom_modules/oe-module-copilot/public/copilot-panel.js');

const { describeDocument, DocumentsSection } = lib;

function json(status, body) {
    return { ok: status >= 200 && status < 300, status: status, headers: { get: () => 'application/json' }, json: async () => body };
}

function intakeDoc(over) {
    return Object.assign({ document_id: 8, doc_type: 'intake_form', uploaded_at: '2026-09-20 10:00:00', status: 'extracted', error_code: null, identity_check: 'match', pending_count: 3 }, over || {});
}

const ITEMS = [
    { result_index: 0, test_name: 'Chief concern', value_text: 'Tired and thirsty', unit: null, reference_range: null, abnormal_flag: null, flag_source: null, collection_date: null, verification_status: 'verified_exact', page: 1, bbox: [0.1, 0.2, 0.5, 0.23], status: 'candidate', procedure_result_id: null },
    { result_index: 1, test_name: 'Current medication', value_text: '<b>Metformin</b> 500 mg twice daily', unit: null, reference_range: null, abnormal_flag: null, flag_source: null, collection_date: null, verification_status: 'unverified', page: null, bbox: null, status: 'candidate', procedure_result_id: null },
    { result_index: 2, test_name: 'Allergy', value_text: null, unit: null, reference_range: null, abnormal_flag: null, flag_source: null, collection_date: null, verification_status: 'unreadable', page: null, bbox: null, status: 'candidate', procedure_result_id: null }
];

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

async function openPanel(docs) {
    const calls = [];
    global.fetch = jest.fn(async (url, init) => {
        calls.push({ url: url, init: init });
        if (url.endsWith('/documents/process') || /\/documents\?pid=/.test(url)) {
            return json(200, { status: 'ok', processed: [], remaining: 0, documents: docs });
        }
        if (/\/documents\/8\/values$/.test(url)) {
            return json(200, { document_id: 8, status: 'extracted', identity_check: 'match', values: ITEMS });
        }
        if (/\/documents\/5\/values$/.test(url)) {
            return json(200, { document_id: 5, status: 'extracted', identity_check: 'match', values: [] });
        }
        if (url.endsWith('/document-file/8')) {
            return { ok: true, status: 200, headers: { get: () => 'application/pdf' }, arrayBuffer: async () => new Uint8Array([1]).buffer };
        }
        throw new Error('unexpected ' + url);
    });
    Element.prototype.scrollIntoView = jest.fn();
    HTMLCanvasElement.prototype.getContext = jest.fn(() => ({}));
    const c = document.createElement('div');
    c.dataset.pid = '7';
    c.dataset.csrf = 'csrf-token';
    c.dataset.ticketUrl = '/openemr/apis/default/api/copilot/briefing-ticket';
    document.body.appendChild(c);
    const section = new DocumentsSection(c, { loadPdfjs: async () => fakePdfjs() });
    await section.start();
    await settle();
    return { c: c, section: section, calls: calls };
}

afterEach(() => {
    document.body.innerHTML = '';
    delete global.fetch;
});

describe('describeDocument - intake forms', () => {
    test('a read intake form lists patient-reported items, not values waiting to be filed', () => {
        const d = describeDocument(intakeDoc());
        expect(d.typeLabel).toBe('Intake form');
        expect(d.intake).toBe(true);
        expect(d.showValues).toBe(true);
        const text = d.notes.join(' ');
        expect(text).toMatch(/3 patient-reported items/);
        expect(text).toMatch(/not filed/);
        expect(text).not.toMatch(/waiting for review/);
    });

    test('a missing identity check on a form is noted without mentioning filing', () => {
        const text = describeDocument(intakeDoc({ identity_check: 'missing' })).notes.join(' ');
        expect(text).toMatch(/could not be compared with this chart/);
        expect(text).not.toMatch(/before filing/);
    });

    test('a lab report is unchanged', () => {
        expect(describeDocument(intakeDoc({ doc_type: 'lab_pdf' })).notes.join(' ')).toMatch(/3 values waiting for review/);
    });
});

describe('DocumentsSection - intake items', () => {
    test('items are listed as patient-reported with View source, and no review, file or reject control', async () => {
        const { c } = await openPanel([intakeDoc(), intakeDoc({ document_id: 5, doc_type: 'lab_pdf', pending_count: 2 })]);
        const rows = c.querySelectorAll('[data-role="document-item"][data-document-id="8"] [data-role="value-row"]');
        expect(rows).toHaveLength(3);
        rows.forEach((row) => {
            expect(row.dataset.kind).toBe('intake-item');
            expect(row.textContent).toContain('Patient-reported');
            expect(row.querySelector('[data-action="view"]')).not.toBeNull();
            expect(row.querySelector('[data-action="review"]')).toBeNull();
            expect(row.textContent).not.toContain('Waiting for review');
        });
        expect(rows[0].textContent).toContain('Chief concern:');
        expect(rows[0].textContent).toContain('Found on the page');
        expect(rows[1].textContent).toContain('<b>Metformin</b> 500 mg twice daily'); // text, never markup
        expect(c.querySelector('[data-role="documents"] b')).toBeNull();
        expect(rows[1].textContent).toContain('Not found on the page');
        expect(rows[2].textContent).toContain('unreadable');
        const item = c.querySelector('[data-role="document-item"][data-document-id="8"]');
        expect(item.textContent).toContain('3 patient-reported');
        expect(item.textContent).not.toContain('to review');
        expect(item.textContent).toContain('not filed into the chart this week');
    });

    test('the summary counts intake items apart from values waiting for review', async () => {
        const { section } = await openPanel([intakeDoc(), intakeDoc({ document_id: 5, doc_type: 'lab_pdf', pending_count: 2 })]);
        expect(section.summary()).toBe('2 document(s) on file; 2 value(s) waiting for review; 3 patient-reported intake item(s), not filed.');
    });

    test('View source opens the form at the item’s box, beside it the reason it is not filed, and no filing controls', async () => {
        const { c } = await openPanel([intakeDoc()]);
        c.querySelector('[data-role="value-row"][data-result-index="0"] [data-action="view"]').click();
        await settle();
        const panel = document.querySelector('[data-role="value-panel"]');
        expect(panel).not.toBeNull();
        expect(panel.textContent).toContain('As written: Tired and thirsty');
        expect(panel.textContent).toContain('Patient-reported');
        expect(panel.querySelector('[data-role="intake-not-filed"]').textContent).toMatch(/No Verify and file or Reject/);
        expect(panel.querySelector('[data-role="filing-form"]')).toBeNull();
        const labels = Array.from(panel.querySelectorAll('button')).map((b) => b.textContent);
        expect(labels).not.toContain('Verify and file');
        expect(labels).not.toContain('Reject');
        expect(document.querySelector('[data-role="bbox-overlay"]')).not.toBeNull();
    });

    test('hover text explains patient-reported and why it is not filed', async () => {
        const { c } = await openPanel([intakeDoc()]);
        const row = c.querySelector('[data-role="value-row"][data-result-index="1"]');
        const titles = Array.from(row.querySelectorAll('[title]')).map((n) => n.getAttribute('title')).join(' ');
        expect(titles).toMatch(/Patient-reported: an observation/);
        expect(titles).toMatch(/Check the form yourself/);
    });
});
