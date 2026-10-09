#!/usr/bin/env bash
# Interview demonstration (SPEC-AMENDMENT-1, A15) against the deployed service.
# Uses the standalone MCP client with real Google identities. No agent, no LLM.
source "$(dirname "$0")/common.sh"
set +x
cd "${REPO_ROOT}"

CLIENT=(uv run python clients/mcp_client.py)
URL="${SERVICE_URL}/mcp"
KEY="$(python3 -c 'import uuid; print(uuid.uuid4())')"
REQUEST="{\"product_id\":\"CRS-1002\",\"quantity\":95,\"justification\":\"Below reorder point; restore target level.\",\"idempotency_key\":\"${KEY}\"}"
step() { echo; echo "################ $*"; }

step "1. Discover the remote MCP tools (as mcp-reader)"
"${CLIENT[@]}" tools --url "${URL}" --impersonate "${READER_SA}" | grep -E '^=== |protocol_version'

step "2. Read Cresenta inventory as mcp-reader"
"${CLIENT[@]}" call get_inventory '{"product_id":"CRS-1002"}' --url "${URL}" --impersonate "${READER_SA}"

step "3. mcp-writer's requests BEFORE (baseline)"
"${CLIENT[@]}" call list_restock_requests '{}' --url "${URL}" --impersonate "${WRITER_SA}" | grep -E '"count"|isError'

step "4. Attempt a restock request as mcp-reader -> expect forbidden"
"${CLIENT[@]}" call create_restock_request "${REQUEST}" --url "${URL}" --impersonate "${READER_SA}"

step "5. Prove no mutation: reader and writer request counts are unchanged"
"${CLIENT[@]}" call list_restock_requests '{}' --url "${URL}" --impersonate "${READER_SA}" | grep -E '"count"'
"${CLIENT[@]}" call list_restock_requests '{}' --url "${URL}" --impersonate "${WRITER_SA}" | grep -E '"count"'

step "6. Create the same restock request as mcp-writer -> expect success, idempotent_replay=false"
"${CLIENT[@]}" call create_restock_request "${REQUEST}" --url "${URL}" --impersonate "${WRITER_SA}"

step "7. Repeat it with the same idempotency key -> same request_id, idempotent_replay=true"
"${CLIENT[@]}" call create_restock_request "${REQUEST}" --url "${URL}" --impersonate "${WRITER_SA}"

step "8. No duplicate: writer count went up by exactly one"
"${CLIENT[@]}" call list_restock_requests '{}' --url "${URL}" --impersonate "${WRITER_SA}" | grep -E '"count"'

step "9. Outsider cannot invoke Cloud Run at all -> expect HTTP 403 from Cloud Run (never reaches the app)"
OUTSIDER_SA="${OUTSIDER_SA}" SERVICE_URL="${SERVICE_URL}" uv run python - <<'PY'
import os
import httpx2
from clients.gcloud_identity import mint_identity_token
token = mint_identity_token(os.environ["OUTSIDER_SA"], os.environ["SERVICE_URL"])
body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
r = httpx2.post(os.environ["SERVICE_URL"] + "/mcp", json=body, timeout=60,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream"})
print(f"outsider -> HTTP {r.status_code}")
PY

step "10. Structured logs for this demo (last 10 minutes; waiting 30 s for Cloud Logging ingestion)"
sleep 30
gcloud logging read "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"${SERVICE}\" AND jsonPayload.event=\"tool_call\"" \
  --project "${PROJECT_ID}" --freshness=10m --limit=20 \
  --format='table(timestamp.date("%H:%M:%S"),jsonPayload.tool,jsonPayload.principal,jsonPayload.authorization,jsonPayload.outcome,jsonPayload.error_category)'
