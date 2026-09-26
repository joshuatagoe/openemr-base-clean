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

    public const LABEL_CHIEF_CONCERN = 'Chief concern';
    public const LABEL_MEDICATION = 'Current medication';
    public const LABEL_MEDICATIONS_NONE = 'Current medications (none reported)';
    public const LABEL_ALLERGY = 'Allergy';
    public const LABEL_ALLERGIES_NONE = 'Allergies (none reported)';
    public const LABEL_FAMILY_HISTORY = 'Family history';
    public const LABEL_FAMILY_HISTORY_NONE = 'Family history (none reported)';

    /**
     * An intake form (the agent's IntakeForm JSON, ADR-010) as candidate rows:
     * one per reported item, in the agent's fixed order (`app.intake.intake_items`):
     * chief concern, medications, allergies, family history, each section's
     * written "none" after its items. `test_name` is the item label, `value_text`
     * the item as written (null when unreadable and nothing legible remains).
     * These rows are patient-reported evidence and never fileable (ValueFiler).
     * Demographics are not items: the name and DOB are an identity check.
     *
     * @param array<mixed> $extraction
     * @return list<CandidateRow>|null  null when the extraction is malformed
     */
    public static function fromIntake(array $extraction): ?array
    {
        $sections = [];
        foreach (['current_medications', 'allergies', 'family_history'] as $key) {
            $list = $extraction[$key] ?? [];
            if (!is_array($list) || !array_is_list($list)) {
                return null;
            }
            $sections[$key] = $list;
        }
        $items = [];
        $single = static function (mixed $field, string $label) use (&$items): void {
            if (is_array($field)) {
                $items[] = [$label, $field['value'] ?? null, $field];
            }
        };
        $single($extraction['chief_concern'] ?? null, self::LABEL_CHIEF_CONCERN);
        foreach ($sections['current_medications'] as $m) {
            if (is_array($m)) {
                $items[] = [self::LABEL_MEDICATION, self::join([$m['name'] ?? null, $m['dose'] ?? null, $m['frequency'] ?? null], ' '), $m];
            }
        }
        $single($extraction['medications_none_stated'] ?? null, self::LABEL_MEDICATIONS_NONE);
        foreach ($sections['allergies'] as $a) {
            if (is_array($a)) {
                $reaction = self::text($a['reaction'] ?? null, 255);
                $items[] = [self::LABEL_ALLERGY, self::join([$a['substance'] ?? null, $reaction === null ? null : "reaction: {$reaction}"], '; '), $a];
            }
        }
        $single($extraction['allergies_none_stated'] ?? null, self::LABEL_ALLERGIES_NONE);
        foreach ($sections['family_history'] as $f) {
            if (is_array($f)) {
                $relation = self::text($f['relation'] ?? null, 255);
                $condition = self::text($f['condition'] ?? null, 255);
                $text = $relation !== null && $condition !== null ? "{$relation}: {$condition}" : self::join([$relation, $condition], ' ');
                $items[] = [self::LABEL_FAMILY_HISTORY, $text, $f];
            }
        }
        $single($extraction['family_history_none_stated'] ?? null, self::LABEL_FAMILY_HISTORY_NONE);

        $out = [];
        foreach ($items as $index => [$label, $value, $item]) {
            $citation = is_array($item['citation'] ?? null) ? $item['citation'] : [];
            $page = $citation['page'] ?? null;
            $status = Scalar::str($item['verification_status'] ?? null);
            $status = in_array($status, self::VERIFICATION_STATUSES, true) ? $status : 'unverified';
            $verified = $status === 'verified_exact' || $status === 'verified_fuzzy';
            $out[] = [
                'result_index' => $index,
                'test_name' => $label,
                'value_text' => self::text($value, 255),
                'unit' => null,
                'reference_range' => null,
                'abnormal_flag' => null,
                'flag_source' => null,
                'collection_date' => null,
                'verification_status' => $status,
                'page' => is_int($page) && $page >= 1 && $page <= 32767 ? $page : null,
                // Only a verified item carries a box (the agent's own invariant, re-checked here).
                'bbox' => $verified ? self::bbox($citation['bbox'] ?? null) : null,
            ];
        }
        return $out;
    }

    /**
     * Every citation in an intake extraction (demographics and items), for the attribution check.
     *
     * @param array<mixed> $extraction
     * @return list<array<mixed>>
     */
    public static function intakeCitations(array $extraction): array
    {
        $out = [];
        $demographics = is_array($extraction['demographics'] ?? null) ? $extraction['demographics'] : [];
        $fields = array_merge(
            array_values($demographics),
            [$extraction['chief_concern'] ?? null, $extraction['medications_none_stated'] ?? null, $extraction['allergies_none_stated'] ?? null, $extraction['family_history_none_stated'] ?? null],
        );
        foreach (['current_medications', 'allergies', 'family_history'] as $key) {
            foreach (is_array($extraction[$key] ?? null) ? $extraction[$key] : [] as $item) {
                $fields[] = $item;
            }
        }
        foreach ($fields as $f) {
            if (is_array($f) && is_array($f['citation'] ?? null)) {
                $out[] = $f['citation'];
            }
        }
        return $out;
    }

    /** @param list<mixed> $parts */
    private static function join(array $parts, string $separator): ?string
    {
        $kept = [];
        foreach ($parts as $p) {
            $t = self::text($p, 255);
            if ($t !== null) {
                $kept[] = $t;
            }
        }
        return $kept === [] ? null : implode($separator, $kept);
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
