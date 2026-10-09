#!/usr/bin/env bash
# Verifies the deployed service: configuration, IAM, real-identity integration tests, logs.
# Read-only against cloud configuration. The integration tests create a few synthetic
# restock requests in the service's ephemeral database.
source "$(dirname "$0")/common.sh"
set +x
cd "${REPO_ROOT}"
FAILED=()  # every section runs; failures are reported at the end

echo "=== 1. Deployed configuration"
gcloud run services describe "${SERVICE}" --project "${PROJECT_ID}" --region "${REGION}" --format=json \
  | python3 deployment/tools/check_service.py || FAILED+=("configuration")

echo "=== 2. Invoker bindings (expect exactly reader and writer)"
INVOKERS="$(gcloud run services get-iam-policy "${SERVICE}" --project "${PROJECT_ID}" --region "${REGION}" \
  --flatten="bindings[].members" --filter="bindings.role:roles/run.invoker" --format="value(bindings.members)" | sort)"
EXPECTED="$(printf 'serviceAccount:%s\n' "${READER_SA}" "${WRITER_SA}" | sort)"
echo "${INVOKERS}" | sed 's/^/  /'
if [[ "${INVOKERS}" == "${EXPECTED}" ]]; then echo "  [PASS] invokers are exactly reader and writer"
else echo "  [FAIL] unexpected invoker set"; FAILED+=("invoker bindings"); fi

echo "=== 3. Real-identity integration tests"
export MCP_REMOTE_URL="${SERVICE_URL}" MCP_PROJECT_ID="${PROJECT_ID}" MCP_REGION="${REGION}" MCP_SERVICE="${SERVICE}" \
  MCP_READER_SA="${READER_SA}" MCP_WRITER_SA="${WRITER_SA}" MCP_OUTSIDER_SA="${OUTSIDER_SA}"
MISSING=()
for var in MCP_REMOTE_URL MCP_PROJECT_ID MCP_REGION MCP_SERVICE MCP_READER_SA MCP_WRITER_SA MCP_OUTSIDER_SA; do
  [[ -n "${!var:-}" ]] || MISSING+=("${var}")
done
if (( ${#MISSING[@]} )); then
  echo "  [FAIL] missing integration environment: ${MISSING[*]}"; FAILED+=("integration environment")
else
  JUNIT="$(mktemp -d)/integration.xml"
  # pytest's own exit code is not trusted alone: a module-level skip or zero collected tests can
  # still look like success. The JUnit report is gated below.
  uv run pytest -m integration tests/integration -v -rs -s --tb=short -p no:cacheprovider \
    --junitxml="${JUNIT}" || true
  python3 deployment/tools/check_junit.py "${JUNIT}" deployment/required_integration_tests.txt \
    || FAILED+=("integration tests")
fi

echo "=== 4. Logs (all entries for the service, last 30 min) + token-leak check (waiting 60 s for ingestion)"
sleep 60
gcloud logging read "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"${SERVICE}\"" \
  --project "${PROJECT_ID}" --freshness=30m --limit=2000 --format=json \
  | python3 deployment/tools/summarize_logs.py --require-tool-calls --require-denial || FAILED+=("log verification")

echo
if (( ${#FAILED[@]} )); then
  echo "VERIFY: FAIL (${FAILED[*]})"; exit 1
fi
echo "VERIFY: PASS"
