// Every clinical result must belong to the selected patient. Phase B found an
// OpenEMR search that ignores its `patient` parameter (Observation in patient
// context), so the dashboard checks the subject of every result itself and
// drops anything else. Only a count and the resource type are logged: never an
// id, a name or any clinical content.

export type DropLogger = (message: string) => void;

const defaultLogger: DropLogger = (message) => {
  // eslint-disable-next-line no-console -- the one deliberate log line; carries a count only.
  console.warn(message);
};

/** True when `reference` is `Patient/<id>` (relative or absolute URL) for exactly this patient. */
export function isForPatient(reference: string | undefined, patientId: string): boolean {
  if (!reference) return false;
  const expected = `Patient/${patientId}`;
  return reference === expected || reference.endsWith(`/${expected}`);
}

export function guardSubject<T>(
  items: readonly T[],
  patientId: string,
  referenceOf: (item: T) => string | undefined,
  resourceType: string,
  log: DropLogger = defaultLogger,
): T[] {
  const kept = items.filter((item) => isForPatient(referenceOf(item), patientId));
  const dropped = items.length - kept.length;
  if (dropped > 0) log(`[dashboard] dropped ${dropped} ${resourceType} result(s) that were not for the selected patient`);
  return kept;
}

/** Same guard for standard-API rows, which carry the numeric pid instead of a reference. */
export function guardPid<T extends Readonly<Record<string, unknown>>>(rows: readonly T[], pid: string, resourceType: string, log: DropLogger = defaultLogger): T[] {
  const kept = rows.filter((r) => {
    const v = r['pid'];
    return (typeof v === 'string' || typeof v === 'number') && String(v) === pid;
  });
  const dropped = rows.length - kept.length;
  if (dropped > 0) log(`[dashboard] dropped ${dropped} ${resourceType} result(s) that were not for the selected patient`);
  return kept;
}
