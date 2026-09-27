// Medications vs Prescriptions. OpenEMR's FHIR MedicationRequest is a UNION of
// the `prescriptions` table and `lists` medication rows with nothing that tells
// them apart reliably (A2 Q1, Phase B T7). The exact split: every row of the
// standard API `GET /api/patient/:pid/medication` is a list medication (the
// PHP Medications card); every MedicationRequest whose id is not one of those
// uuids is a prescription (the PHP Prescriptions card).
import type { MedicationRequest } from 'fhir/r4';
import type { StdMedicationRow } from '../data/DataSource';
import { openemrWallClock } from '../format/date';
import { conceptText } from './allergy';

export interface MedListRow {
  key: string;
  title: string;
  /** `lists_medication.drug_dosage_instructions`, from the matching MedicationRequest ('' when none). */
  dosage: string;
}

export interface RxRow {
  id: string;
  drug: string;
  details: string;
  qty: string;
  /** `authoredOn` (= prescriptions.date_added) as `YYYY-MM-DD HH:MM:SS`, raw like the PHP card. */
  filled: string;
}

export interface RxTable {
  /** Every prescription for the patient, active or not (PHP prints "None" only when this is 0). */
  total: number;
  /** The active ones (PHP prints rows with `active > 0`), newest modified first. */
  rows: RxRow[];
}

export interface CombinedRow {
  id: string;
  name: string;
  dosage: string;
}

function str(v: unknown): string {
  return typeof v === 'string' ? v : typeof v === 'number' ? String(v) : '';
}

/** Parse OpenEMR's `YYYY-MM-DD[ HH:MM:SS]` as local time. */
function parseLocal(value: string): number {
  return Date.parse(value.trim().replace(' ', 'T'));
}

/** PHP `filterActiveIssues`: outcome is not 1 (Resolved) and the end date is empty or in the future. */
export function isActiveIssue(row: StdMedicationRow, now: Date): boolean {
  if (str(row.outcome) === '1') return false;
  const end = str(row.enddate).trim();
  if (!end || end.startsWith('0000-00-00')) return true;
  const t = parseLocal(end);
  return Number.isNaN(t) || t > now.getTime();
}

function sig(m: MedicationRequest | undefined): string {
  return m?.dosageInstruction?.[0]?.text?.trim() ?? '';
}

export function drugName(m: MedicationRequest): string {
  return conceptText(m.medicationCodeableConcept) || 'Unnamed medication';
}

/**
 * The Medications card: list rows (not the FHIR rows, which leave out list
 * medications linked to a prescription, while the PHP card lists them), PHP
 * issue filter, ordered by begdate ascending with no begdate first.
 */
export function medicationListRows(stdRows: readonly StdMedicationRow[], medReqs: readonly MedicationRequest[], now: Date): MedListRow[] {
  const byId = new Map(medReqs.filter((m) => m.id).map((m) => [m.id as string, m]));
  return stdRows
    .filter((r) => isActiveIssue(r, now))
    .map((r, order) => ({ r, order, beg: str(r.begdate).trim() }))
    .sort((a, b) => {
      if (a.beg === b.beg) return Number(str(a.r.id)) - Number(str(b.r.id)) || a.order - b.order;
      if (!a.beg) return -1;
      if (!b.beg) return 1;
      return parseLocal(a.beg) - parseLocal(b.beg);
    })
    .map(({ r, order }) => ({
      key: str(r.uuid) || `list-${order}`,
      title: str(r.title),
      dosage: sig(byId.get(str(r.uuid))),
    }));
}

function isActiveRx(m: MedicationRequest): boolean {
  // OpenEMR: prescriptions.active = 1 → `active`, or `completed` when an end date is set; otherwise `stopped`.
  return m.status === 'active' || m.status === 'completed';
}

function details(m: MedicationRequest): string {
  const d = m.dosageInstruction?.[0];
  const dose = d?.doseAndRate?.[0]?.doseQuantity;
  const size = dose?.value !== undefined ? `${dose.value}${dose.unit ?? ''}` : '';
  const timing = d?.timing?.code?.text?.trim() ?? '';
  return [size, sig(m), timing].filter(Boolean).join(' ');
}

function time(v: string | undefined): number {
  const t = v ? Date.parse(v) : NaN;
  return Number.isNaN(t) ? -Infinity : t;
}

/** The Prescriptions card: PHP order `date_modified DESC, date_added DESC` (= meta.lastUpdated, authoredOn). */
export function prescriptionTable(medReqs: readonly MedicationRequest[], listUuids: ReadonlySet<string>): RxTable {
  const rx = medReqs.filter((m) => !(m.id && listUuids.has(m.id)));
  const rows = rx
    .map((m, order) => ({ m, order }))
    .filter(({ m }) => isActiveRx(m))
    .sort((a, b) => time(b.m.meta?.lastUpdated) - time(a.m.meta?.lastUpdated) || time(b.m.authoredOn) - time(a.m.authoredOn) || a.order - b.order)
    .map(({ m, order }) => {
      const qty = m.dispenseRequest?.quantity?.value;
      return {
        id: m.id ?? `rx-${order}`,
        drug: drugName(m),
        details: details(m),
        qty: qty === undefined ? '' : String(qty),
        filled: openemrWallClock(m.authoredOn), // date_added, raw like the PHP card
      };
    });
  return { total: rx.length, rows };
}

/** Fallback when the list cannot be read: one list, nothing guessed. */
export function combinedRows(medReqs: readonly MedicationRequest[]): CombinedRow[] {
  return medReqs.filter(isActiveRx).map((m, i) => ({ id: m.id ?? `med-${i}`, name: drugName(m), dosage: sig(m) }));
}
