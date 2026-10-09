#!/usr/bin/env bash
# Removes everything deploy_poc.sh created. Deletes cloud resources: run only with approval.
source "$(dirname "$0")/common.sh"

IMAGE_PATH="${REGION}-docker.pkg.dev/${PROJECT_ID}/cloud-run-source-deploy/${SERVICE}"

cat <<PLAN
================ Token-forwarding PoC: cleanup plan ================
Project: ${PROJECT_ID}   Region: ${REGION}

Will delete:
  - Cloud Run service ${SERVICE} (including its invoker binding)
  - Container images under ${IMAGE_PATH}
  - Service accounts ${RUNTIME_SA},
    ${CALLER_SA}, ${OUTSIDER_SA}
    (deleting them also removes the operator's Token Creator bindings on them)

Will NOT touch (shared; may be used by other workloads):
  - the cloud-run-source-deploy repository itself
  - the Cloud Run source-upload bucket
  - enabled APIs
====================================================================
PLAN
confirm "delete-poc"

set -x
gcloud run services delete "${SERVICE}" --project "${PROJECT_ID}" --region "${REGION}" --quiet || true
gcloud artifacts docker images delete "${IMAGE_PATH}" --project "${PROJECT_ID}" --delete-tags --quiet || true
for sa in "${RUNTIME_SA}" "${CALLER_SA}" "${OUTSIDER_SA}"; do
  gcloud iam service-accounts delete "${sa}" --project "${PROJECT_ID}" --quiet || true
done
set +x

echo
echo "Verify nothing remains:"
echo "  gcloud run services list --project ${PROJECT_ID} --region ${REGION} --filter=metadata.name=${SERVICE}"
echo "  gcloud iam service-accounts list --project ${PROJECT_ID} --filter='email~^mcp-poc-'"
