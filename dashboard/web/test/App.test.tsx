import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { useState } from 'react';
import { describe, expect, it } from 'vitest';
import { App } from '../src/App';
import { useDataSource } from '../src/data/DataSourceContext';
import { expired, server, signedIn, signedOut } from './msw/server';

function renderAt(path: string, extraRoutes?: Parameters<typeof App>[0]['extraRoutes']) {
  window.history.replaceState(null, '', path);
  return render(<App extraRoutes={extraRoutes} />);
}

describe('auth shell', () => {
  it('shows a Sign in link pointing at the BFF login when there is no session', async () => {
    server.use(signedOut);
    renderAt('/');
    const link = await screen.findByRole('link', { name: 'Sign in' });
    expect(link).toHaveAttribute('href', '/auth/login');
    expect(screen.getByRole('banner')).toHaveTextContent('Patient Dashboard');
    expect(screen.queryByText(/Signed in as/)).not.toBeInTheDocument();
  });

  it('shows "Signed in as <name>" and a Sign out button, and opens the dashboard route', async () => {
    server.use(signedIn);
    renderAt('/');
    expect(await screen.findByText('Signed in as Dana Testdoctor')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Sign out' })).toBeInTheDocument();
    expect(await screen.findByRole('heading', { name: 'Find a patient' })).toBeInTheDocument();
    expect(window.location.pathname).toBe('/dashboard');
  });

  it('keeps signed-out users off the dashboard route', async () => {
    server.use(signedOut);
    renderAt('/dashboard');
    expect(await screen.findByRole('link', { name: 'Sign in' })).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Find a patient' })).not.toBeInTheDocument();
    expect(window.location.pathname).toBe('/');
  });

  it('signs out through POST /auth/logout and returns to the signed-out view', async () => {
    let logoutMethod = '';
    server.use(
      signedIn,
      http.post('*/auth/logout', ({ request }) => {
        logoutMethod = request.method;
        return new HttpResponse(null, { status: 204 });
      }),
    );
    renderAt('/dashboard');
    await userEvent.click(await screen.findByRole('button', { name: 'Sign out' }));
    expect(await screen.findByText('You have signed out.')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Sign in' })).toBeInTheDocument();
    expect(logoutMethod).toBe('POST');
  });

  it('tells the user when the session has expired', async () => {
    server.use(expired);
    renderAt('/dashboard');
    expect(await screen.findByRole('alert')).toHaveTextContent('Your session expired. Sign in again to continue.');
    expect(screen.getByRole('link', { name: 'Sign in' })).toBeInTheDocument();
  });

  it('shows a readable message for a failed sign-in and removes the code from the URL', async () => {
    server.use(signedOut);
    renderAt('/?auth_error=state_mismatch');
    expect(await screen.findByRole('alert')).toHaveTextContent(/sign-in link expired or was already used/i);
    expect(window.location.search).toBe('');
  });

  it('does not render arbitrary auth_error text from the URL', async () => {
    server.use(signedOut);
    renderAt('/?auth_error=%3Cb%3Einjected%3C%2Fb%3E');
    expect(await screen.findByRole('alert')).toHaveTextContent('Sign-in did not complete: an unexpected error occurred.');
    expect(screen.queryByText(/injected/)).not.toBeInTheDocument();
  });

  it('switches to "session expired" when a data call comes back 401', async () => {
    server.use(
      signedIn,
      http.get('*/api/fhir/Patient', () => HttpResponse.json({ error: 'session_expired' }, { status: 401 })),
    );
    function Probe() {
      const ds = useDataSource();
      const [done, setDone] = useState(false);
      return (
        <button
          type="button"
          onClick={() => {
            void ds.search('Patient', { name: 'x' }).catch(() => setDone(true));
          }}
        >
          {done ? 'probe done' : 'probe'}
        </button>
      );
    }
    renderAt('/probe', [{ path: '/probe', element: <Probe /> }]);
    await screen.findByText('Signed in as Dana Testdoctor');
    await userEvent.click(screen.getByRole('button', { name: 'probe' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Your session expired. Sign in again to continue.');
    expect(screen.queryByText(/Signed in as/)).not.toBeInTheDocument();
  });
});
