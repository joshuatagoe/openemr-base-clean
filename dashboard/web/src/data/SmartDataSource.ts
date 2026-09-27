import type { Bundle, FhirResource } from 'fhir/r4';
import type { DataSource, FhirReadType, FhirSearchType, SearchParams, StdMedicationRow } from './DataSource';
import { DataSourceError } from './errors';

export interface SmartDataSourceOptions {
  /** FHIR base of the same-origin OpenEMR, e.g. https://host/apis/default/fhir. */
  fhirBaseUrl: string;
  /** Returns the in-memory access token (never persisted). */
  getAccessToken: () => string | undefined;
}

/**
 * Modes B/C transport (browser token, same-origin OpenEMR, patient-context
 * `patient/*.rs` scopes). A stub until milestone C5.
 */
export class SmartDataSource implements DataSource {
  readonly transport = 'smart' as const;

  constructor(readonly options: SmartDataSourceOptions) {}

  read<T extends FhirResource = FhirResource>(_type: FhirReadType, _id: string, _signal?: AbortSignal): Promise<T> {
    return Promise.reject(this.notYet());
  }

  search<T extends FhirResource = FhirResource>(_type: FhirSearchType, _params: SearchParams, _signal?: AbortSignal): Promise<Bundle<T>> {
    return Promise.reject(this.notYet());
  }

  patientMedicationList(_pid: string, _signal?: AbortSignal): Promise<StdMedicationRow[]> {
    return Promise.reject(this.notYet());
  }

  patientPid(_patientId: string, _signal?: AbortSignal): Promise<string> {
    return Promise.reject(this.notYet());
  }

  private notYet(): DataSourceError {
    return new DataSourceError('not_implemented', 'The SMART transport (modes B/C) is planned for milestone C5.');
  }
}
