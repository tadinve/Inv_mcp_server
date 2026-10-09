"""Application assembly: MCP server + Streamable HTTP + authentication + health."""

from __future__ import annotations

import logging

import anyio.to_thread
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp

from inventory_mcp import __version__
from inventory_mcp.authentication import (
    AuthenticationMiddleware,
    Authenticator,
    CertificateCache,
    DevAuthenticator,
    GoogleOIDCAuthenticator,
)
from inventory_mcp.authorization import Policy
from inventory_mcp.config import AuthMode, ConfigError, Settings
from inventory_mcp.inventory_service import InventoryService
from inventory_mcp.repository import RestockRepository
from inventory_mcp.tools import ArgumentValidationMiddleware, ToolDependencies, build_tools

logger = logging.getLogger("inventory_mcp.server")

MCP_PATH = "/mcp"
HEALTH_PATH = "/health"

INSTRUCTIONS = (
    "Cresenta inventory service. Read tools need inventory.read. create_restock_request needs "
    "restock.create and only records a request for human review; it never places purchase orders."
)


def build_authenticator(settings: Settings) -> Authenticator:
    if settings.auth_mode is AuthMode.DEV:
        logger.warning(
            "TEST-ONLY dev authentication is enabled: bearer tokens are not verified",
            extra={"fields": {"event": "startup", "auth_mode": "dev"}},
        )
        return DevAuthenticator()
    if settings.auth_mode is AuthMode.GOOGLE and settings.oidc_audience:
        logger.info(
            "Google OIDC authentication enabled",
            extra={"fields": {"event": "startup", "auth_mode": "google", "audience": settings.oidc_audience}},
        )
        return GoogleOIDCAuthenticator(settings.oidc_audience, CertificateCache())
    raise ConfigError(f"unsupported auth mode: {settings.auth_mode}")


def _transport_security(settings: Settings) -> TransportSecuritySettings | None:
    if settings.allowed_hosts:
        return TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=list(settings.allowed_hosts),
            allowed_origins=[],
        )
    # None: the SDK enables localhost-only DNS-rebinding protection when bound to loopback.
    return None


def create_app(
    settings: Settings,
    *,
    inventory: InventoryService | None = None,
    repository: RestockRepository | None = None,
    policy: Policy | None = None,
    authenticator: Authenticator | None = None,
) -> ASGIApp:
    inventory = inventory or InventoryService.from_package_data()
    policy = policy or Policy.from_path(settings.policy_path)
    authenticator = authenticator or build_authenticator(settings)
    if repository is None:
        repository = RestockRepository(settings.db_path)
        repository.initialize()

    tools = build_tools(ToolDependencies(inventory=inventory, repository=repository, policy=policy))
    server = MCPServer(
        name="cresenta-inventory",
        version=__version__,
        instructions=INSTRUCTIONS,
        log_level=settings.log_level,
        tools=tools,
        middleware=[ArgumentValidationMiddleware(tools)],
    )

    @server.custom_route(HEALTH_PATH, methods=["GET"])
    async def health(_: Request) -> JSONResponse:
        try:
            ready = inventory.product_count() > 0 and await _ping(repository)
        except Exception:
            logger.exception("health check failed")
            ready = False
        return JSONResponse({"status": "ok" if ready else "unavailable"}, status_code=200 if ready else 503)

    app = server.streamable_http_app(
        streamable_http_path=MCP_PATH,
        stateless_http=True,
        json_response=True,
        host=settings.host,
        transport_security=_transport_security(settings),
    )
    return AuthenticationMiddleware(app, authenticator, exempt_paths=frozenset({HEALTH_PATH}))


async def _ping(repository: RestockRepository) -> bool:
    return await anyio.to_thread.run_sync(repository.ping)
