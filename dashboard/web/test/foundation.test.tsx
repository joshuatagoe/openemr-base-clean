// UX foundation (DASHBOARD_UX_PLAN.md H1-H5, M5, M7, L1, L2, L4, L7): the shared
// OpenEMR style_light tokens, heading focus without a visible box, the app bar,
// the patient page's title and loading bar. Layout itself (equal-height cards,
// the 390px phone bar) needs a real browser; jsdom has no layout, so those are
// checked by the Selenium run described in docs/dashboard-parity/PARITY.md.
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { useRef } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from '../src/App';
import { useFocusOnChange } from '../src/components/useFocusOnChange';
import { createQueryClient } from '../src/data/queryClient';
import { PATIENT_A_ID, patientA } from './fixtures/patients';
import { server, signedIn, signedOut } from './msw/server';

// The stylesheet as text (Vitest empties CSS imports; jsdom has no layout anyway).
// Vitest runs from the web workspace (npm test -w web); import.meta.url is not a file: URL under jsdom.
const css = readFileSync(resolve(process.cwd(), 'src/styles.css'), 'utf8');

/** The declarations of the first rule whose selector list is exactly `selector`. */
function rule(selector: string): string {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const m = new RegExp(`(?:^|\\n|\\})\\s*${escaped}\\s*\\{([^}]*)\\}`).exec(css);
  if (!m?.[1]) throw new Error(`no rule for ${selector}`);
  return m[1];
}

function renderAt(path: string) {
  window.history.replaceState(null, '', path);
  return render(<App queryClient={createQueryClient({ retryDelay: 0 })} />);
}

describe('design tokens (OpenEMR style_light, plan §3)', () => {
  const root = rule(':root');
  it.each([
    ['--oe-page-bg', '#e6e6e6'],
    ['--oe-shell-bg', '#f9fafb'],
    ['--oe-surface', '#ffffff'],
    ['--oe-text', '#111827'],
    ['--oe-muted', '#4b5563'],
    ['--oe-link', '#1d4ed8'],
    ['--oe-link-hover', '#1e40af'],
    ['--oe-border', 'rgba(0, 0, 0, 0.125)'],
    ['--oe-table-head', '#e5e7eb'],
    ['--oe-stripe', 'rgba(0, 0, 0, 0.05)'],
    ['--oe-hover', 'rgba(0, 0, 0, 0.075)'],
    ['--oe-warning', '#ffc107'],
    ['--oe-btn-primary', '#0d6efd'],
    ['--oe-success-badge', '#198754'],
    ['--oe-focus-ring', '3px solid #86b7fe'],
    ['--oe-font', '"Lato", Helvetica, Arial, sans-serif'],
  ])('defines %s: %s on :root', (name, value) => {
    expect(root).toContain(`${name}: ${value};`);
  });

  it('uses the OpenEMR font stack and text colour, and loads no web font', () => {
    expect(root).toContain('font-family: var(--oe-font);');
    expect(root).toContain('color: var(--oe-text);');
    expect(css).not.toMatch(/@import|@font-face|url\(/);
  });

  it('links are OpenEMR blue in every state (no visited purple), underlined only on hover', () => {
    expect(rule(':where(a, a:visited)')).toMatch(/color: var\(--oe-link\);[\s\S]*text-decoration: none;/);
    expect(rule(':where(a:hover)')).toMatch(/color: var\(--oe-link-hover\);[\s\S]*text-decoration: underline;/);
  });

  it('card titles and the edit pencil use the link colour', () => {
    expect(rule('.card-toggle')).toContain('color: var(--oe-link);');
    expect(rule('.card-edit, .card-edit:visited')).toContain('color: var(--oe-link);');
  });

  it('button labels never wrap', () => {
    expect(rule('.btn')).toContain('white-space: nowrap;');
  });

  it('PAMI cards stretch to one height, as the PHP flex-fill row', () => {
    expect(css).not.toMatch(/\.cards-row-pami\s*\{[^}]*align-items:\s*start/);
  });
});

describe('heading focus (H1)', () => {
  it('draws no outline on a programmatically focused heading; controls keep the 3px ring', () => {
    expect(rule(":is(h1, h2, h3)[tabindex='-1']:focus")).toContain('outline: none;');
    expect(css).not.toMatch(/h1:focus-visible/);
    const controls = /\n((?:[^{}\n]+,\n)*[^{}\n]+)\{\s*outline: var\(--oe-focus-ring\);/.exec(css)?.[1] ?? '';
    for (const sel of ['a:focus-visible', '.btn:focus-visible', '.card-toggle:focus-visible', '.form-field input:focus-visible', '.table-responsive:focus-visible']) {
      expect(controls).toContain(sel);
    }
  });
});

function Probe({ k }: { k: string }) {
  const ref = useRef<HTMLHeadingElement>(null);
  useFocusOnChange(ref, k);
  return (
    <h1 ref={ref} tabIndex={-1}>
      Heading {k}
    </h1>
  );
}

describe('useFocusOnChange', () => {
  afterEach(() => vi.restoreAllMocks());

  it('focuses without scrolling when the heading is already in view', () => {
    const focus = vi.spyOn(HTMLElement.prototype, 'focus');
    render(<Probe k="a" />);
    expect(screen.getByRole('heading')).toHaveFocus();
    expect(focus).toHaveBeenLastCalledWith({ preventScroll: true });
  });

  it('lets the browser scroll the heading into view when it is off screen', () => {
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue({ top: -400, bottom: -360, left: 0, right: 100, width: 100, height: 40, x: 0, y: -400, toJSON: () => ({}) });
    const focus = vi.spyOn(HTMLElement.prototype, 'focus');
    render(<Probe k="a" />);
    expect(screen.getByRole('heading')).toHaveFocus();
    expect(focus).toHaveBeenLastCalledWith({ preventScroll: false });
  });

  it('moves focus again when the key changes', () => {
    const { rerender } = render(<Probe k="a" />);
    screen.getByRole('heading').blur();
    rerender(<Probe k="b" />);
    expect(screen.getByRole('heading', { name: 'Heading b' })).toHaveFocus();
  });
});

describe('app bar (L1, H5)', () => {
  it('signed in: the product name links home and the user name stays readable to screen readers', async () => {
    server.use(signedIn);
    renderAt('/dashboard');
    const banner = screen.getByRole('banner');
    const home = await within(banner).findByRole('link', { name: 'Patient Dashboard' });
    expect(home).toHaveAttribute('href', '/dashboard');
    expect(within(banner).getByText('Signed in as Dana Testdoctor')).toBeInTheDocument();
    expect(within(banner).getByRole('button', { name: 'Sign out' })).toBeInTheDocument();
  });

  it('signed out: the product name is plain text', async () => {
    server.use(signedOut);
    renderAt('/');
    const banner = screen.getByRole('banner');
    await within(banner).findByRole('link', { name: 'Sign in' });
    expect(banner).toHaveTextContent('Patient Dashboard');
    expect(within(banner).queryByRole('link', { name: 'Patient Dashboard' })).toBeNull();
  });
});

describe('patient page (L4, M7)', () => {
  beforeEach(() => {
    document.title = 'Patient Dashboard';
  });

  it('titles the tab "Chart – Patient Dashboard" (never the patient name) and restores it on leaving', async () => {
    server.use(signedIn, http.get('*/api/fhir/Patient/:id', () => HttpResponse.json(patientA)));
    renderAt(`/patient/${PATIENT_A_ID}`);
    await screen.findByRole('heading', { level: 1, name: 'Ada Samplefamily' });
    expect(document.title).toBe('Chart – Patient Dashboard');
    expect(document.title).not.toContain('Samplefamily');
    await userEvent.click(screen.getByRole('link', { name: 'Find another patient' }));
    await screen.findByRole('heading', { level: 1, name: 'Find a patient' });
    // Leaving the chart restores the title; the landing page then sets its own.
    await waitFor(() => expect(document.title).toBe('Find a patient – Patient Dashboard'));
  });

  it('while loading, holds the patient bar\'s place with a spinner and "Loading patient…"', async () => {
    let release: () => void = () => {};
    const gate = new Promise<void>((r) => {
      release = r;
    });
    server.use(
      signedIn,
      http.get('*/api/fhir/Patient/:id', async () => {
        await gate;
        return HttpResponse.json(patientA);
      }),
    );
    renderAt(`/patient/${PATIENT_A_ID}`);
    const status = await screen.findByRole('status');
    expect(status).toHaveTextContent('Loading patient…');
    expect(status.closest('.patient-bar')).not.toBeNull();
    expect(status.querySelector('.spinner')).toHaveAttribute('aria-hidden', 'true');
    release();
    await screen.findByRole('heading', { level: 1, name: 'Ada Samplefamily' });
  });

  it('card loading states show the same spinner', async () => {
    server.use(
      signedIn,
      http.get('*/api/fhir/Patient/:id', () => HttpResponse.json(patientA)),
      http.get('*/api/fhir/AllergyIntolerance', () => new Promise(() => {})),
    );
    renderAt(`/patient/${PATIENT_A_ID}`);
    const card = await screen.findByRole('region', { name: 'Allergies' });
    const status = within(card).getByRole('status');
    expect(status).toHaveTextContent('Loading allergies…');
    expect(status.querySelector('.spinner')).toHaveAttribute('aria-hidden', 'true');
  });
});
