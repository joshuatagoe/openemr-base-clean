import { useMemo, type ReactNode } from 'react';
import { useAuth } from '../auth/authContext';
import { BffDataSource } from './BffDataSource';
import { DataSourceContext } from './DataSourceContext';

/** Mode A: the BFF transport. Modes B/C will choose SmartDataSource here (C5). */
export function DataSourceProvider({ children }: { children: ReactNode }) {
  const { markSessionExpired } = useAuth();
  const ds = useMemo(() => new BffDataSource({ onSessionExpired: markSessionExpired }), [markSessionExpired]);
  return <DataSourceContext.Provider value={ds}>{children}</DataSourceContext.Provider>;
}
