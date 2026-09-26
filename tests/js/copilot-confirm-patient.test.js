/**
 * @jest-environment jsdom
 */

/**
 * Clinical Co-Pilot panel - "This is the right patient" on a held document (ADR-012 §4a).
 *
 * Run with: npx jest tests/js/copilot-confirm-patient.test.js
 *
 * @package   OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

const lib = require('../../interface/modules/custom_modules/oe-module-copilot/public/copilot-panel.js');

const { describeDocument, classifyConfirmResponse, DocumentsSection } = lib;

const TICKET_URL = '/openemr/apis/default/api/copilot/briefing-ticket';
const BASE = '/openemr/apis/default/api/copilot';

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

const HELD = doc(9, { status: 'held_identity', identity_check: 'mismatch', pending_count: 0 });
const CONFIRMED = doc(9, { status: 'extracted', identity_check: 'mismatch', error_code: 'identity_confirmed_by_clinician', pending_count: 2 });

async function flush() {
    for (let i = 0; i < 30; i += 1) {
        await Promise.resolve();
    }
}

afterEach(() => {
    document.body.innerHTML = '';
    delete global.fetch;
});

describe('describeDocument - held and confirmed documents', () => {
    test('a held document can be confirmed and shows no values', () => {
        const d = describeDocument(HELD);
        expect(d.canConfirm).toBe(true);
        expect(d.showValues).toBe(false);
        expect(d.notes.join(' ')).toMatch(/does not match this chart/);
    });

    test('a held copy of a file in another chart does not claim a name mismatch it did not find', () => {
        const d = describeDocument(doc(9, { status: 'held_identity', identity_check: 'missing', error_code: 'same_file_in_other_chart', pending_count: 0 }));
        expect(d.canConfirm).toBe(true);
        const text = d.notes.join(' ');
        expect(text).not.toMatch(/does not match this chart/);
        expect(text).toMatch(/may be misfiled/);
    });

    test('a confirmed document is read, its values shown, and the confirmation stated truthfully', () => {
        const d = describeDocument(CONFIRMED);
        expect(d.canConfirm).toBe(false);
        expect(d.showValues).toBe(true);
        expect(d.statusLabel).toBe('Read');
        const text = d.notes.join(' ');
        expect(text).toMatch(/clinician confirmed this is the right patient/);
        expect(text).toMatch(/did not match/);
        expect(text).not.toMatch(/Reason code/);
        expect(text).toMatch(/2 values waiting for review/);
    });

    test('other documents cannot be confirmed', () => {
        expect(describeDocument(doc(5)).canConfirm).toBe(false);
        expect(describeDocument(doc(5, { status: 'failed', error_code: 'agent_timeout' })).canConfirm).toBe(false);
    });
});

describe('classifyConfirmResponse', () => {
    test('confirmed', () => {
        expect(classifyConfirmResponse(200, { status: 'extracted', pending_count: 2 }).kind).toBe('confirmed');
    });

    test('no longer held or gone: refresh the list', () => {
        expect(classifyConfirmResponse(409, { detail: { code: 'not_held', message: 'm' } }).kind).toBe('closed');
        expect(classifyConfirmResponse(404, { detail: { code: 'document_not_found', message: 'm' } }).kind).toBe('closed');
    });

    test('permission refused: say why', () => {
        const r = classifyConfirmResponse(403, { detail: { code: 'confirm_not_permitted', message: 'Confirming needs lab write and sign permissions.' } });
        expect(r.kind).toBe('error');
        expect(r.message).toMatch(/lab write and sign/);
        expect(r.message).toMatch(/confirm_not_permitted/);
        expect(classifyConfirmResponse(0, null).kind).toBe('error');
    });
});

function stubFetch(onConfirm) {
    const calls = [];
    let confirmed = false;
    global.fetch = jest.fn(async (url, init) => {
        calls.push({ url: url, init: init });
        if (url.endsWith('/documents/process')) {
            return jsonResponse(200, { status: 'ok', processed: [], remaining: 0, documents: [HELD] });
        }
        if (url.endsWith('/confirm-patient')) {
            const r = onConfirm();
            confirmed = r.status === 200;
            return jsonResponse(r.status, r.body);
        }
        if (url.startsWith(BASE + '/documents?pid=')) {
            return jsonResponse(200, { status: 'ok', documents: [confirmed ? CONFIRMED : HELD] });
        }
        if (/\/documents\/\d+\/values$/.test(url)) {
            return jsonResponse(200, { document_id: 9, status: 'extracted', identity_check: 'mismatch', values: [] });
        }
        throw new Error('unexpected ' + url);
    });
    return calls;
}

async function heldSection(onConfirm) {
    const calls = stubFetch(onConfirm);
    const section = new DocumentsSection(container());
    section.viewer.open = jest.fn(async () => undefined);
    await section.start();
    await flush();
    return { section: section, calls: calls };
}

function item() {
    return document.querySelector('[data-role="document-item"][data-document-id="9"]');
}

describe('DocumentsSection - This is the right patient', () => {
    test('after the explanation: View document, then the confirm button, enabled only once the document was viewed', async () => {
        const { section, calls } = await heldSection(() => ({ status: 200, body: {} }));
        const it = item();
        const view = it.querySelector('[data-role="held-actions"] [data-action="view-document"]');
        const confirm = it.querySelector('[data-role="held-actions"] [data-action="confirm-patient"]');
        expect(view).not.toBeNull();
        expect(confirm).not.toBeNull();
        expect(confirm.textContent).toBe('This is the right patient');
        expect(it.querySelector('[data-role="held-actions"]').previousElementSibling.textContent).toMatch(/does not match this chart/);
        expect(view.compareDocumentPosition(confirm) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
        expect(confirm.disabled).toBe(true);
        expect(confirm.title).toMatch(/lab-write and sign/);
        expect(calls.filter((c) => /\/values$/.test(c.url))).toHaveLength(0);

        view.click();
        await flush();
        expect(section.viewer.open).toHaveBeenCalledWith(expect.objectContaining({ documentId: 9 }));
        expect(item().querySelector('[data-action="confirm-patient"]').disabled).toBe(false);
    });

    test('an explicit confirmation step, then the POST, then the list refreshes with the values', async () => {
        const { calls } = await heldSection(() => ({ status: 200, body: { document_id: 9, status: 'extracted', identity_check: 'mismatch', error_code: 'identity_confirmed_by_clinician', pending_count: 2 } }));
        item().querySelector('[data-role="held-actions"] [data-action="view-document"]').click();
        await flush();
        const confirm = item().querySelector('[data-action="confirm-patient"]');

        confirm.click();
        await flush();
        expect(calls.filter((c) => c.url.endsWith('/confirm-patient'))).toHaveLength(0);
        expect(confirm.textContent).toBe('Confirm: this is the right patient');
        const message = item().querySelector('[data-role="held-message"]');
        expect(message.textContent).toMatch(/move it to the right chart in Documents instead/);
        expect(message.textContent).toMatch(/audit/);

        confirm.click();
        await flush();
        const posts = calls.filter((c) => c.url.endsWith('/confirm-patient'));
        expect(posts).toHaveLength(1);
        expect(posts[0].url).toBe(BASE + '/documents/9/confirm-patient');
        expect(posts[0].init.method).toBe('POST');
        expect(posts[0].init.headers.APICSRFTOKEN).toBe('csrf-token');
        expect(JSON.parse(posts[0].init.body)).toEqual({ confirm: true });

        expect(calls.some((c) => c.url === BASE + '/documents?pid=7')).toBe(true);
        expect(calls.some((c) => c.url === BASE + '/documents/9/values')).toBe(true);
        const it = item();
        expect(it.textContent).toMatch(/Read/);
        expect(it.textContent).toMatch(/clinician confirmed this is the right patient/);
        expect(it.querySelector('[data-action="confirm-patient"]')).toBeNull();
    });

    test('cancel disarms the confirmation', async () => {
        const { calls } = await heldSection(() => ({ status: 200, body: {} }));
        item().querySelector('[data-role="held-actions"] [data-action="view-document"]').click();
        await flush();
        const confirm = item().querySelector('[data-action="confirm-patient"]');
        confirm.click();
        await flush();
        const cancel = item().querySelector('[data-action="cancel-confirm-patient"]');
        expect(cancel.hidden).toBe(false);
        cancel.click();
        await flush();
        expect(confirm.textContent).toBe('This is the right patient');
        expect(cancel.hidden).toBe(true);
        confirm.click();
        await flush();
        expect(calls.filter((c) => c.url.endsWith('/confirm-patient'))).toHaveLength(0);
    });

    test('a refusal is stated and nothing is refreshed', async () => {
        const { calls } = await heldSection(() => ({ status: 403, body: { detail: { code: 'confirm_not_permitted', message: 'Confirming the patient of a held document requires lab write and sign permissions.' } } }));
        item().querySelector('[data-role="held-actions"] [data-action="view-document"]').click();
        await flush();
        const confirm = item().querySelector('[data-action="confirm-patient"]');
        confirm.click();
        await flush();
        confirm.click();
        await flush();
        expect(item().querySelector('[data-role="held-message"]').textContent).toMatch(/confirm_not_permitted/);
        expect(calls.some((c) => c.url.startsWith(BASE + '/documents?pid='))).toBe(false);
        expect(item().querySelector('[data-action="confirm-patient"]').disabled).toBe(false);
    });

    test('no longer held (another click or user got there first): the list refreshes', async () => {
        const { calls } = await heldSection(() => ({ status: 409, body: { detail: { code: 'not_held', message: 'This document is not held for an identity check.' } } }));
        item().querySelector('[data-role="held-actions"] [data-action="view-document"]').click();
        await flush();
        const confirm = item().querySelector('[data-action="confirm-patient"]');
        confirm.click();
        await flush();
        confirm.click();
        await flush();
        expect(calls.some((c) => c.url === BASE + '/documents?pid=7')).toBe(true);
    });
});
