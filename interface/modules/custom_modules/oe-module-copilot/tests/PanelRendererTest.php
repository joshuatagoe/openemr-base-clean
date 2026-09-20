<?php

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Tests;

use OpenEMR\Modules\Copilot\Panel\PanelRenderer;
use PHPUnit\Framework\TestCase;

/**
 * The panel mount point: escaped attributes, one external script, no inline
 * script, no data access (AUDIT ARCH-005, SEC-005).
 */
final class PanelRendererTest extends TestCase
{
    public function testRendersContainerWithEscapedAttributesAndExternalScriptOnly(): void
    {
        $html = (new PanelRenderer())->render(7, 'tok"><script>alert(1)</script>', '/apis/default/api/copilot/briefing-ticket', '/interface/modules/custom_modules/oe-module-copilot/public/copilot-panel.js');

        self::assertStringContainsString('id="oe-copilot-panel"', $html);
        self::assertStringContainsString('data-pid="7"', $html);
        self::assertStringContainsString('data-ticket-url="/apis/default/api/copilot/briefing-ticket"', $html);
        self::assertStringContainsString('data-csrf="tok&quot;&gt;&lt;script&gt;alert(1)&lt;/script&gt;"', $html);
        self::assertStringNotContainsString('<script>alert', $html);
        self::assertStringContainsString('<script src="/interface/modules/custom_modules/oe-module-copilot/public/copilot-panel.js" defer></script>', $html);
        // Exactly one script tag, and it has a src (no inline code).
        self::assertSame(1, substr_count($html, '<script'));
        self::assertMatchesRegularExpression('/<script src="[^"]+" defer><\/script>$/', $html);
        self::assertStringContainsString('aria-busy="true"', $html);
    }

    public function testNonPositivePidRendersNothing(): void
    {
        self::assertSame('', (new PanelRenderer())->render(0, 't', '/u', '/s'));
        self::assertSame('', (new PanelRenderer())->render(-3, 't', '/u', '/s'));
    }

    public function testPanelScriptVersionIsTheContentHashSoDeploysBustTheCache(): void
    {
        $version = \OpenEMR\Modules\Copilot\Bootstrap::panelScriptVersion();
        self::assertMatchesRegularExpression('/^[0-9a-f]{8}$/', $version);
        self::assertSame(hash_file('crc32b', __DIR__ . '/../public/copilot-panel.js'), $version);
    }
}
