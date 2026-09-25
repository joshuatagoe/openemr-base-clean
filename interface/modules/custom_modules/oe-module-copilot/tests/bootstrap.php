<?php

/**
 * PHPUnit bootstrap for the module's isolated tests (no database, no session).
 * Run from the repository root inside the development container:
 *   php vendor/bin/phpunit --bootstrap interface/modules/custom_modules/oe-module-copilot/tests/bootstrap.php \
 *       interface/modules/custom_modules/oe-module-copilot/tests
 */

declare(strict_types=1);

$root = dirname(__DIR__, 5);
/** @var \Composer\Autoload\ClassLoader $loader */
$loader = require $root . '/vendor/autoload.php';
$loader->addPsr4('OpenEMR\\Modules\\Copilot\\', __DIR__ . '/../src');
$loader->addPsr4('OpenEMR\\Modules\\Copilot\\Tests\\', __DIR__);
require_once __DIR__ . '/Fakes.php'; // several small doubles in one file; not PSR-4 discoverable
require_once __DIR__ . '/DocumentFakes.php'; // Week 2 Final document-processing doubles, same reason
require_once __DIR__ . '/FilingFakes.php'; // Week 2 Final file route + filing doubles, same reason
