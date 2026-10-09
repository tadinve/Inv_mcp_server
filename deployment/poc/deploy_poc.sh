#!/usr/bin/env bash
# Deploys the token-forwarding PoC. Changes cloud resources: run only with approval.
source "$(dirname "$0")/common.sh"

cat <<PLAN
================ Token-forwarding PoC: deployment plan ================
Project:          ${PROJECT_ID} (number ${PROJECT_NUMBER})
Region:           ${REGION}
Operator:         ${OPERATOR}

APIs to enable (no-op if already enabled):
  run.googleapis.com, cloudbuild.googleapis.com,
  artifactregistry.googleapis.com, iamcredentials.googleapis.com

Service accounts to create (no keys are created):
  ${RUNTIME_SA}   runtime identity; NO roles granted
  ${CALLER_SA}    test caller; gets roles/run.invoker on the PoC service only
  ${OUTSIDER_SA}  test caller; gets NO invoker access

IAM changes:
  - ${OPERATOR} gets roles/iam.serviceAccountTokenCreator on the caller and
    outsider service accounts only (resource-level, not project-level),
    so it can mint their ID tokens by impersonation
  - ${CALLER_SA} gets roles/run.invoker on service ${SERVICE} only

Cloud Run service:
  name ${SERVICE}, built from ${SOURCE_DIR} via Cloud Build (--source)
  --no-allow-unauthenticated, --invoker-iam-check (explicitly ON), ingress all, label purpose=mcp-auth-poc
  --min-instances=0 (scales to zero when idle) --max-instances=1 --memory=256Mi --cpu=1 --timeout=30s --concurrency=10
  EXPECTED_AUDIENCE=${SERVICE_URL}

Side effects of a --source deploy:
  - an Artifact Registry repository named cloud-run-source-deploy in ${REGION} (created if missing)
  - a Cloud Storage bucket for uploaded source (created if missing)
Expected cost: small (one short build, a few requests). Check current pricing.
=======================================================================
PLAN
confirm "deploy-poc"
assert_all_ours  # refuse to touch same-named resources we did not create

set -x
gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com iamcredentials.googleapis.com --project "${PROJECT_ID}"

for sa in mcp-poc-runtime mcp-poc-caller mcp-poc-outsider; do
  gcloud iam service-accounts describe "${sa}@${PROJECT_ID}.iam.gserviceaccount.com" \
    --project "${PROJECT_ID}" >/dev/null 2>&1 \
  || gcloud iam service-accounts create "${sa}" --project "${PROJECT_ID}" \
       --display-name "MCP auth PoC (${sa}); safe to delete"
done

for sa in "${CALLER_SA}" "${OUTSIDER_SA}"; do
  gcloud iam service-accounts add-iam-policy-binding "${sa}" --project "${PROJECT_ID}" \
    --member "${OPERATOR}" --role roles/iam.serviceAccountTokenCreator --condition=None >/dev/null
done

gcloud run deploy "${SERVICE}" --project "${PROJECT_ID}" --region "${REGION}" \
  --source "${SOURCE_DIR}" \
  --no-allow-unauthenticated \
  --invoker-iam-check \
  --ingress all \
  --labels purpose=mcp-auth-poc \
  --service-account "${RUNTIME_SA}" \
  --min-instances 0 --max-instances 1 --memory 256Mi --cpu 1 --timeout 30 --concurrency 10 \
  --set-env-vars "EXPECTED_AUDIENCE=${SERVICE_URL}" \
  --quiet

gcloud run services add-iam-policy-binding "${SERVICE}" --project "${PROJECT_ID}" --region "${REGION}" \
  --member "serviceAccount:${CALLER_SA}" --role roles/run.invoker --condition=None >/dev/null
set +x

echo
echo "Deployed. Service URL (expected audience): ${SERVICE_URL}"
echo "IAM changes can take a minute to propagate. Next: ./probe_poc.sh"
