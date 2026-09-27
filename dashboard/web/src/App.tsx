import type { QueryClient } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { BrowserRouter, Route, Routes } from 'react-router';
import { AuthProvider } from './auth/AuthProvider';
import { AppHeader } from './components/AppHeader';
import { NoticeBanner } from './components/NoticeBanner';
import { RequireAuth } from './components/RequireAuth';
import { DataSourceProvider } from './data/DataSourceProvider';
import { QueryProvider } from './data/QueryProvider';
import { HomePage } from './pages/HomePage';
import { PatientPage } from './pages/PatientPage';
import { PatientSearchPage } from './pages/PatientSearchPage';

export interface AppProps {
  /** Additional routes (used by tests; later milestones add real pages here). */
  extraRoutes?: ReadonlyArray<{ path: string; element: ReactNode }> | undefined;
  /** Tests inject a client with retryDelay 0 and inspect its cache. */
  queryClient?: QueryClient | undefined;
}

export function App({ extraRoutes = [], queryClient }: AppProps) {
  return (
    <AuthProvider>
      <QueryProvider client={queryClient}>
        <DataSourceProvider>
          <BrowserRouter>
            <AppHeader />
            <main className="app-main">
              <NoticeBanner />
              <Routes>
                <Route path="/" element={<HomePage />} />
                <Route
                  path="/dashboard"
                  element={
                    <RequireAuth>
                      <PatientSearchPage />
                    </RequireAuth>
                  }
                />
                <Route
                  path="/patient/:id"
                  element={
                    <RequireAuth>
                      <PatientPage />
                    </RequireAuth>
                  }
                />
                {extraRoutes.map((r) => (
                  <Route key={r.path} path={r.path} element={r.element} />
                ))}
                <Route path="*" element={<p>Page not found.</p>} />
              </Routes>
            </main>
          </BrowserRouter>
        </DataSourceProvider>
      </QueryProvider>
    </AuthProvider>
  );
}
