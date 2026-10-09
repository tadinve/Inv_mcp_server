# Token-forwarding PoC: results

- **Run:** 2026-10-08 (local date), by the project owner, using `deploy_poc.sh` and `probe_poc.sh`
- **Project:** a temporary Qwiklabs lab project
- **Region:** `us-central1`
- **Revision:** `mcp-auth-poc-00001-6hs`
- **Service:** unauthenticated invocation disabled, `--invoker-iam-check` on, ingress all, max 1 instance (min 0 is the default)
- **Tokens:** ID tokens minted by Google for `mcp-poc-caller` and `mcp-poc-outsider` through impersonation (`gcloud auth print-identity-token`), with audience equal to the service URL. No tokens were printed or stored.

## Results

| Probe | Request | Expected | Observed HTTP | Token verification in container | Verdict |
|---|---|---|---|---|---|
| A | Authorized caller, token in `Authorization` | 200, signature verifies | **200** | 3 segments, signature 342 chars, **`signature_verifies: true`**, issuer `https://accounts.google.com`, `audience_matches: true`, `subject_present: true`, `email_verified: true` | **PASS** |
| B | Authorized caller, token in `X-Serverless-Authorization` | 200, signature removed | **200** | 3 segments, signature segment **replaced** (27 chars), `signature_verifies: false` (`MalformedError`) | **PASS** (unverifiable, as documented) |
| C | Anonymous | 401/403 from Cloud Run | **403** | Never reached the container | **PASS** |
| D | Outsider: valid Google token, no `run.invoker` | 403 from Cloud Run | **403** | Never reached the container | **PASS** |
| E | Authorized caller, wrong audience | 401/403 from Cloud Run | **401** | Never reached the container | **PASS** |
| F | Authorized caller, corrupted signature | 401/403 from Cloud Run | **401** | Never reached the container | **PASS** |

Note on B: the plan predicted an empty signature segment (length 0). Cloud Run
instead replaced it with a 27-character value. That length matches the
placeholder `SIGNATURE_REMOVED_BY_GOOGLE`, though the PoC deliberately does not
return segment contents, so this is inferred. The outcome is the same either
way: the token cannot be verified. The application's verifier would reject it
(`invalid_signature`).

## Conclusion

The A1 assumption holds. With Cloud Run IAM enabled, the original
Google-signed ID token sent in `Authorization` reaches the container intact and
can be verified independently with `google-auth`: signature, issuer, audience
and subject.

Architectural consequences:

1. **Keep the two-layer design as specified.** Cloud Run IAM is the outer
   boundary: C–F were rejected before reaching the container. The application
   verifies the same `Authorization` token itself (A, and the simulated test
   suite), then authorizes each tool call by verified `sub`.
2. **`X-Serverless-Authorization` must not carry identity for the application.**
   Its signature does not survive Cloud Run (B). The application already ignores
   that header (`test_simulated_e2e_identity_headers_cannot_replace_a_token`).
3. **No weakening was required.** No verification step was relaxed or skipped to
   obtain these results.

## Evidence boundaries

- Probes C–F show Cloud Run's behavior. They do not test the application's
  verifier, because those requests never reached it. The application's own
  rejection of wrong audience, wrong issuer, forged, expired and non-RS256 tokens
  is covered by the local simulated tests (`tests/test_google_oidc_simulated.py`).
- The PoC ran the standalone probe app, not the MCP server. Running the MCP
  server's `GoogleOIDCAuthenticator` against real tokens on Cloud Run is the
  Checkpoint 3 integration test.
- One run, one region, on 2026-10-08. Platform behavior could change. The
  Checkpoint 3 integration tests re-check it on every deployment.

## Cleanup

To be completed with `cleanup_poc.sh`. Record the verification output here:

```
(paste the output of the two verification commands)
```
