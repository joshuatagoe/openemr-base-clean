<?php

/**
 * Read what a route needs from the OpenEMR session, then release the session
 * lock before any slow work.
 *
 * Local API routes run with a writable PHP session (SiteSetupListener sets
 * `$sessionAllowWrite = true`), and PHP's file session handler holds an
 * exclusive lock on the session until it is written and closed. A module route
 * that waits on the agent (or reads a large document) with the session open
 * would freeze every other OpenEMR page the user opens meanwhile. The routes
 * only read the session, so it is saved (written and closed) right after the
 * read; nothing in the module writes to it afterwards.
 *
 * @package   OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Support;

use Symfony\Component\HttpFoundation\Session\SessionInterface;

final class SessionRelease
{
    /**
     * @param list<string> $keys
     * @return array<string, mixed>  each key's value (null when absent)
     */
    public static function readAndRelease(SessionInterface $session, array $keys = ['authUserID', 'authUser', 'pid']): array
    {
        $values = [];
        foreach ($keys as $key) {
            $values[$key] = $session->get($key);
        }
        if ($session->isStarted()) {
            $session->save();
        }
        return $values;
    }
}
