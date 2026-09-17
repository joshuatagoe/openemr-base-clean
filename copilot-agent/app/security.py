"""Trust-boundary primitives for the agent service (ARCHITECTURE.md sections 5 and 10).

Two credentials exist, both derived from one shared secret (``COPILOT_TICKET_SECRET``)
that the OpenEMR module and this service hold and nothing else does:

* **Bundle signature** (module -> agent, server to server). ``POST /v1/bundles``
  carries ``X-Copilot-Timestamp`` and ``X-Copilot-Signature: v1=<hex>`` where the
  hex is HMAC-SHA256 over ``"<timestamp>.<raw body>"``. The timestamp bounds
  replay to a small skew window.

* **Briefing ticket** (module -> panel -> agent). A compact HS256 JWT minted by
  the module after it has authorized the physician: ``{sub, puuid, bundle_id,
  cid, jti, iat, exp}``. The agent verifies the signature, the expiry, that the
  ``jti`` has not been used, and that ``bundle_id``/``puuid`` match the stored
  bundle. The agent never mints tickets; it only verifies them.

No third-party JWT library: the token is fixed to ``alg=HS256`` and the
verifier rejects anything else, so the classic algorithm-confusion attacks do
not apply. Comparisons are constant time.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any
from uuid import UUID

from pydantic import Field, ValidationError, field_validator

from app.contracts import StrictModel

SIGNATURE_HEADER = "X-Copilot-Signature"
TIMESTAMP_HEADER = "X-Copilot-Timestamp"
SIGNATURE_VERSION = "v1"

JWT_HEADER = {"alg": "HS256", "typ": "JWT"}


class SecurityError(Exception):
    """Base class. Messages are fixed strings suitable for a response body."""

    code: str = "unauthorized"


class InvalidSignatureError(SecurityError):
    code = "invalid_signature"


class StaleSignatureError(SecurityError):
    code = "stale_signature"


class InvalidTicketError(SecurityError):
    code = "invalid_ticket"


class ExpiredTicketError(SecurityError):
    code = "expired_ticket"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _hmac_sha256(secret: str, message: bytes) -> bytes:
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).digest()


# --------------------------------------------------------------------------- #
# Bundle signature (server to server)
# --------------------------------------------------------------------------- #


def sign_body(secret: str, body: bytes, timestamp: int) -> str:
    """Return the ``X-Copilot-Signature`` header value for ``body`` at ``timestamp``.

    Exposed so tests and the module's PHP implementation can be checked against
    the same reference: ``v1=hex(HMAC_SHA256(secret, f"{timestamp}." + body))``.
    """
    digest = _hmac_sha256(secret, f"{timestamp}.".encode("ascii") + body)
    return f"{SIGNATURE_VERSION}={digest.hex()}"


def verify_body_signature(
    secret: str,
    body: bytes,
    timestamp_header: str | None,
    signature_header: str | None,
    *,
    max_skew_seconds: int,
    now: float | None = None,
) -> int:
    """Verify a signed bundle post. Returns the accepted timestamp.

    Raises ``StaleSignatureError`` when the timestamp is outside the skew
    window and ``InvalidSignatureError`` for anything else that does not
    verify. The order matters: a bad signature is reported even when the
    timestamp is stale, so an attacker learns nothing from the distinction.
    """
    if not timestamp_header or not signature_header:
        raise InvalidSignatureError("missing signature headers")
    try:
        timestamp = int(timestamp_header)
    except ValueError:
        raise InvalidSignatureError("malformed signature timestamp") from None

    expected = sign_body(secret, body, timestamp)
    if not hmac.compare_digest(expected, signature_header.strip()):
        raise InvalidSignatureError("signature does not verify")

    current = time.time() if now is None else now
    if abs(current - timestamp) > max_skew_seconds:
        raise StaleSignatureError("signature timestamp outside the accepted window")
    return timestamp


# --------------------------------------------------------------------------- #
# Briefing ticket
# --------------------------------------------------------------------------- #


class TicketClaims(StrictModel):
    """Verified claims of a briefing ticket. Field names follow the JWT the module mints."""

    sub: str = Field(min_length=1, description="Authorized user's uuid (opaque to the agent).")
    puuid: UUID
    bundle_id: UUID
    cid: UUID
    jti: UUID
    iat: int = Field(ge=0)
    exp: int = Field(ge=0)

    @field_validator("exp")
    @classmethod
    def _exp_after_iat(cls, exp: int, info: Any) -> int:
        iat = info.data.get("iat")
        if iat is not None and exp <= iat:
            raise ValueError("exp must be after iat")
        return exp


def mint_ticket(secret: str, claims: TicketClaims) -> str:
    """Produce a ticket. The agent does not call this in production; the
    module does. It lives here so the verifier and the reference minting share
    one definition and the tests can produce tickets without a second library."""
    header = _b64url_encode(json.dumps(JWT_HEADER, separators=(",", ":")).encode("utf-8"))
    payload = _b64url_encode(claims.model_dump_json().encode("utf-8"))
    signing_input = f"{header}.{payload}".encode("ascii")
    signature = _b64url_encode(_hmac_sha256(secret, signing_input))
    return f"{header}.{payload}.{signature}"


def verify_ticket(secret: str, token: str, *, now: float | None = None, check_expiry: bool = True) -> TicketClaims:
    """Verify signature and structure, then expiry. Never returns claims from an unsigned token."""
    parts = token.strip().split(".")
    if len(parts) != 3 or not all(parts):
        raise InvalidTicketError("malformed ticket")
    header_b64, payload_b64, signature_b64 = parts

    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    expected = _hmac_sha256(secret, signing_input)
    try:
        provided = _b64url_decode(signature_b64)
    except (ValueError, TypeError):
        raise InvalidTicketError("malformed ticket signature") from None
    if not hmac.compare_digest(expected, provided):
        raise InvalidTicketError("ticket signature does not verify")

    try:
        header = json.loads(_b64url_decode(header_b64))
        payload = json.loads(_b64url_decode(payload_b64))
    except (ValueError, TypeError):
        raise InvalidTicketError("malformed ticket encoding") from None
    if not isinstance(header, dict) or header.get("alg") != "HS256":
        raise InvalidTicketError("unsupported ticket algorithm")
    try:
        claims = TicketClaims.model_validate(payload)
    except ValidationError:
        raise InvalidTicketError("ticket claims failed validation") from None

    if check_expiry:
        current = time.time() if now is None else now
        if current >= claims.exp:
            raise ExpiredTicketError("ticket has expired")
    return claims


def parse_bearer(authorization: str | None) -> str:
    """Extract the token from an ``Authorization: Bearer <token>`` header."""
    if not authorization:
        raise InvalidTicketError("missing ticket")
    scheme, _, token = authorization.strip().partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise InvalidTicketError("missing ticket")
    return token.strip()


__all__ = [
    "JWT_HEADER",
    "SIGNATURE_HEADER",
    "SIGNATURE_VERSION",
    "TIMESTAMP_HEADER",
    "ExpiredTicketError",
    "InvalidSignatureError",
    "InvalidTicketError",
    "SecurityError",
    "StaleSignatureError",
    "TicketClaims",
    "mint_ticket",
    "parse_bearer",
    "sign_body",
    "verify_body_signature",
    "verify_ticket",
]
