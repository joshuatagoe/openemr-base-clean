/**
 * @jest-environment jsdom
 */

/**
 * Clinical Co-Pilot source viewer in a popup (ADR-008): "View source" and
 * "View document" open the viewer in one modal dialog over the page.
 *
 * Run with: npx jest tests/js/copilot-popup.test.js
 *
 * @package   OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

const lib = require('../../interface/modules/custom_modules/oe-module-copilot/public/copilot-panel.js');

const { DocumentsSection, overlayRect, pdfFrame } = lib;

function json(status, body) {
    return { ok: status >= 200 && status < 300, status: status, headers: { get: () => 'application/json' }, json: async () => body };
}

function fileResponse() {
    return {
        ok: true,
        status: 200,
        headers: { get: (name) => (name.toLowerCase() === 'content-type' ? 'application/pdf' : null) },
        arrayBuffer: async () => new Uint8Array([1, 2, 3]).buffer
    };
}

function fakePdfjs() {
    const lastViewport = {};
    return {
        lastViewport: lastViewport,
        lib: {
            GlobalWorkerOptions: {},
            getDocument: () => ({
                promise: Promise.resolve({
                    numPages: 2,
                    getPage: async () => ({
                        rotate: 0,
                        getViewport: ({ scale }) => {
                            const vp = { width: 612 * scale, height: 792 * scale, rotation: 0, scale: scale };
                            Object.assign(lastViewport, vp);
                            return vp;
                        },
                        render: () => ({ promise: Promise.resolve(), cancel: () => undefined })
                    }),
                    destroy: () => undefined
                }),
                destroy: () => undefined
            })
        }
    };
}

const BBOX = [0.1, 0.2, 0.3, 0.25];
const LAB = { document_id: 5, doc_type: 'lab_pdf', uploaded_at: '2026-09-20 10:00:00', status: 'extracted', error_code: null, identity_check: 'match', pending_count: 1 };
const HELD = { document_id: 9, doc_type: 'lab_pdf', uploaded_at: '2026-09-21 10:00:00', status: 'held_identity', error_code: null, identity_check: 'mismatch', pending_count: 0 };

function install() {
    global.fetch = jest.fn(async (url) => {
        if (url.endsWith('/documents/process')) {
            return json(200, { status: 'ok', processed: [], remaining: 0, documents: [LAB, HELD] });
        }
        if (url.endsWith('/documents/5/values')) {
            return json(200, { document_id: 5, status: 'extracted', identity_check: 'match', values: [
                { result_index: 0, test_name: 'Hemoglobin A1c', value_text: '8.4', unit: '%', reference_range: '4.0-5.6', abnormal_flag: 'H', flag_source: 'extracted', collection_date: '2026-09-01', verification_status: 'verified_exact', page: 2, bbox: BBOX, status: 'candidate', procedure_result_id: null }
            ] });
        }
        if (/\/document-file\/\d+$/.test(url)) {
            return fileResponse();
        }
        throw new Error('unexpected ' + url);
    });
}

async function flush() {
    for (let i = 0; i < 30; i += 1) {
        await Promise.resolve();
    }
    await new Promise((r) => setTimeout(r, 0));
}

let c;
let pdf;

async function section() {
    install();
    c = document.createElement('div');
    c.dataset.pid = '7';
    c.dataset.csrf = 'csrf-token';
    c.dataset.ticketUrl = '/openemr/apis/default/api/copilot/briefing-ticket';
    document.body.appendChild(c);
    pdf = fakePdfjs();
    const s = new DocumentsSection(c, { loadPdfjs: async () => pdf.lib });
    await s.start();
    return s;
}

function reviewButton() {
    return c.querySelector('[data-role="value-row"] [data-action="review"]');
}

function dialogs() {
    return document.querySelectorAll('[role="dialog"]');
}

beforeEach(() => {
    global.URL.createObjectURL = jest.fn(() => 'blob:http://openemr/1');
    global.URL.revokeObjectURL = jest.fn();
    HTMLCanvasElement.prototype.getContext = jest.fn(() => ({}));
    Element.prototype.scrollIntoView = jest.fn();
    document.body.style.overflow = '';
});

afterEach(() => {
    document.body.innerHTML = '';
    delete global.fetch;
});

describe('Source viewer popup', () => {
    test('nothing is shown inline and no dialog exists before a view is asked for', async () => {
        await section();
        expect(dialogs()).toHaveLength(0);
        expect(c.querySelector('[data-role="viewer"]')).toBeNull();
    });

    test('View source opens one labelled modal dialog, focuses it, locks the page scroll, and draws the page inside it', async () => {
        await section();
        const button = reviewButton();
        button.focus();
        button.click();
        await flush();

        const list = dialogs();
        expect(list).toHaveLength(1);
        const dialog = list[0];
        expect(dialog.getAttribute('aria-modal')).toBe('true');
        const title = document.getElementById(dialog.getAttribute('aria-labelledby'));
        expect(title).not.toBeNull();
        expect(title.textContent).toContain('Lab report');
        expect(title.textContent).toContain('uploaded');
        expect(dialog.contains(document.activeElement)).toBe(true);
        expect(document.body.style.overflow).toBe('hidden');
        expect(dialog.querySelector('canvas')).not.toBeNull();
        expect(dialog.querySelector('[data-role="bbox-overlay"]')).not.toBeNull();
        expect(dialog.querySelector('[data-role="value-panel"]').textContent).toContain('Hemoglobin A1c');
        // The value and its filing controls sit beside the page: the side column starts at the
        // 240px stageWidth() reserves room for, rather than at its content's width (which wraps it below).
        expect(dialog.querySelector('[data-role="value-panel"]').parentNode.style.flexBasis).toBe('240px');
        expect(dialog.style.maxHeight).toBe('90vh');
        expect(dialog.style.maxWidth).toBe('90vw');
        expect(c.contains(dialog)).toBe(false);
    });

    test('the close button closes it, unlocks the scroll and returns focus to the opening button', async () => {
        await section();
        const button = reviewButton();
        button.focus();
        button.click();
        await flush();
        dialogs()[0].querySelector('[data-action="close-viewer"]').click();
        expect(dialogs()).toHaveLength(0);
        expect(document.body.style.overflow).toBe('');
        expect(document.activeElement).toBe(button);
        expect(URL.revokeObjectURL).not.toHaveBeenCalled(); // PDFs draw from bytes, not a blob URL
    });

    test('Esc closes it and returns focus', async () => {
        await section();
        const button = reviewButton();
        button.click();
        await flush();
        document.activeElement.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
        expect(dialogs()).toHaveLength(0);
        expect(document.activeElement).toBe(button);
    });

    test('a click on the backdrop closes it; a click inside the dialog does not', async () => {
        await section();
        reviewButton().click();
        await flush();
        const dialog = dialogs()[0];
        dialog.click();
        expect(dialogs()).toHaveLength(1);
        dialog.parentNode.click();
        expect(dialogs()).toHaveLength(0);
    });

    test('only one dialog at a time: opening another view reuses it', async () => {
        const s = await section();
        reviewButton().click();
        await flush();
        await s.openSource(9, null, null);
        await flush();
        expect(dialogs()).toHaveLength(1);
        expect(document.querySelectorAll('[data-role="viewer"]')).toHaveLength(1);
    });

    test('Tab stays inside the dialog', async () => {
        await section();
        reviewButton().click();
        await flush();
        const dialog = dialogs()[0];
        const focusable = Array.from(dialog.querySelectorAll('button:not([disabled]):not([hidden]), a[href]'));
        focusable[focusable.length - 1].focus();
        const e = new KeyboardEvent('keydown', { key: 'Tab', bubbles: true, cancelable: true });
        document.activeElement.dispatchEvent(e);
        expect(e.defaultPrevented).toBe(true);
        expect(document.activeElement).toBe(focusable[0]);
    });

    test('the box lands where the pure overlay functions put it for the canvas drawn in the popup', async () => {
        const s = await section();
        Object.defineProperty(s.viewerHost, 'clientWidth', { configurable: true, value: 1100 });
        reviewButton().click();
        await flush();
        const dialog = dialogs()[0];
        const canvas = dialog.querySelector('canvas');
        const width = parseFloat(canvas.style.width);
        const height = parseFloat(canvas.style.height);
        const expected = overlayRect(BBOX, pdfFrame(pdf.lastViewport, 0));
        const box = dialog.querySelector('[data-role="bbox-overlay"]');
        expect(parseFloat(box.style.left)).toBeCloseTo(expected.left, 3);
        expect(parseFloat(box.style.top)).toBeCloseTo(expected.top, 3);
        expect(parseFloat(box.style.width)).toBeCloseTo(expected.width, 3);
        expect(parseFloat(box.style.height)).toBeCloseTo(expected.height, 3);
        expect(parseFloat(box.style.left)).toBeCloseTo(0.1 * width, 3);
        expect(parseFloat(box.style.top)).toBeCloseTo(0.2 * height, 3);
        // positioned against the page stage, not the dialog
        expect(box.parentNode.dataset.role).toBe('viewer-stage');
        expect(box.parentNode.style.position).toBe('relative');
    });

    test('viewing a held document in the popup enables "This is the right patient" after the popup closes', async () => {
        await section();
        const held = c.querySelector('[data-role="document-item"][data-document-id="9"]');
        const confirm = held.querySelector('[data-action="confirm-patient"]');
        expect(confirm.disabled).toBe(true);
        const view = held.querySelector('[data-action="view-document"]');
        view.click();
        await flush();
        expect(dialogs()).toHaveLength(1);
        document.activeElement.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
        expect(dialogs()).toHaveLength(0);
        expect(document.activeElement).toBe(view);
        expect(confirm.disabled).toBe(false);
    });
});
