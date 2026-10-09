#!/usr/bin/env bash
# Shared settings for the token-forwarding PoC. Sourced by the other scripts.
#
# Each setting uses the first value found:
#   PROJECT_ID: $PROJECT_ID, $GOOGLE_CLOUD_PROJECT, $CLOUDSDK_CORE_PROJECT, gcloud config core/project
#   REGION:     $REGION, $CLOUDSDK_RUN_REGION, gcloud config run/region, us-central1
#   OPERATOR:   $OPERATOR, else user:<gcloud config core/account>
# The deploy and cleanup plans print the resolved values before asking for confirmation.
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

SERVICE="mcp-auth-poc"
RUNTIME_SA="mcp-poc-runtime@${PROJECT_ID}.iam.gserviceaccount.com"
CALLER_SA="mcp-poc-caller@${PROJECT_ID}.iam.gserviceaccount.com"
OUTSIDER_SA="mcp-poc-outsider@${PROJECT_ID}.iam.gserviceaccount.com"
PROJECT_NUMBER="$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)')"
# Cloud Run's deterministic service URL; used as the expected token audience.
SERVICE_URL="https://${SERVICE}-${PROJECT_NUMBER}.${REGION}.run.app"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

confirm() {
  local phrase="$1"
  echo
  read -r -p "Type '${phrase}' to proceed (anything else aborts): " answer
  if [[ "${answer}" != "${phrase}" ]]; then
    echo "Aborted. Nothing was changed by this step."
    exit 1
  fi
}

# --- Ownership guards (see deployment/common.sh) ------------------------------------------
SERVICE_LABEL="mcp-auth-poc"
SA_MARKER="MCP auth PoC"

assert_all_ours() {
  local label display
  if label="$(gcloud run services describe "${SERVICE}" --project "${PROJECT_ID}" --region "${REGION}" \
      --format='value(metadata.labels.purpose)' 2>/dev/null)" && [[ "${label}" != "${SERVICE_LABEL}" ]]; then
    echo "REFUSING: service ${SERVICE} exists without label purpose=${SERVICE_LABEL}; it is not ours." >&2
    exit 1
  fi
  for sa in "${RUNTIME_SA}" "${CALLER_SA}" "${OUTSIDER_SA}"; do
    if display="$(gcloud iam service-accounts describe "${sa}" --project "${PROJECT_ID}" \
        --format='value(displayName)' 2>/dev/null)" && [[ "${display}" != "${SA_MARKER}"* ]]; then
      echo "REFUSING: service account ${sa} exists but was not created by these scripts." >&2
      exit 1
    fi
  done
}
