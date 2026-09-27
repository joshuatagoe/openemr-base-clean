// The only OpenEMR routes the BFF will call, and the only query parameters each
// accepts. Anything else is refused before a request leaves the BFF:
//   unknown route -> 404, unknown / repeated / malformed parameter -> 400.
// Phase B found that OpenEMR errors on unsupported search parameters, and a
// tight list keeps the proxy from becoming a general-purpose token relay.

export type MatchResult =
  | { ok: true; upstreamPath: string; query: URLSearchParams; resource: string; kind: 'read' | 'search' }
  | { ok: false; status: 400 | 404; error: 'not_found' | 'bad_request'; detail?: string };

interface ParamRule {
  name: string;
  required?: boolean;
  pattern?: RegExp;
  values?: readonly string[];
}

// FHIR ids: [A-Za-z0-9\-\.]{1,64} (FHIR R4 id datatype).
const FHIR_ID = /^[A-Za-z0-9.-]{1,64}$/;
const PATIENT_REF: ParamRule = { name: 'patient', required: true, pattern: FHIR_ID };

const SEARCHES: Readonly<Record<string, { params: readonly ParamRule[]; atLeastOne?: boolean }>> = {
  Patient: {
    atLeastOne: true,
    params: [
      // Letters (any script), spaces, apostrophes, hyphens and dots.
      { name: 'name', pattern: /^[\p{L}\p{M}' .-]{1,64}$/u },
      { name: 'birthdate', pattern: /^(eq|ge|le|gt|lt)?\d{4}(-\d{2}(-\d{2})?)?$/ },
      { name: 'identifier', pattern: /^[A-Za-z0-9._|:-]{1,64}$/ },
    ],
  },
  AllergyIntolerance: { params: [PATIENT_REF] },
  Condition: {
    params: [PATIENT_REF, { name: 'category', values: ['problem-list-item', 'encounter-diagnosis', 'health-concern'] }],
  },
  MedicationRequest: { params: [PATIENT_REF] },
  CareTeam: { params: [PATIENT_REF] },
  Observation: { params: [PATIENT_REF, { name: 'category', required: true, values: ['laboratory'] }] },
};

const READS: ReadonlySet<string> = new Set(['Patient', 'Practitioner', 'Organization']);

const NOT_FOUND = { ok: false, status: 404, error: 'not_found' } as const;

function badRequest(detail: string): MatchResult {
  return { ok: false, status: 400, error: 'bad_request', detail };
}

function checkParams(query: URLSearchParams, rules: readonly ParamRule[], atLeastOne: boolean): string | null {
  const allowed = new Map(rules.map((r) => [r.name, r]));
  const seen = new Set<string>();
  for (const [name, value] of query) {
    const rule = allowed.get(name);
    if (!rule) return `parameter not allowed: ${name}`;
    if (seen.has(name)) return `parameter repeated: ${name}`;
    seen.add(name);
    if (value === '') return `parameter empty: ${name}`;
    if (rule.pattern && !rule.pattern.test(value)) return `parameter malformed: ${name}`;
    if (rule.values && !rule.values.includes(value)) return `parameter value not allowed: ${name}`;
  }
  for (const r of rules) if (r.required && !seen.has(r.name)) return `parameter required: ${r.name}`;
  if (atLeastOne && seen.size === 0) return 'at least one search parameter is required';
  return null;
}

/** `path` is what follows `/api/fhir/`, e.g. `Patient` or `Patient/<id>`. */
export function matchFhirRequest(path: string, query: URLSearchParams): MatchResult {
  const parts = path.split('/');
  const resource = parts[0] ?? '';
  if (parts.length === 1) {
    const search = Object.hasOwn(SEARCHES, resource) ? SEARCHES[resource] : undefined;
    if (!search) return NOT_FOUND;
    const problem = checkParams(query, search.params, search.atLeastOne ?? false);
    if (problem) return badRequest(problem);
    return { ok: true, upstreamPath: resource, query, resource, kind: 'search' };
  }
  if (parts.length === 2) {
    const id = parts[1] ?? '';
    if (!READS.has(resource) || !FHIR_ID.test(id) || id.startsWith('.')) return NOT_FOUND;
    if ([...query.keys()].length > 0) return badRequest('read takes no parameters');
    return { ok: true, upstreamPath: `${resource}/${id}`, query, resource, kind: 'read' };
  }
  return NOT_FOUND;
}

/** Standard API `GET /api/patient/:pid/medication` (pid is OpenEMR's numeric patient id). */
export function matchStdMedicationRequest(pid: string, query: URLSearchParams): MatchResult {
  if (!/^\d{1,10}$/.test(pid)) return NOT_FOUND;
  if ([...query.keys()].length > 0) return badRequest('this route takes no parameters');
  return { ok: true, upstreamPath: `patient/${pid}/medication`, query, resource: 'patient-medication', kind: 'search' };
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/**
 * Standard API `GET /api/patient/:puuid`: used only to map the FHIR Patient id
 * (a uuid) to OpenEMR's numeric pid, which the medication-list route needs.
 * The BFF answers `{ pid, uuid }` only (see app.ts), never the patient_data row.
 */
export function matchStdPatientRequest(puuid: string, query: URLSearchParams): MatchResult {
  if (!UUID.test(puuid)) return NOT_FOUND;
  if ([...query.keys()].length > 0) return badRequest('this route takes no parameters');
  return { ok: true, upstreamPath: `patient/${puuid}`, query, resource: 'patient', kind: 'read' };
}
