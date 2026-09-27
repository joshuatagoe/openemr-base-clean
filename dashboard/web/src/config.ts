import { parseDateDisplayFormat, type DateDisplayFormat } from './format/date';

/**
 * OpenEMR's `date_display_format` global, set at build time with
 * VITE_DATE_DISPLAY_FORMAT (0 = YYYY-MM-DD, 1 = MM/DD/YYYY, 2 = DD/MM/YYYY).
 * Default 0: OpenEMR's default and what the dev stack uses.
 */
export const DATE_DISPLAY_FORMAT: DateDisplayFormat = parseDateDisplayFormat(import.meta.env.VITE_DATE_DISPLAY_FORMAT as string | undefined);
