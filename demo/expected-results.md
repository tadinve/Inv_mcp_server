# Test evidence

What has been tested, how, and what has not. Counts are from actual runs; nothing
listed as passed was skipped or inferred.

## Four kinds of evidence

| Kind | What is real | What is simulated | Runs in CI |
|---|---|---|---|
| **Local** | Real uvicorn server, real HTTP, official MCP client, real SQLite (file-backed), real threads | Identity: TEST-ONLY dev tokens (`dev:<name>`) | Yes |
| **Simulated Google tokens** | The production `GoogleOIDCAuthenticator` code path, real HTTP | Signing keys: RSA keys generated at test time stand in for Google's | Yes |
| **Live Google identity** | Google-signed ID tokens minted by impersonation, Cloud Run IAM, the deployed container | Nothing | No: needs GCP credentials; run with `deployment/verify_cloud_run.sh` |
| **Not validated** | n/a | n/a | n/a |

## Local and simulated suite (current)

Command: `uv run pytest`. Result: **243 passed, 1 skipped**. The skip is the
live-integration module, which reports itself as NOT RUN without GCP settings.

| File | Tests | Kind | Covers |
|---|---|---|---|
| `test_inventory_logic.py` | 19 | Local | Reorder formula, catalog edge cases, low-stock ordering |
| `test_persistence.py` | 10 | Local | Idempotency, per-caller scoping, 16 and 12 concurrent threads on file-backed SQLite |
| `test_protocol.py` | 18 | Local | Discovery, schemas, both handshakes, each error class, sanitized errors and logs |
| `test_tool_calls.py` | 11 | Local | Behavior of each tool, no write on failure |
| `test_authorization.py` | 46 | Local | Read/write separation, deny by default, escalation attempts, 401 paths |
| `test_configuration.py` | 30 | Local | Fail-closed startup guards, including a real process exiting with code 2 |
| `test_host_validation.py` | 10 | Local | Host pin 421, Origin 403, `/health` not pinned |
| `test_deployment_tools.py` | 21 | Local | Verify-script helpers detect misconfiguration, leaks, skips and missing tests |
| `test_deployment_scripts.py` | 32 | Local | Static audit: scripts cannot modify unowned or shared cloud resources |
| `test_google_oidc_simulated.py` | 46 | **Simulated** | Signature, issuer, audience, expiry, `sub`, algorithm, `kid`, certificate cache, end-to-end over HTTP |

Breakdown: 197 local and 46 simulated (`uv run pytest -m simulated_google_auth`).

## Live Google identity evidence (2026-10-08, temporary lab project)

| Run | Result | Record |
|---|---|---|
| Checkpoint 2: token-forwarding PoC, 6 probes | **6/6 PASS**. The original signed `Authorization` token reached the container and verified; `X-Serverless-Authorization` arrived with its signature replaced; anonymous 403, outsider 403, wrong audience 401, forged 401 | [deployment/poc/RESULTS.md](../deployment/poc/RESULTS.md) |
| Checkpoint 3: `verify_cloud_run.sh` | **15/15 integration tests passed**, 10/10 config checks, invoker set exact, `VERIFY: PASS` | [deployment/RESULTS.md](../deployment/RESULTS.md) |
| Checkpoint 3: interview demo | 10/10 steps as expected | same |
| Checkpoint 3: logs (re-read after ingestion) | 150 entries, **0** JWT-like strings, 0 errors | same |
| Post-run: `sub == uniqueId` for reader, writer, outsider | **3/3 True**, verified with the server's own verifier against Google's live certificates | same |

That verify run used the earlier gate, which relied on pytest's exit code. The
hardened gate, which requires all 18 integration cases (16 functions) in one run
with none skipped, has not been run against a deployment yet.

## Not yet validated

- The hardened `verify_cloud_run.sh` gate in one live run (see above).
- The redeployed revision with the improved argument-rejection log fields.
- Behavior under real load, multiple instances, or longer than one session.
- A Google certificate-endpoint outage in production (fails closed locally in tests).
- Cleanup of the live resources (pending at the time of writing).
- Any MCP client other than the official Python SDK client and MCP Inspector
  (Inspector verified manually against the local server).
