<?php

/**
 * Renders the Co-Pilot panel mount point on the Patient Summary
 * (ARCHITECTURE.md section 4). The output is a container with data
 * attributes and one external script tag - no inline script, no data access
 * (AUDIT ARCH-005). Everything the panel needs to start (pid, API CSRF token,
 * ticket endpoint, asset URL) travels as escaped attributes; the panel JS
 * reads them with `dataset` and renders all text with `textContent`.
 *
 * @package   OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Panel;

final class PanelRenderer
{
    public const CONTAINER_ID = 'oe-copilot-panel';

    /**
     * @param int    $pid          the patient the page was rendered for (staleness check only; never the binding)
     * @param string $apiCsrfToken the session's API CSRF token (APICSRFTOKEN header)
     * @param string $ticketUrl    absolute path of the briefing-ticket route
     * @param string $scriptUrl    absolute path of the panel script
     */
    public function render(int $pid, string $apiCsrfToken, string $ticketUrl, string $scriptUrl): string
    {
        if ($pid <= 0) {
            return '';
        }
        return '<div id="' . self::CONTAINER_ID . '" class="card mb-3"'
            . ' data-pid="' . self::attr((string) $pid) . '"'
            . ' data-csrf="' . self::attr($apiCsrfToken) . '"'
            . ' data-ticket-url="' . self::attr($ticketUrl) . '"'
            . ' role="region" aria-live="polite" aria-busy="true"'
            . ' aria-label="' . self::attr('Clinical Co-Pilot') . '">'
            . '<div class="card-header"><strong>' . self::attr('Clinical Co-Pilot') . '</strong>'
            . ' <span class="badge badge-secondary" data-role="status">' . self::attr('loading') . '</span></div>'
            . '<div class="card-body" data-role="body"><p class="text-muted mb-0">' . self::attr('Preparing pre-visit briefing…') . '</p></div>'
            . '</div>'
            . '<script src="' . self::attr($scriptUrl) . '" defer></script>';
    }

    private static function attr(string $value): string
    {
        return htmlspecialchars($value, ENT_QUOTES | ENT_HTML5 | ENT_SUBSTITUTE, 'UTF-8');
    }
}
