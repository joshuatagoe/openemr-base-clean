import type { Transport } from './data/DataSource';
import { parseDateDisplayFormat, type DateDisplayFormat } from './format/date';

/**
 * OpenEMR's `date_display_format` global, set at build time with
 * VITE_DATE_DISPLAY_FORMAT (0 = YYYY-MM-DD, 1 = MM/DD/YYYY, 2 = DD/MM/YYYY).
 * Default 0: OpenEMR's default and what the dev stack uses.
 */
export const DATE_DISPLAY_FORMAT: DateDisplayFormat = parseDateDisplayFormat(import.meta.env.VITE_DATE_DISPLAY_FORMAT as string | undefined);

/**
 * OpenEMR's web origin for the cards' edit links (VITE_OPENEMR_WEB_URL, e.g.
 * https://emr.example.org). Only http(s) URLs are accepted; when unset or
 * invalid the cards show no edit links.
 */
export function parseWebUrl(raw: string | undefined): string | undefined {
  if (!raw) return undefined;
  try {
    const u = new URL(raw.trim());
    if (u.protocol !== 'https:' && u.protocol !== 'http:') return undefined;
    return `${u.origin}${u.pathname}`.replace(/\/+$/, '');
  } catch {
    return undefined;
  }
}

export const OPENEMR_WEB_URL: string | undefined = parseWebUrl(import.meta.env.VITE_OPENEMR_WEB_URL as string | undefined);

/**
 * Which transport the build uses (VITE_TRANSPORT): 'smart' for modes B/C (the
 * build served by OpenEMR; `vite build --mode smart` sets it), otherwise 'bff'
 * (mode A).
 */
export function parseTransport(raw: string | undefined): Transport {
  return raw?.trim().toLowerCase() === 'smart' ? 'smart' : 'bff';
}

export const TRANSPORT: Transport = parseTransport(import.meta.env.VITE_TRANSPORT as string | undefined);

/**
 * The SMART client config (`{ "clientId": "..." }`) sits next to the app's
 * folder, not inside it, so a rebuild (which empties the folder) keeps it. It is
 * written once the client is registered (dev: the registration script; image:
 * the start script, from DASHBOARD_SMART_CLIENT_ID).
 */
export function smartConfigUrl(baseUrl: string, origin: string): string {
  return new URL('../dashboard.config.json', new URL(baseUrl, origin)).toString();
}
