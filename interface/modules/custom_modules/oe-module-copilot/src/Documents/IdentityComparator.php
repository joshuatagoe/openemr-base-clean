<?php

/**
 * ADR-012 wrong-patient check, done in PHP so the printed identity never has to
 * be logged or kept: compares the name and DOB printed on a document with the
 * chart and returns only `match`, `mismatch` or `missing`.
 *
 * Rule: exact DOB and normalised last name -> match; either disagrees ->
 * mismatch (a disagreement wins over an absence); otherwise, either absent or
 * unreadable on either side -> missing.
 *
 * Normalisation: diacritics removed, case-folded, apostrophes and periods
 * dropped, every other non-alphanumeric character treated as a word break. The
 * chart last name matches when its words appear, in order and adjacent, in the
 * printed name - so "SMITH, JOHN", "John Smith" and "Dr. John A. Smith" all
 * match chart last name "Smith", and "Smith-Jones" matches "SMITH JONES".
 *
 * @package   OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Documents;

final class IdentityComparator
{
    public const MATCH = 'match';
    public const MISMATCH = 'mismatch';
    public const MISSING = 'missing';

    /**
     * @param string|null $printedName  as printed on the document, or null
     * @param string|null $printedDob   YYYY-MM-DD, or null
     * @param string $chartLastName     patient_data.lname
     * @param string $chartDob          patient_data.DOB (YYYY-MM-DD)
     */
    public static function compare(?string $printedName, ?string $printedDob, string $chartLastName, string $chartDob): string
    {
        $dob = self::compareDob($printedDob, $chartDob);
        $name = self::compareName($printedName, $chartLastName);
        if ($dob === false || $name === false) {
            return self::MISMATCH;
        }
        if ($dob === null || $name === null) {
            return self::MISSING;
        }
        return self::MATCH;
    }

    /** @return bool|null  null when either side is absent or unreadable */
    private static function compareDob(?string $printed, string $chart): ?bool
    {
        $p = self::isoDate($printed);
        $c = self::isoDate($chart);
        if ($p === null || $c === null) {
            return null;
        }
        return $p === $c;
    }

    private static function isoDate(?string $value): ?string
    {
        $value = trim((string) $value);
        if (!preg_match('/^(\d{4})-(\d{2})-(\d{2})/', $value, $m) || $m[1] === '0000') {
            return null;
        }
        return checkdate((int) $m[2], (int) $m[3], (int) $m[1]) ? "{$m[1]}-{$m[2]}-{$m[3]}" : null;
    }

    /** @return bool|null */
    private static function compareName(?string $printed, string $chartLast): ?bool
    {
        $p = self::words((string) $printed);
        $c = self::words($chartLast);
        if ($p === [] || $c === []) {
            return null;
        }
        $n = count($c);
        for ($i = 0; $i + $n <= count($p); $i++) {
            if (array_slice($p, $i, $n) === $c) {
                return true;
            }
        }
        return false;
    }

    /** @return list<string> */
    public static function words(string $name): array
    {
        $text = $name;
        if (class_exists(\Normalizer::class)) {
            $text = (string) \Normalizer::normalize($text, \Normalizer::FORM_D);
            $text = (string) preg_replace('/\p{Mn}+/u', '', $text);
        } else {
            $converted = @iconv('UTF-8', 'ASCII//TRANSLIT//IGNORE', $text);
            $text = $converted === false ? $text : $converted;
        }
        $text = mb_strtolower($text, 'UTF-8');
        $text = (string) preg_replace("/['\x{2019}.`]/u", '', $text);
        $text = (string) preg_replace('/[^\p{L}\p{N}]+/u', ' ', $text);
        $words = preg_split('/\s+/', trim($text), -1, PREG_SPLIT_NO_EMPTY);
        return $words === false ? [] : array_values($words);
    }
}
