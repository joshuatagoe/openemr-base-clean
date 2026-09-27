import { createContext, useContext } from 'react';
import type { SignedInUser } from './authApi';

export type Notice =
  | { kind: 'session_expired' }
  | { kind: 'signed_out' }
  | { kind: 'auth_error'; message: string }
  | { kind: 'error'; message: string };

export type AuthState =
  | { status: 'loading' }
  | { status: 'signedOut' }
  | { status: 'signedIn'; user: SignedInUser; expiresAt: string };

export interface AuthContextValue {
  state: AuthState;
  notice: Notice | undefined;
  signOut(): Promise<void>;
  /** Called by the data layer when the BFF answers 401. */
  markSessionExpired(): void;
}

export const AuthContext = createContext<AuthContextValue | null>(null);

export function useAuth(): AuthContextValue {
  const v = useContext(AuthContext);
  if (!v) throw new Error('useAuth must be used inside <AuthProvider>');
  return v;
}
