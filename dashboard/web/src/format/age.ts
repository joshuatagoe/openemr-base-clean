// Age text exactly as OpenEMR's patient bar builds it (default settings).

interface Ymd {
  y: number;
  m: number;
  d: number;
}

function parseYmd(value: string): Ymd {
  const digits = value.replace(/-/g, '');
  return { y: Number(digits.slice(0, 4)), m: Number(digits.slice(4, 6)), d: Number(digits.slice(6, 8)) };
}

/**
 * PatientService::getPatientAge (used by getPatientAgeDisplay with the default
 * `age_display_format` = 0): whole years when older than 24 full months,
 * otherwise "<n> month" (singular, as the PHP prints it).
 * `age_display_format` = 1 ("#y #m #d" below a limit) is not supported.
 */
export function patientAgeDisplay(dobYmd: string | undefined, todayYmd: string): string {
  if (!dobYmd) return '';
  const dob = parseYmd(dobYmd);
  const now = parseYmd(todayYmd);
  const dayDiff = now.d - dob.d;
  const monthDiff = now.m - dob.m;
  let months = now.y * 12 + now.m - (dob.y * 12 + dob.m);
  if (dayDiff < 0) months -= 1;
  if (months > 24) {
    let years = now.y - dob.y;
    if ((monthDiff === 0 && dayDiff < 0) || monthDiff < 0) years -= 1;
    return String(years);
  }
  return `${months} month`;
}

/**
 * oeFormatAge($dob, $dateOfDeath) with format 0, used for "Age at death":
 * years from 24 full months (>= 24), otherwise months. The PHP has an operator
 * precedence slip that prints "11months" with no space; we print "11 months"
 * ("1 month") and document the difference.
 */
export function ageAtDeathDisplay(dobYmd: string, deathYmd: string): string {
  const dob = parseYmd(dobYmd);
  const death = parseYmd(deathYmd);
  const dayDiff = death.d - dob.d;
  const monthDiff = death.m - dob.m;
  const yearDiff = death.y - dob.y;
  let months = yearDiff * 12 + monthDiff;
  if (dayDiff < 0) months -= 1;
  if (months >= 24) {
    let years = yearDiff;
    if (monthDiff < 0 || (monthDiff === 0 && dayDiff < 0)) years -= 1;
    return String(years);
  }
  return `${months} ${months === 1 ? 'month' : 'months'}`;
}
