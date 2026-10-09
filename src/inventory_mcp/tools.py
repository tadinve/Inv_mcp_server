"""MCP tool handlers.

Every handler goes through `_execute`, which:
  1. reads the verified Principal from the HTTP request (fails closed if absent),
  2. authorizes the specific permission BEFORE any business logic or DB access,
  3. runs the blocking operation in a worker thread,
  4. maps domain errors to typed `isError=true` results and hides internal errors,
  5. emits one structured log line per invocation.

`ArgumentValidationMiddleware` runs first for every `tools/call`: it validates
arguments with the SDK's own validator and, on failure, returns a typed
`invalid_argument` error naming only fields and error types. The SDK's default
message would echo submitted values back to the caller.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated

import anyio.to_thread
from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.tools import Tool
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, Field, ValidationError

from inventory_mcp.authentication import CORRELATION_SCOPE_KEY, PRINCIPAL_SCOPE_KEY, Principal
from inventory_mcp.authorization import Permission, Policy
from inventory_mcp.errors import DomainError, InvalidArgument
from inventory_mcp.inventory_service import InventoryService
from inventory_mcp.repository import RestockRepository
from inventory_mcp.schemas import (
    MAX_RESTOCK_QUANTITY,
    ErrorBody,
    ErrorCode,
    InventoryRecord,
    ListLimit,
    LowStockReport,
    ProductId,
    ReorderStatus,
    RestockRequestList,
    RestockRequestReceipt,
    ToolErrorPayload,
)

logger = logging.getLogger("inventory_mcp.tools")

MIN_JUSTIFICATION_CHARS = 10


@dataclass(frozen=True)
class ToolDependencies:
    inventory: InventoryService
    repository: RestockRepository
    policy: Policy


def _request_scope(ctx: Context) -> dict:
    try:
        request = ctx.request_context.request
    except ValueError:
        return {}
    return getattr(request, "scope", None) or {}


def _error_result(code: ErrorCode, message: str) -> CallToolResult:
    payload = ToolErrorPayload(error=ErrorBody(code=code, message=message)).model_dump(mode="json")
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload))],
        structured_content=payload,
        is_error=True,
    )


def _success_result(model: BaseModel) -> CallToolResult:
    payload = model.model_dump(mode="json")
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(payload))], structured_content=payload)


async def _execute(
    ctx: Context,
    deps: ToolDependencies,
    tool: str,
    permission: Permission,
    operation: Callable[[Principal], BaseModel],
) -> CallToolResult:
    started = time.perf_counter()
    scope = _request_scope(ctx)
    principal: Principal | None = scope.get(PRINCIPAL_SCOPE_KEY)
    fields: dict[str, object] = {
        "event": "tool_call",
        "tool": tool,
        "correlation_id": scope.get(CORRELATION_SCOPE_KEY),
        "principal": principal.label if principal else None,
        "required_permission": permission.value,
    }

    def finish(outcome: str, result: CallToolResult, level: int = logging.INFO, **extra: object) -> CallToolResult:
        fields.update(outcome=outcome, latency_ms=round((time.perf_counter() - started) * 1000, 2), **extra)
        logger.log(level, f"{tool} {outcome}", extra={"fields": fields})
        return result

    decision = deps.policy.authorize(principal, permission)
    fields["authorization"] = "allow" if decision.allowed else "deny"
    if not decision.allowed:
        if principal is None:
            return finish(
                "denied",
                _error_result("unauthenticated", "Authentication is required."),
                logging.WARNING,
                error_category="unauthenticated",
            )
        return finish(
            "denied",
            _error_result("forbidden", f"Caller is not authorized to call {tool}."),
            logging.WARNING,
            error_category="forbidden",
        )

    try:
        model = await anyio.to_thread.run_sync(operation, principal)
    except DomainError as exc:
        return finish("error", _error_result(exc.code, exc.message), error_category=exc.code)
    except Exception:
        logger.exception("unexpected tool failure", extra={"fields": dict(fields)})
        return finish(
            "error", _error_result("internal_error", "Internal error."), logging.ERROR, error_category="internal_error"
        )
    return finish("success", _success_result(model))


class ArgumentValidationMiddleware:
    """Replaces the SDK's argument-validation error text with a sanitized, typed error.

    Uses the same validator the SDK runs (`FuncMetadata.validate_arguments`), so it
    accepts and rejects exactly the same inputs. Anything it does not recognize
    (unknown tools, malformed params, other methods) passes through unchanged.
    """

    def __init__(self, tools: list[Tool]) -> None:
        self._tools = {tool.name: tool for tool in tools}

    async def __call__(self, ctx: ServerRequestContext, call_next: CallNext) -> HandlerResult:
        params = ctx.params if ctx.method == "tools/call" else None
        tool = self._tools.get(params.get("name")) if params and isinstance(params.get("name"), str) else None
        arguments = params.get("arguments") if params else None
        if tool is None or not isinstance(arguments if arguments is not None else {}, dict):
            return await call_next(ctx)
        try:
            tool.fn_metadata.validate_arguments(arguments or {})
        except ValidationError as exc:
            known = set(tool.parameters.get("properties", {}))
            problems = sorted({(_field_name(err["loc"], known), err["type"]) for err in exc.errors()})
            logger.info(
                f"{tool.name} rejected arguments",
                extra={
                    "fields": {
                        "event": "tool_call",
                        "tool": tool.name,
                        "outcome": "invalid_arguments",
                        "error_category": "invalid_argument",
                        "fields": [f for f, _ in problems],
                    }
                },
            )
            detail = "; ".join(f"{field}: {kind}" for field, kind in problems)
            return _error_result("invalid_argument", f"Arguments do not match the input schema ({detail}).")
        except Exception:
            return await call_next(ctx)  # not a schema failure: let the SDK report it
        return await call_next(ctx)


def _field_name(loc: tuple, known: set[str]) -> str:
    """Field path for a validation error. Names the caller invented are not echoed."""
    if not loc:
        return "<arguments>"
    head = str(loc[0])
    return ".".join(str(part) for part in loc) if head in known else "<unrecognized field>"


def build_tools(deps: ToolDependencies) -> list[Tool]:
    read_only = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
    tools: list[Tool] = []

    def tool(annotations: ToolAnnotations) -> Callable[[Callable], Callable]:
        def register(fn: Callable) -> Callable:
            tools.append(Tool.from_function(fn, annotations=annotations))
            return fn

        return register

    @tool(annotations=read_only)
    async def get_inventory(product_id: ProductId, ctx: Context) -> Annotated[CallToolResult, InventoryRecord]:
        """Get current stock for one product across all Cresenta warehouses. Requires inventory.read."""
        return await _execute(
            ctx, deps, "get_inventory", Permission.INVENTORY_READ, lambda _: deps.inventory.get_inventory(product_id)
        )

    @tool(annotations=read_only)
    async def get_reorder_status(product_id: ProductId, ctx: Context) -> Annotated[CallToolResult, ReorderStatus]:
        """Decide whether a product needs reordering and how much to order.

        Formula: target_level = 2 x reorder_point; reorder when on_hand < reorder_point;
        recommended quantity = target_level - on_hand. Requires inventory.read.
        """
        return await _execute(
            ctx,
            deps,
            "get_reorder_status",
            Permission.INVENTORY_READ,
            lambda _: deps.inventory.get_reorder_status(product_id),
        )

    @tool(annotations=read_only)
    async def list_low_stock_items(ctx: Context, limit: ListLimit = 20) -> Annotated[CallToolResult, LowStockReport]:
        """List products below their reorder point, largest shortfall first. Requires inventory.read."""
        return await _execute(
            ctx,
            deps,
            "list_low_stock_items",
            Permission.INVENTORY_READ,
            lambda _: deps.inventory.list_low_stock_items(limit),
        )

    @tool(
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)
    )
    async def create_restock_request(
        product_id: ProductId,
        quantity: Annotated[int, Field(ge=1, le=MAX_RESTOCK_QUANTITY, description="Units requested.")],
        justification: Annotated[
            str, Field(min_length=MIN_JUSTIFICATION_CHARS, max_length=500, description="Why the restock is needed.")
        ],
        idempotency_key: Annotated[
            uuid.UUID, Field(description="Client-generated UUID. Retrying with the same key never duplicates.")
        ],
        ctx: Context,
    ) -> Annotated[CallToolResult, RestockRequestReceipt]:
        """Record a restock REQUEST for human review. Does not place a purchase order or contact suppliers.

        Requires restock.create. Idempotent per caller and idempotency_key: an identical retry returns the
        original request; reusing a key with different details is rejected.
        """

        def operation(principal: Principal) -> RestockRequestReceipt:
            cleaned = justification.strip()
            if len(cleaned) < MIN_JUSTIFICATION_CHARS:
                raise InvalidArgument(
                    f"justification must contain at least {MIN_JUSTIFICATION_CHARS} non-whitespace-padded characters."
                )
            deps.inventory.get_inventory(product_id)  # raises ProductNotFound
            outcome = deps.repository.create(
                principal_sub=principal.subject,
                idempotency_key=str(idempotency_key),
                product_id=product_id,
                quantity=quantity,
                justification=cleaned,
            )
            return RestockRequestReceipt(**outcome.record.model_dump(), idempotent_replay=outcome.replayed)

        return await _execute(ctx, deps, "create_restock_request", Permission.RESTOCK_CREATE, operation)

    @tool(annotations=read_only)
    async def list_restock_requests(
        ctx: Context, limit: ListLimit = 20
    ) -> Annotated[CallToolResult, RestockRequestList]:
        """List restock requests submitted by the calling identity, newest first. Requires inventory.read."""

        def operation(principal: Principal) -> RestockRequestList:
            records = deps.repository.list_for_principal(principal.subject, limit)
            return RestockRequestList(requests=records, count=len(records))

        return await _execute(ctx, deps, "list_restock_requests", Permission.INVENTORY_READ, operation)

    return tools
