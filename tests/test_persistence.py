"""SQLite persistence and principal-scoped idempotency (SPEC-AMENDMENT-1, A5).

Uses a file-backed database and real threads for the concurrency tests.
"""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from inventory_mcp.errors import IdempotencyConflict
from inventory_mcp.repository import CreateOutcome, RestockRepository, request_fingerprint


@pytest.fixture
def repo(tmp_path: Path) -> RestockRepository:
    repository = RestockRepository(tmp_path / "restock.db")
    repository.initialize()
    return repository


def _create(
    repo: RestockRepository,
    *,
    sub: str = "dev:writer",
    key: str | None = None,
    product: str = "CRS-1002",
    quantity: int = 10,
    why: str = "restore stock level",
) -> CreateOutcome:
    return repo.create(
        principal_sub=sub,
        idempotency_key=key or str(uuid.uuid4()),
        product_id=product,
        quantity=quantity,
        justification=why,
    )


def test_new_key_creates_request(repo: RestockRepository) -> None:
    outcome = _create(repo)
    assert outcome.replayed is False
    assert outcome.record.status == "pending_review"
    assert outcome.record.submitted_by == "dev:writer"
    assert uuid.UUID(outcome.record.request_id)
    assert repo.count_all() == 1


def test_same_key_same_payload_replays_original(repo: RestockRepository) -> None:
    key = str(uuid.uuid4())
    first = _create(repo, key=key)
    second = _create(repo, key=key)
    assert second.replayed is True
    assert second.record == first.record
    assert repo.count_all() == 1


def test_same_key_different_payload_conflicts_without_writing(repo: RestockRepository) -> None:
    key = str(uuid.uuid4())
    _create(repo, key=key, quantity=10)
    with pytest.raises(IdempotencyConflict):
        _create(repo, key=key, quantity=11)
    assert repo.count_all() == 1


def test_idempotency_is_scoped_per_principal(repo: RestockRepository) -> None:
    key = str(uuid.uuid4())
    a = _create(repo, sub="dev:writer", key=key)
    b = _create(repo, sub="dev:other-writer", key=key)
    assert a.record.request_id != b.record.request_id
    assert b.replayed is False
    assert repo.count_all() == 2


def test_list_returns_only_the_callers_requests(repo: RestockRepository) -> None:
    _create(repo, sub="dev:writer")
    _create(repo, sub="dev:writer")
    _create(repo, sub="dev:other-writer")
    mine = repo.list_for_principal("dev:writer", limit=20)
    assert len(mine) == 2
    assert {r.submitted_by for r in mine} == {"dev:writer"}
    assert len(repo.list_for_principal("dev:writer", limit=1)) == 1


def test_list_order_is_newest_first(tmp_path: Path) -> None:
    ticks = iter(["2026-01-01T00:00:00.000Z", "2026-01-02T00:00:00.000Z", "2026-01-03T00:00:00.000Z"])
    repo = RestockRepository(tmp_path / "ordered.db", clock=lambda: next(ticks))
    repo.initialize()
    ids = [_create(repo).record.request_id for _ in range(3)]
    assert [r.request_id for r in repo.list_for_principal("dev:writer", 20)] == list(reversed(ids))


def test_fingerprint_is_canonical_and_payload_sensitive() -> None:
    base = request_fingerprint("CRS-1002", 10, "restore stock level")
    assert base == request_fingerprint("CRS-1002", 10, "restore stock level")
    assert base != request_fingerprint("CRS-1002", 11, "restore stock level")
    assert base != request_fingerprint("CRS-1003", 10, "restore stock level")
    assert base != request_fingerprint("CRS-1002", 10, "restore stock level!")


def test_data_survives_a_new_repository_instance(tmp_path: Path) -> None:
    path = tmp_path / "persist.db"
    first = RestockRepository(path)
    first.initialize()
    created = _create(first)
    second = RestockRepository(path)
    second.initialize()
    assert second.list_for_principal("dev:writer", 20)[0].request_id == created.record.request_id


def test_concurrent_identical_submissions_persist_exactly_once(repo: RestockRepository) -> None:
    key = str(uuid.uuid4())
    workers = 16
    barrier = threading.Barrier(workers)

    def submit(_: int) -> CreateOutcome:
        barrier.wait()  # release all threads together to maximize contention
        return _create(repo, key=key)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        outcomes = list(pool.map(submit, range(workers)))

    assert repo.count_all() == 1
    assert len({o.record.request_id for o in outcomes}) == 1
    assert sum(1 for o in outcomes if not o.replayed) == 1


def test_concurrent_conflicting_submissions_create_one_and_reject_the_rest(repo: RestockRepository) -> None:
    key = str(uuid.uuid4())
    workers = 12
    barrier = threading.Barrier(workers)

    def submit(i: int) -> str:
        barrier.wait()
        try:
            _create(repo, key=key, quantity=100 + i)  # every payload differs
            return "created"
        except IdempotencyConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(submit, range(workers)))

    assert results.count("created") == 1
    assert results.count("conflict") == workers - 1
    assert repo.count_all() == 1
