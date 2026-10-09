"""Deterministic inventory business logic. No I/O beyond loading the catalog."""

from __future__ import annotations

import json
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from inventory_mcp.errors import ProductNotFound
from inventory_mcp.schemas import (
    InventoryRecord,
    LowStockItem,
    LowStockReport,
    ReorderStatus,
    WarehouseStock,
)


@dataclass(frozen=True)
class ReorderDecision:
    target_level: int
    reorder_required: bool
    recommended_order_quantity: int


def compute_reorder(quantity_on_hand: int, reorder_point: int) -> ReorderDecision:
    """Reorder formula (SPEC-AMENDMENT-1, A10).

    target_level = 2 * reorder_point
    reorder_required = on_hand < reorder_point
    recommended = max(0, target_level - on_hand) if reorder_required else 0
    """
    if quantity_on_hand < 0 or reorder_point < 0:
        raise ValueError("quantities must be non-negative")
    target_level = 2 * reorder_point
    reorder_required = quantity_on_hand < reorder_point
    recommended = max(0, target_level - quantity_on_hand) if reorder_required else 0
    return ReorderDecision(target_level, reorder_required, recommended)


def shortfall(quantity_on_hand: int, reorder_point: int) -> int:
    return max(0, reorder_point - quantity_on_hand)


class InventoryService:
    def __init__(self, catalog: dict[str, InventoryRecord]) -> None:
        self._catalog = catalog

    @classmethod
    def from_json(cls, raw: str) -> InventoryService:
        data = json.loads(raw)
        warehouse_names = {w["warehouse_id"]: w["warehouse_name"] for w in data["warehouses"]}
        catalog: dict[str, InventoryRecord] = {}
        for product in data["products"]:
            stock = [
                WarehouseStock(
                    warehouse_id=entry["warehouse_id"],
                    warehouse_name=warehouse_names[entry["warehouse_id"]],
                    quantity=entry["quantity"],
                )
                for entry in product["warehouses"]
            ]
            record = InventoryRecord(
                product_id=product["product_id"],
                product_name=product["product_name"],
                supplier=product["supplier"],
                quantity_on_hand=sum(s.quantity for s in stock),
                reorder_point=product["reorder_point"],
                unit_cost_minor=product["unit_cost_minor"],
                currency=product["currency"],
                warehouses=sorted(stock, key=lambda s: s.warehouse_id),
            )
            if record.product_id in catalog:
                raise ValueError(f"duplicate product_id in catalog: {record.product_id}")
            catalog[record.product_id] = record
        return cls(catalog)

    @classmethod
    def from_package_data(cls) -> InventoryService:
        return cls.from_json(resources.files("inventory_mcp").joinpath("data", "inventory.json").read_text())

    @classmethod
    def from_path(cls, path: Path) -> InventoryService:
        return cls.from_json(path.read_text())

    def product_count(self) -> int:
        return len(self._catalog)

    def get_inventory(self, product_id: str) -> InventoryRecord:
        record = self._catalog.get(product_id)
        if record is None:
            raise ProductNotFound(f"Product {product_id} does not exist.")
        return record

    def get_reorder_status(self, product_id: str) -> ReorderStatus:
        record = self.get_inventory(product_id)
        decision = compute_reorder(record.quantity_on_hand, record.reorder_point)
        if decision.reorder_required:
            reason = (
                f"On hand ({record.quantity_on_hand}) is below the reorder point ({record.reorder_point}); "
                f"ordering {decision.recommended_order_quantity} restores the target level ({decision.target_level})."
            )
        else:
            reason = f"On hand ({record.quantity_on_hand}) is at or above the reorder point ({record.reorder_point})."
        return ReorderStatus(
            product_id=record.product_id,
            quantity_on_hand=record.quantity_on_hand,
            reorder_point=record.reorder_point,
            target_level=decision.target_level,
            reorder_required=decision.reorder_required,
            recommended_order_quantity=decision.recommended_order_quantity,
            estimated_order_cost_minor=decision.recommended_order_quantity * record.unit_cost_minor,
            currency=record.currency,
            reason=reason,
        )

    def list_low_stock_items(self, limit: int) -> LowStockReport:
        low = [r for r in self._catalog.values() if r.quantity_on_hand < r.reorder_point]
        low.sort(key=lambda r: (-shortfall(r.quantity_on_hand, r.reorder_point), r.product_id))
        items = [
            LowStockItem(
                product_id=r.product_id,
                product_name=r.product_name,
                quantity_on_hand=r.quantity_on_hand,
                reorder_point=r.reorder_point,
                shortfall=shortfall(r.quantity_on_hand, r.reorder_point),
            )
            for r in low[:limit]
        ]
        return LowStockReport(items=items, count=len(items))
