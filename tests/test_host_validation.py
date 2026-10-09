"""Host-header pinning (DNS-rebinding protection) as configured for Cloud Run.

Runs the real app with INVENTORY_ALLOWED_HOSTS set, the way the Cloud Run
deployment does, and checks what the SDK's transport security actually does.
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx2
import pytest

from inventory_mcp.authorization import Policy
from inventory_mcp.config import Settings
from inventory_mcp.repository import RestockRepository
from inventory_mcp.server import create_app
from tests.conftest import DEV_POLICY, LiveServer, serve

PINNED = "cresenta-inventory-123456789012.us-central1.run.app"
BODY = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
BASE_HEADERS = {
    "Authorization": "Bearer dev:reader",
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


@pytest.fixture(scope="module")
def pinned_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[LiveServer]:
    db_path = tmp_path_factory.mktemp("host") / "restock.db"
    settings = Settings.from_env(
        {
            "INVENTORY_AUTH_MODE": "dev",
            "INVENTORY_POLICY_PATH": str(DEV_POLICY),
            "INVENTORY_DB_PATH": str(db_path),
            "INVENTORY_ALLOWED_HOSTS": PINNED,
        }
    )
    repository = RestockRepository(db_path)
    repository.initialize()
    with serve(create_app(settings, repository=repository, policy=Policy.from_path(DEV_POLICY)), repository) as live:
        yield live


def _post(server: LiveServer, **headers: str) -> httpx2.Response:
    return httpx2.post(server.mcp_url, json=BODY, headers={**BASE_HEADERS, **headers})


def test_pinned_host_is_accepted(pinned_server: LiveServer) -> None:
    assert _post(pinned_server, Host=PINNED).status_code == 200


@pytest.mark.parametrize("host", ["evil.example.com", "127.0.0.1", "other-service.run.app", f"{PINNED}.evil.com"])
def test_other_hosts_are_rejected_with_421(pinned_server: LiveServer, host: str) -> None:
    response = _post(pinned_server, Host=host)
    assert response.status_code == 421


def test_browser_origin_is_rejected(pinned_server: LiveServer) -> None:
    response = _post(pinned_server, Host=PINNED, Origin="https://evil.example.com")
    assert response.status_code == 403


def test_host_check_runs_after_authentication(pinned_server: LiveServer) -> None:
    """A wrong Host without credentials gets 401 first: unauthenticated callers learn nothing else."""
    response = httpx2.post(pinned_server.mcp_url, json=BODY, headers={"Host": "evil.example.com"})
    assert response.status_code == 401


@pytest.mark.parametrize("host", ["localhost:8080", "10.0.0.7:8080", "evil.example.com"])
def test_health_is_not_host_pinned(pinned_server: LiveServer, host: str) -> None:
    """Cloud Run startup probes reach the container directly; /health must answer whatever Host they send."""
    response = httpx2.get(f"{pinned_server.base_url}/health", headers={"Host": host})
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
