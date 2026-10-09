"""SIMULATED Google OIDC verification tests.

These tests use RSA keys generated locally at test time, standing in for
Google's signing keys. They prove that the application's own verifier
(GoogleOIDCAuthenticator) checks signature, issuer, audience, expiry and
subject correctly.

They are NOT real Google authentication tests: no token here was issued by
Google, and nothing here exercises Cloud Run IAM. Those are the opt-in
`integration` tests (SPEC-AMENDMENT-1, A9.1).
"""

from __future__ import annotations

import base64
import json
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx2
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from google.auth import crypt
from google.auth import jwt as google_jwt

from inventory_mcp.authentication import AuthenticationError, CertificateCache, GoogleOIDCAuthenticator, Principal
from inventory_mcp.authorization import Permission, Policy
from inventory_mcp.config import Settings
from inventory_mcp.repository import RestockRepository
from inventory_mcp.server import create_app
from tests.conftest import LiveServer, mcp_session, serve

pytestmark = pytest.mark.simulated_google_auth

AUDIENCE = "https://cresenta-inventory-123456789012.us-central1.run.app"
ISSUER = "https://accounts.google.com"
KID = "simulated-kid-1"
READER_SUB = "100000000000000000001"
WRITER_SUB = "100000000000000000002"


def _new_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _private_pem(key: rsa.RSAPrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )


def _public_pem(key: rsa.RSAPrivateKey) -> str:
    return (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )


GOOGLE_KEY = _new_key()  # plays the role of Google's signing key
ATTACKER_KEY = _new_key()  # anyone else's key


def _claims(**overrides: object) -> dict[str, object]:
    now = int(time.time())
    claims: dict[str, object] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": READER_SUB,
        "email": "mcp-reader@cresenta-demo.iam.gserviceaccount.com",
        "email_verified": True,
        "iat": now,
        "exp": now + 3600,
    }
    claims.update(overrides)
    return {k: v for k, v in claims.items() if v is not None}


def sign(claims: dict[str, object], *, key: rsa.RSAPrivateKey = GOOGLE_KEY, kid: str | None = KID) -> str:
    signer = crypt.RSASigner.from_string(_private_pem(key), key_id=kid)
    return google_jwt.encode(signer, claims).decode()


def _b64(data: dict[str, object]) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


class FakeCerts:
    """Stands in for Google's certificate endpoint. Counts fetches."""

    def __init__(self, certs: dict[str, str] | None = None, max_age: float = 3600.0) -> None:
        self.certs = certs if certs is not None else {KID: _public_pem(GOOGLE_KEY)}
        self.max_age = max_age
        self.fetches = 0
        self.fail = False

    def __call__(self) -> tuple[dict[str, str], float]:
        self.fetches += 1
        if self.fail:
            raise httpx2.ConnectError("certificate endpoint unreachable")
        return dict(self.certs), self.max_age


@pytest.fixture
def certs() -> FakeCerts:
    return FakeCerts()


@pytest.fixture
def verifier(certs: FakeCerts) -> GoogleOIDCAuthenticator:
    return GoogleOIDCAuthenticator(AUDIENCE, CertificateCache(certs))


def _rejects(verifier: GoogleOIDCAuthenticator, token: str) -> str:
    with pytest.raises(AuthenticationError) as exc:
        verifier.authenticate(token)
    return exc.value.category


# --- verifier unit tests -----------------------------------------------------------------------


def test_simulated_valid_token_yields_verified_subject(verifier: GoogleOIDCAuthenticator) -> None:
    principal = verifier.authenticate(sign(_claims()))
    assert principal == Principal(subject=READER_SUB, label="mcp-reader@cresenta-demo.iam.gserviceaccount.com")


def test_simulated_scheme_less_google_issuer_is_accepted(verifier: GoogleOIDCAuthenticator) -> None:
    assert verifier.authenticate(sign(_claims(iss="accounts.google.com"))).subject == READER_SUB


def test_simulated_unverified_email_is_not_used_as_label(verifier: GoogleOIDCAuthenticator) -> None:
    principal = verifier.authenticate(sign(_claims(email_verified=False)))
    assert principal.label == f"sub:{READER_SUB}"
    assert verifier.authenticate(sign(_claims(email_verified="true"))).label == f"sub:{READER_SUB}"


def test_simulated_expired_token_is_rejected(verifier: GoogleOIDCAuthenticator) -> None:
    now = int(time.time())
    assert _rejects(verifier, sign(_claims(iat=now - 7200, exp=now - 3600))) == "expired"


def test_simulated_expiry_allows_only_small_clock_skew(verifier: GoogleOIDCAuthenticator) -> None:
    now = int(time.time())
    assert verifier.authenticate(sign(_claims(iat=now - 3600, exp=now - 5))).subject == READER_SUB
    assert _rejects(verifier, sign(_claims(iat=now - 3600, exp=now - 120))) == "expired"


def test_simulated_token_issued_in_the_future_is_rejected(verifier: GoogleOIDCAuthenticator) -> None:
    now = int(time.time())
    assert _rejects(verifier, sign(_claims(iat=now + 600, exp=now + 4200))) == "not_yet_valid"


def test_simulated_forged_signature_is_rejected(verifier: GoogleOIDCAuthenticator) -> None:
    """Attacker signs with their own key but claims Google's key id."""
    assert _rejects(verifier, sign(_claims(sub=WRITER_SUB), key=ATTACKER_KEY)) == "invalid_signature"


def test_simulated_tampered_payload_is_rejected(verifier: GoogleOIDCAuthenticator) -> None:
    header, _, signature = sign(_claims()).split(".")
    tampered = ".".join([header, _b64(_claims(sub=WRITER_SUB)), signature])
    assert _rejects(verifier, tampered) == "invalid_signature"


def test_simulated_stripped_signature_is_rejected(verifier: GoogleOIDCAuthenticator) -> None:
    """What Cloud Run does to X-Serverless-Authorization tokens: header.payload. with no signature."""
    header, payload, _ = sign(_claims()).split(".")
    assert _rejects(verifier, f"{header}.{payload}.") == "invalid_signature"


def test_simulated_wrong_audience_is_rejected(verifier: GoogleOIDCAuthenticator) -> None:
    assert _rejects(verifier, sign(_claims(aud="https://some-other-service.run.app"))) == "wrong_audience"


def test_simulated_audience_list_is_not_accepted(verifier: GoogleOIDCAuthenticator) -> None:
    assert _rejects(verifier, sign(_claims(aud=[AUDIENCE, "https://other.run.app"]))) == "wrong_audience"


@pytest.mark.parametrize("issuer", ["https://evil.example.com", "https://accounts.google.com.evil.com", ""])
def test_simulated_wrong_issuer_is_rejected(verifier: GoogleOIDCAuthenticator, issuer: str) -> None:
    assert _rejects(verifier, sign(_claims(iss=issuer))) == "wrong_issuer"


@pytest.mark.parametrize("sub", [None, "", "   ", 12345])
def test_simulated_missing_or_invalid_subject_is_rejected(verifier: GoogleOIDCAuthenticator, sub: object) -> None:
    claims = _claims()
    claims.pop("sub")
    if sub is not None:
        claims["sub"] = sub
    assert _rejects(verifier, sign(claims)) == "missing_subject"


@pytest.mark.parametrize("missing", ["exp", "iat"])
def test_simulated_missing_time_claims_are_rejected(verifier: GoogleOIDCAuthenticator, missing: str) -> None:
    claims = _claims()
    claims.pop(missing)
    assert _rejects(verifier, sign(claims)) == "missing_claim"


@pytest.mark.parametrize("alg", ["none", "HS256", "ES256", "RS512"])
def test_simulated_non_rs256_algorithms_are_rejected(verifier: GoogleOIDCAuthenticator, alg: str) -> None:
    token = ".".join([_b64({"alg": alg, "kid": KID, "typ": "JWT"}), _b64(_claims()), "c2ln"])
    assert _rejects(verifier, token) == "disallowed_algorithm"


def test_simulated_missing_key_id_is_rejected(verifier: GoogleOIDCAuthenticator) -> None:
    assert _rejects(verifier, sign(_claims(), kid=None)) == "missing_key_id"


def test_simulated_unknown_key_id_is_rejected(verifier: GoogleOIDCAuthenticator) -> None:
    assert _rejects(verifier, sign(_claims(), kid="not-a-google-kid")) == "unknown_key_id"


@pytest.mark.parametrize(
    "token",
    ["", "abc", "a.b", "a.b.c.d", "not.a.jwt", "x" * 9000, "eyJhbGciOiJSUzI1NiJ9.e30"],
)
def test_simulated_malformed_tokens_are_rejected(verifier: GoogleOIDCAuthenticator, token: str) -> None:
    assert _rejects(verifier, token) in {"malformed_token", "invalid_signature", "missing_key_id"}


def test_simulated_certificate_outage_fails_closed(certs: FakeCerts, verifier: GoogleOIDCAuthenticator) -> None:
    certs.fail = True
    assert _rejects(verifier, sign(_claims())) == "certificates_unavailable"


def test_simulated_authorization_uses_sub_not_email(verifier: GoogleOIDCAuthenticator) -> None:
    """A token for a different subject that reuses the writer's email gets the stranger's (empty) grants."""
    policy = Policy.from_json(json.dumps({"principals": [{"subject": WRITER_SUB, "permissions": ["restock.create"]}]}))
    impostor = verifier.authenticate(
        sign(_claims(sub="999999999999999999999", email="mcp-writer@cresenta-demo.iam.gserviceaccount.com"))
    )
    writer = verifier.authenticate(sign(_claims(sub=WRITER_SUB)))
    assert policy.authorize(impostor, Permission.RESTOCK_CREATE).allowed is False
    assert policy.authorize(writer, Permission.RESTOCK_CREATE).allowed is True


# --- certificate cache -------------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_certificates_are_cached_within_ttl(certs: FakeCerts) -> None:
    clock = Clock()
    cache = CertificateCache(certs, clock=clock)
    for _ in range(5):
        assert cache.get(KID) is not None
    assert certs.fetches == 1
    clock.now += 3601
    cache.get(KID)
    assert certs.fetches == 2


def test_unknown_kid_refresh_is_rate_limited(certs: FakeCerts) -> None:
    clock = Clock()
    cache = CertificateCache(certs, clock=clock, min_refresh_interval=60)
    cache.get(KID)
    for _ in range(10):
        assert cache.get("rotated-kid") is None
    assert certs.fetches == 1  # within 60s of the last fetch: no refetch
    clock.now += 61
    certs.certs["rotated-kid"] = _public_pem(GOOGLE_KEY)
    assert cache.get("rotated-kid") is not None
    assert certs.fetches == 2


def test_cache_ttl_is_clamped(certs: FakeCerts) -> None:
    clock = Clock()
    certs.max_age = 1  # absurdly short: clamped up to min_ttl
    cache = CertificateCache(certs, clock=clock, min_ttl=300)
    cache.get(KID)
    clock.now += 200
    cache.get(KID)
    assert certs.fetches == 1


# --- end to end over HTTP (simulated keys, real server, real MCP client) -------------------------


@pytest.fixture(scope="module")
def google_mode_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[LiveServer]:
    tmp: Path = tmp_path_factory.mktemp("google")
    policy_path = tmp / "policy.json"
    policy_path.write_text(
        json.dumps(
            {
                "principals": [
                    {"subject": READER_SUB, "permissions": ["inventory.read"]},
                    {"subject": WRITER_SUB, "permissions": ["inventory.read", "restock.create"]},
                ]
            }
        )
    )
    settings = Settings.from_env(
        {
            "INVENTORY_AUTH_MODE": "google",
            "INVENTORY_OIDC_AUDIENCE": AUDIENCE,
            "INVENTORY_POLICY_PATH": str(policy_path),
            "INVENTORY_DB_PATH": str(tmp / "restock.db"),
        }
    )
    repository = RestockRepository(settings.db_path)
    repository.initialize()
    authenticator = GoogleOIDCAuthenticator(AUDIENCE, CertificateCache(FakeCerts()))
    app = create_app(settings, repository=repository, authenticator=authenticator)
    with serve(app, repository) as live:
        yield live


def _restock() -> dict[str, object]:
    return {
        "product_id": "CRS-1002",
        "quantity": 5,
        "justification": "Simulated end-to-end test.",
        "idempotency_key": str(uuid.uuid4()),
    }


async def test_simulated_e2e_reader_reads_but_cannot_write(google_mode_server: LiveServer) -> None:
    rows = google_mode_server.repository.count_all()
    async with mcp_session(google_mode_server, sign(_claims(sub=READER_SUB))) as client:
        read = await client.call_tool("get_inventory", {"product_id": "CRS-1002"})
        write = await client.call_tool("create_restock_request", _restock())
    assert read.is_error is False
    assert write.structured_content["error"]["code"] == "forbidden"
    assert google_mode_server.repository.count_all() == rows


async def test_simulated_e2e_writer_can_write_as_its_subject(google_mode_server: LiveServer) -> None:
    async with mcp_session(google_mode_server, sign(_claims(sub=WRITER_SUB))) as client:
        result = await client.call_tool("create_restock_request", _restock())
    assert result.is_error is False
    assert result.structured_content["submitted_by"] == WRITER_SUB


@pytest.mark.parametrize(
    "token",
    [
        sign(_claims(sub=WRITER_SUB), key=ATTACKER_KEY),
        sign(_claims(sub=WRITER_SUB, aud="https://other.run.app")),
        sign(_claims(sub=WRITER_SUB, iss="https://evil.example.com")),
        sign(_claims(sub=WRITER_SUB, iat=int(time.time()) - 7200, exp=int(time.time()) - 3600)),
        "dev:writer",  # dev tokens mean nothing in google mode
    ],
    ids=["forged", "wrong-audience", "wrong-issuer", "expired", "dev-token"],
)
def test_simulated_e2e_invalid_tokens_get_401(google_mode_server: LiveServer, token: str) -> None:
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    response = httpx2.post(
        google_mode_server.mcp_url,
        json=body,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream"},
    )
    assert response.status_code == 401
    assert response.json() == {"error": "unauthenticated"}


def test_simulated_e2e_identity_headers_cannot_replace_a_token(google_mode_server: LiveServer) -> None:
    """Headers a proxy might add are ignored: only a verified Authorization token counts."""
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    headers = {
        "Accept": "application/json, text/event-stream",
        "X-Serverless-Authorization": f"Bearer {sign(_claims(sub=WRITER_SUB))}",
        "X-Goog-Authenticated-User-Email": "accounts.google.com:mcp-writer@cresenta-demo.iam.gserviceaccount.com",
        "X-Goog-Authenticated-User-Id": f"accounts.google.com:{WRITER_SUB}",
    }
    response = httpx2.post(google_mode_server.mcp_url, json=body, headers=headers)
    assert response.status_code == 401
