<?php

/**
 * Mints the briefing ticket: a compact HS256 JWT bound to user, patient,
 * bundle and correlation id, single-use (`jti`), short-lived (`exp`).
 * Byte-compatible with the agent's `app/security.py::mint_ticket`; the agent
 * verifies, never mints. Claim order and encoding are fixed so the two
 * implementations can be checked against shared test vectors.
 *
 * @package   OpenEMR
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot\Ticket;

use JsonException;
use RuntimeException;

final class TicketSigner
{
    private const HEADER = '{"alg":"HS256","typ":"JWT"}';

    public function __construct(private readonly string $secret)
    {
    }

    /**
     * @param int $issuedAt unix seconds
     */
    public function mint(
        string $userUuid,
        string $patientUuid,
        string $bundleId,
        string $correlationId,
        string $jti,
        int $issuedAt,
        int $ttlSeconds,
    ): string {
        if ($ttlSeconds <= 0) {
            throw new RuntimeException('ticket ttl must be positive');
        }
        $claims = [
            'sub' => $userUuid,
            'puuid' => $patientUuid,
            'bundle_id' => $bundleId,
            'cid' => $correlationId,
            'jti' => $jti,
            'iat' => $issuedAt,
            'exp' => $issuedAt + $ttlSeconds,
        ];
        try {
            $payload = json_encode($claims, JSON_THROW_ON_ERROR | JSON_UNESCAPED_SLASHES);
        } catch (JsonException $e) {
            throw new RuntimeException('ticket claims could not be encoded', 0, $e);
        }
        $signingInput = self::b64url(self::HEADER) . '.' . self::b64url($payload);
        $signature = hash_hmac('sha256', $signingInput, $this->secret, true);
        return $signingInput . '.' . self::b64url($signature);
    }

    private static function b64url(string $raw): string
    {
        return rtrim(strtr(base64_encode($raw), '+/', '-_'), '=');
    }
}
