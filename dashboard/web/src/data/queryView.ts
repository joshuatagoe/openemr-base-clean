import { DataSourceError } from './errors';

/** A query's state as plain data, so components render one `switch`. */
export type QueryView<T> =
  | { status: 'idle' }
  | { status: 'loading' }
  | { status: 'error'; error: DataSourceError }
  | { status: 'empty' }
  | { status: 'ready'; data: T };

interface QueryLike<T> {
  data: T | undefined;
  error: unknown;
  isPending: boolean;
  isError: boolean;
  isSuccess: boolean;
  fetchStatus: 'fetching' | 'paused' | 'idle';
}

export function toView<T>(q: QueryLike<T>, isEmpty: (data: T) => boolean = () => false): QueryView<T> {
  if (q.isError) {
    const error = q.error instanceof DataSourceError ? q.error : new DataSourceError('upstream', 'Unexpected error.');
    return { status: 'error', error };
  }
  if (q.isSuccess && q.data !== undefined) return isEmpty(q.data) ? { status: 'empty' } : { status: 'ready', data: q.data };
  if (q.isPending && q.fetchStatus === 'idle') return { status: 'idle' };
  return { status: 'loading' };
}
