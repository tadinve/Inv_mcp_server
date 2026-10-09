#!/usr/bin/env bash
# Deploys the five-tool MCP server to Cloud Run. Changes cloud resources: run only with approval.
source "$(dirname "$0")/common.sh"

cat <<PLAN
================ Cresenta inventory MCP server: deployment plan ================
Project:   ${PROJECT_ID} (number ${PROJECT_NUMBER})
Region:    ${REGION}
Operator:  ${OPERATOR}

APIs to enable (no-op if already enabled):
  run, cloudbuild, artifactregistry, iamcredentials

Service accounts to create (no keys):
  ${RUNTIME_SA}  runtime identity; NO roles
  ${READER_SA}             roles/run.invoker on ${SERVICE}; app permission inventory.read
  ${WRITER_SA}             roles/run.invoker on ${SERVICE}; app permissions inventory.read, restock.create
  ${OUTSIDER_SA}           NO invoker; no app permissions

IAM changes (all resource-level, none project-wide):
  - ${OPERATOR}: roles/iam.serviceAccountTokenCreator on reader, writer, outsider
    (to mint their ID tokens for testing)
  - reader and writer: roles/run.invoker on service ${SERVICE} only

Application policy (INVENTORY_POLICY_JSON), keyed by service-account uniqueId (the token 'sub'):
  reader -> inventory.read
  writer -> inventory.read, restock.create

Cloud Run service ${SERVICE}, built from ${REPO_ROOT} (Dockerfile) via Cloud Build:
  --no-allow-unauthenticated --invoker-iam-check --ingress all
  --min-instances 0 (scales to zero) --max-instances 1 --cpu 1 --memory 512Mi
  --timeout 60 --concurrency 20, startup probe GET /health
  labels purpose=mcp-inventory-demo, data=synthetic
  env: INVENTORY_AUTH_MODE=google
       INVENTORY_OIDC_AUDIENCE=${SERVICE_URL}
       INVENTORY_ALLOWED_HOSTS=${SERVICE_HOST}
       INVENTORY_DB_PATH=/tmp/restock_requests.db (ephemeral, in-memory; demo storage only)

Data: the synthetic Cresenta catalog packaged in the image. No other data.
Side effects of --source: reuses or creates Artifact Registry repo cloud-run-source-deploy
  and a source-upload bucket.
Expected cost: one Cloud Build run (a few minutes), an image of about 200 MB, and test
  traffic. Scales to zero when idle. Check current pricing.
================================================================================
PLAN
confirm "deploy-mcp"
assert_all_ours  # refuse to touch same-named resources we did not create

ENV_FILE="$(mktemp)"; chmod 600 "${ENV_FILE}"
trap 'rm -f "${ENV_FILE}"' EXIT

set -x
gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com iamcredentials.googleapis.com --project "${PROJECT_ID}"

for sa in mcp-inventory-runtime mcp-reader mcp-writer mcp-outsider; do
  gcloud iam service-accounts describe "${sa}@${SA_DOMAIN}" --project "${PROJECT_ID}" >/dev/null 2>&1 \
  || gcloud iam service-accounts create "${sa}" --project "${PROJECT_ID}" \
       --display-name "Cresenta MCP demo (${sa}); safe to delete"
done

for sa in "${READER_SA}" "${WRITER_SA}" "${OUTSIDER_SA}"; do
  gcloud iam service-accounts add-iam-policy-binding "${sa}" --project "${PROJECT_ID}" \
    --member "${OPERATOR}" --role roles/iam.serviceAccountTokenCreator --condition=None >/dev/null
done

READER_ID="$(gcloud iam service-accounts describe "${READER_SA}" --project "${PROJECT_ID}" --format='value(uniqueId)')"
WRITER_ID="$(gcloud iam service-accounts describe "${WRITER_SA}" --project "${PROJECT_ID}" --format='value(uniqueId)')"
set +x

READER_ID="${READER_ID}" WRITER_ID="${WRITER_ID}" SERVICE_URL="${SERVICE_URL}" SERVICE_HOST="${SERVICE_HOST}" \
python3 - "${ENV_FILE}" <<'PY'
import json, os, sys
policy = {"principals": [
    {"subject": os.environ["READER_ID"], "permissions": ["inventory.read"]},
    {"subject": os.environ["WRITER_ID"], "permissions": ["inventory.read", "restock.create"]},
]}
env = {
    "INVENTORY_AUTH_MODE": "google",
    "INVENTORY_OIDC_AUDIENCE": os.environ["SERVICE_URL"],
    "INVENTORY_ALLOWED_HOSTS": os.environ["SERVICE_HOST"],
    "INVENTORY_POLICY_JSON": json.dumps(policy, separators=(",", ":")),
    "INVENTORY_DB_PATH": "/tmp/restock_requests.db",
    "INVENTORY_LOG_LEVEL": "INFO",
}
with open(sys.argv[1], "w") as fh:
    for key, value in env.items():
        fh.write(f"{key}: {json.dumps(value)}\n")  # JSON strings are valid YAML scalars
PY
echo "Policy subjects: reader=${READER_ID} writer=${WRITER_ID}"

set -x
gcloud run deploy "${SERVICE}" --project "${PROJECT_ID}" --region "${REGION}" \
  --source "${REPO_ROOT}" \
  --no-allow-unauthenticated \
  --invoker-iam-check \
  --ingress all \
  --min-instances 0 --max-instances 1 \
  --cpu 1 --memory 512Mi --timeout 60 --concurrency 20 \
  --service-account "${RUNTIME_SA}" \
  --env-vars-file "${ENV_FILE}" \
  --startup-probe "httpGet.path=/health,httpGet.port=8080,periodSeconds=2,timeoutSeconds=2,failureThreshold=15" \
  --labels purpose=mcp-inventory-demo,data=synthetic \
  --quiet

for sa in "${READER_SA}" "${WRITER_SA}"; do
  gcloud run services add-iam-policy-binding "${SERVICE}" --project "${PROJECT_ID}" --region "${REGION}" \
    --member "serviceAccount:${sa}" --role roles/run.invoker --condition=None >/dev/null
done
set +x

echo
echo "Deployed ${SERVICE_URL}"
echo "IAM changes can take a minute to propagate. Next: ./deployment/verify_cloud_run.sh"
