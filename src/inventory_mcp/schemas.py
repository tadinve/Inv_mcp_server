"""Typed contracts for tool inputs and outputs.

These models are the single source of truth for the JSON Schemas the MCP server
publishes in `tools/list` (input and output schemas).
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

PRODUCT_ID_PATTERN = r"^CRS-\d{4}$"
MAX_LIST_LIMIT = 100
MAX_RESTOCK_QUANTITY = 10_000

ProductId = Annotated[
    str,
    Field(pattern=PRODUCT_ID_PATTERN, description="Cresenta product ID, e.g. CRS-1002.", examples=["CRS-1002"]),
]
ListLimit = Annotated[
    int, Field(ge=1, le=MAX_LIST_LIMIT, description=f"Maximum results to return (1-{MAX_LIST_LIMIT}).")
]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class WarehouseStock(_Model):
    warehouse_id: str
    warehouse_name: str
    quantity: int = Field(ge=0)


class InventoryRecord(_Model):
    product_id: str
    product_name: str
    supplier: str
    quantity_on_hand: int = Field(ge=0, description="Sum of quantities across all warehouses.")
    reorder_point: int = Field(ge=0)
    unit_cost_minor: int = Field(ge=0, description="Unit cost in minor currency units (e.g. cents).")
    currency: str = Field(description="ISO 4217 currency code.")
    warehouses: list[WarehouseStock]


class ReorderStatus(_Model):
    product_id: str
    quantity_on_hand: int
    reorder_point: int
    target_level: int = Field(description="2 x reorder_point.")
    reorder_required: bool = Field(description="True when quantity_on_hand < reorder_point.")
    recommended_order_quantity: int = Field(ge=0)
    estimated_order_cost_minor: int = Field(ge=0, description="recommended_order_quantity x unit_cost_minor.")
    currency: str
    reason: str


class LowStockItem(_Model):
    product_id: str
    product_name: str
    quantity_on_hand: int
    reorder_point: int
    shortfall: int = Field(description="reorder_point - quantity_on_hand.")


class LowStockReport(_Model):
    items: list[LowStockItem]
    count: int


class RestockRequestRecord(_Model):
    request_id: str
    product_id: str
    quantity: int
    justification: str
    status: Literal["pending_review"]
    created_at: str = Field(description="UTC timestamp, ISO 8601.")
    submitted_by: str = Field(description="Verified subject of the principal that submitted the request.")


class RestockRequestReceipt(RestockRequestRecord):
    idempotent_replay: bool = Field(
        description="True when this response replays an earlier request with the same idempotency key."
    )


class RestockRequestList(_Model):
    requests: list[RestockRequestRecord]
    count: int


ErrorCode = Literal[
    "unauthenticated",
    "forbidden",
    "not_found",
    "invalid_argument",
    "idempotency_conflict",
    "internal_error",
]


class ErrorBody(_Model):
    code: ErrorCode
    message: str


class ToolErrorPayload(_Model):
    """Structured content of every `isError=true` tool result produced by this server."""

    error: ErrorBody
