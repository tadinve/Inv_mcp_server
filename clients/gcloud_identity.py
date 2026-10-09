"""Mint short-lived Google ID tokens by service-account impersonation (no keys).

Wraps `gcloud auth print-identity-token --impersonate-service-account`. The
caller needs roles/iam.serviceAccountTokenCreator on the target account.
Tokens are returned to the caller only: never printed, logged, or passed on a
command line.
"""

from __future__ import annotations

import shutil
import subprocess
from urllib.parse import urlsplit


class IdentityTokenError(RuntimeError):
    pass


def audience_for(url: str) -> str:
    """Cloud Run audience for a service URL: scheme + host, no path."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def mint_identity_token(service_account: str, audience: str, *, timeout: float = 60.0) -> str:
    gcloud = shutil.which("gcloud")
    if gcloud is None:
        raise IdentityTokenError("gcloud is not installed or not on PATH")
    proc = subprocess.run(  # noqa: S603 - fixed executable, arguments are not shell-interpreted
        [
            gcloud,
            "auth",
            "print-identity-token",
            f"--impersonate-service-account={service_account}",
            f"--audiences={audience}",
            "--include-email",
            "--quiet",
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    token = proc.stdout.strip()
    if proc.returncode != 0 or token.count(".") != 2:
        # stderr leads with an impersonation WARNING; the cause is on the ERROR line.
        lines = [line for line in proc.stderr.strip().splitlines() if line.strip()]
        cause = next((line for line in lines if line.startswith("ERROR")), lines[-1] if lines else "unknown error")
        raise IdentityTokenError(f"could not mint an ID token for {service_account}: {cause[:300]}")
    return token
