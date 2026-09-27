/**
 * @jest-environment jsdom
 */

/**
 * Clinical Co-Pilot panel - follow-ups see documents read on this chart open.
 *
 * The follow-up bundle is built when the panel requests its ticket, which runs
 * alongside the chart-open processing loop. When the loop reads a document, or
 * a value is filed / rejected / un-filed, the panel asks the module to refresh
 * the follow-up context: the same ticket-refresh request with
 * refresh_pending_facts. It never re-runs the briefing (no GET /v1/briefings)
 * and never posts a new bundle.
 *
 * Run with: npx jest tests/js/copilot-followup-refresh.test.js
 *
 * @package   OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

const lib = require('../../interface/modules/custom_modules/oe-module-copilot/public/copilot-panel.js');

const { CopilotPanel, DocumentsSection } = lib;

const TICKET_URL = '/openemr/apis/default/api/copilot/briefing-ticket';
const BOUND = {
    correlation_id: '7f0e6b2a-3c4d-4e5f-9a1b-2c3d4e5f6a7b',
    patient_uuid: '3b9d2c1e-8f7a-4b6c-9d0e-1f2a3b4c5d6e',
    bundle_id: 'e6b2abe5-8664-4ebb-bf81-e3d5cca35df4',
    ticket: 'ticket-1',
    agent_url: 'http://agent.test:8765'
};

function container() {
    const c = document.createElement('div');
    c.id = 'oe-copilot-panel';
    c.dataset.pid = '7';
    c.dataset.csrf = 'csrf-token';
    c.dataset.ticketUrl = TICKET_URL;
    const status = document.createElement('span');
    status.dataset.role = 'status';
    const body = document.createElement('div');
    body.dataset.role = 'body';
    c.appendChild(status);
    c.appendChild(body);
    document.body.appendChild(c);
    return c;
}

function jsonResponse(status, body) {
    return { ok: status >= 200 && status < 300, status: status, headers: { get: () => 'application/json' }, json: async () => body };
}

function doc(id, over) {
    return Object.assign({ document_id: id, doc_type: 'lab_pdf', uploaded_at: '2026-09-26 10:00:00', status: 'extracted', error_code: null, identity_check: 'match', pending_count: 1 }, over || {});
}

function refreshed(over) {
    return jsonResponse(200, Object.assign({
        schema_version: '1.0', correlation_id: BOUND.correlation_id, patient_uuid: BOUND.patient_uuid, agent_url: BOUND.agent_url,
        bundle_id: BOUND.bundle_id, ticket: 'ticket-2', ticket_expires_at: '2026-09-26T10:02:00Z', sections: null, degraded: null, warnings: []
    }, over || {}));
}

async function flush() {
    for (let i = 0; i < 30; i += 1) {
        await Promise.resolve();
    }
}

afterEach(() => {
    document.body.innerHTML = '';
    delete global.fetch;
});

describe('DocumentsSection tells follow-ups when the pending facts changed', () => {
    function processing(outcomes) {
        return jest.fn(async (url) => {
            if (url.endsWith('/documents/process')) {
                return jsonResponse(200, { status: 'ok', processed: outcomes.map((o, i) => ({ document_id: 5 + i, outcome: o })), remaining: 0, documents: [doc(5)] });
            }
            return jsonResponse(200, { document_id: 5, status: 'extracted', identity_check: 'match', values: [] });
        });
    }

    test('a document read on this chart open triggers one refresh, after the loop', async () => {
        global.fetch = processing(['extracted', 'failed']);
        const section = new DocumentsSection(container());
        section.onFactsChanged = jest.fn();
        await section.start();
        expect(section.onFactsChanged).toHaveBeenCalledTimes(1);
    });

    test('nothing read now (nothing new, or only failures): no refresh', async () => {
        global.fetch = processing([]);
        const quiet = new DocumentsSection(container());
        quiet.onFactsChanged = jest.fn();
        await quiet.start();
        expect(quiet.onFactsChanged).not.toHaveBeenCalled();

        document.body.innerHTML = '';
        global.fetch = processing(['failed', 'held_identity']);
        const failed = new DocumentsSection(container());
        failed.onFactsChanged = jest.fn();
        await failed.start();
        expect(failed.onFactsChanged).not.toHaveBeenCalled();
    });

    test('Verify and file / Reject / Un-file refresh too (afterAction)', async () => {
        global.fetch = jest.fn(async (url) => (url.includes('/documents?pid=')
            ? jsonResponse(200, { status: 'ok', documents: [doc(5)] })
            : jsonResponse(200, { document_id: 5, status: 'extracted', identity_check: 'match', values: [] })));
        const section = new DocumentsSection(container());
        section.onFactsChanged = jest.fn();
        await section.afterAction(doc(5), 0, { text: 'Filed.' });
        expect(section.onFactsChanged).toHaveBeenCalledTimes(1);
    });
});

describe('CopilotPanel.refreshFollowUpContext', () => {
    test('asks for a pending-facts refresh of the same bundle and takes the fresh ticket; no briefing, no new bundle', async () => {
        const seen = [];
        global.fetch = jest.fn(async (url, init) => {
            seen.push({ url: url, init: init });
            return refreshed();
        });
        const panel = new CopilotPanel(container());
        panel.bound = Object.assign({}, BOUND);
        expect(await panel.refreshFollowUpContext()).toBe(true);

        expect(seen).toHaveLength(1);
        expect(seen[0].url).toBe(TICKET_URL);
        expect(seen[0].init.method).toBe('POST');
        expect(JSON.parse(seen[0].init.body)).toEqual({
            pid: 7, refresh_bundle_id: BOUND.bundle_id, refresh_correlation_id: BOUND.correlation_id, refresh_pending_facts: true
        });
        expect(seen.some((c) => c.url.includes('/v1/briefings') || c.url.includes('/v1/bundles'))).toBe(false);
        expect(panel.bound.ticket).toBe('ticket-2');
    });

    test('a response for another bundle or patient is ignored', async () => {
        global.fetch = jest.fn(async () => refreshed({ bundle_id: '00000000-0000-4000-8000-000000000000' }));
        const panel = new CopilotPanel(container());
        panel.bound = Object.assign({}, BOUND);
        expect(await panel.refreshFollowUpContext()).toBe(false);
        expect(panel.bound.ticket).toBe('ticket-1');

        global.fetch = jest.fn(async () => refreshed({ patient_uuid: '00000000-0000-4000-8000-000000000001' }));
        expect(await panel.refreshFollowUpContext()).toBe(false);
        expect(panel.bound.ticket).toBe('ticket-1');
    });

    test('changes during a refresh run one more refresh after it, not one each', async () => {
        let release;
        const gate = new Promise((r) => {
            release = r;
        });
        const bodies = [];
        global.fetch = jest.fn(async (url, init) => {
            bodies.push(JSON.parse(init.body));
            if (bodies.length === 1) {
                await gate;
            }
            return refreshed();
        });
        const panel = new CopilotPanel(container());
        panel.bound = Object.assign({}, BOUND);
        const first = panel.refreshFollowUpContext();
        panel.refreshFollowUpContext();
        panel.refreshFollowUpContext();
        release();
        await first;
        await flush();
        expect(bodies).toHaveLength(2);
        expect(bodies.every((b) => b.refresh_pending_facts === true)).toBe(true);
    });

    test('a change before the ticket arrives is refreshed as soon as it does', async () => {
        const posts = [];
        global.fetch = jest.fn(async (url, init) => {
            if (url === TICKET_URL) {
                posts.push(JSON.parse(init.body));
                return posts.length === 1
                    ? refreshed({ ticket: 'ticket-1', sections: {} })
                    : refreshed();
            }
            return { ok: false, status: 503, body: null, headers: { get: () => null } }; // the briefing stream: not under test
        });
        const panel = new CopilotPanel(container());
        expect(await panel.refreshFollowUpContext()).toBe(false); // no ticket yet: remembered
        await panel.start();
        await flush();
        expect(posts).toHaveLength(2);
        expect(posts[0]).toEqual({ pid: 7 });
        expect(posts[1].refresh_pending_facts).toBe(true);
        expect(posts[1].refresh_bundle_id).toBe(BOUND.bundle_id);
    });
});

describe('follow-up sources', () => {
    test('an intake item is shown as patient-reported beside its id', () => {
        const panel = new CopilotPanel(container());
        const item = document.createElement('li');
        panel.renderTurn({
            statements: [{ kind: 'fact', text: 'Metformin 1000 mg twice daily (patient-reported (from the intake form), not in the chart).', citations: [
                { record_type: 'patient_reported', record_id: 'copilot_extracted_value:91', timestamp: '2026-09-25T00:00:00Z' },
                { record_type: 'medication', record_id: 'prescriptions:31', timestamp: '2026-05-01T00:00:00Z' }
            ] }],
            rejected_count: 0, tool_calls: []
        }, item);
        const sources = item.textContent;
        expect(sources).toContain('copilot_extracted_value:91 \u2014 patient-reported, intake form');
        expect(sources).not.toContain('prescriptions:31 \u2014 patient-reported');
    });
});
