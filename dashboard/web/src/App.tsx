import type { ReactNode } from 'react';
import { BrowserRouter, Route, Routes } from 'react-router';
import { AuthProvider } from './auth/AuthProvider';
import { AppHeader } from './components/AppHeader';
import { NoticeBanner } from './components/NoticeBanner';
import { RequireAuth } from './components/RequireAuth';
import { DataSourceProvider } from './data/DataSourceProvider';
import { DashboardPage } from './pages/DashboardPage';
import { HomePage } from './pages/HomePage';

export interface AppProps {
  /** Additional routes (used by tests; later milestones add real pages here). */
  extraRoutes?: ReadonlyArray<{ path: string; element: ReactNode }> | undefined;
}

export function App({ extraRoutes = [] }: AppProps) {
  return (
    <AuthProvider>
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
                    <DashboardPage />
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
    </AuthProvider>
  );
}
