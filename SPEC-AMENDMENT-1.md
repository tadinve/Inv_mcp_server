# SPEC.md — Architecture Amendment 1

This amendment supersedes conflicting decisions in the original specification
(`SPEC.md`). Where the two disagree, this document is authoritative.

Items marked **LOCKED** are design decisions, not suggestions. Changing them
requires a new amendment.

---

## A1. Authentication — LOCKED

Use Cloud Run IAM as the outer authentication boundary.

- Require authenticated invocation.
- Grant `roles/run.invoker` only to explicitly authorized test identities.
- The client sends a Google-signed OIDC ID token in the `Authorization: Bearer` header.
- In the application, independently verify the original signed token using `google-auth`.
- Validate signature, issuer, audience, expiration, and identity claims.
- Map the verified caller identity to application permissions (see A1.2).
- Never authorize from unverified headers, unsigned claims, or an untrusted development identity.
- Reject missing or unverifiable tokens.

Do not use `X-Serverless-Authorization` for the primary design: Google documents
that Cloud Run removes the signature from that header's token before forwarding
it. Google's documentation does not state the same for `Authorization`, but it
also does not explicitly guarantee the signature arrives intact. That is why the
proof-of-concept in A1.1 is mandatory.

Do not disable Cloud Run Invoker IAM checks.

### A1.1 Signed-token proof-of-concept — sequencing

Resolves the conflict between "prove before implementation" and "do not deploy
before local tests pass":

1. Implement and test Checkpoint 1 locally first.
2. At the start of Checkpoint 2, prepare a minimal Cloud Run proof-of-concept
   service that reports only whether the forwarded `Authorization` token's
   signature verifies (never the token itself).
3. Deploying it requires explicit owner approval.
4. Deleting it afterward also requires explicit owner approval.
5. If the signed token does not reach the container intact, stop and report the
   evidence. Do not weaken verification to compensate.

### A1.2 Identity mapping — LOCKED

- The verified `sub` claim is the primary authorization identity.
- Never authorize on service-account email alone. Email may be logged as a
  human-readable label only after `email_verified` is true.
- Issuer validation is explicit: accept only `https://accounts.google.com`
  (and the scheme-less `accounts.google.com` form Google also issues).
- Audience validation is explicit: see A1.3.

### A1.3 Audience — LOCKED

- The expected OIDC audience is configured through an environment variable.
- In deployed (Google) authentication mode, startup fails if no valid audience
  is configured.

### A1.4 Development authentication

- Local development uses an explicit, test-only authentication mode.
- The application refuses to start when test-only authentication is enabled and
  `K_SERVICE` is present (Cloud Run sets `K_SERVICE`). This fail-closed behavior
  is tested.
- `K_SERVICE` is an additional safety check, not the only safeguard. The deployed
  configuration must also explicitly select Google authentication, and the
  deployment script must set it.

### A1.5 Test identities

- `mcp-reader`: Cloud Run Invoker; `inventory.read`
- `mcp-writer`: Cloud Run Invoker; `inventory.read`, `restock.create`
- `mcp-outsider`: no Invoker access

Document the trust boundaries and the rejected alternatives.

## A2. MCP-native OAuth — OUT OF SCOPE

This project uses Google Cloud service-to-service authentication rather than the
full MCP OAuth authorization workflow.

Document the distinction between:

- Google Cloud IAM/OIDC authentication
- Application-level tool permissions
- MCP-native OAuth authorization and Protected Resource Metadata

Explain that the GCP-specific approach suits this controlled enterprise reference
implementation, but does not provide the automatic OAuth discovery and consent
workflow that general-purpose remote MCP clients expect.

Do not claim MCP OAuth authorization compliance.

## A3. Persistence — LOCKED

- SQLite locally.
- SQLite is also permitted for the Cloud Run demonstration, explicitly identified
  as ephemeral demonstration storage.
- Configure `--max-instances=1`.
- This is NOT a durability guarantee. Requests can be lost after instance
  replacement, revision rollout, or scale-to-zero. Idempotency guarantees do not
  survive loss of the database.
- Firestore is the documented production alternative; it is not implemented in
  the initial checkpoints.
- Test SQLite idempotency only within the lifetime of a persistent local database.

## A4. MCP transport — LOCKED

- Streamable HTTP.
- Configure the supported equivalent of `stateless_http=True` and
  `json_response=True`.
- Avoid server-side MCP session-affinity requirements.
- Document the negotiated MCP protocol version and SDK-specific behavior.

## A5. Idempotency — LOCKED

`create_restock_request` takes a required `idempotency_key: str` in UUID format.

Storage:

- Unique SQLite constraint on `(principal_sub, idempotency_key)`.
- A canonical JSON fingerprint of the request payload (product ID, quantity,
  justification) is stored with each request.
- Insert and conflict resolution happen atomically inside one transaction.

Behavior:

- New key: create the request.
- Existing key + identical payload: return the original result.
- Existing key + different payload: return a typed conflict error.
- Concurrent duplicate requests: exactly one persisted request.
- Authorization happens before any existing request is returned.
- Idempotency is scoped to the verified principal as well as the key.

Testing: concurrent submissions are tested with a file-backed SQLite database and
real threads.

## A6. Monetary representation — LOCKED

Replace `unit_cost: float` with `unit_cost_minor: int` and `currency: str`.
Use integer minor units internally.

## A7. Error contracts — LOCKED

Follow the resolved MCP specification revision and Python SDK behavior. Do not
hard-code assumptions that contradict them. Distinguish protocol-level errors
from tool execution errors, and **document and test the actual behavior** for:

- malformed JSON-RPC
- invalid tool arguments
- business validation failures
- unknown tools
- authorization failures

Intended mapping (verified and corrected by tests where the SDK differs):

| Situation | Response |
|---|---|
| Malformed JSON-RPC / protocol-invalid request | JSON-RPC protocol error |
| Unknown tool | As the SDK and spec revision define it; documented |
| Invalid tool arguments | As the SDK and spec revision define it; documented |
| Unknown product | Tool result, `isError=true`, typed error |
| Business validation failure | Tool result, `isError=true`, typed error |
| Idempotency conflict | Tool result, `isError=true`, typed error |
| Authorized caller lacking tool permission | Tool result, `isError=true`, typed `forbidden` error, no side effects |
| Missing/invalid bearer token at the application | HTTP 401 from application authentication middleware |
| Caller lacking `roles/run.invoker` | Rejected by Cloud Run IAM before reaching the application |

Never leak stack traces or authorization policy in error details.

`tools/list` may expose all tool definitions to authenticated callers.
Authorization MUST be enforced on every tool execution regardless of discovery.
Document that tool discovery is not an authorization grant.

## A8. Health check — LOCKED

`GET /health`, not `/healthz` (Cloud Run reserves some paths ending in `z`).
Return minimal readiness information only: no credentials, principal identities,
or environment secrets.

## A9. Cloud test identities and security test layers

Provide an opt-in provisioning script for the reader, writer, and outsider
service accounts. Do not run it automatically. Before running it, display the
exact project, resources, IAM changes, and permissions required, and obtain
approval. Prefer least-privilege Token Creator grants to an approved test
operator.

### A9.1 Two separate test layers — LOCKED

**Real Google authentication tests (Cloud Run, integration, opt-in).** These
prove the Cloud Run IAM boundary and end-to-end verification:

- Reader: read succeeds, write denied.
- Writer: read and authorized write succeed.
- Outsider: Cloud Run invocation denied.
- Anonymous request: denied.
- Invalid audience: denied.
- Forged or malformed token: denied.

Several of these are rejected by Cloud Run *before* the application sees them.
They prove Google's boundary, not the application's verifier.

**Application JWT verification tests (local, simulated).** These prove the
application's own authentication boundary: signature, issuer, audience, expiry,
missing claims, and algorithm confusion. They use locally generated RSA test
keys. They are explicitly labeled as simulated verification tests and must never
be reported as real Google authentication tests.

Capture actual results without copying tokens into logs or reports. Report
integration tests that could not run as "not run", never as passed.

## A10. Reorder formula — LOCKED

    target_level = 2 * reorder_point
    reorder_required = on_hand < reorder_point
    recommended_order_quantity = max(0, target_level - on_hand) if reorder_required else 0

`list_low_stock_items` order:

    shortfall descending, then product_id ascending
    shortfall = max(0, reorder_point - on_hand)

## A11. Repository-specific test permission

For this repository only, Claude Code may run pytest, Ruff, local Docker builds
and tests, and local MCP integration tests, and may correct failures and rerun
tests. This overrides conflicting general CLAUDE.md restrictions on test
execution only. It does not authorize cloud deployment, cloud resource changes,
GitHub publishing, or destructive operations. Do not modify the global CLAUDE.md.

## A12. Repository naming and simplification

- Repository name: `secure-mcp-server-gcp`. The local folder may remain
  `Inv_mcp_server` until explicitly renamed.
- Two architecture documents: `architecture/system-design.md` and
  `architecture/threat-model.md` (security boundaries live in the threat model).
- Consolidate test files where this improves clarity without weakening coverage.

## A13. Fifth tool: `list_restock_requests` — LOCKED

The tool count changes from four to five.

- Requires `inventory.read`.
- Returns typed request records: request ID, product ID, quantity, status,
  creation timestamp, and submitting principal.
- Returns only requests submitted by the calling principal. Cross-principal
  visibility is not exposed without an explicitly defined permission (none is
  defined in this amendment).

Purpose: lets the demonstration prove that denied writes cause no mutation and
that repeated authorized submissions do not create duplicates.

## A14. Typed schemas — LOCKED

Define explicit Pydantic models for warehouse entries, low-stock entries, and
restock-request records. No untyped `list` output fields.

## A15. Interview demonstration

The repository must support a reproducible demonstration:

1. Discover the remote MCP tools.
2. Read Cresenta inventory as `mcp-reader`.
3. Attempt to create a restock request as `mcp-reader`.
4. Show the authorization denial, and prove no mutation using `list_restock_requests`.
5. Create a restock request as `mcp-writer`.
6. Repeat the same request with the same idempotency key.
7. Show with `list_restock_requests` that no duplicate was created.
8. Show the relevant structured logs.

The README begins with the business problem:

> Cresenta needs enterprise applications and AI assistants to read inventory
> safely, while only specifically authorized callers can initiate restock
> requests.

The demo uses a standalone MCP client. Do not add an AI agent.

## A16. Implementation order

1. Checkpoint 1 from `SPEC.md`, entirely local.
2. Do not provision or deploy cloud resources until local implementation and
   tests pass, and then only with approval (A1.1, A9).
3. Explicitly report technical assumptions that cannot be verified before cloud
   deployment.
