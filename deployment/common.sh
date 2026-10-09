#!/usr/bin/env bash
# Shared settings for the Cloud Run deployment of the MCP server. Sourced by the other scripts.
#
# Each setting uses the first value found:
#   PROJECT_ID: $PROJECT_ID, $GOOGLE_CLOUD_PROJECT, $CLOUDSDK_CORE_PROJECT, gcloud config core/project
#   REGION:     $REGION, $CLOUDSDK_RUN_REGION, gcloud config run/region, us-central1
#   OPERATOR:   $OPERATOR, else user:<gcloud config core/account>
# Plans print the resolved values before asking for confirmation.
set -euo pipefail

_gcloud_config() { gcloud config get-value "$1" 2>/dev/null || true; }

PROJECT_ID="${PROJECT_ID:-${GOOGLE_CLOUD_PROJECT:-${CLOUDSDK_CORE_PROJECT:-$(_gcloud_config core/project)}}}"
REGION="${REGION:-${CLOUDSDK_RUN_REGION:-$(_gcloud_config run/region)}}"
REGION="${REGION:-us-central1}"
if [[ -z "${OPERATOR:-}" ]]; then
  _account="$(_gcloud_config core/account)"
  OPERATOR="${_account:+user:${_account}}"
fi
: "${PROJECT_ID:?No project: set PROJECT_ID or run gcloud config set project PROJECT}"
: "${OPERATOR:?No operator: set OPERATOR (e.g. user:you@example.com) or log in with gcloud auth login}"

SERVICE="cresenta-inventory"
SA_DOMAIN="${PROJECT_ID}.iam.gserviceaccount.com"
RUNTIME_SA="mcp-inventory-runtime@${SA_DOMAIN}"
READER_SA="mcp-reader@${SA_DOMAIN}"
WRITER_SA="mcp-writer@${SA_DOMAIN}"
OUTSIDER_SA="mcp-outsider@${SA_DOMAIN}"
PROJECT_NUMBER="$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)')"
# Cloud Run's deterministic URL: the token audience and the only allowed Host.
SERVICE_HOST="${SERVICE}-${PROJECT_NUMBER}.${REGION}.run.app"
SERVICE_URL="https://${SERVICE_HOST}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

confirm() {
  local phrase="$1"
  echo
  read -r -p "Type '${phrase}' to proceed (anything else aborts): " answer
  if [[ "${answer}" != "${phrase}" ]]; then
    echo "Aborted. Nothing was changed by this step."
    exit 1
  fi
}

# --- Ownership guards ----------------------------------------------------------------------
# Every mutating command targets fixed names. These guards also refuse to touch a resource
# with one of those names that this project did not create (for example another team's
# service that happens to share a name). Our resources carry a label or display-name marker.
SERVICE_LABEL="mcp-inventory-demo"
SA_MARKER="Cresenta MCP demo"

assert_service_is_ours() {
  local label
  if ! label="$(gcloud run services describe "${SERVICE}" --project "${PROJECT_ID}" --region "${REGION}" \
      --format='value(metadata.labels.purpose)' 2>/dev/null)"; then
    return 0  # absent: nothing to protect
  fi
  if [[ "${label}" != "${SERVICE_LABEL}" ]]; then
    echo "REFUSING: service ${SERVICE} exists without label purpose=${SERVICE_LABEL}; it is not ours." >&2
    exit 1
  fi
}

assert_sa_is_ours() {
  local email="$1" display
  if ! display="$(gcloud iam service-accounts describe "${email}" --project "${PROJECT_ID}" \
      --format='value(displayName)' 2>/dev/null)"; then
    return 0
  fi
  if [[ "${display}" != "${SA_MARKER}"* ]]; then
    echo "REFUSING: service account ${email} exists but was not created by these scripts." >&2
    exit 1
  fi
}

assert_all_ours() {
  assert_service_is_ours
  for sa in "${RUNTIME_SA}" "${READER_SA}" "${WRITER_SA}" "${OUTSIDER_SA}"; do assert_sa_is_ours "${sa}"; done
}
