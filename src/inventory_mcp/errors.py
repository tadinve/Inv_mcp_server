"""Domain errors. Each maps to a typed `isError=true` tool result."""

from __future__ import annotations

from inventory_mcp.schemas import ErrorCode


class DomainError(Exception):
    code: ErrorCode = "internal_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ProductNotFound(DomainError):
    code: ErrorCode = "not_found"


class InvalidArgument(DomainError):
    code: ErrorCode = "invalid_argument"


class IdempotencyConflict(DomainError):
    code: ErrorCode = "idempotency_conflict"
