#!/usr/bin/env bash
# Shared settings for the token-forwarding PoC. Sourced by the other scripts.
# Required: PROJECT_ID, OPERATOR (e.g. user:you@example.com). Optional: REGION.
set -euo pipefail

: "${PROJECT_ID:?Set PROJECT_ID to the Google Cloud project to use}"
: "${OPERATOR:?Set OPERATOR to the principal running the test, e.g. user:you@example.com}"
REGION="${REGION:-us-central1}"

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
