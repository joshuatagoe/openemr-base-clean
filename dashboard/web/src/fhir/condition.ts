// Problem list model. PHP "Medical Problems" card: every `lists` row of type
// medical_problem that is not resolved and not ended, title only, ordered by
// begdate ascending (templates/patient/card/medical_problems.html.twig,
// PatientIssuesService ORDER BY lists.begdate).
//
// OpenEMR's FHIR splits that list (A2 §2.2, gap G3): a problem never linked to
// an encounter is a `problem-list-item`; a linked one disappears from that
// category and comes back once per linked encounter as `encounter-diagnosis`.
// Both categories are fetched and the per-encounter copies are merged here.
import type { Condition } from 'fhir/r4';
import { conceptText } from './allergy';

export interface ProblemRow {
  key: string;
  title: string;
  /**
   * FHIR says `resolved`. OpenEMR also emits `resolved` for a problem whose
   * occurrence is "First" (an open first episode, which the PHP card shows),
   * so resolved problems are kept and marked instead of hidden.
   */
  resolved: boolean;
}

const OPEN = new Set(['active', 'recurrence', 'relapse']);

function clinical(c: Condition): string | undefined {
  return c.clinicalStatus?.coding?.[0]?.code;
}

function isEnteredInError(c: Condition): boolean {
  return c.verificationStatus?.coding?.[0]?.code === 'entered-in-error';
}

/** Dedupe key: same code (or same free text) and same onset = the same `lists` row. */
function problemKey(c: Condition): string {
  const coding = c.code?.coding?.find((x) => x.code);
  const what = coding ? `code:${coding.system ?? ''}|${coding.code ?? ''}` : `text:${(c.code?.text ?? '').trim().toLowerCase()}`;
  return `${what}@${c.onsetDateTime ?? ''}`;
}

interface Merged {
  key: string;
  title: string;
  onset: string | undefined;
  statuses: (string | undefined)[];
  order: number;
}

export function problemRows(conditions: readonly Condition[]): ProblemRow[] {
  const byKey = new Map<string, Merged>();
  const seenIds = new Set<string>();
  conditions.forEach((c, order) => {
    if (isEnteredInError(c)) return;
    if (c.id) {
      if (seenIds.has(c.id)) return;
      seenIds.add(c.id);
    }
    const key = problemKey(c);
    const existing = byKey.get(key);
    if (existing) {
      existing.statuses.push(clinical(c));
      return;
    }
    byKey.set(key, {
      key,
      title: conceptText(c.code) || 'Unnamed problem',
      onset: c.onsetDateTime,
      statuses: [clinical(c)],
      order,
    });
  });

  const rows: (ProblemRow & { onset: string | undefined; order: number })[] = [];
  for (const m of byKey.values()) {
    const open = m.statuses.some((s) => s === undefined || OPEN.has(s));
    const resolved = !open && m.statuses.includes('resolved');
    if (!open && !resolved) continue; // inactive = end date in the past; PHP hides these too
    rows.push({ key: m.key, title: m.title, resolved, onset: m.onset, order: m.order });
  }
  rows.sort((a, b) => {
    if (a.onset === b.onset) return a.order - b.order;
    if (a.onset === undefined) return -1;
    if (b.onset === undefined) return 1;
    const diff = Date.parse(a.onset) - Date.parse(b.onset);
    return diff !== 0 && !Number.isNaN(diff) ? diff : a.order - b.order;
  });
  return rows.map(({ key, title, resolved }) => ({ key, title, resolved }));
}
