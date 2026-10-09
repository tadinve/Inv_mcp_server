"""Functional behavior of each tool through the real MCP client."""

from __future__ import annotations

import uuid

from tests.conftest import LiveServer, mcp_session

WRITER = "dev:writer"


def _restock(**overrides: object) -> dict[str, object]:
    args: dict[str, object] = {
        "product_id": "CRS-1003",
        "quantity": 80,
        "justification": "Zero stock in all warehouses.",
        "idempotency_key": str(uuid.uuid4()),
    }
    args.update(overrides)
    return args


async def test_get_inventory_returns_typed_record(live_server: LiveServer) -> None:
    async with mcp_session(live_server, WRITER) as client:
        result = await client.call_tool("get_inventory", {"product_id": "CRS-1001"})
    data = result.structured_content
    assert data["quantity_on_hand"] == 260
    assert data["unit_cost_minor"] == 2499 and data["currency"] == "USD"
    assert [w["warehouse_id"] for w in data["warehouses"]] == ["WH-CMH", "WH-RNO", "WH-SAV"]


async def test_unknown_product_returns_typed_not_found(live_server: LiveServer) -> None:
    async with mcp_session(live_server, WRITER) as client:
        result = await client.call_tool("get_inventory", {"product_id": "CRS-9999"})
    assert result.is_error is True
    assert result.structured_content == {"error": {"code": "not_found", "message": "Product CRS-9999 does not exist."}}


async def test_get_reorder_status_applies_formula(live_server: LiveServer) -> None:
    async with mcp_session(live_server, WRITER) as client:
        below = (await client.call_tool("get_reorder_status", {"product_id": "CRS-1002"})).structured_content
        at = (await client.call_tool("get_reorder_status", {"product_id": "CRS-1005"})).structured_content
    assert (below["reorder_required"], below["recommended_order_quantity"], below["target_level"]) == (True, 95, 120)
    assert (at["reorder_required"], at["recommended_order_quantity"]) == (False, 0)


async def test_list_low_stock_items_is_deterministic(live_server: LiveServer) -> None:
    async with mcp_session(live_server, WRITER) as client:
        first = (await client.call_tool("list_low_stock_items", {"limit": 4})).structured_content
        second = (await client.call_tool("list_low_stock_items", {"limit": 4})).structured_content
    assert first == second
    assert [i["product_id"] for i in first["items"]] == ["CRS-1017", "CRS-1003", "CRS-1002", "CRS-1011"]
    assert first["count"] == 4


async def test_list_low_stock_items_uses_default_limit(live_server: LiveServer) -> None:
    async with mcp_session(live_server, WRITER) as client:
        report = (await client.call_tool("list_low_stock_items", {})).structured_content
    assert report["count"] == 10  # all low-stock products; fewer than the default limit of 20


async def test_create_restock_request_persists_and_is_listed(live_server: LiveServer) -> None:
    async with mcp_session(live_server, WRITER) as client:
        before = (await client.call_tool("list_restock_requests", {"limit": 100})).structured_content["count"]
        created = await client.call_tool("create_restock_request", _restock())
        after = (await client.call_tool("list_restock_requests", {"limit": 100})).structured_content
    assert created.is_error is False
    receipt = created.structured_content
    assert receipt["status"] == "pending_review"
    assert receipt["idempotent_replay"] is False
    assert receipt["submitted_by"] == WRITER
    assert after["count"] == before + 1
    assert receipt["request_id"] in {r["request_id"] for r in after["requests"]}


async def test_identical_retry_returns_original_without_duplicate(live_server: LiveServer) -> None:
    args = _restock()
    async with mcp_session(live_server, WRITER) as client:
        first = (await client.call_tool("create_restock_request", args)).structured_content
        rows_after_first = live_server.repository.count_all()
        second = (await client.call_tool("create_restock_request", args)).structured_content
    assert second["request_id"] == first["request_id"]
    assert second["idempotent_replay"] is True
    assert live_server.repository.count_all() == rows_after_first


async def test_reused_key_with_different_payload_is_typed_conflict(live_server: LiveServer) -> None:
    args = _restock()
    async with mcp_session(live_server, WRITER) as client:
        await client.call_tool("create_restock_request", args)
        rows = live_server.repository.count_all()
        result = await client.call_tool("create_restock_request", {**args, "quantity": 81})
    assert result.is_error is True
    assert result.structured_content["error"]["code"] == "idempotency_conflict"
    assert live_server.repository.count_all() == rows


async def test_restock_for_unknown_product_is_not_found_and_not_persisted(live_server: LiveServer) -> None:
    rows = live_server.repository.count_all()
    async with mcp_session(live_server, WRITER) as client:
        result = await client.call_tool("create_restock_request", _restock(product_id="CRS-9999"))
    assert result.structured_content["error"]["code"] == "not_found"
    assert live_server.repository.count_all() == rows


async def test_whitespace_padded_justification_is_business_validation_error(live_server: LiveServer) -> None:
    rows = live_server.repository.count_all()
    async with mcp_session(live_server, WRITER) as client:
        result = await client.call_tool("create_restock_request", _restock(justification="   short     "))
    assert result.is_error is True
    assert result.structured_content["error"]["code"] == "invalid_argument"
    assert live_server.repository.count_all() == rows


async def test_quantity_and_key_bounds_are_enforced(live_server: LiveServer) -> None:
    rows = live_server.repository.count_all()
    async with mcp_session(live_server, WRITER) as client:
        for bad in (
            _restock(quantity=0),
            _restock(quantity=-5),
            _restock(quantity=10_001),
            _restock(idempotency_key="not-a-uuid"),
            _restock(justification="too short"),
        ):
            result = await client.call_tool("create_restock_request", bad)
            assert result.is_error is True, bad
    assert live_server.repository.count_all() == rows
