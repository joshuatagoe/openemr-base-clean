<?php

/**
 * Read what a route needs from the OpenEMR session, then release the session
 * lock before any slow work.
 *
 * Local API routes run with a writable PHP session (SiteSetupListener sets
 * `$sessionAllowWrite = true`), and PHP's file session handler holds an
 * exclusive lock on the session until it is written and closed. A module route
 * that waits on the agent (or reads a large document) with the session open
 * would freeze every other OpenEMR page the user opens meanwhile.
 *
 * The native session is written and closed (`session_write_close()`), which
 * releases the lock and keeps what is already in it. The Symfony session
 * object is deliberately left as it is: after the response is sent, the
 * kernel's SessionCleanupListener still reads it (`has()`), and a Symfony
 * `save()` here would make that read try to start the session again after the
 * headers were sent - an error appended to the response body (seen on the dev
 * stack). The module never writes to the session after this point.
 *
 * @package   OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Support;

use Closure;
use Symfony\Component\HttpFoundation\Session\SessionInterface;

final class SessionRelease
{
    /** @var (Closure(): void)|null  test seam; production closes the native session */
    private static ?Closure $closer = null;

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
        if (self::$closer !== null) {
            (self::$closer)();
        } elseif (session_status() === PHP_SESSION_ACTIVE) {
            session_write_close();
        }
        return $values;
    }

    /** @param (Closure(): void)|null $closer  replaces the native close; null restores it */
    public static function useCloser(?Closure $closer): void
    {
        self::$closer = $closer;
    }
}
