# Demonstration walkthrough

Two versions of the same story: locally in about 2 minutes with no cloud account,
and on Cloud Run with real Google identities.

The story: *a reader can look but not write; a writer can raise a restock
request; a retry never duplicates it; an outsider cannot even reach the service.*

## Local (no cloud)

Terminal 1:

```bash
uv sync
INVENTORY_AUTH_MODE=dev INVENTORY_POLICY_PATH=config/policy.dev.json uv run python -m inventory_mcp
```

Terminal 2:

```bash
uv run python clients/mcp_client.py tools --token dev:reader
uv run python clients/mcp_client.py call get_inventory '{"product_id":"CRS-1002"}' --token dev:reader
uv run python clients/mcp_client.py call create_restock_request '{"product_id":"CRS-1002","quantity":95,"justification":"Below reorder point.","idempotency_key":"7d1f4a52-1111-4c4c-9a9a-000000000001"}' --token dev:reader
uv run python clients/mcp_client.py call list_restock_requests '{}' --token dev:reader
uv run python clients/mcp_client.py call create_restock_request '{"product_id":"CRS-1002","quantity":95,"justification":"Below reorder point.","idempotency_key":"7d1f4a52-1111-4c4c-9a9a-000000000001"}' --token dev:writer
uv run python clients/mcp_client.py call create_restock_request '{"product_id":"CRS-1002","quantity":95,"justification":"Below reorder point.","idempotency_key":"7d1f4a52-1111-4c4c-9a9a-000000000001"}' --token dev:writer
uv run python clients/mcp_client.py call list_restock_requests '{}' --token dev:writer
```

What to point out:

| Step | Expected |
|---|---|
| `tools` | 5 tools with input and output schemas; protocol `2026-07-28` |
| Reader `get_inventory` | CRS-1002: 25 on hand, cost in integer minor units |
| Reader `create_restock_request` | `isError: true`, `code: forbidden` |
| Reader `list_restock_requests` | `count: 0`: the denied write left nothing behind |
| Writer create (1st) | `idempotent_replay: false` |
| Writer create (2nd, same key) | Same `request_id`, `idempotent_replay: true` |
| Writer list | `count: 1`: no duplicate |

Terminal 1 shows one structured JSON log line per call, including
`"authorization": "deny"` for the reader's write. No token appears in any log.

To start from an empty restock database, delete `var/restock_requests.db` first.

## Cloud Run (real Google identities)

Prerequisites: a deployment from [deployment/README.md](../deployment/README.md).

```bash
./deployment/demo_cloud_run.sh
```

| Step | Expected |
|---|---|
| 1. Discover tools as `mcp-reader` | 5 tools, protocol `2026-07-28` |
| 2. Reader reads CRS-1002 | Typed record |
| 3. Writer baseline | Current count N |
| 4. Reader creates request | `forbidden` |
| 5. Counts | Reader 0, writer still N |
| 6. Writer creates request | `idempotent_replay: false`; `submitted_by` = the writer's verified `sub` |
| 7. Same key again | Same `request_id`, `idempotent_replay: true` |
| 8. Writer count | N + 1 |
| 9. Outsider | `HTTP 403` from Cloud Run; never reaches the app |
| 10. Cloud Logging | One row per call: tool, principal, allow/deny, outcome |

Observed results from the live run: [deployment/RESULTS.md](../deployment/RESULTS.md).

## Talking points

- **Two boundaries.** Cloud Run IAM decides *who may reach* the service. The app
  decides *what each caller may do*, per tool, from the verified token `sub`.
- **Verify, don't trust.** The app re-verifies the Google-signed token. Measured:
  Cloud Run forwards it intact in `Authorization` but strips the signature from
  `X-Serverless-Authorization`, so the app refuses the latter.
- **Discovery isn't permission.** Every authenticated caller sees all five tools.
  Calls are authorized individually.
- **Safe retries.** Idempotency keys are scoped per caller. Eight concurrent
  identical requests persist once.
- **Honest limits.** One instance, ephemeral SQLite, no MCP OAuth. See the README.
