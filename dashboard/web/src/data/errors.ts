/** Why a data call failed, in terms the UI can act on. */
export type DataErrorKind =
  | 'unauthenticated'
  | 'session_expired'
  | 'forbidden'
  | 'not_accessible'
  | 'not_found'
  | 'bad_request'
  | 'timeout'
  | 'upstream'
  | 'network'
  | 'not_implemented';

export class DataSourceError extends Error {
  readonly kind: DataErrorKind;
  readonly status: number | undefined;

  constructor(kind: DataErrorKind, message: string, status?: number) {
    super(message);
    this.name = 'DataSourceError';
    this.kind = kind;
    this.status = status;
  }
}
