# Checkpoint 3: Cloud Run deployment results

- **Run:** 2026-10-09 06:3x UTC (2026-10-08 local), by the project owner, with `deploy_cloud_run.sh`, `verify_cloud_run.sh` and `demo_cloud_run.sh`
- **Project:** a temporary Qwiklabs lab project
- **Region:** `us-central1`
- **Service / revision:** `cresenta-inventory` / `cresenta-inventory-00001-q6q`
- **URL / audience / pinned host:** `https://cresenta-inventory-<PROJECT_NUMBER>.us-central1.run.app`
- **Identities:** real Google service accounts, with ID tokens minted by impersonation. No keys, no simulated tokens.
- **Redaction:** the project number and service-account unique IDs are replaced with placeholders. The comparisons below were made against the real values.
  - `mcp-reader`: uniqueId `<READER_UNIQUE_ID>`, invoker, `inventory.read`
  - `mcp-writer`: uniqueId `<WRITER_UNIQUE_ID>`, invoker, `inventory.read` and `restock.create`
  - `mcp-outsider`: no invoker, no permissions

## 1. Deployed configuration (`verify_cloud_run.sh`, section 1)

All 10 checks **PASS**:
- invoker IAM check enabled; ingress all (IAM-protected); min instances 0; max instances 1;
- auth mode google; https audience; allowed hosts set; no dev override;
- policy has exactly 2 subjects; label `data=synthetic`.

Invoker bindings are exactly `mcp-reader` and `mcp-writer`: **PASS**.

## 2. Real-identity integration tests: 15/15 PASSED (15.49 s)

| Test | Observed | Verdict |
|---|---|---|
| Reader discovers tools | 5 tools, protocol `2026-07-28` | PASS |
| Reader reads synthetic inventory | CRS-1002, 25 on hand | PASS |
| Reader creates restock request | `forbidden`; reader and writer counts unchanged | PASS |
| Writer creates request | Success; `submitted_by` = writer uniqueId | PASS |
| Identical retry | Same `request_id`, `idempotent_replay: true`, count unchanged | PASS |
| Same key, different payload | `idempotency_conflict`, count unchanged | PASS |
| 8 concurrent identical submissions | One `request_id`, one non-replay, count +1 | PASS |
| Invalid arguments | Typed `invalid_argument`, value not echoed | PASS |
| Anonymous | HTTP 403 (Cloud Run) | PASS |
| Outsider (valid token, no invoker) | HTTP 403 (Cloud Run) | PASS |
| Wrong audience | HTTP 401 (Cloud Run) | PASS |
| Forged signature | HTTP 401 (Cloud Run) | PASS |
| Valid writer token only in `X-Serverless-Authorization` | HTTP 401 with the **application's** body: Cloud Run admitted it, the app refused the stripped token | PASS |
| `/health` | 403 without a token, 200 `{"status":"ok"}` with the reader's token | PASS |
| Legacy `*.a.run.app` hostname, valid token | HTTP **421** from the app's `Host` pin | PASS |

## 3. Interview demonstration (`demo_cloud_run.sh`)

| Step | Observed | Verdict |
|---|---|---|
| 1. Discover tools as reader | Protocol `2026-07-28`, 5 tools | PASS |
| 2. Reader reads CRS-1002 | Full typed record | PASS |
| 3. Writer baseline | count 4 (left by the integration tests on the same instance) | n/a |
| 4. Reader creates request | `forbidden` | PASS |
| 5. No mutation | Reader count 0, writer count still 4 | PASS |
| 6. Writer creates request | `request_id f3fae1a5…`, `idempotent_replay: false`, `submitted_by <WRITER_UNIQUE_ID>` | PASS |
| 7. Same key again | Same `request_id`, `idempotent_replay: true` | PASS |
| 8. No duplicate | Writer count 5 (+1) | PASS |
| 9. Outsider | Rejected. The client showed the SDK's generic `-32603` wrapper rather than the HTTP status (see below) | PASS (presentation fixed) |
| 10. Structured logs | Tool, verified principal label, allow/deny, outcome and category per call | PASS |

## 4. Logs

**Complete check, re-read after ingestion settled.** All 150 entries for the
service in a 3-hour window, application logs and Cloud Run request logs:

| Check | Result |
|---|---|
| Entries containing a JWT-like string | **0** |
| ERROR or CRITICAL entries | **0** |
| Cloud Run request statuses | 200 × 89, 403 × 5, 401 × 3, 421 × 1 |

| event | tool | outcome | authz | category | count |
|---|---|---|---|---|---|
| authentication | - | denied | - | missing_bearer_token | 1 (the `X-Serverless-Authorization`-only test) |
| tool_call | create_restock_request | denied | deny | forbidden | 2 (verify + demo) |
| tool_call | create_restock_request | error | allow | idempotency_conflict | 1 |
| tool_call | create_restock_request | invalid_arguments | - | invalid_argument | 1 |
| tool_call | create_restock_request | success | allow | - | 14 (includes replays) |
| tool_call | get_inventory | success | allow | - | 2 |
| tool_call | list_restock_requests | success | allow | - | 16 |

Only `X-Serverless-Authorization` reached the application's authentication layer
and was refused. Anonymous, outsider, wrong-audience and forged requests were
stopped by Cloud Run first.

The summary printed by `verify_cloud_run.sh` itself ran seconds after the tests,
so Cloud Logging ingestion delay made it partial. The table above supersedes it.

## 5. Assumptions now verified

1. **The verified `sub` of a service-account ID token is its `uniqueId`.**
   - *Indirect:* `submitted_by` equals the writer's uniqueId, and policy lookups
     keyed on uniqueIds granted exactly the intended permissions.
   - *Direct (2026-10-08, after the run):* for reader, writer and outsider, a real
     Google-signed ID token was verified with the server's own
     `GoogleOIDCAuthenticator` against Google's live certificates. Its `sub` was
     compared with `gcloud iam service-accounts describe … uniqueId`. All three:
     `signature verified, sub == uniqueId: True`. Only the comparison result was
     printed; no token or claim values.
   - This check is now the integration test
     `test_real_token_sub_equals_service_account_unique_id`, so every verify run
     repeats it.
2. **`Host` pinning works on Cloud Run.** The service is reachable on two hostnames.
   The non-pinned one gets 421 even with a valid token.
3. **Cloud Run forwards the signed `Authorization` token** to the real MCP server,
   not only to the PoC.
4. **SQLite on Cloud Run's in-memory `/tmp`** works for the demo. State persisted
   across the verify and demo runs on the same instance; it is still ephemeral by
   design.

## Conclusion

The five-tool MCP server runs on Cloud Run behind IAM. With real Google identities:
- application-level permissions are enforced per tool by verified `sub`;
- denied writes do not mutate state;
- idempotent replay and concurrent duplicates persist exactly once.

The two security layers behave as designed: Cloud Run IAM rejects anonymous,
outsider, wrong-audience and forged requests; the application rejects what IAM
admits but must not trust (a stripped `X-Serverless-Authorization` token, a
non-pinned `Host`).

## Remaining

- Cleanup has not run yet, for this service or for the earlier PoC. Record the
  verification output here after `cleanup.sh` and `poc/cleanup_poc.sh`.
