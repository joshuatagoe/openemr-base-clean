<?php

/**
 * `doc_type` from the OpenEMR document category, resolved by category *name*
 * (never by id, which differs between databases) - ADR-012.
 *
 *   "Lab Report"  -> lab_pdf
 *   "Intake Form" -> intake_form   (created by an administrator)
 *   anything else -> unsupported   (listed as "needs a category", never analysed)
 *
 * Names compare case-insensitively after trimming. A document filed under both
 * categories is ambiguous and treated as unsupported rather than guessed.
 *
 * @package   OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Documents;

final class DocType
{
    public const LAB_PDF = 'lab_pdf';
    public const INTAKE_FORM = 'intake_form';
    public const UNSUPPORTED = 'unsupported';

    public const CATEGORY_LAB_REPORT = 'Lab Report';
    public const CATEGORY_INTAKE_FORM = 'Intake Form';

    /** @param list<string> $categoryNames */
    public static function fromCategoryNames(array $categoryNames): string
    {
        $found = [];
        foreach ($categoryNames as $name) {
            $key = strtolower(trim($name));
            if ($key === strtolower(self::CATEGORY_LAB_REPORT)) {
                $found[self::LAB_PDF] = true;
            } elseif ($key === strtolower(self::CATEGORY_INTAKE_FORM)) {
                $found[self::INTAKE_FORM] = true;
            }
        }
        return count($found) === 1 ? (string) array_key_first($found) : self::UNSUPPORTED;
    }
}
