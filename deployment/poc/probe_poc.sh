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
# Headers carrying tokens are passed to curl from a private temp file (curl -H @file),
# never on the command line, where other local processes could read them.
WORKDIR="$(mktemp -d)"; chmod 700 "${WORKDIR}"
trap 'rm -rf "${WORKDIR}"' EXIT
call() { # $1 = label, $2 = header name or "", $3 = token or "". Prints status + PoC JSON only.
  local label="$1" header="$2" token="$3"
  local args=(-sS -o "${WORKDIR}/body" -w '%{http_code}')
  if [[ -n "${header}" ]]; then
    ( umask 077; printf '%s: Bearer %s\n' "${header}" "${token}" > "${WORKDIR}/headers" )
    args+=(-H "@${WORKDIR}/headers")
  fi
  local status
  status="$(curl "${args[@]}" "${PROBE_URL}")"
  rm -f "${WORKDIR}/headers"
  echo "--- ${label}: HTTP ${status}"
  if [[ "${status}" == "200" ]]; then python3 -m json.tool "${WORKDIR}/body"; fi
  rm -f "${WORKDIR}/body"
}

# The expected audience is the deterministic URL; make sure the service actually serves it.
URLS="$(gcloud run services describe "${SERVICE}" --project "${PROJECT_ID}" --region "${REGION}" \
  --format='value(metadata.annotations."run.googleapis.com/urls")')"
if [[ "${URLS}" != *"${SERVICE_URL}"* ]]; then
  echo "ABORT: ${SERVICE_URL} is not among the service URLs (${URLS}). Audience would be wrong." >&2
  exit 1
fi

CALLER_TOKEN="$(mint "${CALLER_SA}" "${SERVICE_URL}")"
OUTSIDER_TOKEN="$(mint "${OUTSIDER_SA}" "${SERVICE_URL}")"
WRONG_AUD_TOKEN="$(mint "${CALLER_SA}" "https://wrong-audience.example.com")"
for name in CALLER_TOKEN OUTSIDER_TOKEN WRONG_AUD_TOKEN; do
  if [[ "${!name}" != eyJ*.*.* ]]; then
    echo "ABORT: could not mint ${name} (IAM grants can take a minute to propagate; retry shortly)." >&2
    exit 1
  fi
done
# Forged: the caller's real token with one character in the middle of the signature changed.
# (Not the last character: in base64url that can hold only padding bits and still verify.)
FORGED_TOKEN="$(TOKEN="${CALLER_TOKEN}" python3 -c '
import os
h, p, s = os.environ["TOKEN"].split(".")
i = len(s) // 2
print(".".join([h, p, s[:i] + ("A" if s[i] != "A" else "B") + s[i + 1:]]))
')"

echo "Probing ${PROBE_URL}"
call "A. caller token in Authorization (KEY QUESTION: expect 200 and authorization.signature_verifies=true)" \
  "Authorization" "${CALLER_TOKEN}"
call "B. caller token in X-Serverless-Authorization (expect 200; signature removed or replaced, must not verify)" \
  "X-Serverless-Authorization" "${CALLER_TOKEN}"
call "C. anonymous (expect 401 or 403 from Cloud Run)" "" ""
call "D. outsider token, no invoker role (expect 403 from Cloud Run)" "Authorization" "${OUTSIDER_TOKEN}"
call "E. caller token with wrong audience (expect 401 or 403 from Cloud Run)" "Authorization" "${WRONG_AUD_TOKEN}"
call "F. forged signature (expect 401 or 403 from Cloud Run)" "Authorization" "${FORGED_TOKEN}"

unset CALLER_TOKEN OUTSIDER_TOKEN WRONG_AUD_TOKEN FORGED_TOKEN
