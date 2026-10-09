"""Entry point: `python -m inventory_mcp`."""

from __future__ import annotations

import logging
import sys

import uvicorn

from inventory_mcp.config import ConfigError, Settings
from inventory_mcp.observability import configure_logging
from inventory_mcp.server import create_app


def main() -> int:
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        configure_logging("INFO")
        logging.getLogger("inventory_mcp").critical(f"refusing to start: {exc}")
        return 2
    configure_logging(settings.log_level)
    try:
        app = create_app(settings)
    except ConfigError as exc:
        logging.getLogger("inventory_mcp").critical(f"refusing to start: {exc}")
        return 2
    # MCPServer installs its own log handler; restore ours afterwards.
    configure_logging(settings.log_level)
    uvicorn.run(app, host=settings.host, port=settings.port, log_config=None, timeout_graceful_shutdown=10)
    return 0


if __name__ == "__main__":
    sys.exit(main())
