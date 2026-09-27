import type { Bundle, FhirResource } from 'fhir/r4';
import type { DataSource, FhirReadType, FhirSearchType, SearchParams, StdMedicationRow } from './DataSource';
import { DataSourceError, type DataErrorKind } from './errors';

export interface SmartDataSourceOptions {
  /** FHIR base of the same-origin OpenEMR, e.g. https://host/apis/default/fhir. */
  fhirBaseUrl: string;
  /** Returns the in-memory access token (never persisted). */
  getAccessToken: () => string | undefined;
  /** Called when OpenEMR answers 401 (token expired): the app asks for a relaunch. */
  onSessionExpired?: () => void;
}

function kindFor(status: number): DataErrorKind {
  if (status === 401) return 'session_expired';
  if (status === 403) return 'forbidden';
  if (status === 404) return 'not_found';
  if (status === 400) return 'bad_request';
  if (status === 504) return 'timeout';
  // Includes OpenEMR's 500 for a patient outside the token's context (Phase B T4).
  return 'upstream';
}

/**
 * Modes B/C transport: the browser holds a patient-context token (`patient/*.rs`)
 * and calls OpenEMR's FHIR API directly. Same origin only: OpenEMR answers every
 * cross-origin CORS preflight on /apis/* with 404 (Phase B T5), and a token must
 * never be sent anywhere else.
 */
export class SmartDataSource implements DataSource {
  readonly transport = 'smart' as const;
  private readonly base: string;

  constructor(private readonly options: SmartDataSourceOptions) {
    const u = new URL(options.fhirBaseUrl);
    if (u.origin !== window.location.origin) throw new Error('The FHIR base must be on the same origin as the app.');
    this.base = `${u.origin}${u.pathname.replace(/\/+$/, '')}`;
  }

  read<T extends FhirResource = FhirResource>(type: FhirReadType, id: string, signal?: AbortSignal): Promise<T> {
    return this.getJson<T>(`${this.base}/${type}/${encodeURIComponent(id)}`, signal);
  }

  search<T extends FhirResource = FhirResource>(type: FhirSearchType, params: SearchParams, signal?: AbortSignal): Promise<Bundle<T>> {
    const qs = new URLSearchParams(params).toString();
    return this.getJson<Bundle<T>>(`${this.base}/${type}${qs ? `?${qs}` : ''}`, signal);
  }

  /**
   * OpenEMR's standard API refuses patient-context tokens (403, Phase B T7), so
   * no request is made: the cards fall back to the combined medications card.
   */
  patientMedicationList(_pid: string, _signal?: AbortSignal): Promise<StdMedicationRow[]> {
    return Promise.reject(this.noStandardApi());
  }

  patientPid(_patientId: string, _signal?: AbortSignal): Promise<string> {
    return Promise.reject(this.noStandardApi());
  }

  private noStandardApi(): DataSourceError {
    return new DataSourceError('forbidden', "OpenEMR's standard API is not available to a SMART patient-context token.");
  }

  private async getJson<T>(url: string, signal?: AbortSignal): Promise<T> {
    const token = this.options.getAccessToken();
    if (!token) {
      this.options.onSessionExpired?.();
      throw new DataSourceError('session_expired', 'No access token.');
    }
    let res: Response;
    try {
      res = await fetch(url, {
        method: 'GET',
        credentials: 'omit',
        headers: { accept: 'application/fhir+json, application/json', authorization: `Bearer ${token}` },
        ...(signal ? { signal } : {}),
      });
    } catch (e) {
      if (e instanceof DOMException && e.name === 'AbortError') throw e;
      throw new DataSourceError('network', 'OpenEMR could not be reached.');
    }
    if (res.ok) return (await res.json()) as T;
    const kind = kindFor(res.status);
    if (kind === 'session_expired') this.options.onSessionExpired?.();
    throw new DataSourceError(kind, `Request failed (${res.status}).`, res.status);
  }
}
