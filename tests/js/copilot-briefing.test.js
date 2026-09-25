/**
 * @jest-environment jsdom
 */

/**
 * Clinical Co-Pilot panel - the document briefing now comes from the stored
 * extractions of every read document (ADR-012), and its document citations
 * open the source viewer at the cited page and box (ADR-008).
 *
 * Run with: npx jest tests/js/copilot-briefing.test.js
 *
 * @package   OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

const lib = require('../../interface/modules/custom_modules/oe-module-copilot/public/copilot-panel.js');

const { DocumentBriefingSection } = lib;

function container() {
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

afterEach(() => {
    document.body.innerHTML = '';
    delete global.fetch;
});

test('the button is labelled as a briefing from the read documents, not the latest file', () => {
    const section = new DocumentBriefingSection(container(), null);
    expect(section.button.textContent).toBe('Brief from all read lab documents');
    expect(section.button.textContent).not.toMatch(/latest/);
});

test('it posts to the document-briefing route and explains when nothing has been read yet', async () => {
    global.fetch = jest.fn(async () => json(200, { status: 'degraded', degraded_reason: 'no_extracted_documents', briefing: null }));
    const section = new DocumentBriefingSection(container(), null);
    await section.run();
    expect(global.fetch.mock.calls[0][0]).toBe('/openemr/apis/default/api/copilot/document-briefing');
    expect(section.output.textContent).toMatch(/No lab document has been read yet/);
});

test('a document citation opens the viewer at its page and box', async () => {
    global.fetch = jest.fn(async () => json(200, {
        status: 'ok',
        briefing: {
            what_changed: [{
                tier: 'document_stated',
                text: 'Hemoglobin A1c 8.4 %',
                not_yet_in_chart: true,
                document_citation: { source_type: 'document', source_id: '5', page_or_section: 'p. 2', field_or_chunk_id: 'results[0]', quote_or_value: '8.4', page: 2, bbox: [0.1, 0.2, 0.3, 0.25] }
            }, {
                tier: 'document_stated',
                text: 'Glucose 140 mg/dL',
                not_yet_in_chart: true,
                document_citation: { source_type: 'document', source_id: '5', page_or_section: 'p. 1', field_or_chunk_id: 'results[1]', quote_or_value: '140', page: null, bbox: null }
            }],
            needs_attention: [],
            what_to_consider: []
        }
    }));
    const opened = [];
    const documents = { openSource: (did, idx, source) => opened.push([did, idx, source]) };
    const section = new DocumentBriefingSection(container(), documents);
    await section.run();
    const buttons = section.output.querySelectorAll('[data-action="citation-source"]');
    expect(buttons).toHaveLength(2);
    buttons[0].click();
    buttons[1].click();
    expect(opened[0]).toEqual([5, null, { page: 2, bbox: [0.1, 0.2, 0.3, 0.25], located: true }]);
    expect(opened[1]).toEqual([5, null, { page: null, bbox: null, located: false }]);
});
