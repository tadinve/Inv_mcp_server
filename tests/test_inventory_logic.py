"""Deterministic business logic, tested without MCP or HTTP."""

from __future__ import annotations

import json

import pytest

from inventory_mcp.errors import ProductNotFound
from inventory_mcp.inventory_service import InventoryService, compute_reorder, shortfall


@pytest.fixture(scope="module")
def service() -> InventoryService:
    return InventoryService.from_package_data()


@pytest.mark.parametrize(
    ("on_hand", "reorder_point", "required", "recommended"),
    [
        (25, 60, True, 95),  # below: 2*60 - 25
        (0, 40, True, 80),  # zero stock orders the full target level
        (45, 45, False, 0),  # exactly at the reorder point is not below it
        (260, 100, False, 0),  # adequate stock
        (0, 0, False, 0),  # reorder point 0 never triggers
        (1, 2, True, 3),  # smallest positive shortfall
        (900, 1000, True, 1100),
    ],
)
def test_reorder_formula(on_hand: int, reorder_point: int, required: bool, recommended: int) -> None:
    decision = compute_reorder(on_hand, reorder_point)
    assert decision.target_level == 2 * reorder_point
    assert decision.reorder_required is required
    assert decision.recommended_order_quantity == recommended


def test_reorder_rejects_negative_quantities() -> None:
    with pytest.raises(ValueError):
        compute_reorder(-1, 10)


def test_shortfall_is_never_negative() -> None:
    assert shortfall(50, 10) == 0
    assert shortfall(3, 25) == 22


def test_catalog_has_twenty_products_across_three_warehouses(service: InventoryService) -> None:
    assert service.product_count() == 20
    warehouses = {
        w.warehouse_id for pid in (f"CRS-{n}" for n in range(1001, 1021)) for w in service.get_inventory(pid).warehouses
    }
    assert warehouses == {"WH-CMH", "WH-RNO", "WH-SAV"}


def test_quantity_on_hand_sums_warehouses(service: InventoryService) -> None:
    record = service.get_inventory("CRS-1001")
    assert record.quantity_on_hand == 260
    assert [w.warehouse_id for w in record.warehouses] == ["WH-CMH", "WH-RNO", "WH-SAV"]


def test_product_with_no_warehouse_entries_has_zero_stock(service: InventoryService) -> None:
    record = service.get_inventory("CRS-1009")
    assert record.warehouses == []
    assert record.quantity_on_hand == 0


def test_unknown_product_raises_not_found(service: InventoryService) -> None:
    with pytest.raises(ProductNotFound) as exc:
        service.get_inventory("CRS-9999")
    assert exc.value.code == "not_found"


def test_reorder_cost_uses_integer_minor_units(service: InventoryService) -> None:
    status = service.get_reorder_status("CRS-1008")  # forklift battery, 1,249,900 minor units
    assert status.recommended_order_quantity == 3
    assert status.estimated_order_cost_minor == 3 * 1_249_900
    assert isinstance(status.estimated_order_cost_minor, int)


def test_reorder_status_keeps_product_currency(service: InventoryService) -> None:
    assert service.get_reorder_status("CRS-1019").currency == "EUR"


def test_low_stock_order_is_shortfall_desc_then_product_id(service: InventoryService) -> None:
    report = service.list_low_stock_items(limit=100)
    assert [i.product_id for i in report.items] == [
        "CRS-1017",
        "CRS-1003",
        "CRS-1002",
        "CRS-1011",
        "CRS-1013",
        "CRS-1006",
        "CRS-1019",
        "CRS-1020",
        "CRS-1008",
        "CRS-1015",
    ]
    assert report.count == 10


def test_low_stock_respects_limit(service: InventoryService) -> None:
    report = service.list_low_stock_items(limit=3)
    assert report.count == 3
    assert [i.product_id for i in report.items] == ["CRS-1017", "CRS-1003", "CRS-1002"]


def test_low_stock_supports_empty_result() -> None:
    raw = json.dumps(
        {
            "warehouses": [{"warehouse_id": "WH-A", "warehouse_name": "A"}],
            "products": [
                {
                    "product_id": "CRS-0001",
                    "product_name": "x",
                    "supplier": "s",
                    "unit_cost_minor": 1,
                    "currency": "USD",
                    "reorder_point": 1,
                    "warehouses": [{"warehouse_id": "WH-A", "quantity": 5}],
                }
            ],
        }
    )
    report = InventoryService.from_json(raw).list_low_stock_items(limit=20)
    assert report.items == []
    assert report.count == 0


def test_catalog_rejects_duplicate_product_ids() -> None:
    product = {
        "product_id": "CRS-0001",
        "product_name": "x",
        "supplier": "s",
        "unit_cost_minor": 1,
        "currency": "USD",
        "reorder_point": 1,
        "warehouses": [],
    }
    raw = json.dumps({"warehouses": [], "products": [product, product]})
    with pytest.raises(ValueError, match="duplicate"):
        InventoryService.from_json(raw)
