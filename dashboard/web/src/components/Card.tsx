import { useId, useState, type ReactNode } from 'react';
import type { DataSourceError } from '../data/errors';
import type { QueryView } from '../data/queryView';

// Collapse state is remembered per browser under the PHP user-setting names
// (allergy_ps_expand, ...). PHP stores it per user on the server; this port
// has no write access, so it uses localStorage and falls back to the PHP
// default whenever storage is unavailable.
const STORAGE_PREFIX = 'dashboard.card.';

function readExpanded(id: string, fallback: boolean): boolean {
  try {
    const v = window.localStorage.getItem(STORAGE_PREFIX + id);
    return v === '1' ? true : v === '0' ? false : fallback;
  } catch {
    return fallback;
  }
}

function writeExpanded(id: string, expanded: boolean): void {
  try {
    window.localStorage.setItem(STORAGE_PREFIX + id, expanded ? '1' : '0');
  } catch {
    // Storage blocked: the toggle still works for this page view.
  }
}

function ExpandIcon({ expanded }: { expanded: boolean }) {
  // fa-compress when open, fa-expand when collapsed, as in card_base.html.twig.
  return (
    <svg className="card-toggle-icon" viewBox="0 0 16 16" width="14" height="14" aria-hidden="true" focusable="false">
      {expanded ? (
        <path d="M6 1v5H1M10 1v5h5M6 15v-5H1M10 15v-5h5" fill="none" stroke="currentColor" strokeWidth="1.6" />
      ) : (
        <path d="M1 6V1h5M15 6V1h-5M1 10v5h5M15 10v5h-5" fill="none" stroke="currentColor" strokeWidth="1.6" />
      )}
    </svg>
  );
}

function PencilIcon() {
  return (
    <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true" focusable="false">
      <path d="M11.5 1.5l3 3L5 14H2v-3z" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round" />
    </svg>
  );
}

export interface CardProps {
  /** The PHP user-setting id of the card (e.g. `allergy_ps_expand`). */
  id: string;
  title: string;
  /** PHP default: expanded, except Care Team (no seeded setting → collapsed). */
  defaultExpanded: boolean;
  /** Link out to the OpenEMR screen where the PHP card's pencil leads. */
  edit?: { href: string; label: string } | undefined;
  className?: string;
  children: ReactNode;
}

export function Card({ id, title, defaultExpanded, edit, className, children }: CardProps) {
  const [expanded, setExpanded] = useState(() => readExpanded(id, defaultExpanded));
  const uid = useId();
  const titleId = `${uid}-title`;
  const bodyId = `${uid}-body`;
  const toggle = () => {
    setExpanded((prev) => {
      writeExpanded(id, !prev);
      return !prev;
    });
  };
  return (
    <section className={`card ${className ?? ''}`.trim()} aria-labelledby={titleId}>
      <h2 className="card-title">
        <button type="button" className="card-toggle" aria-expanded={expanded} aria-controls={bodyId} onClick={toggle}>
          <span id={titleId}>{title}</span>
          <ExpandIcon expanded={expanded} />
        </button>
        {edit && (
          <a className="card-edit" href={edit.href} target="_blank" rel="noopener noreferrer" aria-label={edit.label} title={edit.label}>
            <PencilIcon />
          </a>
        )}
      </h2>
      <div className="card-body" id={bodyId} hidden={!expanded}>
        {children}
      </div>
    </section>
  );
}

export interface CardViewText {
  /** Lower-case noun for messages: "allergies", "the care team". */
  noun: string;
  /** Sentence start for load failures: "Allergies". */
  subject: string;
  empty: ReactNode;
}

function errorMessage(error: DataSourceError, text: CardViewText): string | null {
  switch (error.kind) {
    case 'forbidden':
    case 'not_accessible':
      return `You don't have permission to view ${text.noun}.`;
    case 'session_expired':
    case 'unauthenticated':
      return null; // The auth layer ends the session and says so.
    default:
      return `${text.subject} could not be loaded.`;
  }
}

const TRANSIENT = new Set(['upstream', 'timeout', 'network']);

/** Loading / error / empty states shared by every card; `ready` renders `children`. */
export function CardView<T>({ view, retry, text, children }: { view: QueryView<T>; retry: () => void; text: CardViewText; children: (data: T) => ReactNode }) {
  switch (view.status) {
    case 'idle':
    case 'loading':
      return (
        <p className="card-state muted" role="status">
          Loading {text.noun}…
        </p>
      );
    case 'error': {
      const message = errorMessage(view.error, text);
      if (message === null) return null;
      return (
        <div className="card-state">
          <p className="card-error">{message}</p>
          {TRANSIENT.has(view.error.kind) && (
            <button type="button" className="btn btn-secondary btn-sm" onClick={retry}>
              Try again
            </button>
          )}
        </div>
      );
    }
    case 'empty':
      return <div className="card-state">{text.empty}</div>;
    case 'ready':
      return <>{children(view.data)}</>;
  }
}
