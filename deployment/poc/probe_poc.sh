#!/usr/bin/env bash
# Probes the deployed PoC. Read-only against cloud resources: mints short-lived
# ID tokens by impersonation and sends requests. Tokens are held in variables
# and never printed.
source "$(dirname "$0")/common.sh"
set +x

PROBE_URL="${SERVICE_URL}/probe"
mint() { # $1 = service account, $2 = audience
  gcloud auth print-identity-token --impersonate-service-account="$1" \
    --audiences="$2" --include-email 2>/dev/null
}
call() { # $1 = label, rest = curl args. Prints the HTTP status and the PoC's JSON (no tokens).
  local label="$1"; shift
  local body status
  body="$(mktemp)"
  status="$(curl -sS -o "${body}" -w '%{http_code}' "$@" "${PROBE_URL}")"
  echo "--- ${label}: HTTP ${status}"
  if [[ "${status}" == "200" ]]; then python3 -m json.tool "${body}"; fi
  rm -f "${body}"
}

CALLER_TOKEN="$(mint "${CALLER_SA}" "${SERVICE_URL}")"
OUTSIDER_TOKEN="$(mint "${OUTSIDER_SA}" "${SERVICE_URL}")"
WRONG_AUD_TOKEN="$(mint "${CALLER_SA}" "https://wrong-audience.example.com")"
# Forged: the caller's real token with one character in the middle of the signature changed.
# (Not the last character: in base64url that can hold only padding bits and still verify.)
FORGED_TOKEN="$(TOKEN="${CALLER_TOKEN}" python3 -c '
import os
h, p, s = os.environ["TOKEN"].split(".")
i = len(s) // 2
print(".".join([h, p, s[:i] + ("A" if s[i] != "A" else "B") + s[i + 1:]]))
')"

echo "Probing ${PROBE_URL}"
call "A. caller token in Authorization (THE KEY QUESTION: expect 200 and authorization.signature_verifies=true)" \
  -H "Authorization: Bearer ${CALLER_TOKEN}"
call "B. caller token in X-Serverless-Authorization (expect 200; Google documents the signature is removed)" \
  -H "X-Serverless-Authorization: Bearer ${CALLER_TOKEN}"
call "C. anonymous (expect 401 or 403 from Cloud Run)"
call "D. outsider token, no invoker role (expect 403 from Cloud Run)" \
  -H "Authorization: Bearer ${OUTSIDER_TOKEN}"
call "E. caller token with wrong audience (expect 401 or 403 from Cloud Run)" \
  -H "Authorization: Bearer ${WRONG_AUD_TOKEN}"
call "F. forged signature (expect 401 or 403 from Cloud Run)" \
  -H "Authorization: Bearer ${FORGED_TOKEN}"

unset CALLER_TOKEN OUTSIDER_TOKEN WRONG_AUD_TOKEN FORGED_TOKEN
