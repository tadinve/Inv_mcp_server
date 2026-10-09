"""REAL Google identity tests against the deployed Cloud Run service.

Unlike tests/test_google_oidc_simulated.py, every token here is a real
Google-signed ID token, minted by impersonating the reader, writer and
outsider service accounts. Requests go through Cloud Run IAM to the deployed
MCP server.

Opt-in only (marker `integration`, excluded by default). Run through
deployment/verify_cloud_run.sh, which sets:

    MCP_REMOTE_URL   https://SERVICE-PROJECT_NUMBER.REGION.run.app
    MCP_PROJECT_ID, MCP_REGION, MCP_SERVICE
    MCP_READER_SA, MCP_WRITER_SA, MCP_OUTSIDER_SA

If they are missing, the tests are skipped and must be reported as NOT RUN.
Tokens are never printed or placed in assertion messages.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import cache

import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from clients.gcloud_identity import mint_identity_token
from inventory_mcp.authentication import CertificateCache, GoogleOIDCAuthenticator

pytestmark = pytest.mark.integration

ENV = {name: os.environ.get(name, "") for name in (
    "MCP_REMOTE_URL", "MCP_PROJECT_ID", "MCP_REGION", "MCP_SERVICE",
    "MCP_READER_SA", "MCP_WRITER_SA", "MCP_OUTSIDER_SA",
)}  # fmt: skip
if not all(ENV.values()) or shutil.which("gcloud") is None:
    pytest.skip("Cloud Run integration environment not configured: NOT RUN", allow_module_level=True)

BASE_URL = ENV["MCP_REMOTE_URL"].rstrip("/")
MCP_URL = f"{BASE_URL}/mcp"
ACCEPT = {"Accept": "application/json, text/event-stream"}
LIST_TOOLS = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
APP_401 = {"error": "unauthenticated"}  # body our middleware returns; Cloud Run's own 401/403 differ


@cache
def token(role: str, audience: str = BASE_URL) -> str:
    return mint_identity_token(ENV[f"MCP_{role.upper()}_SA"], audience)


@cache
def unique_id(role: str) -> str:
    proc = subprocess.run(
        [shutil.which("gcloud") or "gcloud", "iam", "service-accounts", "describe", ENV[f"MCP_{role.upper()}_SA"],
         "--project", ENV["MCP_PROJECT_ID"], "--format=value(uniqueId)"],
        capture_output=True, text=True, check=True, timeout=60,
    )  # fmt: skip
    return proc.stdout.strip()


@asynccontextmanager
async def session(role: str) -> AsyncIterator[Client]:
    http_client = httpx2.AsyncClient(headers={"Authorization": f"Bearer {token(role)}"}, timeout=60.0)
    async with Client(streamable_http_client(MCP_URL, http_client=http_client)) as client:
        yield client


async def request_count(client: Client) -> int:
    result = await client.call_tool("list_restock_requests", {"limit": 100})
    assert result.is_error is False
    return result.structured_content["count"]


def restock_args(**overrides: object) -> dict[str, object]:
    args: dict[str, object] = {
        "product_id": "CRS-1003",
        "quantity": 80,
        "justification": "Integration test: zero stock in all warehouses.",
        "idempotency_key": str(uuid.uuid4()),
    }
    args.update(overrides)
    return args


def raw_post(headers: dict[str, str], url: str = MCP_URL) -> httpx2.Response:
    return httpx2.post(url, json=LIST_TOOLS, headers={**ACCEPT, **headers}, timeout=60.0)


# --- identity mapping -------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["reader", "writer", "outsider"])
def test_real_token_sub_equals_service_account_unique_id(role: str) -> None:
    """The policy is keyed by uniqueId; prove a real Google-signed token's `sub` is exactly that.

    The token is verified with the server's own GoogleOIDCAuthenticator (signature against Google's
    live certificates, issuer, audience, expiry). Only the comparison result is reported.
    """
    verifier = GoogleOIDCAuthenticator(BASE_URL, CertificateCache())
    principal = verifier.authenticate(token(role))
    matches = principal.subject == unique_id(role)
    print(f"{role}: signature verified, sub == uniqueId: {matches}")
    assert matches, f"{role}: verified sub does not equal the service account uniqueId"


# --- MCP protocol with real identities -------------------------------------------------------


async def test_real_reader_discovers_five_tools() -> None:
    async with session("reader") as client:
        names = {t.name for t in (await client.list_tools()).tools}
        assert client.protocol_version == "2026-07-28"
    assert names == {
        "get_inventory", "get_reorder_status", "list_low_stock_items",
        "create_restock_request", "list_restock_requests",
    }  # fmt: skip


async def test_real_reader_reads_synthetic_inventory() -> None:
    async with session("reader") as client:
        result = await client.call_tool("get_inventory", {"product_id": "CRS-1002"})
    assert result.is_error is False
    assert result.structured_content["product_name"] == "Nitrile Gloves L (case)"
    assert result.structured_content["quantity_on_hand"] == 25


async def test_real_reader_write_is_forbidden_and_nothing_changes() -> None:
    async with session("writer") as writer, session("reader") as reader:
        writer_before, reader_before = await request_count(writer), await request_count(reader)
        result = await reader.call_tool("create_restock_request", restock_args())
        writer_after, reader_after = await request_count(writer), await request_count(reader)
    assert result.is_error is True
    assert result.structured_content["error"]["code"] == "forbidden"
    assert (writer_after, reader_after) == (writer_before, reader_before)
    assert reader_after == 0


async def test_real_writer_creates_request_attributed_to_verified_sub() -> None:
    async with session("writer") as writer:
        before = await request_count(writer)
        result = await writer.call_tool("create_restock_request", restock_args())
        after = await request_count(writer)
    assert result.is_error is False
    receipt = result.structured_content
    assert receipt["status"] == "pending_review"
    assert receipt["idempotent_replay"] is False
    # The verified `sub` of a service-account ID token is the account's uniqueId.
    assert receipt["submitted_by"] == unique_id("writer")
    assert after == before + 1


async def test_real_identical_retry_replays_without_duplicate() -> None:
    args = restock_args()
    async with session("writer") as writer:
        first = (await writer.call_tool("create_restock_request", args)).structured_content
        count_after_first = await request_count(writer)
        second = (await writer.call_tool("create_restock_request", args)).structured_content
        count_after_second = await request_count(writer)
    assert second["request_id"] == first["request_id"]
    assert (first["idempotent_replay"], second["idempotent_replay"]) == (False, True)
    assert count_after_second == count_after_first


async def test_real_reused_key_with_different_payload_conflicts() -> None:
    args = restock_args()
    async with session("writer") as writer:
        await writer.call_tool("create_restock_request", args)
        before = await request_count(writer)
        result = await writer.call_tool("create_restock_request", {**args, "quantity": 81})
        after = await request_count(writer)
    assert result.structured_content["error"]["code"] == "idempotency_conflict"
    assert after == before


async def test_real_concurrent_duplicates_persist_once() -> None:
    args = restock_args()
    clients = 8

    async def submit() -> dict:
        async with session("writer") as writer:
            return (await writer.call_tool("create_restock_request", args)).structured_content

    async with session("writer") as writer:
        before = await request_count(writer)
    receipts = await asyncio.gather(*(submit() for _ in range(clients)))
    async with session("writer") as writer:
        after = await request_count(writer)
    assert len({r["request_id"] for r in receipts}) == 1
    assert sum(1 for r in receipts if r["idempotent_replay"] is False) == 1
    assert after == before + 1


async def test_real_invalid_arguments_are_sanitized() -> None:
    secret = "CONFIDENTIAL-INTEGRATION-VALUE-4417"
    async with session("writer") as writer:
        result = await writer.call_tool("create_restock_request", restock_args(product_id=secret, quantity=secret))
    assert result.structured_content["error"]["code"] == "invalid_argument"
    assert secret not in result.model_dump_json()


# --- HTTP boundary with real tokens ------------------------------------------------------------


def test_real_anonymous_request_is_rejected_by_cloud_run() -> None:
    response = raw_post({})
    assert response.status_code == 403


def test_real_outsider_is_rejected_by_cloud_run() -> None:
    response = raw_post({"Authorization": f"Bearer {token('outsider')}"})
    assert response.status_code == 403


def test_real_wrong_audience_is_rejected() -> None:
    response = raw_post({"Authorization": f"Bearer {token('writer', 'https://wrong-audience.example.com')}"})
    assert response.status_code == 401


def test_real_forged_signature_is_rejected() -> None:
    header, payload, signature = token("writer").split(".")
    i = len(signature) // 2
    forged = ".".join([header, payload, signature[:i] + ("A" if signature[i] != "A" else "B") + signature[i + 1 :]])
    response = raw_post({"Authorization": f"Bearer {forged}"})
    assert response.status_code == 401


def test_real_x_serverless_authorization_alone_is_refused_by_the_app() -> None:
    """Cloud Run admits the request (valid writer token), but strips the signature; the app refuses it."""
    response = raw_post({"X-Serverless-Authorization": f"Bearer {token('writer')}"})
    assert response.status_code == 401
    assert response.json() == APP_401  # our middleware, not Cloud Run


def test_real_health_requires_invoker_but_answers_minimally() -> None:
    assert httpx2.get(f"{BASE_URL}/health", timeout=60.0).status_code == 403
    response = httpx2.get(f"{BASE_URL}/health", headers={"Authorization": f"Bearer {token('reader')}"}, timeout=60.0)
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_real_requests_via_non_pinned_hostname_are_rejected() -> None:
    """Cloud Run serves the service on more than one hostname; only the pinned one reaches MCP."""
    proc = subprocess.run(
        [shutil.which("gcloud") or "gcloud", "run", "services", "describe", ENV["MCP_SERVICE"],
         "--project", ENV["MCP_PROJECT_ID"], "--region", ENV["MCP_REGION"], "--format=json"],
        capture_output=True, text=True, check=True, timeout=60,
    )  # fmt: skip
    raw = json.loads(proc.stdout)["metadata"]["annotations"].get("run.googleapis.com/urls", "")
    urls = json.loads(raw) if raw.startswith("[") else [u for u in raw.split(",") if u]
    others = [u for u in urls if u.rstrip("/") != BASE_URL]
    if not others:
        pytest.skip("service exposes only the pinned hostname")
    for url in others:
        response = raw_post({"Authorization": f"Bearer {token('writer')}"}, url=f"{url.rstrip('/')}/mcp")
        print(f"non-pinned hostname -> HTTP {response.status_code}")
        assert response.status_code == 421, f"expected the app's Host pin (421), got {response.status_code}"
