import { Navigate } from 'react-router';
import { LOGIN_PATH } from '../auth/authApi';
import { useAuth } from '../auth/authContext';

export function HomePage() {
  const { state } = useAuth();
  if (state.status === 'loading') return <p className="muted">Checking your session...</p>;
  if (state.status === 'signedIn') return <Navigate to="/dashboard" replace />;
  return (
    <section className="signed-out">
      <p>Sign in with your OpenEMR account to open the patient dashboard.</p>
      <a className="btn btn-primary" href={LOGIN_PATH}>
        Sign in with OpenEMR
      </a>
    </section>
  );
}
