# Deploying the MCP server to Cloud Run (Checkpoint 3)

**Status: prepared, NOT deployed.** Deployment needs explicit owner approval. The
owner runs these scripts; Claude Code does not.

Every script prints its plan with the resolved project, region and operator, and
requires a typed confirmation phrase before changing anything.

| Script | Changes cloud resources? | Confirmation |
|---|---|---|
| `deploy_cloud_run.sh` | Yes: creates the service, 4 service accounts, IAM bindings | `deploy-mcp` |
| `verify_cloud_run.sh` | No config changes. The integration tests add synthetic restock requests to the ephemeral DB | none |
| `demo_cloud_run.sh` | Same as verify: adds 1 synthetic request | none |
| `cleanup.sh` | Yes: deletes everything deploy created | `delete-mcp` |

Settings come from your gcloud config unless exported: `PROJECT_ID`, `REGION`,
`OPERATOR`. See [common.sh](common.sh).

## What gets created

| Resource | Purpose | Privileges |
|---|---|---|
| Cloud Run `cresenta-inventory` | The five-tool MCP server | Unauthenticated access disabled; `--invoker-iam-check`; ingress all (IAM-protected, reachable for testing); min 0 / max 1 instance; 1 vCPU, 512 MiB; 60 s timeout; concurrency 20; startup probe `GET /health`; labels `purpose=mcp-inventory-demo`, `data=synthetic` |
| SA `mcp-inventory-runtime` | Runtime identity of the service | **No roles** (the server calls no Google APIs except fetching Google's public certificates) |
| SA `mcp-reader` | Real test identity | `roles/run.invoker` on the service; app policy `inventory.read` |
| SA `mcp-writer` | Real test identity | `roles/run.invoker` on the service; app policy `inventory.read`, `restock.create` |
| SA `mcp-outsider` | Real test identity | **No invoker**, no app permissions |
| Token Creator bindings | Lets the operator mint ID tokens for reader, writer and outsider | `roles/iam.serviceAccountTokenCreator` on those three accounts only |

No service account keys are created. Tokens are short-lived, minted by
impersonation.

### Container configuration

| Variable | Value | Why |
|---|---|---|
| `INVENTORY_AUTH_MODE` | `google` | Verify Google-signed ID tokens in-process |
| `INVENTORY_OIDC_AUDIENCE` | `https://cresenta-inventory-<PROJECT_NUMBER>.<REGION>.run.app` | Exact audience match |
| `INVENTORY_ALLOWED_HOSTS` | `cresenta-inventory-<PROJECT_NUMBER>.<REGION>.run.app` | `Host` pinning (DNS-rebinding protection). Startup is refused without it in deployed Google mode |
| `INVENTORY_POLICY_JSON` | reader `uniqueId` → `inventory.read`; writer `uniqueId` → both | Permissions keyed by the verified `sub`. Generated at deploy time from the accounts' unique IDs |
| `INVENTORY_DB_PATH` | `/tmp/restock_requests.db` | Ephemeral in-memory storage, demo only (A3) |

Values go through a temporary `--env-vars-file` (mode 600, deleted on exit). None
are secrets.

The image contains only the installed package and its runtime dependencies:
- no tests, dev tools, `.env`, policies or databases;
- the synthetic Cresenta catalog is the only data.

This was verified locally (Checkpoint 3 report).

## IAM the operator needs

`roles/owner` on a sandbox project covers everything. Least-privilege
alternative, the same as the PoC:
- `serviceusage.serviceUsageAdmin`
- `iam.serviceAccountAdmin`
- `run.admin`
- `iam.serviceAccountUser` on `mcp-inventory-runtime`
- `cloudbuild.builds.editor`
- `artifactregistry.writer` (or `admin` if the `cloud-run-source-deploy` repo does not exist)
- `storage.admin` (first source deploy only)
- `logging.viewer` (for verify and demo)

## Expected cost

Based on Google's published free tiers; current pricing not checked:
- **Cloud Build:** one build, a few minutes.
- **Artifact Registry:** an image of about 200 MB, deleted by cleanup.
- **Cloud Run:** around 100 test requests. Min instances 0 means no idle cost.
- **Cloud Logging:** a few hundred entries.

Expected total: **$0 within free tiers**, well under $1 otherwise. On a Qwiklabs
project, the lab pays.

## Procedure

```bash
cd ~/Documents/FDE-Prep/Inv_mcp_server

# 0. Local gate: everything must pass first
uv run pytest -q && uv run ruff check .

# 1. Deploy (type deploy-mcp)
./deployment/deploy_cloud_run.sh

# 2. Verify. Wait about a minute for IAM to propagate.
./deployment/verify_cloud_run.sh

# 3. Interview demo (optional)
./deployment/demo_cloud_run.sh

# 4. Clean up (type delete-mcp). Verification lists print at the end and should be empty.
./deployment/cleanup.sh
```

## What `verify_cloud_run.sh` proves

1. **Deployed configuration:**
   - invoker IAM check on, ingress all, min 0 and max 1 instances;
   - Google auth mode, https audience, allowed hosts set, no dev override;
   - exactly 2 policy subjects, label `data=synthetic`.
2. **Invoker bindings** are exactly reader and writer.
3. **Real-identity integration tests** in [tests/integration/test_cloud_run.py](../tests/integration/test_cloud_run.py), using real Google ID tokens:

| Test | Expected |
|---|---|
| Reader discovers tools | 5 tools, protocol `2026-07-28` |
| Reader reads inventory | Success, synthetic CRS-1002 data |
| Reader creates a restock request | `forbidden`; reader **and** writer request counts unchanged |
| Writer creates a request | Success; `submitted_by` equals the writer account's `uniqueId` (confirms `sub` = uniqueId) |
| Writer retries with the same key | Same `request_id`, `idempotent_replay: true`, count unchanged |
| Same key, different payload | `idempotency_conflict`, count unchanged |
| 8 concurrent identical submissions | One `request_id`, one non-replay, count +1 |
| Invalid arguments | Typed `invalid_argument`, submitted value not echoed |
| Anonymous | 403 (Cloud Run) |
| Outsider | 403 (Cloud Run) |
| Wrong audience | 401 (Cloud Run) |
| Forged signature | 401 (Cloud Run) |
| Valid writer token only in `X-Serverless-Authorization` | 401 with **our** body `{"error":"unauthenticated"}`: Cloud Run admits it, the app refuses it |
| `/health` | 403 without a token; 200 `{"status":"ok"}` with the reader's token |
| Request through the legacy `*.a.run.app` hostname | 421 from the app's `Host` pin |

4. **Logs:** a summary of tool calls and authentication events from Cloud Logging, by tool, outcome, authorization decision and category.
5. **Token leak check:** the number of log entries containing a JWT-like string must be 0.

The script ends with `VERIFY: PASS` or `VERIFY: FAIL (<sections>)`. Every section
runs, and any one failing makes the result `FAIL`. It fails if:
- an integration environment variable is missing;
- no integration tests are collected;
- any test is skipped, fails or errors;
- any function listed in
  [required_integration_tests.txt](required_integration_tests.txt) does not run.
  This is checked from the JUnit report, not pytest's exit code, because a
  module-level skip can still exit 0;
- the deployed configuration or the invoker set is wrong;
- the logs contain no tool calls, no logged denial, or any JWT-like string. The
  leak check covers every log entry, including Cloud Run request logs.

## Safety: the scripts cannot touch other workloads

This project also hosts the A2A services (`a2a-auth-broker`, `a2a-inventory-ui`,
`remote-browser-vm1`), their service accounts, and the `a2a-demo` image repository.
Three layers of protection:

1. **Fixed names.** Every mutating command targets one exact resource:
   - service `cresenta-inventory` (or `mcp-auth-poc`);
   - the `mcp-*` / `mcp-poc-*` service accounts;
   - the package of the same name inside `cloud-run-source-deploy`.

   No filters, wildcards, repository deletes, path-based image deletes, or
   project-level IAM.
2. **Ownership guards** (`assert_all_ours`, after confirmation and before the first
   change). They refuse to deploy over or delete a same-named service without our
   `purpose` label, or a same-named service account without our display-name
   marker. Tested against the live project: our resources pass; pointing the guard
   at `a2a-auth-broker` (service or account) or `a2a-inventory-ui` is refused.
3. **Static audit** in [tests/test_deployment_scripts.py](../tests/test_deployment_scripts.py),
   run with the normal test suite. It fails on any unreviewed mutating `gcloud`
   command. It was checked against 8 deliberately unsafe commands (project IAM,
   deleting an A2A service, filter deletes, repo and image-path deletes,
   key creation, foreign service accounts) and caught each one.

## Known limitations of this deployment

- SQLite in `/tmp` is ephemeral. Requests and idempotency records are lost when the
  instance stops, including on scale to zero. Firestore is the production
  alternative (not implemented).
- One instance maximum. This is what makes SQLite consistent here, not a scaling
  design.
- Ingress `all`: the service is reachable from the internet, but only by IAM-authorized
  callers. Internal-only ingress would block testing from a laptop.
