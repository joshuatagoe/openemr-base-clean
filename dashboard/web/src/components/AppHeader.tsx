import { Link } from 'react-router';
import { LOGIN_PATH } from '../auth/authApi';
import { useAuth } from '../auth/authContext';

export function AppHeader() {
  const { state, signOut } = useAuth();
  return (
    <header className="app-header" role="banner">
      {state.status === 'signedIn' ? (
        <Link className="app-title" to="/dashboard">
          Patient Dashboard
        </Link>
      ) : (
        <span className="app-title">Patient Dashboard</span>
      )}
      <div className="app-auth">
        {state.status === 'signedIn' && (
          <>
            {/* Visually hidden on phones (styles.css), still read by screen readers. */}
            <span className="app-user">Signed in as {state.user.displayName}</span>
            <button type="button" className="btn btn-secondary" onClick={() => void signOut()}>
              Sign out
            </button>
          </>
        )}
        {state.status === 'signedOut' && (
          <a className="btn btn-primary" href={LOGIN_PATH}>
            Sign in
          </a>
        )}
      </div>
    </header>
  );
}
