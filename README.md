# secure-mcp-server-gcp

> Cresenta needs enterprise applications and AI assistants to read inventory
> safely, while only specifically authorized callers can initiate restock
> requests.

A standalone [Model Context Protocol](https://modelcontextprotocol.io) server,
written in Python with the official MCP SDK. It exposes inventory tools for
**Cresenta**, a fictional company with synthetic data, and separates *who
the caller is* (authentication) from *what they may do* (authorization for
each tool).

This is an **educational reference implementation**, not a production-certified
service. The server contains no AI agent and no LLM. Any MCP client can consume
it, including AI assistants.

## Status

| Checkpoint | Scope | State |
|---|---|---|
| 1. MCP fundamentals | Five tools, synthetic data, deterministic logic, standalone client, tests | **Done** (local) |
| 2. Application security | Google OIDC verification, `K_SERVICE` guard, simulated adversarial tests, Cloud Run token PoC | **Code done locally; PoC prepared, not deployed** |
| 3. Containers + Cloud Run | Dockerfile, IAM-protected deployment, real Cloud Run tests | Not started |
| 4. Portfolio readiness | Architecture docs, CI, threat model, diagrams | Not started |

Two authentication modes exist:
- `google`: verifies Google-signed OIDC ID tokens in-process. This is the deployed design.
- `dev`: **TEST-ONLY**, and refused on Cloud Run.

Google mode is tested locally only, with **simulated** keys. Whether Cloud Run
forwards the signed token intact is unverified until the PoC in
[deployment/poc/](deployment/poc/README.md) runs.

## Tools

| Tool | Permission | Purpose |
|---|---|---|
| `get_inventory` | `inventory.read` | Stock for one product across all warehouses |
| `get_reorder_status` | `inventory.read` | Whether to reorder, and how much |
| `list_low_stock_items` | `inventory.read` | Products below their reorder point, largest shortfall first |
| `list_restock_requests` | `inventory.read` | The caller's own restock requests |
| `create_restock_request` | `restock.create` | Record a restock *request* for human review. Never places an order |

Every tool publishes typed input and output JSON Schemas generated from Pydantic
models. Money is in integer minor units (`unit_cost_minor`, `currency`).

**Reorder formula**

```
target_level     = 2 × reorder_point
reorder_required = on_hand < reorder_point
recommended      = max(0, target_level − on_hand) if reorder_required else 0
```

**Idempotency.** `create_restock_request` requires a UUID `idempotency_key`.
Requests are unique per `(caller subject, key)`:
- An identical retry returns the original request with `idempotent_replay: true`.
- Reusing a key with different details returns an `idempotency_conflict` error.
- Concurrent duplicates are stored exactly once.

## Run locally

Requires Python 3.13+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
INVENTORY_AUTH_MODE=dev INVENTORY_POLICY_PATH=config/policy.dev.json \
  uv run python -m inventory_mcp
# serves MCP at http://127.0.0.1:8000/mcp and GET /health
```

In another terminal:

```bash
uv run python clients/mcp_client.py demo  --token dev:writer   # exercise every tool
uv run python clients/mcp_client.py tools --token dev:reader   # list tools and schemas
uv run python clients/mcp_client.py call create_restock_request \
  '{"product_id":"CRS-1002","quantity":5,"justification":"Reader tries to write","idempotency_key":"6f1c1f1e-1111-4a4a-9b9b-000000000001"}' \
  --token dev:reader                                             # -> forbidden
```

**MCP Inspector:** run `npx @modelcontextprotocol/inspector` and connect with
transport *Streamable HTTP* to `http://127.0.0.1:8000/mcp`. Add **one** custom
header: name `Authorization`, value `Bearer dev:reader`, all in the value field.
Then reconnect. If the header is missing or malformed, the server returns 401
and the Inspector falls back to MCP OAuth discovery (`/.well-known/...`,
`/register`). Those requests also return 401, because MCP OAuth is out of scope.

### Google authentication (deployed mode)

```bash
INVENTORY_AUTH_MODE=google \
INVENTORY_OIDC_AUDIENCE=https://SERVICE-PROJECT_NUMBER.REGION.run.app \
INVENTORY_POLICY_PATH=policy.json uv run python -m inventory_mcp
```

The server checks each token's signature against Google's published
certificates (cached, with rotation handled). It accepts only RS256 tokens with
a `kid`, and requires:
- issuer `accounts.google.com`;
- an audience exactly equal to `INVENTORY_OIDC_AUDIENCE`;
- a valid `iat`/`exp`;
- a non-empty `sub`.

Permissions are looked up by `sub`, the service account's unique ID, never by
email. See [config/policy.example.json](config/policy.example.json).

### Dev authentication is TEST-ONLY

With `INVENTORY_AUTH_MODE=dev`, the bearer token `dev:<name>` authenticates as
subject `dev:<name>` **without verification**. Anyone who can reach the port can
claim any dev identity. Startup is refused when a Cloud Run marker (`K_SERVICE`,
`K_REVISION`, `K_CONFIGURATION`) is set. It is also refused when binding to a
non-loopback address, unless `INVENTORY_DEV_ALLOW_NON_LOOPBACK=true` is set
explicitly for a local container. Permissions still come only from the server-side policy
([config/policy.dev.json](config/policy.dev.json)):

| Token | Permissions |
|---|---|
| `dev:reader` | `inventory.read` |
| `dev:writer` | `inventory.read`, `restock.create` |
| `dev:no-access` | none |
| anything else matching `dev:<name>` | none (deny by default) |

## Protocol behavior (measured, mcp SDK 2.3.0)

- **Transport:** Streamable HTTP at `/mcp`, stateless, JSON responses (no SSE
  sessions), so no session affinity is needed.
- **Version negotiation:** the SDK client negotiates protocol **2026-07-28** via
  `server/discover` by default. The pre-2026 `initialize` handshake
  (**2025-11-25**) also works and is tested.

What each failure looks like is tested in [tests/test_protocol.py](tests/test_protocol.py)
and [tests/test_authorization.py](tests/test_authorization.py):

| Situation | Response |
|---|---|
| Malformed JSON body | HTTP 400, JSON-RPC `-32700` parse error |
| Structurally invalid JSON-RPC | HTTP 400, JSON-RPC error |
| Missing or invalid bearer token (forged, expired, wrong audience or issuer, non-RS256, unknown key) | HTTP 401 `{"error":"unauthenticated"}` from our middleware; MCP never runs |
| Arguments violate the input schema | Tool result `isError: true`, `{"error":{"code":"invalid_argument","message":"… (field: error_type)"}}`. The SDK reports this as a tool error, not a JSON-RPC error. Our middleware replaces the SDK's text, which echoed submitted values, with field names and error types only |
| Unknown tool name | Tool result `isError: true`, `Unknown tool: …` (**SDK behavior**: not JSON-RPC `-32602`) |
| Unknown product | `isError: true`, `{"error":{"code":"not_found",…}}` |
| Business validation failure | `isError: true`, `{"error":{"code":"invalid_argument",…}}` |
| Idempotency key reused with different details | `isError: true`, `{"error":{"code":"idempotency_conflict",…}}` |
| Authenticated caller lacks permission | `isError: true`, `{"error":{"code":"forbidden",…}}`, no side effects |

`tools/list` shows all five tools to every authenticated caller. **Seeing a tool
in discovery does not authorize calling it.** Permissions are checked on every
call, before any business logic or database access.

## Tests

```bash
uv run pytest        # everything that runs locally
uv run pytest -m simulated_google_auth   # only the simulated Google-token tests
uv run ruff check . && uv run ruff format --check .
```

The MCP tests start a real uvicorn server on a free localhost port and use the
official SDK client over HTTP.

**Simulated vs. real Google authentication.** The tests in
`tests/test_google_oidc_simulated.py` sign tokens with RSA keys generated at
test time, standing in for Google's keys. They prove the application's verifier
logic. They are **not** real Google authentication tests. Real tokens and Cloud
Run IAM are exercised only by the opt-in PoC in `deployment/poc/`, and later by
`integration`-marked tests, which are excluded by default. None have run yet.

## Known limitations (so far)

- Google OIDC verification is tested only with simulated keys. Cloud Run's
  forwarding of the signed token is unverified until the PoC runs. See
  [architecture/threat-model.md](architecture/threat-model.md).
- SQLite is local storage. On Cloud Run it will be ephemeral demo storage
  (`--max-instances=1`), not durable. Firestore is the documented production
  alternative.
- This project does **not** implement MCP-native OAuth (authorization-server
  discovery, Protected Resource Metadata). It is designed for service-to-service
  callers on Google Cloud.

## Repository layout

```
src/inventory_mcp/   server.py (assembly) · tools.py (MCP handlers) · inventory_service.py (logic)
                     repository.py (SQLite) · authentication.py · authorization.py · schemas.py
                     config.py · observability.py · data/inventory.json (synthetic catalog)
clients/             mcp_client.py (standalone MCP client, no LLM)
config/              policy.dev.json (TEST-ONLY) · policy.example.json (Google mode template)
deployment/poc/      Cloud Run token-forwarding proof-of-concept (opt-in, not deployed)
architecture/        threat-model.md
tests/               logic · persistence · protocol · tool calls · authorization · configuration ·
                     google_oidc_simulated
```

The design is specified in [SPEC.md](SPEC.md), as amended by
[SPEC-AMENDMENT-1.md](SPEC-AMENDMENT-1.md).
