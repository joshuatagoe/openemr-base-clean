// Labs card model. PHP: interface/patient_file/summary/labdata_fragment.php
// shows only the single most recent report: `ORDER BY procedure_report.date_collected
// DESC`, first row; its procedure name, the raw date_collected and the order's
// encounter number. OpenEMR's FHIR has no report resource the Physicians group
// can read (DiagnosticReport needs admin/super, A2 §4), so the report is
// rebuilt from lab Observations (one per procedure_result row, A2 §2.6), which
// carry neither a report id, the procedure name, the collection date nor the
// encounter number. Gaps G13, G16–G18, G22: README.
import type { Observation } from 'fhir/r4';
import { openemrWallClock } from '../format/date';

export interface LabReportSummary {
  /** The report's result names, each once, in server order (the procedure name is not in FHIR, G17). */
  tests: string;
  /** procedure_report.date_report as OpenEMR stored it (`YYYY-MM-DD HH:MM:SS`), '' when absent (G16). */
  date: string;
  /** `Encounter/<uuid>`; OpenEMR's numeric encounter id is not in FHIR. */
  encounter: string | undefined;
}

export interface LabReportGroup {
  key: string;
  /** effectiveDateTime of the report (= date_report), as sent. */
  effective: string | undefined;
  encounter: string | undefined;
  observations: Observation[];
}

const NULL_FLAVOR = 'http://terminology.hl7.org/CodeSystem/v3-NullFlavor';
const DATA_ABSENT = 'http://terminology.hl7.org/CodeSystem/data-absent-reason';

/** OpenEMR sends nullFlavor UNK when result_code or result_text is empty (G13). */
function testName(o: Observation): string {
  if (o.code?.text?.trim()) return o.code.text.trim();
  const coding = o.code?.coding?.find((c) => c.display?.trim() && c.system !== NULL_FLAVOR && c.system !== DATA_ABSENT);
  return coding?.display?.trim() || 'Unnamed result';
}

function instant(v: string | undefined): number {
  const t = v ? Date.parse(v) : NaN;
  return Number.isNaN(t) ? -Infinity : t;
}

/**
 * Results of one report share the order's encounter and the report date, and
 * nothing else identifies the report. Two reports of one encounter with the
 * same report date merge (heuristic, G22). Entered-in-error results are
 * dropped first (G18); newest report first, ties in server order.
 */
export function groupLabReports(observations: readonly Observation[]): LabReportGroup[] {
  const groups = new Map<string, LabReportGroup>();
  for (const o of observations) {
    if (o.status === 'entered-in-error') continue;
    const encounter = o.encounter?.reference;
    const effective = o.effectiveDateTime;
    const key = `${encounter ?? ''}|${effective ?? ''}`;
    const g = groups.get(key);
    if (g) g.observations.push(o);
    else groups.set(key, { key, effective, encounter, observations: [o] });
  }
  // Array.prototype.sort is stable, so equal dates keep server order. Undated
  // reports go last, as NULL does in MySQL's DESC order.
  return [...groups.values()].sort((a, b) => {
    const ta = instant(a.effective);
    const tb = instant(b.effective);
    return ta === tb ? 0 : tb > ta ? 1 : -1;
  });
}

/** The PHP card's single most recent report, or null ("No lab data documented."). */
export function latestLabReport(observations: readonly Observation[]): LabReportSummary | null {
  const latest = groupLabReports(observations)[0];
  if (!latest) return null;
  const names = [...new Set(latest.observations.map(testName))];
  return { tests: names.join(', '), date: openemrWallClock(latest.effective), encounter: latest.encounter };
}
