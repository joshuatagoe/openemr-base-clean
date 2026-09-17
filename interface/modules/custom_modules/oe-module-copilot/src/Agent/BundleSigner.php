<?php

/**
 * HMAC signature over an outbound bundle body, byte-compatible with the
 * agent's `app/security.py::sign_body`:
 *
 *   X-Copilot-Timestamp: <unix seconds>
 *   X-Copilot-Signature: v1=hex(HMAC_SHA256(secret, "<timestamp>." . body))
 *
 * @package   OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Agent;

final class BundleSigner
{
    public const HEADER_TIMESTAMP = 'X-Copilot-Timestamp';
    public const HEADER_SIGNATURE = 'X-Copilot-Signature';
    public const VERSION = 'v1';

    public static function sign(string $secret, string $body, int $timestamp): string
    {
        return self::VERSION . '=' . hash_hmac('sha256', $timestamp . '.' . $body, $secret);
    }

    /** @return array<string,string> */
    public static function headers(string $secret, string $body, int $timestamp): array
    {
        return [
            self::HEADER_TIMESTAMP => (string) $timestamp,
            self::HEADER_SIGNATURE => self::sign($secret, $body, $timestamp),
        ];
    }
}
