import { createContext, useContext } from 'react';
import type { DataSource } from './DataSource';

export const DataSourceContext = createContext<DataSource | null>(null);

export function useDataSource(): DataSource {
  const ds = useContext(DataSourceContext);
  if (!ds) throw new Error('useDataSource must be used inside <DataSourceProvider>');
  return ds;
}
