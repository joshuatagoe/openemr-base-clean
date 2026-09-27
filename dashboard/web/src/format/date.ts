// Mirrors DateFormatterUtils::oeFormatShortDate and OpenEMR's
// `date_display_format` global: 0 = YYYY-MM-DD (OpenEMR default and the dev
// stack's setting), 1 = MM/DD/YYYY, 2 = DD/MM/YYYY.
export type DateDisplayFormat = 0 | 1 | 2;

export function parseDateDisplayFormat(raw: string | undefined): DateDisplayFormat {
  return raw === '1' ? 1 : raw === '2' ? 2 : 0;
}

/** `ymd` is YYYY-MM-DD (anything after the 10th character is ignored). Shorter input is returned unchanged, as in PHP. */
export function formatShortDate(ymd: string | undefined, format: DateDisplayFormat): string {
  if (!ymd) return '';
  if (ymd.length < 10) return ymd;
  const year = ymd.slice(0, 4);
  const month = ymd.slice(5, 7);
  const day = ymd.slice(8, 10);
  if (format === 1) return `${month}/${day}/${year}`;
  if (format === 2) return `${day}/${month}/${year}`;
  return `${year}-${month}-${day}`;
}

/** Today in the browser's local time zone, as YYYY-MM-DD. */
export function localToday(now: Date = new Date()): string {
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
}

/**
 * OpenEMR's FHIR writes local timestamps as the server's wall-clock time plus
 * its UTC offset (UtilsService::getLocalDateAsUTC), so the text before the
 * offset is exactly the stored value that PHP cards print raw
 * (`YYYY-MM-DD HH:MM:SS`, or the date alone).
 */
export function openemrWallClock(value: string | undefined): string {
  if (!value) return '';
  const m = /^(\d{4}-\d{2}-\d{2})(?:T(\d{2}:\d{2}:\d{2}))?/.exec(value);
  if (!m) return value;
  return m[2] ? `${m[1]} ${m[2]}` : (m[1] as string);
}
