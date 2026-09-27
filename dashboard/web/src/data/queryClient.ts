import { QueryClient } from '@tanstack/react-query';
import { DataSourceError, type DataErrorKind } from './errors';

/** Only transient failures are retried; auth, access and "not there" answers are final. */
const RETRYABLE: ReadonlySet<DataErrorKind> = new Set(['upstream', 'timeout', 'network']);
const MAX_RETRIES = 2;

export function shouldRetry(failureCount: number, error: unknown): boolean {
  return error instanceof DataSourceError && RETRYABLE.has(error.kind) && failureCount < MAX_RETRIES;
}

export interface QueryClientOptions {
  /** Milliseconds between retries (tests pass 0). Default: exponential, capped at 4 s. */
  retryDelay?: number;
}

export function createQueryClient(options: QueryClientOptions = {}): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: shouldRetry,
        retryDelay: options.retryDelay ?? ((attempt) => Math.min(500 * 2 ** attempt, 4000)),
        staleTime: 60_000,
        gcTime: 5 * 60_000,
        refetchOnWindowFocus: false,
        refetchOnReconnect: false,
      },
    },
  });
}
