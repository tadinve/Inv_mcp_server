# Threat model and trust boundaries

Scope: the Cresenta inventory MCP server deployed on Cloud Run (Checkpoint 3
design), plus local development. Status: authentication and authorization are
implemented and tested locally. The Cloud Run assumptions are **unverified** until
the token-forwarding PoC runs (`deployment/poc/`).

**Update (2026-10-08):** the PoC ran and passed. See
[deployment/poc/RESULTS.md](../deployment/poc/RESULTS.md).

## Assets

- Integrity of restock requests: only authorized callers create them; no duplicates.
- Confidentiality of inventory data (synthetic here, but treated as business data).
- Integrity of the permission policy.

## Trust boundaries

```
 caller ──(1) HTTPS + Google ID token──▶ Cloud Run IAM ──(2)──▶ container
                                           checks invoker          AuthenticationMiddleware
                                                                   verifies the token again ──(3)──▶ tool handler
                                                                                                    checks permission per call
```

1. **Internet → Cloud Run front end.** Cloud Run requires authenticated invocation.
   Only principals with `roles/run.invoker` reach the container. Proven by PoC
   probes C–F: anonymous 403, outsider 403, wrong audience 401, forged 401.
2. **Cloud Run → container.** The application does not trust that it sits behind
   IAM. It verifies the `Authorization` token itself:
   - signature against Google's certificates;
   - `alg` must be RS256 and a `kid` must be present;
   - issuer, exact audience, `iat`/`exp` (30 s skew), and a non-empty `sub`.

   Any failure gives HTTP 401 and MCP never runs. Proven locally by the simulated
   tests. PoC probe A confirmed that the signed token reaches the container intact
   and verifies.
3. **Authenticated → authorized.** Every tool call is checked against the
   server-side policy using the verified `sub` only:
   - deny by default;
   - checked before business logic or database access;
   - discovery (`tools/list`) grants nothing.

## Threats and mitigations

| Threat | Mitigation | Evidence |
|---|---|---|
| Anonymous or non-invoker caller | Cloud Run IAM | PoC C, D; Checkpoint 3 integration tests (403 on the real MCP server) |
| Forged, tampered or signature-stripped token | In-app signature verification | `test_simulated_forged_*`, `*_tampered_*`, `*_stripped_*` |
| Token minted for another service (audience confusion) | Exact audience match, configured via `INVENTORY_OIDC_AUDIENCE`; required in Google mode | `test_simulated_wrong_audience_*`, config tests |
| Non-Google issuer | Issuer allowlist (`https://accounts.google.com`, `accounts.google.com`) | `test_simulated_wrong_issuer_*` |
| `alg: none` or HS256 key confusion | RS256-only allowlist checked before verification | `test_simulated_non_rs256_*` |
| Replay of an expired token | `exp` check, 30 s skew | `test_simulated_expired_*` |
| Identity spoofing via headers (`X-Goog-Authenticated-User-*`, `X-Serverless-Authorization`) | Ignored; only the verified `Authorization` token counts | `test_simulated_e2e_identity_headers_*`, `test_caller_supplied_identity_headers_are_ignored` |
| Email reuse after a service account is deleted and recreated | Authorize on `sub` (the account's unique ID), never on email | `test_simulated_authorization_uses_sub_not_email` |
| Privilege escalation through tool arguments | Permissions come only from the policy; extra arguments are ignored | `test_caller_supplied_role_arguments_cannot_elevate` |
| Reader writes | `restock.create` checked before any database access | `test_reader_cannot_create_restock_and_nothing_is_written` |
| Duplicate or conflicting writes on retry or under concurrency | Unique `(principal_sub, idempotency_key)`, canonical fingerprint, `BEGIN IMMEDIATE` | `test_concurrent_*` |
| TEST-ONLY dev authentication deployed by mistake | Refused when `K_SERVICE`/`K_REVISION`/`K_CONFIGURATION` is set; refused on non-loopback binds without an explicit override; Google mode needs an explicit audience | `tests/test_configuration.py`, including a real process-exit test |
| Google certificate endpoint unreachable | Fail closed (401); cached certificates; forced refresh rate-limited to one per 60 s | `test_simulated_certificate_outage_fails_closed`, cache tests |
| Submitted values echoed back in validation errors | `ArgumentValidationMiddleware` returns field names and error types only | `test_invalid_arguments_never_echo_submitted_values` |
| Tokens or payloads in logs | Logs carry reason categories, labels and field names only | Manual log review (Checkpoint 1); log assertions planned for Checkpoint 3 |
| Stack traces to clients | Unexpected errors become `internal_error`; traces stay server-side | `_execute` handler |

## Rejected alternatives

| Alternative | Why rejected |
|---|---|
| Trust Cloud Run IAM alone and read the identity from the forwarded token without verifying it | Authorizes on unverified claims (A1). Breaks if ingress is ever misconfigured or the container is reachable another way |
| Disable the Invoker IAM check (`--no-invoker-iam-check`) and verify only in the app | Loses the outer boundary; every anonymous request reaches application code (A1: do not disable) |
| Use `X-Serverless-Authorization` | Google documents that Cloud Run strips the signature, so the app cannot verify it |
| MCP-native OAuth (authorization server, Protected Resource Metadata) | Out of scope (A2). Suits general-purpose remote MCP clients with user consent, not controlled service-to-service callers |
| Authorize by service-account email | Emails can be reused after an account is deleted and recreated; `sub` is the stable unique ID |
| Hide unauthorized tools from `tools/list` | Possible later. Not a security control on its own; enforcement must be at call time either way (A7) |

## Open questions

1. ~~**Does Cloud Run forward `Authorization` with the signature intact?**~~
   **Resolved: yes** (PoC probe A, 2026-10-08). The signature verified in the
   container. `X-Serverless-Authorization` arrived with its signature replaced
   and did not verify (probe B), confirming it must not carry identity.
2. **Host header validation on Cloud Run.** *Resolved: verified on Cloud Run
   (2026-10-08).* A valid token sent to the legacy `*.a.run.app` hostname got 421
   from the application ([deployment/RESULTS.md](../deployment/RESULTS.md)).
   - Google mode off loopback now refuses to start without `INVENTORY_ALLOWED_HOSTS`.
     The SDK silently disables DNS-rebinding protection otherwise.
   - Locally: the pinned host is accepted, other hosts get 421, a foreign `Origin`
     gets 403, and `/health` is not pinned, so Cloud Run startup probes work
     (`tests/test_host_validation.py`).
   - Cloud Run serves each service on two hostnames, the deterministic one and a
     legacy `*.a.run.app` one. Only the deterministic one is pinned. The integration
     test `test_real_requests_via_non_pinned_hostname_are_rejected` checks for 421
     on the other.
3. **Policy distribution.** *Decided:* `INVENTORY_POLICY_JSON` environment
   variable, generated by `deploy_cloud_run.sh` from the reader and writer
   `uniqueId`s. It is not baked into the image, so the image stays
   environment-neutral. Changing the policy requires a new revision (a deploy),
   which leaves an audit record. Exactly one policy source is allowed (file or
   JSON); both or neither refuses to start.
4. **SDK validation-error format is SDK-version-specific.** The sanitizing
   middleware reuses `FuncMetadata.validate_arguments`. A test pins the sanitized
   output, so an SDK upgrade that changes behavior fails CI rather than silently
   leaking values.
