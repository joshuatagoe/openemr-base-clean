<?php

/**
 * Turns a stored lab extraction (the agent's LabDocument JSON, contract C4)
 * into candidate rows for `copilot_extracted_value` (ADR-009). Pure.
 *
 * `result_index` is the position in `results`, so a candidate always points
 * back at the exact extracted result (and its citation) it came from. Values
 * outside the closed vocabularies become null (flags) or `unverified`
 * (verification status) - never guessed upward. A result without a test name
 * is skipped; a document whose `results` is not a list is malformed (null).
 *
 * @phpstan-import-type CandidateRow from ProcessingRepositoryInterface
 *
 * @package   OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Documents;

use OpenEMR\Modules\Copilot\Support\Scalar;

final class CandidateMapper
{
    public const ABNORMAL_FLAGS = ['H', 'L', 'HH', 'LL', 'A', 'N'];
    public const FLAG_SOURCES = ['extracted', 'derived', 'unavailable'];
    public const VERIFICATION_STATUSES = ['verified_exact', 'verified_fuzzy', 'unverified', 'unreadable'];

    /**
     * @param array<mixed> $extraction
     * @return list<CandidateRow>|null  null when the extraction is malformed
     */
    public static function fromExtraction(array $extraction): ?array
    {
        $results = $extraction['results'] ?? null;
        if (!is_array($results) || !array_is_list($results)) {
            return null;
        }
        $documentDate = self::date($extraction['collection_date'] ?? null);
        $out = [];
        foreach ($results as $index => $r) {
            if (!is_array($r)) {
                continue;
            }
            $testName = self::text($r['test_name'] ?? null, 255);
            if ($testName === null) {
                continue;
            }
            $citation = is_array($r['citation'] ?? null) ? $r['citation'] : [];
            $page = $citation['page'] ?? null;
            $flag = Scalar::str($r['abnormal_flag'] ?? null);
            $source = Scalar::str($r['abnormal_flag_source'] ?? null);
            $status = Scalar::str($r['verification_status'] ?? null);
            $out[] = [
                'result_index' => $index,
                'test_name' => $testName,
                'value_text' => self::text($r['value'] ?? null, 255),
                'unit' => self::text($r['unit'] ?? null, 50),
                'reference_range' => self::text($r['reference_range'] ?? null, 100),
                'abnormal_flag' => in_array($flag, self::ABNORMAL_FLAGS, true) ? $flag : null,
                'flag_source' => in_array($source, self::FLAG_SOURCES, true) ? $source : null,
                'collection_date' => self::date($r['collection_date'] ?? null) ?? $documentDate,
                'verification_status' => in_array($status, self::VERIFICATION_STATUSES, true) ? $status : 'unverified',
                'page' => is_int($page) && $page >= 1 && $page <= 32767 ? $page : null,
                'bbox' => self::bbox($citation['bbox'] ?? null),
            ];
        }
        return $out;
    }

    /** "x0,y0,x1,y1" (0-1, top-left origin) or null; the storage form of a citation box. */
    public static function bbox(mixed $box): ?string
    {
        if (!is_array($box) || count($box) !== 4 || !array_is_list($box)) {
            return null;
        }
        $nums = [];
        foreach ($box as $v) {
            if (!is_int($v) && !is_float($v)) {
                return null;
            }
            $nums[] = (float) $v;
        }
        [$x0, $y0, $x1, $y1] = $nums;
        if (!(0 <= $x0 && $x0 < $x1 && $x1 <= 1 && 0 <= $y0 && $y0 < $y1 && $y1 <= 1)) {
            return null;
        }
        return implode(',', array_map(static fn(float $v): string => rtrim(rtrim(number_format($v, 6, '.', ''), '0'), '.') ?: '0', $nums));
    }

    /** @return list<float>|null the stored box back as four numbers */
    public static function bboxToList(?string $stored): ?array
    {
        if ($stored === null || $stored === '') {
            return null;
        }
        $parts = explode(',', $stored);
        if (count($parts) !== 4) {
            return null;
        }
        $out = [];
        foreach ($parts as $p) {
            if (!is_numeric($p)) {
                return null;
            }
            $out[] = (float) $p;
        }
        return $out;
    }

    private static function text(mixed $value, int $max): ?string
    {
        if (!is_string($value) && !is_int($value) && !is_float($value)) {
            return null;
        }
        $text = trim((string) $value);
        return $text === '' ? null : mb_substr($text, 0, $max);
    }

    private static function date(mixed $value): ?string
    {
        if (!is_string($value) || !preg_match('/^(\d{4})-(\d{2})-(\d{2})$/', $value, $m)) {
            return null;
        }
        return checkdate((int) $m[2], (int) $m[3], (int) $m[1]) ? $value : null;
    }
}
