import { QueryClientProvider, useQueryClient, type QueryClient } from '@tanstack/react-query';
import { useEffect, useState, type ReactNode } from 'react';
import { useAuth } from '../auth/authContext';
import { createQueryClient } from './queryClient';

/** Drops every cached patient resource as soon as the user is signed out or the session ends. */
function ClearOnSignOut() {
  const { state } = useAuth();
  const qc = useQueryClient();
  useEffect(() => {
    if (state.status === 'signedOut') qc.clear();
  }, [state.status, qc]);
  return null;
}

export function QueryProvider({ client, children }: { client?: QueryClient | undefined; children: ReactNode }) {
  const [qc] = useState(() => client ?? createQueryClient());
  return (
    <QueryClientProvider client={qc}>
      <ClearOnSignOut />
      {children}
    </QueryClientProvider>
  );
}
