# Cloud Run token-forwarding proof-of-concept

**Status: prepared, NOT deployed.** Deploying and cleaning up both need owner
approval (SPEC-AMENDMENT-1, A1.1).

## The question

The application verifies Google-signed ID tokens itself, as well as relying on
Cloud Run IAM. That only works if Cloud Run forwards the `Authorization` token
to the container **with its signature intact**.

- Google documents that Cloud Run *removes* the signature from
  `X-Serverless-Authorization` tokens.
- Google's documentation does not say the same for `Authorization`, but does
  not explicitly guarantee it either.

This PoC measures it. The service at `GET /probe` reports, for each header, the
JWT segment count, the signature length, and whether `google-auth` verifies the
token against the expected audience. It never returns or logs tokens or subjects.

## Decision rule

| Probe A result (`authorization`) | Meaning | Next step |
|---|---|---|
| `signature_verifies: true` | Assumption holds | Proceed with A1 as designed (Checkpoint 3) |
| `signature_segment_length: 0` or `signature_verifies: false` | Assumption fails | **Stop and report.** Do not weaken verification. Revisit A1 with the evidence |

## Planned resources

| Resource | Purpose | Privileges |
|---|---|---|
| Cloud Run service `mcp-auth-poc` | The probe | Unauthenticated invocation disabled; max 1 instance, 256 MiB, 30 s timeout |
| SA `mcp-poc-runtime` | Runtime identity of the service | **No roles** |
| SA `mcp-poc-caller` | Test caller | `roles/run.invoker` on `mcp-auth-poc` only |
| SA `mcp-poc-outsider` | Negative test caller | No invoker access |
| Token Creator bindings | Lets the operator mint ID tokens for the two test SAs | `roles/iam.serviceAccountTokenCreator` on those two SAs only, not project-wide |

No service account keys are created. The operator mints short-lived ID tokens by
impersonation.

`gcloud run deploy --source` also creates, if missing:
- the Artifact Registry repository `cloud-run-source-deploy`;
- a Cloud Storage bucket for source uploads.

APIs enabled: `run`, `cloudbuild`, `artifactregistry`, `iamcredentials`.

## IAM the operator needs

`roles/owner` on a personal sandbox project covers everything. Least-privilege
alternative:

| Role | Why |
|---|---|
| `roles/serviceusage.serviceUsageAdmin` | Enable the four APIs |
| `roles/iam.serviceAccountAdmin` | Create the SAs and set Token Creator on them |
| `roles/run.admin` | Deploy the service and set its invoker policy |
| `roles/iam.serviceAccountUser` on `mcp-poc-runtime` | Deploy a service that runs as that SA |
| `roles/cloudbuild.builds.editor` | Run the source build |
| `roles/artifactregistry.admin` (or `writer` if the repo already exists) | Create the repo and push the image |
| `roles/storage.admin` (first source deploy only) | Create the source-upload bucket |

In some newer projects, a `--source` build fails until the build service account
has `roles/run.builder`. If that happens the deploy stops with a permission
error. Report it rather than granting broad roles.

## Commands (owner runs these; Claude Code does not)

```bash
cd deployment/poc
export PROJECT_ID=your-sandbox-project
export REGION=us-central1
export OPERATOR=user:you@example.com

./deploy_poc.sh    # prints the full plan, then requires typing: deploy-poc
./probe_poc.sh     # read-only: mints tokens, runs probes A-F, prints status + PoC JSON (no tokens)
./cleanup_poc.sh   # prints the deletion plan, then requires typing: delete-poc
```

Probes:

| | Request | Expected |
|---|---|---|
| A | Caller token in `Authorization` | 200, **`authorization.signature_verifies: true`** (the key question) |
| B | Caller token in `X-Serverless-Authorization` | 200; signature expected to be stripped |
| C | No token | 401/403 from Cloud Run |
| D | Outsider token (no invoker role) | 403 from Cloud Run |
| E | Caller token, wrong audience | 401/403 from Cloud Run |
| F | Caller token with a corrupted signature | 401/403 from Cloud Run |

C–F prove the Cloud Run IAM boundary. The application's own checks for wrong
audience, forged tokens, expiry and so on are covered by the local *simulated*
tests in `tests/test_google_oidc_simulated.py`, because Cloud Run rejects those
requests before the application sees them.

## Cleanup

`cleanup_poc.sh` deletes the service, the PoC images, and the three service
accounts. Deleting the accounts also removes the Token Creator bindings on them.
It deliberately leaves shared things alone: the `cloud-run-source-deploy`
repository, the source bucket, and enabled APIs. To remove them in a dedicated
sandbox project:

```bash
gcloud artifacts repositories delete cloud-run-source-deploy --location "$REGION" --project "$PROJECT_ID"
gcloud storage buckets list --project "$PROJECT_ID"   # then delete the run-sources bucket if unused
```
