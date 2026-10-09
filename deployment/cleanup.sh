#!/usr/bin/env bash
# Removes everything deploy_cloud_run.sh created. Deletes cloud resources: run only with approval.
source "$(dirname "$0")/common.sh"

cat <<PLAN
================ Cresenta inventory MCP server: cleanup plan ================
Project: ${PROJECT_ID}   Region: ${REGION}

Will delete:
  - Cloud Run service ${SERVICE} (its ephemeral restock database goes with it)
  - The ${SERVICE} image package in Artifact Registry repo cloud-run-source-deploy
  - Service accounts ${RUNTIME_SA}, ${READER_SA},
    ${WRITER_SA}, ${OUTSIDER_SA}
    (also removes the operator's Token Creator bindings on them)

Will NOT touch: the shared cloud-run-source-deploy repo, source bucket, enabled APIs,
or any other service or account in the project.
=============================================================================
PLAN
confirm "delete-mcp"
assert_all_ours  # refuse to touch same-named resources we did not create

set -x
gcloud run services delete "${SERVICE}" --project "${PROJECT_ID}" --region "${REGION}" --quiet || true
gcloud artifacts packages delete "${SERVICE}" --repository cloud-run-source-deploy --location "${REGION}" \
  --project "${PROJECT_ID}" --quiet || true
for sa in "${RUNTIME_SA}" "${READER_SA}" "${WRITER_SA}" "${OUTSIDER_SA}"; do
  gcloud iam service-accounts delete "${sa}" --project "${PROJECT_ID}" --quiet || true
done
set +x

echo
echo "=== Verification (each list should be empty)"
echo "--- service:";  gcloud run services list --project "${PROJECT_ID}" --region "${REGION}" --filter="metadata.name=${SERVICE}" --format="value(metadata.name)"
echo "--- accounts:"; gcloud iam service-accounts list --project "${PROJECT_ID}" --filter="email~^mcp-(inventory-runtime|reader|writer|outsider)@" --format="value(email)"
echo "--- image:";    gcloud artifacts packages list --repository cloud-run-source-deploy --location "${REGION}" --project "${PROJECT_ID}" --filter="name~${SERVICE}" --format="value(name)" 2>/dev/null || true
