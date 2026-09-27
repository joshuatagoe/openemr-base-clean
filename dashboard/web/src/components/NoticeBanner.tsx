import { useAuth } from '../auth/authContext';

export function NoticeBanner() {
  const { notice } = useAuth();
  if (!notice) return null;
  if (notice.kind === 'signed_out') {
    return (
      <p className="notice notice-info" role="status">
        You have signed out.
      </p>
    );
  }
  const text = notice.kind === 'session_expired' ? 'Your session expired. Sign in again to continue.' : notice.message;
  return (
    <p className="notice notice-warning" role="alert">
      {text}
    </p>
  );
}
