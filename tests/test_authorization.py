"""Authentication boundary (dev mode) and tool-level authorization.

Checkpoint 1 scope: test-only dev authentication plus real, policy-driven
authorization. Google OIDC verification tests arrive in Checkpoint 2.
"""

from __future__ import annotations

import uuid

import httpx2
import pytest

from inventory_mcp.authentication import AuthenticationError, DevAuthenticator, Principal
from inventory_mcp.authorization import Permission, Policy
from inventory_mcp.config import ConfigError, Settings
from tests.conftest import DEV_POLICY, LiveServer, mcp_session

READ_TOOLS = [
    ("get_inventory", {"product_id": "CRS-1002"}),
    ("get_reorder_status", {"product_id": "CRS-1002"}),
    ("list_low_stock_items", {"limit": 5}),
    ("list_restock_requests", {}),
]


def _restock_args() -> dict[str, object]:
    return {
        "product_id": "CRS-1002",
        "quantity": 5,
        "justification": "Attempted restock request.",
        "idempotency_key": str(uuid.uuid4()),
    }


# --- tool-level authorization over MCP ---------------------------------------------------------


@pytest.mark.parametrize(("tool", "args"), READ_TOOLS)
async def test_reader_can_call_read_tools(live_server: LiveServer, tool: str, args: dict) -> None:
    async with mcp_session(live_server, "dev:reader") as client:
        result = await client.call_tool(tool, args)
    assert result.is_error is False


async def test_reader_cannot_create_restock_and_nothing_is_written(live_server: LiveServer) -> None:
    rows = live_server.repository.count_all()
    async with mcp_session(live_server, "dev:reader") as client:
        result = await client.call_tool("create_restock_request", _restock_args())
        listed = (await client.call_tool("list_restock_requests", {})).structured_content
    assert result.is_error is True
    assert result.structured_content == {
        "error": {"code": "forbidden", "message": "Caller is not authorized to call create_restock_request."}
    }
    assert live_server.repository.count_all() == rows
    assert listed["count"] == 0


async def test_writer_can_create_restock(live_server: LiveServer) -> None:
    async with mcp_session(live_server, "dev:writer") as client:
        result = await client.call_tool("create_restock_request", _restock_args())
    assert result.is_error is False


@pytest.mark.parametrize("token", ["dev:no-access", "dev:unlisted-identity"])
@pytest.mark.parametrize(("tool", "args"), [*READ_TOOLS, ("create_restock_request", None)])
async def test_identities_without_grants_are_denied_everything(
    live_server: LiveServer, token: str, tool: str, args: dict | None
) -> None:
    rows = live_server.repository.count_all()
    async with mcp_session(live_server, token) as client:
        result = await client.call_tool(tool, args if args is not None else _restock_args())
    assert result.is_error is True
    assert result.structured_content["error"]["code"] == "forbidden"
    assert live_server.repository.count_all() == rows


async def test_discovery_is_not_an_authorization_grant(live_server: LiveServer) -> None:
    async with mcp_session(live_server, "dev:no-access") as client:
        names = {t.name for t in (await client.list_tools()).tools}
        result = await client.call_tool("get_inventory", {"product_id": "CRS-1002"})
    assert "get_inventory" in names
    assert result.is_error is True


async def test_caller_supplied_role_arguments_cannot_elevate(live_server: LiveServer) -> None:
    rows = live_server.repository.count_all()
    escalation = {**_restock_args(), "role": "admin", "permissions": ["restock.create"], "principal": "dev:writer"}
    async with mcp_session(live_server, "dev:reader") as client:
        result = await client.call_tool("create_restock_request", escalation)
    assert result.is_error is True
    assert live_server.repository.count_all() == rows


async def test_caller_supplied_identity_headers_are_ignored(live_server: LiveServer) -> None:
    rows = live_server.repository.count_all()
    headers = {
        "Authorization": "Bearer dev:reader",
        "X-Goog-Authenticated-User-Email": "mcp-writer@example.com",
        "X-Serverless-Authorization": "Bearer dev:writer",
        "X-Role": "restock.create",
    }
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client

    async with Client(
        streamable_http_client(live_server.mcp_url, http_client=httpx2.AsyncClient(headers=headers))
    ) as c:
        result = await c.call_tool("create_restock_request", _restock_args())
    assert result.structured_content["error"]["code"] == "forbidden"
    assert live_server.repository.count_all() == rows


async def test_restock_requests_are_not_visible_across_principals(live_server: LiveServer) -> None:
    async with mcp_session(live_server, "dev:writer") as client:
        await client.call_tool("create_restock_request", _restock_args())
    async with mcp_session(live_server, "dev:reader") as client:
        listed = (await client.call_tool("list_restock_requests", {"limit": 100})).structured_content
    assert all(r["submitted_by"] == "dev:reader" for r in listed["requests"])


# --- HTTP authentication boundary --------------------------------------------------------------


def _post(server: LiveServer, headers: dict[str, str]) -> httpx2.Response:
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    return httpx2.post(server.mcp_url, json=body, headers={"Accept": "application/json, text/event-stream", **headers})


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer"},
        {"Authorization": "Basic ZGV2OndyaXRlcg=="},
        {"Authorization": "Bearer dev:WRITER"},
        {"Authorization": "Bearer writer"},
        {"Authorization": "Bearer eyJhbGciOiJub25lIn0.eyJzdWIiOiJkZXY6d3JpdGVyIn0."},  # unsigned JWT
    ],
    ids=["missing", "bearer-without-token", "basic-scheme", "bad-case", "no-dev-prefix", "unsigned-jwt"],
)
def test_missing_or_malformed_credentials_get_401(live_server: LiveServer, headers: dict[str, str]) -> None:
    response = _post(live_server, headers)
    assert response.status_code == 401
    assert response.json() == {"error": "unauthenticated"}
    assert response.headers["www-authenticate"].startswith("Bearer")


def test_duplicate_authorization_headers_are_rejected(live_server: LiveServer) -> None:
    headers = [
        ("Authorization", "Bearer dev:reader"),
        ("Authorization", "Bearer dev:writer"),
        ("Accept", "application/json, text/event-stream"),
    ]
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    response = httpx2.post(live_server.mcp_url, json=body, headers=headers)
    assert response.status_code == 401


async def test_mcp_client_without_token_cannot_connect(live_server: LiveServer) -> None:
    with pytest.raises(Exception):
        async with mcp_session(live_server, None) as client:
            await client.list_tools()


# --- unit level --------------------------------------------------------------------------------


def test_dev_authenticator_maps_token_to_subject() -> None:
    assert DevAuthenticator().authenticate("dev:reader") == Principal(subject="dev:reader", label="dev-reader")


@pytest.mark.parametrize("token", ["", "dev:", "dev:Reader", "dev:a b", "reader", "dev:" + "a" * 64])
def test_dev_authenticator_rejects_malformed_tokens(token: str) -> None:
    with pytest.raises(AuthenticationError):
        DevAuthenticator().authenticate(token)


def test_policy_denies_by_default() -> None:
    policy = Policy.from_path(DEV_POLICY)
    assert policy.authorize(None, Permission.INVENTORY_READ).allowed is False
    stranger = Principal(subject="dev:stranger", label="dev-stranger")
    assert policy.authorize(stranger, Permission.INVENTORY_READ).allowed is False
    reader = Principal(subject="dev:reader", label="dev-reader")
    assert policy.authorize(reader, Permission.INVENTORY_READ).allowed is True
    assert policy.authorize(reader, Permission.RESTOCK_CREATE).allowed is False


def test_policy_authorizes_on_subject_not_label() -> None:
    policy = Policy.from_path(DEV_POLICY)
    impostor = Principal(subject="dev:someone-else", label="dev-writer")
    assert policy.authorize(impostor, Permission.RESTOCK_CREATE).allowed is False


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        '{"principals": [{"subject": "x", "permissions": ["inventory.admin"]}]}',
        '{"principals": [{"subject": "", "permissions": []}]}',
        '{"principals": [{"subject": "x"}, {"subject": "x"}]}',
        '{"grants": []}',
    ],
)
def test_invalid_policies_fail_closed(raw: str) -> None:
    with pytest.raises(ConfigError):
        Policy.from_json(raw)


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"INVENTORY_AUTH_MODE": "none", "INVENTORY_POLICY_PATH": "p.json"},
        {"INVENTORY_AUTH_MODE": "dev"},
        {"INVENTORY_AUTH_MODE": "google", "INVENTORY_POLICY_PATH": "p.json"},
    ],
    ids=["nothing-set", "unknown-mode", "no-policy", "google-without-audience"],
)
def test_unsafe_or_incomplete_configuration_refuses_to_start(env: dict[str, str]) -> None:
    with pytest.raises(ConfigError):
        Settings.from_env(env)
