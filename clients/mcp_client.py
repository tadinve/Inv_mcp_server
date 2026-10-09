"""Standalone MCP client for the Cresenta inventory server. No LLM, no agent framework.

Usage:
    uv run python clients/mcp_client.py demo  --token dev:writer
    uv run python clients/mcp_client.py tools --token dev:reader
    uv run python clients/mcp_client.py call get_inventory '{"product_id": "CRS-1002"}' --token dev:reader

The bearer token can also come from MCP_BEARER_TOKEN; the URL from MCP_SERVER_URL.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from typing import Any

import httpx2
from mcp import Client, MCPError
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult

DEFAULT_URL = "http://127.0.0.1:8000/mcp"


def connect(url: str, token: str | None) -> Client:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    http_client = httpx2.AsyncClient(headers=headers, timeout=httpx2.Timeout(30.0, read=60.0))
    return Client(streamable_http_client(url, http_client=http_client))


def _print(label: str, payload: Any) -> None:
    print(f"\n=== {label} ===")
    print(json.dumps(payload, indent=2, default=str) if not isinstance(payload, str) else payload)


def describe_result(result: CallToolResult) -> dict[str, Any]:
    if result.structured_content is not None:
        body: Any = result.structured_content
    else:
        body = [getattr(block, "text", repr(block)) for block in result.content]
    return {"isError": bool(result.is_error), "result": body}


async def call(client: Client, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        return describe_result(await client.call_tool(name, arguments))
    except MCPError as exc:
        return {"protocolError": {"code": exc.code, "message": str(exc)}}


async def show_session(client: Client) -> None:
    info = client.server_info
    _print(
        "Connected",
        {
            "protocol_version": client.protocol_version,
            "server": {"name": info.name, "version": info.version} if info else None,
            "tools_capability": client.server_capabilities.tools is not None,
        },
    )


async def show_tools(client: Client) -> None:
    listing = await client.list_tools()
    for tool in listing.tools:
        _print(
            f"Tool: {tool.name}",
            {
                "description": tool.description,
                "annotations": tool.annotations.model_dump(exclude_none=True) if tool.annotations else None,
                "inputSchema": tool.input_schema,
                "outputSchema": tool.output_schema,
            },
        )


async def demo(client: Client) -> None:
    await show_session(client)
    listing = await client.list_tools()
    _print("Discovered tools", [t.name for t in listing.tools])

    key = str(uuid.uuid4())
    restock = {
        "product_id": "CRS-1002",
        "quantity": 95,
        "justification": "Below reorder point; restore target level per reorder status.",
        "idempotency_key": key,
    }
    steps: list[tuple[str, str, dict[str, Any]]] = [
        ("Read inventory", "get_inventory", {"product_id": "CRS-1002"}),
        ("Reorder status", "get_reorder_status", {"product_id": "CRS-1002"}),
        ("Low-stock items (limit 5)", "list_low_stock_items", {"limit": 5}),
        ("Unknown product -> typed error", "get_inventory", {"product_id": "CRS-9999"}),
        ("Invalid argument -> rejected", "get_inventory", {"product_id": "not-an-id"}),
        ("Restock requests before", "list_restock_requests", {}),
        ("Create restock request", "create_restock_request", restock),
        ("Retry with the same idempotency key", "create_restock_request", restock),
        ("Same key, different quantity -> conflict", "create_restock_request", {**restock, "quantity": 10}),
        ("Restock requests after", "list_restock_requests", {}),
    ]
    for label, name, arguments in steps:
        _print(f"{label}  [{name}]", await call(client, name, arguments))


async def run(args: argparse.Namespace) -> int:
    try:
        async with connect(args.url, args.token) as client:
            if args.command == "tools":
                await show_session(client)
                await show_tools(client)
            elif args.command == "call":
                _print(args.tool, await call(client, args.tool, json.loads(args.arguments)))
            else:
                await demo(client)
    except Exception as exc:  # transport failures can arrive wrapped in an ExceptionGroup
        for leaf in _leaves(exc):
            if isinstance(leaf, httpx2.HTTPStatusError):
                print(f"HTTP error: {leaf.response.status_code}", file=sys.stderr)
            elif isinstance(leaf, MCPError):
                print(f"MCP error {leaf.code}: {leaf}", file=sys.stderr)
            else:
                print(f"Connection failed: {type(leaf).__name__}: {leaf}", file=sys.stderr)
        return 1
    return 0


def _leaves(exc: BaseException) -> list[BaseException]:
    if isinstance(exc, BaseExceptionGroup):
        return [leaf for inner in exc.exceptions for leaf in _leaves(inner)]
    return [exc]


def main() -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--url", default=os.environ.get("MCP_SERVER_URL", DEFAULT_URL))
    common.add_argument("--token", default=os.environ.get("MCP_BEARER_TOKEN"))
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter, parents=[common]
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("demo", parents=[common], help="discover tools and exercise every tool (default)")
    sub.add_parser("tools", parents=[common], help="list tools with input and output schemas")
    call_parser = sub.add_parser("call", parents=[common], help="call one tool")
    call_parser.add_argument("tool")
    call_parser.add_argument("arguments", nargs="?", default="{}")
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
