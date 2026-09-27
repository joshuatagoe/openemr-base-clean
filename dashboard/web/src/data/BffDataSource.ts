import type { Bundle, FhirResource } from 'fhir/r4';
import type { DataSource, FhirReadType, FhirSearchType, SearchParams, StdMedicationRow } from './DataSource';
import { DataSourceError, type DataErrorKind } from './errors';

export interface BffDataSourceOptions {
  /** Origin of the BFF. Defaults to the page's own origin (same-origin only). */
  origin?: string;
  /** Called when the BFF reports that the session is gone (401). */
  onSessionExpired?: () => void;
  fetchImpl?: typeof fetch;
}

async function errorCode(res: Response): Promise<string | undefined> {
  try {
    const body: unknown = await res.json();
    if (body && typeof body === 'object' && 'error' in body && typeof body.error === 'string') return body.error;
  } catch {
    // Body absent or not JSON.
  }
  return undefined;
}

function kindFor(status: number, code: string | undefined): DataErrorKind {
  if (status === 401) return code === 'unauthenticated' ? 'unauthenticated' : 'session_expired';
  if (status === 403) return code === 'not_accessible' ? 'not_accessible' : 'forbidden';
  if (status === 404) return 'not_found';
  if (status === 400) return 'bad_request';
  if (status === 504) return 'timeout';
  return 'upstream';
}

/** Mode A transport: same-origin calls to the BFF's allow-listed proxy. */
export class BffDataSource implements DataSource {
  readonly transport = 'bff' as const;
  private readonly origin: string;
  private readonly onSessionExpired: (() => void) | undefined;
  private readonly fetchImpl: typeof fetch;

  constructor(options: BffDataSourceOptions = {}) {
    this.origin = options.origin ?? window.location.origin;
    this.onSessionExpired = options.onSessionExpired;
    this.fetchImpl = options.fetchImpl ?? ((input, init) => fetch(input, init));
  }

  read<T extends FhirResource = FhirResource>(type: FhirReadType, id: string, signal?: AbortSignal): Promise<T> {
    return this.getJson<T>(`/api/fhir/${type}/${encodeURIComponent(id)}`, signal);
  }

  search<T extends FhirResource = FhirResource>(type: FhirSearchType, params: SearchParams, signal?: AbortSignal): Promise<Bundle<T>> {
    const qs = new URLSearchParams(params).toString();
    return this.getJson<Bundle<T>>(`/api/fhir/${type}${qs ? `?${qs}` : ''}`, signal);
  }

  async patientMedicationList(pid: string, signal?: AbortSignal): Promise<StdMedicationRow[]> {
    try {
      // ListRestController::getAll answers a bare JSON array (verified live);
      // the `{ data: [...] }` envelope used by other standard routes is accepted too.
      const body = await this.getJson<unknown>(`/api/patient/${encodeURIComponent(pid)}/medication`, signal);
      if (Array.isArray(body)) return body as StdMedicationRow[];
      if (body && typeof body === 'object' && 'data' in body && Array.isArray(body.data)) return body.data as StdMedicationRow[];
      return [];
    } catch (e) {
      if (e instanceof DataSourceError && e.kind === 'not_found') return [];
      throw e;
    }
  }

  private async getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
    let res: Response;
    try {
      res = await this.fetchImpl(new URL(path, this.origin), {
        method: 'GET',
        credentials: 'same-origin',
        headers: { accept: 'application/json' },
        ...(signal ? { signal } : {}),
      });
    } catch (e) {
      if (e instanceof DOMException && e.name === 'AbortError') throw e;
      throw new DataSourceError('network', 'The dashboard server could not be reached.');
    }
    if (res.ok) return (await res.json()) as T;
    const kind = kindFor(res.status, await errorCode(res));
    if (kind === 'session_expired' || kind === 'unauthenticated') this.onSessionExpired?.();
    throw new DataSourceError(kind, `Request failed (${res.status}).`, res.status);
  }
}
