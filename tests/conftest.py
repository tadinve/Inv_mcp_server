"""Shared fixtures: a real uvicorn server on a free localhost port, per test module.

Tests talk to it over real Streamable HTTP with the official MCP client, so
protocol behavior is exercised end to end (no in-process shortcuts).
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx2
import pytest
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from starlette.types import ASGIApp

from inventory_mcp.authorization import Policy
from inventory_mcp.config import Settings
from inventory_mcp.repository import RestockRepository
from inventory_mcp.server import create_app

REPO_ROOT = Path(__file__).resolve().parent.parent
DEV_POLICY = REPO_ROOT / "config" / "policy.dev.json"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@dataclass(frozen=True)
class LiveServer:
    base_url: str
    repository: RestockRepository

    @property
    def mcp_url(self) -> str:
        return f"{self.base_url}/mcp"


@contextmanager
def serve(app: ASGIApp, repository: RestockRepository) -> Iterator[LiveServer]:
    """Run an ASGI app under a real uvicorn server on a free localhost port."""
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("test server did not start")
        time.sleep(0.05)
    try:
        yield LiveServer(base_url=f"http://127.0.0.1:{port}", repository=repository)
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@pytest.fixture(scope="module")
def live_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[LiveServer]:
    db_path = tmp_path_factory.mktemp("db") / "restock.db"
    settings = Settings.from_env(
        {
            "INVENTORY_AUTH_MODE": "dev",
            "INVENTORY_POLICY_PATH": str(DEV_POLICY),
            "INVENTORY_DB_PATH": str(db_path),
        }
    )
    repository = RestockRepository(db_path)
    repository.initialize()
    app = create_app(settings, repository=repository, policy=Policy.from_path(DEV_POLICY))
    with serve(app, repository) as live:
        yield live


@asynccontextmanager
async def mcp_session(server: LiveServer, token: str | None, **client_kwargs) -> AsyncIterator[Client]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    http_client = httpx2.AsyncClient(headers=headers, timeout=10.0)
    async with Client(streamable_http_client(server.mcp_url, http_client=http_client), **client_kwargs) as client:
        yield client
