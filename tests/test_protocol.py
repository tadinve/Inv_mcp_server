"""MCP protocol behavior over real Streamable HTTP.

These tests document what the resolved SDK (mcp 2.3.0) actually does,
per SPEC-AMENDMENT-1 A7, rather than what we assumed it would do.
"""

from __future__ import annotations

import httpx2
import pytest
from mcp_types.version import LATEST_HANDSHAKE_VERSION, LATEST_MODERN_VERSION

from tests.conftest import LiveServer, mcp_session

EXPECTED_TOOLS = {
    "get_inventory",
    "get_reorder_status",
    "list_low_stock_items",
    "create_restock_request",
    "list_restock_requests",
}
JSON_HEADERS = {
    "Authorization": "Bearer dev:reader",
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


async def test_client_negotiates_modern_protocol_by_default(live_server: LiveServer) -> None:
    async with mcp_session(live_server, "dev:reader") as client:
        assert client.protocol_version == LATEST_MODERN_VERSION == "2026-07-28"
        assert client.server_info is not None and client.server_info.name == "cresenta-inventory"
        assert client.server_capabilities.tools is not None


async def test_legacy_initialize_handshake_also_works(live_server: LiveServer) -> None:
    async with mcp_session(live_server, "dev:reader", mode="legacy") as client:
        assert client.protocol_version == LATEST_HANDSHAKE_VERSION
        result = await client.call_tool("get_inventory", {"product_id": "CRS-1001"})
        assert result.is_error is False


async def test_discovery_lists_exactly_the_five_tools(live_server: LiveServer) -> None:
    async with mcp_session(live_server, "dev:reader") as client:
        tools = (await client.list_tools()).tools
    assert {t.name for t in tools} == EXPECTED_TOOLS
    assert len(tools) == 5


async def test_every_tool_publishes_input_and_output_schemas(live_server: LiveServer) -> None:
    async with mcp_session(live_server, "dev:reader") as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    for tool in tools.values():
        assert tool.input_schema["type"] == "object"
        assert tool.output_schema is not None and tool.output_schema["type"] == "object"
        assert tool.description

    create = tools["create_restock_request"]
    assert set(create.input_schema["required"]) == {"product_id", "quantity", "justification", "idempotency_key"}
    props = create.input_schema["properties"]
    assert props["idempotency_key"]["format"] == "uuid"
    assert props["quantity"]["minimum"] == 1 and props["quantity"]["maximum"] == 10_000
    assert props["product_id"]["pattern"] == r"^CRS-\d{4}$"
    assert set(create.output_schema["required"]) >= {"request_id", "product_id", "quantity", "status", "created_at"}

    inventory_out = tools["get_inventory"].output_schema
    assert "unit_cost_minor" in inventory_out["properties"]
    assert "unit_cost" not in inventory_out["properties"]

    limit = tools["list_low_stock_items"].input_schema["properties"]["limit"]
    assert (limit["minimum"], limit["maximum"], limit["default"]) == (1, 100, 20)
    assert "ctx" not in tools["list_low_stock_items"].input_schema["properties"]


async def test_tool_annotations_distinguish_read_and_write(live_server: LiveServer) -> None:
    async with mcp_session(live_server, "dev:reader") as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    for name in EXPECTED_TOOLS - {"create_restock_request"}:
        assert tools[name].annotations.read_only_hint is True
    assert tools["create_restock_request"].annotations.read_only_hint is False


async def test_valid_invocation_returns_structured_content(live_server: LiveServer) -> None:
    async with mcp_session(live_server, "dev:reader") as client:
        result = await client.call_tool("get_inventory", {"product_id": "CRS-1002"})
    assert result.is_error is False
    assert result.structured_content["quantity_on_hand"] == 25
    assert result.content and result.content[0].type == "text"  # text fallback for older clients


async def test_schema_violating_arguments_are_a_typed_tool_execution_error(live_server: LiveServer) -> None:
    """Invalid arguments come back as isError=true (per the SDK), but in our typed format.

    Checkpoint 2 change: ArgumentValidationMiddleware replaces the SDK's raw Pydantic text,
    which echoed the submitted value, with field names and error types only.
    """
    async with mcp_session(live_server, "dev:reader") as client:
        result = await client.call_tool("get_inventory", {"product_id": "not-an-id"})
    assert result.is_error is True
    assert result.structured_content == {
        "error": {
            "code": "invalid_argument",
            "message": "Arguments do not match the input schema (product_id: string_pattern_mismatch).",
        }
    }
    assert "not-an-id" not in result.content[0].text
    assert "Traceback" not in result.content[0].text
    assert "pydantic" not in result.content[0].text


async def test_invalid_arguments_never_echo_submitted_values(live_server: LiveServer) -> None:
    secret = "CONFIDENTIAL-SUPPLIER-TERMS-7731"
    arguments = {
        "product_id": f"CRS-{secret}",
        "quantity": secret,
        "justification": secret * 40,  # too long
        "idempotency_key": secret,
        secret: secret,  # caller-invented field name
    }
    async with mcp_session(live_server, "dev:writer") as client:
        result = await client.call_tool("create_restock_request", arguments)
    assert result.is_error is True
    assert result.structured_content["error"]["code"] == "invalid_argument"
    serialized = result.model_dump_json()
    assert secret not in serialized
    for field in ("product_id", "quantity", "justification", "idempotency_key"):
        assert field in result.structured_content["error"]["message"]


async def test_missing_required_argument_is_a_tool_execution_error(live_server: LiveServer) -> None:
    async with mcp_session(live_server, "dev:reader") as client:
        result = await client.call_tool("get_inventory", {})
    assert result.is_error is True


@pytest.mark.parametrize("limit", [0, 101, 1_000_000])
async def test_out_of_bounds_limit_is_rejected(live_server: LiveServer, limit: int) -> None:
    async with mcp_session(live_server, "dev:reader") as client:
        result = await client.call_tool("list_low_stock_items", {"limit": limit})
    assert result.is_error is True


async def test_unknown_tool_is_a_tool_execution_error(live_server: LiveServer) -> None:
    """SDK behavior (mcp 2.3.0 MCPServer): unknown tools return isError=true rather than JSON-RPC -32602."""
    async with mcp_session(live_server, "dev:reader") as client:
        result = await client.call_tool("delete_everything", {})
    assert result.is_error is True
    assert "Unknown tool" in result.content[0].text


def test_malformed_json_is_a_jsonrpc_parse_error(live_server: LiveServer) -> None:
    response = httpx2.post(live_server.mcp_url, headers=JSON_HEADERS, content=b"{not json")
    assert response.status_code == 400
    body = response.json()
    assert body["jsonrpc"] == "2.0"
    assert body["error"]["code"] == -32700


def test_structurally_invalid_jsonrpc_is_rejected(live_server: LiveServer) -> None:
    response = httpx2.post(live_server.mcp_url, headers=JSON_HEADERS, json={"jsonrpc": "2.0", "id": 1})
    assert response.status_code == 400
    assert response.json()["error"]["code"] in (-32600, -32602)


def test_health_endpoint_is_public_and_minimal(live_server: LiveServer) -> None:
    response = httpx2.get(f"{live_server.base_url}/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_healthz_is_not_served(live_server: LiveServer) -> None:
    response = httpx2.get(f"{live_server.base_url}/healthz", headers={"Authorization": "Bearer dev:reader"})
    assert response.status_code == 404


async def test_rejected_arguments_are_logged_with_caller_but_without_values(
    live_server: LiveServer, caplog: pytest.LogCaptureFixture
) -> None:
    secret = "LOGGED-VALUE-MUST-NOT-APPEAR-5521"
    with caplog.at_level("INFO", logger="inventory_mcp.tools"):
        async with mcp_session(live_server, "dev:reader") as client:
            await client.call_tool("get_inventory", {"product_id": secret})
    records = [r for r in caplog.records if getattr(r, "fields", {}).get("outcome") == "invalid_arguments"]
    assert records, "expected a structured invalid_arguments log line"
    fields = records[-1].fields
    assert fields["principal"] == "dev-reader"
    assert fields["correlation_id"]
    assert fields["invalid_fields"] == ["product_id"]
    assert secret not in caplog.text
