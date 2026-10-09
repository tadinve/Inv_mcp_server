"""SQLite persistence for restock requests, with principal-scoped idempotency.

Each call opens its own connection, so the repository is safe to use from
worker threads. Writes run inside `BEGIN IMMEDIATE`, which takes SQLite's
write lock up front; together with the UNIQUE constraint this makes
insert-or-replay atomic (SPEC-AMENDMENT-1, A5).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from inventory_mcp.errors import IdempotencyConflict
from inventory_mcp.schemas import RestockRequestRecord

_SCHEMA = """
CREATE TABLE IF NOT EXISTS restock_requests (
    request_id      TEXT PRIMARY KEY,
    principal_sub   TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    fingerprint     TEXT NOT NULL,
    product_id      TEXT NOT NULL,
    quantity        INTEGER NOT NULL CHECK (quantity > 0),
    justification   TEXT NOT NULL,
    status          TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    UNIQUE (principal_sub, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_restock_principal_created
    ON restock_requests (principal_sub, created_at);
"""

_SELECT_BY_KEY = """
SELECT request_id, principal_sub, product_id, quantity, justification, status, created_at, fingerprint
FROM restock_requests
WHERE principal_sub = ? AND idempotency_key = ?
"""

_SELECT_FOR_PRINCIPAL = """
SELECT request_id, principal_sub, product_id, quantity, justification, status, created_at
FROM restock_requests
WHERE principal_sub = ?
ORDER BY created_at DESC, request_id ASC
LIMIT ?
"""


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def request_fingerprint(product_id: str, quantity: int, justification: str) -> str:
    """SHA-256 over canonical JSON of the business payload (keys sorted, no whitespace)."""
    canonical = json.dumps(
        {"justification": justification, "product_id": product_id, "quantity": quantity},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CreateOutcome:
    record: RestockRequestRecord
    replayed: bool


class RestockRepository:
    def __init__(self, db_path: Path, clock: Callable[[], str] = _utc_now) -> None:
        self._db_path = db_path
        self._clock = clock

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        # isolation_level=None: we issue BEGIN/COMMIT explicitly.
        conn = sqlite3.connect(self._db_path, timeout=10.0, isolation_level=None)
        try:
            conn.execute("PRAGMA busy_timeout = 10000")
            yield conn
        finally:
            conn.close()

    def initialize(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(_SCHEMA)

    def ping(self) -> bool:
        with self._connect() as conn:
            conn.execute("SELECT 1 FROM restock_requests LIMIT 1").fetchall()
        return True

    def create(
        self,
        *,
        principal_sub: str,
        idempotency_key: str,
        product_id: str,
        quantity: int,
        justification: str,
    ) -> CreateOutcome:
        fingerprint = request_fingerprint(product_id, quantity, justification)
        new_id = str(uuid.uuid4())
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO restock_requests
                        (request_id, principal_sub, idempotency_key, fingerprint,
                         product_id, quantity, justification, status, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'pending_review', ?)
                    ON CONFLICT (principal_sub, idempotency_key) DO NOTHING
                    """,
                    (
                        new_id,
                        principal_sub,
                        idempotency_key,
                        fingerprint,
                        product_id,
                        quantity,
                        justification,
                        self._clock(),
                    ),
                )
                inserted = cursor.rowcount == 1
                row = conn.execute(_SELECT_BY_KEY, (principal_sub, idempotency_key)).fetchone()
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        if not inserted and row[-1] != fingerprint:
            raise IdempotencyConflict(
                "This idempotency key was already used for a different restock request. "
                "Use a new key for a new request."
            )
        return CreateOutcome(record=_to_record(row[:-1]), replayed=not inserted)

    def list_for_principal(self, principal_sub: str, limit: int) -> list[RestockRequestRecord]:
        with self._connect() as conn:
            rows = conn.execute(_SELECT_FOR_PRINCIPAL, (principal_sub, limit)).fetchall()
        return [_to_record(row) for row in rows]

    def count_all(self) -> int:
        """Total rows across all principals. Used by tests to prove denied writes do not mutate state."""
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM restock_requests").fetchone()[0]


def _to_record(row: tuple) -> RestockRequestRecord:
    request_id, principal_sub, product_id, quantity, justification, status, created_at = row
    return RestockRequestRecord(
        request_id=request_id,
        product_id=product_id,
        quantity=quantity,
        justification=justification,
        status=status,
        created_at=created_at,
        submitted_by=principal_sub,
    )
