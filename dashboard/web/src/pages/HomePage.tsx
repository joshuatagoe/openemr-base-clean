import { useRef } from 'react';
import { Navigate } from 'react-router';
import { LOGIN_PATH } from '../auth/authApi';
import { useAuth } from '../auth/authContext';
import { Spinner } from '../components/Card';
import { SIGN_IN_TITLE, useDocumentTitle } from '../components/useDocumentTitle';
import { useFocusOnChange } from '../components/useFocusOnChange';

export function HomePage() {
  const { state, notice } = useAuth();
  const headingRef = useRef<HTMLHeadingElement>(null);
  // Keyed on the auth status: the heading mounts once the sign-in check is done.
  useFocusOnChange(headingRef, state.status);
  useDocumentTitle(SIGN_IN_TITLE);
  if (state.status === 'loading')
    return (
      <p className="muted" role="status">
        <Spinner />
        Checking your sign-in…
      </p>
    );
  if (state.status === 'signedIn') return <Navigate to="/dashboard" replace />;
  return (
    <section className="signed-out">
      <h1 className="page-title" ref={headingRef} tabIndex={-1}>
        Sign in
      </h1>
      <p>Use your OpenEMR account to open the patient dashboard.</p>
      <a className="btn btn-primary" href={LOGIN_PATH}>
        {notice?.kind === 'auth_error' ? 'Sign in again' : 'Sign in with OpenEMR'}
      </a>
    </section>
  );
}
