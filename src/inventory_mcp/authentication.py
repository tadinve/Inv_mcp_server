"""Transport-level authentication: turns a bearer token into a verified Principal.

Two authenticators share one interface:
  - GoogleOIDCAuthenticator: verifies Google-signed OIDC ID tokens in-process
    (SPEC-AMENDMENT-1, A1). The deployed mode.
  - DevAuthenticator: TEST-ONLY, refused on Cloud Run and on non-loopback binds.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

import anyio.to_thread
import httpx2
from google.auth import exceptions as google_exceptions
from google.auth import jwt as google_jwt
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger("inventory_mcp.auth")

PRINCIPAL_SCOPE_KEY = "inventory_mcp.principal"
CORRELATION_SCOPE_KEY = "inventory_mcp.correlation_id"


@dataclass(frozen=True)
class Principal:
    subject: str  # verified, stable identifier; the only authorization input
    label: str  # human-readable, for logs only


class AuthenticationError(Exception):
    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


class Authenticator(Protocol):
    def authenticate(self, token: str) -> Principal: ...


_DEV_TOKEN = re.compile(r"^dev:([a-z0-9][a-z0-9-]{0,62})$")


class DevAuthenticator:
    """TEST-ONLY. The token `dev:<name>` authenticates as subject `dev:<name>`.

    Any local caller can claim any dev identity; this exists so the MCP surface
    can be exercised without Google credentials. Permissions still come only
    from the server-side policy, so a dev token cannot grant itself permissions.
    """

    def authenticate(self, token: str) -> Principal:
        match = _DEV_TOKEN.fullmatch(token)
        if match is None:
            raise AuthenticationError("malformed_dev_token")
        return Principal(subject=f"dev:{match.group(1)}", label=f"dev-{match.group(1)}")


GOOGLE_ISSUERS = frozenset({"https://accounts.google.com", "accounts.google.com"})
GOOGLE_CERTS_URL = "https://www.googleapis.com/oauth2/v1/certs"  # kid -> PEM x509, used for ID tokens
ALLOWED_ALGORITHMS = frozenset({"RS256"})  # Google ID tokens are RS256; never "none" or HS*
MAX_TOKEN_BYTES = 8192

CertsFetcher = Callable[[], tuple[Mapping[str, str], float]]
"""Returns (kid -> PEM certificate or public key, seconds the result may be cached)."""


def fetch_google_certs(url: str = GOOGLE_CERTS_URL) -> tuple[Mapping[str, str], float]:
    response = httpx2.get(url, timeout=5.0)
    response.raise_for_status()
    certs = response.json()
    if not isinstance(certs, dict) or not certs:
        raise ValueError("unexpected certificate document")
    max_age = 3600.0
    match = re.search(r"max-age=(\d+)", response.headers.get("cache-control", ""))
    if match:
        max_age = float(match.group(1))
    return certs, max_age


class CertificateCache:
    """Thread-safe cache of Google's signing certificates.

    Honors Cache-Control max-age (clamped). An unknown `kid` triggers at most one
    forced refresh per `min_refresh_interval`, which handles key rotation without
    letting garbage tokens turn into a request flood against Google.
    """

    def __init__(
        self,
        fetcher: CertsFetcher = fetch_google_certs,
        *,
        clock: Callable[[], float] = time.monotonic,
        min_ttl: float = 300.0,
        max_ttl: float = 86_400.0,
        min_refresh_interval: float = 60.0,
    ) -> None:
        self._fetcher = fetcher
        self._clock = clock
        self._min_ttl, self._max_ttl = min_ttl, max_ttl
        self._min_refresh_interval = min_refresh_interval
        self._lock = threading.Lock()
        self._certs: Mapping[str, str] = {}
        self._expires_at = 0.0
        self._last_fetch = float("-inf")

    def _refresh_locked(self) -> None:
        certs, max_age = self._fetcher()
        now = self._clock()
        self._certs = dict(certs)
        self._expires_at = now + min(max(max_age, self._min_ttl), self._max_ttl)
        self._last_fetch = now

    def get(self, kid: str) -> str | None:
        with self._lock:
            now = self._clock()
            if now >= self._expires_at:
                self._refresh_locked()
            elif kid not in self._certs and now - self._last_fetch >= self._min_refresh_interval:
                self._refresh_locked()  # possible key rotation
            return self._certs.get(kid)


class GoogleOIDCAuthenticator:
    """Verifies a Google-signed OIDC ID token and returns its verified subject.

    Checks, in order: size and structure; header alg is RS256 and a kid is present;
    signature against Google's published certificate for that kid; iat/exp (with
    small clock skew); issuer; audience (exact match with the configured value);
    non-empty string `sub`. Email is used only as a log label, and only when
    `email_verified` is true. Authorization uses `sub` exclusively.
    """

    def __init__(
        self,
        audience: str,
        certificates: CertificateCache,
        *,
        issuers: frozenset[str] = GOOGLE_ISSUERS,
        clock_skew_seconds: int = 30,
    ) -> None:
        if not audience:
            raise ValueError("audience is required")
        self._audience = audience
        self._certificates = certificates
        self._issuers = issuers
        self._clock_skew = clock_skew_seconds

    def authenticate(self, token: str) -> Principal:
        if len(token) > MAX_TOKEN_BYTES or token.count(".") != 2:
            raise AuthenticationError("malformed_token")
        try:
            header = google_jwt.decode_header(token)
        except (ValueError, google_exceptions.GoogleAuthError):
            raise AuthenticationError("malformed_token") from None
        if header.get("alg") not in ALLOWED_ALGORITHMS:
            raise AuthenticationError("disallowed_algorithm")
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise AuthenticationError("missing_key_id")

        try:
            cert = self._certificates.get(kid)
        except Exception:
            logger.exception("could not fetch Google signing certificates")
            raise AuthenticationError("certificates_unavailable") from None  # fail closed
        if cert is None:
            raise AuthenticationError("unknown_key_id")

        try:
            # audience=None here: audience and issuer are checked explicitly below.
            claims = google_jwt.decode(token, certs={kid: cert}, audience=None, clock_skew_in_seconds=self._clock_skew)
        except (ValueError, google_exceptions.GoogleAuthError) as exc:
            raise AuthenticationError(_classify(str(exc))) from None

        if claims.get("iss") not in self._issuers:
            raise AuthenticationError("wrong_issuer")
        if claims.get("aud") != self._audience:
            raise AuthenticationError("wrong_audience")
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject.strip():
            raise AuthenticationError("missing_subject")

        email = claims.get("email")
        label = email if isinstance(email, str) and claims.get("email_verified") is True else f"sub:{subject}"
        return Principal(subject=subject, label=label)


def _classify(message: str) -> str:
    lowered = message.lower()
    if "expired" in lowered:
        return "expired"
    if "too early" in lowered:
        return "not_yet_valid"
    if "signature" in lowered:
        return "invalid_signature"
    if "required claim" in lowered:
        return "missing_claim"
    return "invalid_token"


def _bearer_token(scope: Scope) -> str | None:
    values = [v for k, v in scope.get("headers", []) if k == b"authorization"]
    if len(values) != 1:
        return None  # missing, or ambiguous duplicate headers
    scheme, _, token = values[0].decode("latin-1").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def _correlation_id(scope: Scope) -> str:
    for key, value in scope.get("headers", []):
        if key == b"x-cloud-trace-context":
            trace = value.decode("latin-1").split("/", 1)[0]
            if re.fullmatch(r"[0-9a-f]{32}", trace):
                return trace
    return uuid.uuid4().hex


class AuthenticationMiddleware:
    """Pure ASGI middleware. Every HTTP path except the exempt ones needs a valid bearer token.

    On success the Principal is stored in the ASGI scope for tool handlers.
    On failure the request ends here with HTTP 401 and never reaches MCP.
    """

    def __init__(self, app: ASGIApp, authenticator: Authenticator, exempt_paths: frozenset[str]) -> None:
        self._app = app
        self._authenticator = authenticator
        self._exempt_paths = exempt_paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        correlation_id = _correlation_id(scope)
        scope[CORRELATION_SCOPE_KEY] = correlation_id
        if scope["path"] in self._exempt_paths:
            await self._app(scope, receive, send)
            return

        token = _bearer_token(scope)
        try:
            if token is None:
                raise AuthenticationError("missing_bearer_token")
            # Verification may fetch certificates over the network: keep it off the event loop.
            principal = await anyio.to_thread.run_sync(self._authenticator.authenticate, token)
        except AuthenticationError as exc:
            logger.warning(
                "authentication failed",
                extra={
                    "fields": {
                        "event": "authentication",
                        "outcome": "denied",
                        "reason": exc.category,
                        "correlation_id": correlation_id,
                        "path": scope["path"],
                    }
                },
            )
            await _send_401(send)
            return

        scope[PRINCIPAL_SCOPE_KEY] = principal
        await self._app(scope, receive, send)


async def _send_401(send: Send) -> None:
    body = json.dumps({"error": "unauthenticated"}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"www-authenticate", b'Bearer error="invalid_token"'),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
