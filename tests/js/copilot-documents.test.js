/**
 * @jest-environment jsdom
 */

/**
 * Clinical Co-Pilot panel - document list and chart-open processing (ADR-012).
 *
 * Run with: npx jest tests/js/copilot-documents.test.js
 *
 * @package   OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

const lib = require('../../interface/modules/custom_modules/oe-module-copilot/public/copilot-panel.js');

const { describeDocument, DocumentsSection } = lib;

const TICKET_URL = '/openemr/apis/default/api/copilot/briefing-ticket';

function container() {
    const c = document.createElement('div');
    c.id = 'oe-copilot-panel';
    c.dataset.pid = '7';
    c.dataset.csrf = 'csrf-token';
    c.dataset.ticketUrl = TICKET_URL;
    document.body.appendChild(c);
    return c;
}

function jsonResponse(status, body) {
    return {
        ok: status >= 200 && status < 300,
        status: status,
        headers: { get: () => 'application/json' },
        json: async () => body
    };
}

function doc(id, over) {
    return Object.assign({ document_id: id, doc_type: 'lab_pdf', uploaded_at: '2026-09-20 10:00:00', status: 'extracted', error_code: null, identity_check: 'match', pending_count: 2 }, over || {});
}

async function flush() {
    for (let i = 0; i < 20; i += 1) {
        await Promise.resolve();
    }
}

afterEach(() => {
    document.body.innerHTML = '';
    delete global.fetch;
});

describe('describeDocument', () => {
    test('an extracted lab report with pending values', () => {
        const d = describeDocument(doc(5));
        expect(d.typeLabel).toBe('Lab report');
        expect(d.statusLabel).toBe('Read');
        expect(d.notes.join(' ')).toMatch(/2 values waiting for review/);
        expect(d.showValues).toBe(true);
    });

    test('held for identity: plain explanation, no values', () => {
        const d = describeDocument(doc(5, { status: 'held_identity', identity_check: 'mismatch', pending_count: 0 }));
        expect(d.statusLabel).toBe('Held: identity check');
        expect(d.notes.join(' ')).toMatch(/name or date of birth printed on this document does not match this chart/);
        expect(d.showValues).toBe(false);
    });

    test('needs a category', () => {
        const d = describeDocument(doc(5, { doc_type: 'unsupported', status: 'unsupported', error_code: 'needs_category', identity_check: null, pending_count: 0 }));
        expect(d.statusLabel).toBe('Needs a category');
        expect(d.notes.join(' ')).toMatch(/Lab Report/);
        expect(d.showValues).toBe(false);
    });

    test('same file in another chart is noted', () => {
        const d = describeDocument(doc(5, { error_code: 'same_file_also_in_other_chart' }));
        expect(d.notes.join(' ')).toMatch(/also filed in another patient’s chart/);
        const held = describeDocument(doc(6, { status: 'held_identity', error_code: 'same_file_in_other_chart', identity_check: 'missing' }));
        expect(held.notes.join(' ')).toMatch(/may be misfiled/);
    });

    test('failures say what happened and whether it is retried', () => {
        const d = describeDocument(doc(5, { status: 'failed', error_code: 'agent_unreachable', identity_check: null, pending_count: 0 }));
        expect(d.statusLabel).toBe('Could not be read');
        expect(d.notes.join(' ')).toMatch(/tried again/);
    });

    test('an unknown code is shown as a code, never guessed', () => {
        const d = describeDocument(doc(5, { status: 'failed', error_code: 'something_new', identity_check: null }));
        expect(d.notes.join(' ')).toMatch(/something_new/);
    });

    test('identity missing on a read document is noted', () => {
        const d = describeDocument(doc(5, { identity_check: 'missing' }));
        expect(d.notes.join(' ')).toMatch(/could not be compared/);
    });
});

describe('DocumentsSection - chart-open processing', () => {
    test('repeats the processing call while documents remain, then lists them and loads their values', async () => {
        const calls = [];
        global.fetch = jest.fn(async (url, init) => {
            calls.push({ url: url, init: init });
            if (url.endsWith('/documents/process')) {
                return calls.filter((c) => c.url.endsWith('/process')).length === 1
                    ? jsonResponse(200, { status: 'ok', processed: [{ document_id: 5, outcome: 'extracted' }], remaining: 1, documents: [doc(5), doc(6, { status: 'queued', identity_check: null, pending_count: 0 })] })
                    : jsonResponse(200, { status: 'ok', processed: [{ document_id: 6, outcome: 'extracted' }], remaining: 0, documents: [doc(5), doc(6)] });
            }
            if (/\/documents\/\d+\/values$/.test(url)) {
                return jsonResponse(200, { document_id: 5, status: 'extracted', identity_check: 'match', values: [] });
            }
            throw new Error('unexpected ' + url);
        });
        const section = new DocumentsSection(container());
        await section.start();
        await flush();

        const posts = calls.filter((c) => c.url.endsWith('/documents/process'));
        expect(posts).toHaveLength(2);
        expect(posts[0].url).toBe('/openemr/apis/default/api/copilot/documents/process');
        expect(posts[0].init.method).toBe('POST');
        expect(posts[0].init.headers.APICSRFTOKEN).toBe('csrf-token');
        expect(JSON.parse(posts[0].init.body)).toEqual({ pid: 7 });
        const items = document.querySelectorAll('[data-role="document-item"]');
        expect(items).toHaveLength(2);
        expect(items[0].dataset.documentId).toBe('5');
        const valueCalls = calls.filter((c) => /\/values$/.test(c.url));
        expect(valueCalls.map((c) => c.url)).toEqual([
            '/openemr/apis/default/api/copilot/documents/5/values',
            '/openemr/apis/default/api/copilot/documents/6/values'
        ]);
        expect(valueCalls[0].init.headers.APICSRFTOKEN).toBe('csrf-token');
    });

    test('stops when a call processes nothing even if documents remain (no endless retries)', async () => {
        global.fetch = jest.fn(async (url) => {
            if (url.endsWith('/documents/process')) {
                return jsonResponse(200, { status: 'ok', processed: [], remaining: 1, documents: [doc(6, { status: 'processing', identity_check: null, pending_count: 0 })] });
            }
            return jsonResponse(200, { values: [] });
        });
        await new DocumentsSection(container()).start();
        expect(global.fetch.mock.calls.filter((c) => c[0].endsWith('/process'))).toHaveLength(1);
        expect(document.querySelector('[data-role="documents-status"]').textContent).toMatch(/still being read/);
    });

    test('tables not installed and request failures are stated, not hidden', async () => {
        global.fetch = jest.fn(async () => jsonResponse(200, { status: 'degraded', degraded_reason: 'copilot_tables_not_installed', processed: [], remaining: 0, documents: [] }));
        await new DocumentsSection(container()).start();
        expect(document.querySelector('[data-role="documents-status"]').textContent).toMatch(/copilot_tables_not_installed/);

        document.body.innerHTML = '';
        global.fetch = jest.fn(async () => jsonResponse(403, { detail: { code: 'no_care_relationship', message: 'No care relationship with the selected patient was found.' } }));
        await new DocumentsSection(container()).start();
        expect(document.querySelector('[data-role="documents-status"]').textContent).toMatch(/No care relationship/);
    });

    test('no documents on file', async () => {
        global.fetch = jest.fn(async () => jsonResponse(200, { status: 'ok', processed: [], remaining: 0, documents: [] }));
        await new DocumentsSection(container()).start();
        expect(document.querySelector('[data-role="documents"]').textContent).toMatch(/No documents/);
    });

    test('a value list shows each value with its verification and status, as plain text only', async () => {
        global.fetch = jest.fn(async (url) => {
            if (url.endsWith('/process')) {
                return jsonResponse(200, { status: 'ok', processed: [], remaining: 0, documents: [doc(5)] });
            }
            return jsonResponse(200, {
                document_id: 5,
                status: 'extracted',
                identity_check: 'match',
                values: [
                    { result_index: 0, test_name: '<img src=x onerror=alert(1)>A1c', value_text: '8.4', unit: '%', reference_range: '4.0-5.6', abnormal_flag: 'H', flag_source: 'extracted', collection_date: '2026-09-01', verification_status: 'verified_exact', page: 1, bbox: [0.1, 0.2, 0.3, 0.25], status: 'candidate', procedure_result_id: null },
                    { result_index: 1, test_name: 'Glucose', value_text: null, unit: 'mg/dL', reference_range: null, abnormal_flag: null, flag_source: null, collection_date: null, verification_status: 'unreadable', page: null, bbox: null, status: 'candidate', procedure_result_id: null },
                    { result_index: 2, test_name: 'LDL', value_text: '130', unit: 'mg/dL', reference_range: null, abnormal_flag: null, flag_source: 'derived', collection_date: '2026-09-01', verification_status: 'verified_fuzzy', page: 1, bbox: [0.1, 0.3, 0.3, 0.35], status: 'filed', procedure_result_id: 26 },
                    { result_index: 3, test_name: 'HDL', value_text: '40', unit: 'mg/dL', reference_range: null, abnormal_flag: null, flag_source: null, collection_date: '2026-09-01', verification_status: 'unverified', page: null, bbox: null, status: 'rejected', procedure_result_id: null }
                ]
            });
        });
        await new DocumentsSection(container()).start();
        await flush();

        const rows = document.querySelectorAll('[data-role="value-row"]');
        expect(rows).toHaveLength(4);
        expect(document.querySelector('[data-role="documents"] img')).toBeNull();
        expect(rows[0].textContent).toContain('<img src=x onerror=alert(1)>A1c');
        expect(rows[0].textContent).toContain('8.4 %');
        expect(rows[0].textContent).toContain('Found on the page');
        expect(rows[0].textContent).toContain('flag printed on the report: H');
        expect(rows[0].textContent).toContain('Waiting for review');
        expect(rows[1].textContent).toContain('Unreadable');
        expect(rows[1].textContent).toContain('no collection date');
        expect(rows[2].textContent).toContain('Filed ✓');
        expect(rows[2].textContent).not.toContain('flag printed');
        expect(rows[3].textContent).toContain('Rejected');
        expect(rows[3].textContent).toContain('Not found on the page');
        // Candidates open the viewer for review; filed values can be viewed.
        expect(rows[0].querySelector('[data-action="review"]')).not.toBeNull();
        expect(rows[2].querySelector('[data-action="view"]')).not.toBeNull();
        expect(rows[3].querySelector('[data-action="review"]')).toBeNull();
    });
});
