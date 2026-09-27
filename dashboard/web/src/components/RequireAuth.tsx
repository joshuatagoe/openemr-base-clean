import type { ReactNode } from 'react';
import { Navigate } from 'react-router';
import { useAuth } from '../auth/authContext';
import { Spinner } from './Card';

export function RequireAuth({ children }: { children: ReactNode }) {
  const { state } = useAuth();
  if (state.status === 'loading')
    return (
      <p className="muted" role="status">
        <Spinner />
        Checking your sign-in…
      </p>
    );
  if (state.status === 'signedOut') return <Navigate to="/" replace />;
  return <>{children}</>;
}
