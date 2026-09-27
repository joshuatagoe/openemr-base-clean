import type { QueryClient } from '@tanstack/react-query';
import { useRef, type ReactNode } from 'react';
import { BrowserRouter, Link, Route, Routes } from 'react-router';
import { AuthProvider } from './auth/AuthProvider';
import { AppHeader } from './components/AppHeader';
import { NoticeBanner } from './components/NoticeBanner';
import { RequireAuth } from './components/RequireAuth';
import { useFocusOnChange } from './components/useFocusOnChange';
import { DataSourceProvider } from './data/DataSourceProvider';
import { QueryProvider } from './data/QueryProvider';
import { HomePage } from './pages/HomePage';
import { PatientPage } from './pages/PatientPage';
import { PatientSearchPage } from './pages/PatientSearchPage';
import { RecentPatientsProvider } from './recent/RecentPatientsProvider';

export interface AppProps {
  /** Additional routes (used by tests; later milestones add real pages here). */
  extraRoutes?: ReadonlyArray<{ path: string; element: ReactNode }> | undefined;
  /** Tests inject a client with retryDelay 0 and inspect its cache. */
  queryClient?: QueryClient | undefined;
}

function NotFound() {
  const headingRef = useRef<HTMLHeadingElement>(null);
  useFocusOnChange(headingRef, 'mount');
  return (
    <section>
      <h1 className="page-title" ref={headingRef} tabIndex={-1}>
        This page doesn't exist.
      </h1>
      <Link to="/dashboard">Go to the patient list</Link>
    </section>
  );
}

export function App({ extraRoutes = [], queryClient }: AppProps) {
  return (
    <AuthProvider>
      <QueryProvider client={queryClient}>
        <DataSourceProvider>
          <RecentPatientsProvider>
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
                  <Route path="*" element={<NotFound />} />
                </Routes>
              </main>
            </BrowserRouter>
          </RecentPatientsProvider>
        </DataSourceProvider>
      </QueryProvider>
    </AuthProvider>
  );
}
