import type { ReactNode } from 'react';
import { Navigate } from 'react-router';
import { useAuth } from '../auth/authContext';

export function RequireAuth({ children }: { children: ReactNode }) {
  const { state } = useAuth();
  if (state.status === 'loading') return <p className="muted">Checking your session...</p>;
  if (state.status === 'signedOut') return <Navigate to="/" replace />;
  return <>{children}</>;
}
