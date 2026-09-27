import { describe, expect, it } from 'vitest';
import { createQueryClient, shouldRetry } from '../src/data/queryClient';
import { DataSourceError, type DataErrorKind } from '../src/data/errors';
import { toView } from '../src/data/queryView';

describe('retry policy', () => {
  it.each<DataErrorKind>(['unauthenticated', 'session_expired', 'forbidden', 'not_accessible', 'not_found', 'bad_request', 'not_implemented'])(
    'never retries %s',
    (kind) => {
      expect(shouldRetry(0, new DataSourceError(kind, 'x'))).toBe(false);
    },
  );

  it.each<DataErrorKind>(['upstream', 'timeout', 'network'])('retries %s twice, then stops', (kind) => {
    const e = new DataSourceError(kind, 'x');
    expect(shouldRetry(0, e)).toBe(true);
    expect(shouldRetry(1, e)).toBe(true);
    expect(shouldRetry(2, e)).toBe(false);
  });

  it('does not retry unknown errors (programming errors)', () => {
    expect(shouldRetry(0, new TypeError('boom'))).toBe(false);
  });

  it('applies the policy in a real query: one call for 404, three for 502', async () => {
    const qc = createQueryClient({ retryDelay: 0 });
    let calls = 0;
    await expect(
      qc.fetchQuery({
        queryKey: ['t404'],
        queryFn: () => {
          calls += 1;
          return Promise.reject(new DataSourceError('not_found', 'x', 404));
        },
      }),
    ).rejects.toBeInstanceOf(DataSourceError);
    expect(calls).toBe(1);

    calls = 0;
    await expect(
      qc.fetchQuery({
        queryKey: ['t502'],
        queryFn: () => {
          calls += 1;
          return Promise.reject(new DataSourceError('upstream', 'x', 502));
        },
      }),
    ).rejects.toBeInstanceOf(DataSourceError);
    expect(calls).toBe(3);
  });
});

describe('query state as data', () => {
  const base = { data: undefined, error: null, isPending: false, isError: false, isSuccess: false, fetchStatus: 'idle' as const };

  it('idle when disabled and nothing fetched', () => {
    expect(toView({ ...base, isPending: true })).toEqual({ status: 'idle' });
  });

  it('loading while the first fetch runs', () => {
    expect(toView({ ...base, isPending: true, fetchStatus: 'fetching' })).toEqual({ status: 'loading' });
  });

  it('error carries a DataSourceError (unknown errors are wrapped)', () => {
    const v = toView({ ...base, isError: true, error: new TypeError('x') });
    expect(v.status).toBe('error');
    if (v.status === 'error') expect(v.error.kind).toBe('upstream');
  });

  it('empty vs ready decided by the isEmpty predicate', () => {
    expect(toView({ ...base, isSuccess: true, data: [] as number[] }, (d) => d.length === 0)).toEqual({ status: 'empty' });
    expect(toView({ ...base, isSuccess: true, data: [1] }, (d) => d.length === 0)).toEqual({ status: 'ready', data: [1] });
  });
});
