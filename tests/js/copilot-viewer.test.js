/**
 * @jest-environment jsdom
 */

/**
 * Clinical Co-Pilot source viewer (ADR-008): pdf.js canvas for PDFs, <img> for
 * photos, one overlay box from the stored bbox, a notice (never a box) when
 * the value could not be located.
 *
 * Run with: npx jest tests/js/copilot-viewer.test.js
 *
 * @package   OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

const lib = require('../../interface/modules/custom_modules/oe-module-copilot/public/copilot-panel.js');

const { SourceViewer } = lib;

function api(response) {
    return {
        base: '/openemr/apis/default/api/copilot',
        webroot: '/openemr',
        pid: 7,
        abort: new AbortController(),
        file: jest.fn(async () => response)
    };
}

function fileResponse(status, type, bytes) {
    return {
        ok: status === 200,
        status: status,
        headers: { get: (name) => (name.toLowerCase() === 'content-type' ? type : null) },
        arrayBuffer: async () => (bytes || new Uint8Array([1, 2, 3])).buffer
    };
}

function fakePdfjs(pages, rotate) {
    const rendered = [];
    const getDocument = jest.fn(() => ({
        promise: Promise.resolve({
            numPages: pages,
            getPage: async (n) => ({
                rotate: rotate || 0,
                getViewport: ({ scale }) => {
                    const swapped = (rotate || 0) % 180 !== 0;
                    return { width: (swapped ? 792 : 612) * scale, height: (swapped ? 612 : 792) * scale, rotation: rotate || 0, scale: scale };
                },
                render: (params) => {
                    rendered.push({ page: n, params: params });
                    return { promise: Promise.resolve(), cancel: () => undefined };
                }
            }),
            destroy: () => undefined
        }),
        destroy: () => undefined
    }));
    return { lib: { getDocument: getDocument, GlobalWorkerOptions: {} }, getDocument: getDocument, rendered: rendered };
}

let host;

beforeEach(() => {
    host = document.createElement('div');
    document.body.appendChild(host);
    Object.defineProperty(host, 'clientWidth', { configurable: true, value: 700 });
    global.URL.createObjectURL = jest.fn(() => 'blob:http://openemr/1');
    global.URL.revokeObjectURL = jest.fn();
    HTMLCanvasElement.prototype.getContext = jest.fn(() => ({}));
    Element.prototype.scrollIntoView = jest.fn();
});

afterEach(() => {
    document.body.innerHTML = '';
});

function overlay() {
    return host.querySelector('[data-role="bbox-overlay"]');
}

describe('SourceViewer - PDF', () => {
    test('renders the cited page with scripting off and boxes the value', async () => {
        const pdf = fakePdfjs(2);
        const viewer = new SourceViewer(host, api(fileResponse(200, 'application/pdf')), { loadPdfjs: async () => pdf.lib });
        await viewer.open({ documentId: 5, page: 2, bbox: [0.1, 0.2, 0.3, 0.25] });

        const opts = pdf.getDocument.mock.calls[0][0];
        expect(opts.enableScripting).toBe(false);
        expect(opts.enableXfa).toBe(false);
        expect(opts.data).toBeInstanceOf(Uint8Array);
        expect(opts.url).toBeUndefined();
        expect(pdf.rendered.map((r) => r.page)).toEqual([2]);
        const canvas = host.querySelector('canvas');
        expect(canvas).not.toBeNull();
        const width = parseFloat(canvas.style.width);
        const box = overlay();
        expect(box).not.toBeNull();
        expect(parseFloat(box.style.left)).toBeCloseTo(0.1 * width, 3);
        expect(parseFloat(box.style.width)).toBeCloseTo(0.2 * width, 3);
        expect(parseFloat(box.style.top)).toBeCloseTo(0.2 * width * 792 / 612, 3);
        expect(host.textContent).toContain('page 2 of 2');
        expect(host.textContent).not.toContain('Could not locate');
    });

    test('a rotated page is boxed in its displayed frame', async () => {
        const pdf = fakePdfjs(1, 90);
        const viewer = new SourceViewer(host, api(fileResponse(200, 'application/pdf')), { loadPdfjs: async () => pdf.lib });
        await viewer.open({ documentId: 5, page: 1, bbox: [0.5, 0.5, 1, 1] });
        const canvas = host.querySelector('canvas');
        expect(parseFloat(overlay().style.left)).toBeCloseTo(parseFloat(canvas.style.width) / 2, 3);
        expect(parseFloat(overlay().style.top)).toBeCloseTo(parseFloat(canvas.style.height) / 2, 3);
    });

    test('null bbox: the page is shown with the notice and no box', async () => {
        const pdf = fakePdfjs(1);
        const viewer = new SourceViewer(host, api(fileResponse(200, 'application/pdf')), { loadPdfjs: async () => pdf.lib });
        await viewer.open({ documentId: 5, page: null, bbox: null, located: false });
        expect(pdf.rendered.map((r) => r.page)).toEqual([1]);
        expect(overlay()).toBeNull();
        expect(host.textContent).toContain('Could not locate this value on the page (unverified)');
    });

    test('opening a document without a value shows the first page, no box and no notice', async () => {
        const pdf = fakePdfjs(3);
        const viewer = new SourceViewer(host, api(fileResponse(200, 'application/pdf')), { loadPdfjs: async () => pdf.lib });
        await viewer.open({ documentId: 5 });
        expect(pdf.rendered.map((r) => r.page)).toEqual([1]);
        expect(overlay()).toBeNull();
        expect(host.textContent).not.toContain('Could not locate');
    });

    test('page navigation re-renders; the box shows only on the cited page', async () => {
        const pdf = fakePdfjs(2);
        const viewer = new SourceViewer(host, api(fileResponse(200, 'application/pdf')), { loadPdfjs: async () => pdf.lib });
        await viewer.open({ documentId: 5, page: 1, bbox: [0.1, 0.2, 0.3, 0.25] });
        expect(overlay()).not.toBeNull();
        await viewer.showPage(2);
        expect(overlay()).toBeNull();
        await viewer.showPage(1);
        expect(overlay()).not.toBeNull();
    });
});

describe('SourceViewer - photos', () => {
    test('PNG in an <img> from a blob URL, boxed on its displayed size', async () => {
        const viewer = new SourceViewer(host, api(fileResponse(200, 'image/png')), { loadPdfjs: async () => { throw new Error('not for images'); } });
        const opened = viewer.open({ documentId: 9, page: 1, bbox: [0.5, 0.5, 1, 1] });
        await new Promise((r) => setTimeout(r, 0));
        const img = host.querySelector('img');
        expect(img).not.toBeNull();
        expect(img.getAttribute('src')).toBe('blob:http://openemr/1');
        Object.defineProperty(img, 'clientWidth', { configurable: true, value: 400 });
        Object.defineProperty(img, 'clientHeight', { configurable: true, value: 300 });
        img.dispatchEvent(new Event('load'));
        await opened;
        expect(parseFloat(overlay().style.left)).toBeCloseTo(200, 3);
        expect(parseFloat(overlay().style.top)).toBeCloseTo(150, 3);
        expect(parseFloat(overlay().style.width)).toBeCloseTo(200, 3);
        viewer.close();
        expect(global.URL.revokeObjectURL).toHaveBeenCalledWith('blob:http://openemr/1');
        expect(host.querySelector('img')).toBeNull();
    });
});

describe('SourceViewer - failures', () => {
    test.each([
        [404, 'The document was not found in this chart.'],
        [403, 'You do not have permission to view this document.']
    ])('%i is stated plainly', async (status, text) => {
        const viewer = new SourceViewer(host, api(fileResponse(status, 'application/json')), { loadPdfjs: async () => fakePdfjs(1).lib });
        await viewer.open({ documentId: 5, page: 1, bbox: [0.1, 0.2, 0.3, 0.25] });
        expect(host.textContent).toContain(text);
        expect(host.querySelector('canvas')).toBeNull();
    });

    test('an unexpected content type is refused', async () => {
        const viewer = new SourceViewer(host, api(fileResponse(200, 'text/html')), { loadPdfjs: async () => fakePdfjs(1).lib });
        await viewer.open({ documentId: 5 });
        expect(host.textContent).toContain('cannot be shown');
        expect(host.querySelector('canvas, img, iframe')).toBeNull();
    });

    test('a failed pdf.js load is stated, with the full-document link still offered', async () => {
        const viewer = new SourceViewer(host, api(fileResponse(200, 'application/pdf')), { loadPdfjs: async () => { throw new Error('blocked'); } });
        await viewer.open({ documentId: 5 });
        expect(host.textContent).toContain('could not be displayed');
        const link = host.querySelector('a[data-role="open-full"]');
        expect(link.getAttribute('href')).toBe('/openemr/controller.php?document&retrieve&patient_id=7&document_id=5&as_file=false');
    });
});

describe('DocumentsSection - Review source', () => {
    const { DocumentsSection } = lib;

    function panelContainer() {
        const c = document.createElement('div');
        c.dataset.pid = '7';
        c.dataset.csrf = 'csrf-token';
        c.dataset.ticketUrl = '/openemr/apis/default/api/copilot/briefing-ticket';
        document.body.appendChild(c);
        return c;
    }

    function json(status, body) {
        return { ok: status === 200, status: status, headers: { get: () => 'application/json' }, json: async () => body };
    }

    test('opens the cited page with the box and the value beside it; a value with no box gets the notice', async () => {
        const pdf = fakePdfjs(2);
        const files = [];
        global.fetch = jest.fn(async (url, init) => {
            if (url.endsWith('/documents/process')) {
                return json(200, { status: 'ok', processed: [], remaining: 0, documents: [{ document_id: 5, doc_type: 'lab_pdf', uploaded_at: '2026-09-20 10:00:00', status: 'extracted', error_code: null, identity_check: 'match', pending_count: 2 }] });
            }
            if (url.endsWith('/documents/5/values')) {
                return json(200, { document_id: 5, status: 'extracted', identity_check: 'match', values: [
                    { result_index: 0, test_name: 'Hemoglobin A1c', value_text: '8.4', unit: '%', reference_range: '4.0-5.6', abnormal_flag: 'H', flag_source: 'extracted', collection_date: '2026-09-01', verification_status: 'verified_exact', page: 2, bbox: [0.1, 0.2, 0.3, 0.25], status: 'candidate', procedure_result_id: null },
                    { result_index: 1, test_name: 'Glucose', value_text: '140', unit: 'mg/dL', reference_range: null, abnormal_flag: null, flag_source: null, collection_date: '2026-09-01', verification_status: 'unverified', page: null, bbox: null, status: 'candidate', procedure_result_id: null }
                ] });
            }
            if (url.endsWith('/document-file/5')) {
                files.push(init);
                return fileResponse(200, 'application/pdf');
            }
            throw new Error('unexpected ' + url);
        });
        const c = panelContainer();
        const section = new DocumentsSection(c, { loadPdfjs: async () => pdf.lib });
        await section.start();
        const rows = c.querySelectorAll('[data-role="value-row"]');
        rows[0].querySelector('[data-action="review"]').click();
        await new Promise((r) => setTimeout(r, 0));
        await new Promise((r) => setTimeout(r, 0));

        expect(files[0].headers.APICSRFTOKEN).toBe('csrf-token');
        expect(pdf.rendered.map((r) => r.page)).toEqual([2]);
        expect(c.querySelector('[data-role="bbox-overlay"]')).not.toBeNull();
        const side = c.querySelector('[data-role="value-panel"]');
        expect(side.textContent).toContain('Hemoglobin A1c');
        expect(side.textContent).toContain('8.4 %');
        expect(side.textContent).toContain('Collected 2026-09-01');

        rows[1].querySelector('[data-action="review"]').click();
        await new Promise((r) => setTimeout(r, 0));
        await new Promise((r) => setTimeout(r, 0));
        expect(pdf.rendered.map((r) => r.page)).toEqual([2, 1]);
        expect(c.querySelector('[data-role="bbox-overlay"]')).toBeNull();
        expect(c.querySelector('[data-role="viewer-notice"]').textContent).toBe('Could not locate this value on the page (unverified)');
        expect(c.querySelectorAll('[data-role="viewer"]')).toHaveLength(1);
    });
});
