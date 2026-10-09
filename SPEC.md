# Claude Code Implementation Specification
# Secure MCP Server on Google Cloud

> **Amended.** `SPEC-AMENDMENT-1.md` is authoritative where it conflicts with this
> document. This file has been reconciled with Amendment 1 (five tools, integer
> money, idempotency key, `/health`, consolidated docs and tests).

## 1. Project objective

Build a production-oriented, standalone Model Context Protocol (MCP) server using Python and the official MCP Python SDK.

Repository name: `secure-mcp-server-gcp`

The project must demonstrate that I can independently:

1. Implement MCP protocol capabilities.
2. Define typed MCP tools.
3. Expose the server through Streamable HTTP.
4. Build a standalone MCP client.
5. Implement deterministic business logic.
6. Secure remote access with Google Cloud IAM.
7. Enforce operation-level authorization.
8. Containerize and deploy to Google Cloud Run.
9. Test protocol, functional, authentication, and authorization behavior.
10. Document the implementation for public GitHub review.

**This project must NOT contain an AI agent, LLM, agent framework, or A2A implementation.**

The MCP server must operate independently of Google ADK, Claude, LangGraph, OpenAI Agents SDK, or other agent frameworks.

The example business domain is fictional enterprise inventory management, with synthetic data for a company called Cresenta.

## 2. Reference documentation

Use these primary references:

- Google codelab: https://codelabs.developers.google.com/secure-mcp-server-gcp
- Official MCP specification: https://modelcontextprotocol.io/specification
- Python MCP SDK: https://github.com/modelcontextprotocol/python-sdk
- Google Cloud Run service authentication: https://cloud.google.com/run/docs/authenticating/service-to-service

Before coding:

- Read the current specification and SDK documentation.
- Confirm installed SDK interfaces with runnable examples.
- Prefer MCP SDK 2.x and its `MCPServer` API as described by the Google codelab.
- Pin resolved dependency versions in `uv.lock`.
- Do not mix incompatible SDK versions or use deprecated transports.
- If the implementation differs from the reference, document why.

## 3. Architecture

The system must support two execution modes.

### Local mode

    Standalone Python MCP Client
                 |
                 | Streamable HTTP
                 | localhost:8000/mcp
                 v
           MCP Server
                 |
                 | Typed tool handlers
                 v
           Inventory Service
                 |
                 v
       Synthetic Inventory Data

### Google Cloud mode

    Standalone Python MCP Client
                 |
                 | Google-signed OIDC token
                 | HTTPS
                 v
         Cloud Run IAM Boundary
                 |
                 | Authenticated request
                 v
           MCP Server
                 |
                 | Operation authorization
                 v
           Inventory Service
                 |
                 v
       Synthetic Inventory Data

The server must not depend on an agent or LLM in either mode.

## 4. Technology stack

- Python 3.12 or later
- Official MCP Python SDK 2.x
- `MCPServer` if supported by the resolved SDK
- Streamable HTTP transport
- `uv` for dependency management
- Pydantic for validation and typed contracts
- Starlette/Uvicorn where required
- HTTPX and the official MCP client SDK
- Pytest
- Ruff
- Docker
- Google Cloud Run
- Google Cloud IAM
- Cloud Logging through structured application logs
- GitHub Actions for CI

Prefer the smallest maintainable implementation.

Avoid unnecessary libraries, frameworks, infrastructure, and abstractions.

## 5. MCP tools

Implement exactly five business tools (Amendment 1, A13).

### Tool 1: get_inventory

Input:

    product_id: str

Output:

    product_id: str
    product_name: str
    quantity_on_hand: int
    reorder_point: int
    unit_cost_minor: int
    currency: str
    warehouses: list[WarehouseStock]

Behavior:

- Retrieve product inventory.
- Validate product ID.
- Return deterministic data.
- Return an explicit typed error for unknown products.
- Never fabricate inventory.

### Tool 2: get_reorder_status

Input:

    product_id: str

Output:

    product_id: str
    quantity_on_hand: int
    reorder_point: int
    reorder_required: bool
    recommended_order_quantity: int
    reason: str

Behavior:

- Calculate reorder status using deterministic Python logic.
- Use the reorder formula locked in Amendment 1, A10.
- No LLM computation.
- Ensure calculations are unit tested.

### Tool 3: list_low_stock_items

Input:

    limit: int = 20

Output:

    items: list[LowStockItem]
    count: int

Behavior:

- Find items below their reorder points.
- Validate limit bounds.
- Return deterministic sorted results (shortfall descending, then product_id ascending).
- Support an empty result.
- Reject unreasonably large requests.

### Tool 4: create_restock_request

Input:

    product_id: str
    quantity: int
    justification: str
    idempotency_key: str (UUID)

Output:

    request_id: str
    product_id: str
    quantity: int
    status: str
    created_at: str

Behavior:

- Validate product existence and requested quantity.
- Require write authorization.
- Create a local restock REQUEST, not a purchase order.
- Never execute a purchase or contact suppliers.
- Persist requests in a lightweight local SQLite database.
- Use database-generated or collision-resistant identifiers.
- Idempotency behavior is defined in Amendment 1, A5.

### Tool 5: list_restock_requests

Input:

    limit: int = 20

Output:

    requests: list[RestockRequestRecord]
    count: int

Behavior:

- Requires `inventory.read`.
- Returns only the calling principal's requests (Amendment 1, A13).
- Lets the demonstration prove that denied writes do not mutate state and that
  idempotent replays do not create duplicates.

The objective is to demonstrate that read and write tools have different authorization requirements.

## 6. Data model

Create a synthetic inventory dataset containing approximately 20 products distributed across three warehouses.

Include:

- Products with adequate stock.
- Products below reorder thresholds.
- Products with zero inventory.
- Products located in multiple warehouses.
- A missing product scenario.
- Edge cases for quantity and price calculations.

Use synthetic company names, suppliers, warehouses, and identifiers.

No client data, personal data, production credentials, or proprietary information.

Use integer minor currency units or Decimal internally for financial calculations. Avoid floating-point monetary arithmetic.

## 7. MCP protocol requirements

The implementation must demonstrate real MCP protocol behavior.

Required:

- Server startup and shutdown.
- Protocol discovery and capability negotiation according to the resolved MCP version.
- Tool listing.
- Tool invocation.
- Input schema exposure.
- Typed tool outputs.
- Protocol-compliant errors.
- Streamable HTTP communication.

Implement a standalone Python client that can:

1. Connect to the server.
2. Perform required protocol discovery/negotiation.
3. List available tools.
4. Inspect their schemas.
5. Invoke each tool.
6. Display structured results.
7. Handle tool errors.
8. Terminate cleanly.

Use official SDK client abstractions where possible rather than manually reimplementing MCP protocol messages.

Also document how to inspect the server using MCP Inspector.

## 8. Security architecture

Separate transport authentication from application authorization.

### Layer 1: Cloud Run authentication

Deploy Cloud Run with unauthenticated invocation disabled.

Only explicitly permitted principals receive `roles/run.invoker`.

The remote client must supply a Google-signed OIDC identity token with the correct service audience.

Do not embed credentials in source code.

Do not use downloaded service account keys as the default authentication mechanism.

### Layer 2: Application authorization

Define application permissions:

    inventory.read
    restock.create

Map verified caller identities to permissions using an explicit configurable policy.

For local testing, support a clearly labeled development-only identity fixture.

For remote deployment, use a trustworthy server-verifiable identity mechanism. Do not authorize based solely on client-provided identity headers or decoded, unverified JWT claims.

If Cloud Run IAM does not expose sufficient verified identity information to the application, document the limitation and implement an appropriate additional verification layer or trusted identity propagation design.

Do not assume that possession of Cloud Run Invoker permission automatically implies authorization to every MCP tool.

### Required security rules

- Read tools (including `list_restock_requests`) require `inventory.read`.
- `create_restock_request` requires `restock.create`.
- Deny by default.
- Unauthorized requests must not change database state.
- No caller may select or escalate its own role.
- No write operation through read-only permissions.
- Validation must be enforced on the server.
- Do not expose raw exception traces to clients.
- Do not log credentials or tokens.
- Fail closed when authorization context is unavailable.

No fake security enforcement or merely decorative authorization checks.

## 9. Proposed repository structure

    secure-mcp-server-gcp/
    ├── README.md
    ├── LICENSE
    ├── .gitignore
    ├── .env.example
    ├── pyproject.toml
    ├── uv.lock
    ├── Dockerfile
    ├── .dockerignore
    ├── .github/
    │   └── workflows/
    │       └── ci.yml
    ├── architecture/
    │   ├── system-design.md
    │   └── threat-model.md
    ├── src/
    │   └── inventory_mcp/
    │       ├── __init__.py
    │       ├── server.py
    │       ├── tools.py
    │       ├── schemas.py
    │       ├── inventory_service.py
    │       ├── repository.py
    │       ├── authentication.py
    │       ├── authorization.py
    │       └── config.py
    ├── clients/
    │   └── mcp_client.py
    ├── data/
    │   └── inventory.json
    ├── tests/
    │   ├── test_discovery.py
    │   ├── test_tool_calls.py
    │   ├── test_validation.py
    │   ├── test_authentication.py
    │   ├── test_authorization.py
    │   ├── test_persistence.py
    │   └── test_protocol.py
    ├── deployment/
    │   ├── deploy_cloud_run.sh
    │   ├── verify_cloud_run.sh
    │   └── cleanup.sh
    └── demo/
        ├── walkthrough.md
        └── expected-results.md

The exact structure can be adjusted when justified, but keep a clear separation between MCP transport, tool handlers, deterministic business logic, persistence, and security.

## 10. Deployment requirements

Create a minimal multi-stage Dockerfile.

Requirements:

- Non-root runtime user.
- Only production dependencies.
- Appropriate port binding to `0.0.0.0`.
- Support Cloud Run's `PORT` environment variable.
- No secrets embedded in the image.
- Health endpoint at `GET /health` (not `/healthz`; Amendment 1, A8).
- Graceful shutdown.
- No dependence on local developer paths.

Cloud Run deployment must:

- Specify the Google Cloud project.
- Specify the deployment region.
- Use a dedicated runtime service account with least privilege.
- Require authenticated invocation.
- Grant Invoker only to explicitly authorized caller principals.
- Define timeouts and resource limits.
- Avoid unnecessary GCP API enablement.
- Avoid creating service account keys.

The deployment scripts must not assume a particular personal GCP project.

Cloud deployment requires explicit approval before any resource-changing commands execute.

## 11. Observability

Use structured JSON logging.

Capture:

- Request correlation ID.
- Timestamp.
- Tool name.
- Invocation result.
- Execution latency.
- Authorization decision.
- Error category.

Never log raw authentication tokens or sensitive request content.

Provide a documented way to inspect Cloud Run logs and distinguish successful calls, validation failures, and authorization denials.

## 12. Automated testing

Create executable automated tests.

### Protocol tests

- Client connects successfully.
- Tool discovery works.
- All five tools appear.
- Tool schemas match expected contracts.
- Valid tool invocation succeeds.
- Malformed invocation is rejected.

### Business logic tests

- Inventory read succeeds.
- Unknown product returns a typed error.
- Reorder calculation is correct.
- Low-stock result ordering is deterministic.
- Restock quantity validation works.
- Restock request persists.
- Repeated submissions follow documented idempotency behavior.

### Authorization tests

- Read principal can read inventory.
- Read principal cannot create restock requests.
- Write-authorized principal can create permitted requests.
- Missing identity is denied.
- Invalid or forged identity is denied.
- Caller-controlled role claims cannot elevate privileges.
- Denied operations do not mutate state.

### Cloud Run tests

Verify using real deployed behavior:

- Authorized caller succeeds.
- Anonymous caller is rejected.
- Caller without Cloud Run Invoker is rejected.
- Incorrect audience is rejected.
- Authorized but insufficiently privileged caller cannot execute the write tool.

For tests that cannot run without GCP credentials, mark them as integration tests and report them as not run rather than as passed.

## 13. Public GitHub README

The README must clearly answer:

1. What problem does this MCP server solve?
2. What is MCP?
3. Why MCP rather than a custom REST API?
4. What tools are exposed?
5. How does discovery work?
6. How does invocation work?
7. Why Streamable HTTP?
8. How are authentication and authorization different?
9. How is the server deployed to Cloud Run?
10. How is it tested?
11. What are the known limitations?
12. How could another agent or application consume it?

Include:

- Architecture diagram.
- Installation steps.
- Local execution instructions.
- Standalone client demonstration.
- Tool schema examples.
- Example successful results.
- Example authorization failure.
- Deployment instructions.
- Test execution instructions.
- Security limitations.
- License and source attribution.

Accurately describe the repository as an educational/reference implementation, not a fully production-certified service.

Do not claim unexecuted tests have passed.

## 14. Implementation checkpoints

Implement incrementally and verify each checkpoint.

### Checkpoint 1 — MCP fundamentals

Build a working local MCP server with all five tools, synthetic inventory, deterministic logic, and a standalone MCP client.

Exit criteria:

- Server starts.
- Client connects.
- Discovery works.
- All tool calls behave correctly.
- Core functional tests pass.

### Checkpoint 2 — Application security

Add validation, identity handling, tool-level authorization, and adversarial tests.

Exit criteria:

- Deny-by-default behavior works.
- Read-only identity cannot write.
- Unauthorized requests cause no state changes.
- Security tests pass.

### Checkpoint 3 — Containers and Cloud Run

Containerize the application, add secure deployment scripts, and verify IAM/OIDC access.

Exit criteria:

- Local Docker execution succeeds.
- Authenticated Cloud Run deployment works.
- Anonymous and unauthorized callers are rejected.
- Server logs demonstrate behavior.

### Checkpoint 4 — Public portfolio readiness

Complete documentation, automated CI, diagrams, examples, and security review.

Exit criteria:

- Fresh local clone can run the project.
- Documented tests are reproducible.
- No credentials or sensitive data exist in the repository or history.
- README matches actual implementation.
- Deployment can be cleaned up safely.
- All known limitations are disclosed.

## 15. Engineering standards

- Type hints on public functions.
- Clear Pydantic schemas.
- Deterministic business logic.
- Explicit exception handling.
- No hardcoded secrets.
- No silent authorization bypasses.
- No placeholder security checks.
- No mocks presented as real GCP authentication.
- No unnecessary complexity.
- No undocumented background infrastructure.
- Meaningful test names.
- Reproducible builds.
- Minimal dependency footprint.

Do not suppress failing tests or loosen assertions just to make the test suite green.

## 16. Claude Code working instructions

You are the implementation engineer.

Proceed through the checkpoints in order.

Within the defined scope, make routine implementation decisions independently.

For each checkpoint:

1. Implement the required functionality.
2. Run relevant tests.
3. Fix implementation defects.
4. Review security implications.
5. Update documentation.
6. Summarize files changed, tests executed, results, and outstanding risks.

Do not advance to the next checkpoint when exit criteria are unmet without explicitly documenting the blocker.

You may create and modify local project files and run local tests without asking for routine approvals.

Do NOT:

- Create or change cloud resources without my approval.
- Incur Google Cloud charges without my approval.
- Push commits or create a public repository without my approval.
- Change unrelated repositories.
- Expose credentials.
- Claim deployment success without verifying it.
- Introduce ADK, A2A, LangGraph, Claude API, or other agent frameworks.

Start with Checkpoint 1.

First inspect the current project directory and verify the SDK/API details against official documentation.

Then implement the standalone MCP server, the five tools, the standalone client, and the initial test suite.

At the end of Checkpoint 1, give me a concise report of what works, which tests passed, any deviations from the specification, and what remains for Checkpoint 2.