<?php

/**
 * Whether the module's own tables exist (ADR-009 section 6). Document processing,
 * listing and filing refuse with CODE_NOT_INSTALLED until they do, so a failed
 * migration at container start disables those features instead of breaking OpenEMR.
 *
 * @package   OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Data;

interface SchemaStatusInterface
{
    public const CODE_NOT_INSTALLED = 'copilot_tables_not_installed';

    public const TABLES = ['copilot_document', 'copilot_extracted_value'];

    public function isReady(): bool;
}
