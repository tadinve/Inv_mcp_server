"""Runtime configuration, read from environment variables.

Nothing has an insecure default: authentication mode and the permission policy
must be selected explicitly, or startup fails.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlsplit


class ConfigError(Exception):
    """Invalid or unsafe configuration. Startup must fail."""


class AuthMode(StrEnum):
    # Test-only: the bearer token names the principal. Never valid on Cloud Run.
    DEV = "dev"
    # Google-signed OIDC ID tokens, verified in-process.
    GOOGLE = "google"


# Cloud Run sets these in every service container. Any of them means "deployed".
CLOUD_RUN_ENV_MARKERS = ("K_SERVICE", "K_REVISION", "K_CONFIGURATION")
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _validate_audience(raw: str) -> str:
    """Require an https URL with a host and no query/fragment, e.g. the Cloud Run service URL."""
    audience = raw.strip()
    parts = urlsplit(audience)
    if parts.scheme != "https" or not parts.hostname or parts.query or parts.fragment or " " in audience:
        raise ConfigError("INVENTORY_OIDC_AUDIENCE must be an https URL such as https://SERVICE-PROJECT.REGION.run.app")
    return audience


def _guard_dev_mode(env: Mapping[str, str], host: str) -> None:
    """Refuse TEST-ONLY dev authentication anywhere it could be reachable by others.

    Two independent checks: the Cloud Run markers, and a loopback-only bind unless an
    explicit local-container override is set. Neither is the only safeguard; deployed
    configuration must also select INVENTORY_AUTH_MODE=google explicitly.
    """
    present = [name for name in CLOUD_RUN_ENV_MARKERS if env.get(name)]
    if present:
        raise ConfigError(f"dev authentication is TEST-ONLY and is refused on Cloud Run ({', '.join(present)} is set)")
    if host not in LOOPBACK_HOSTS and env.get("INVENTORY_DEV_ALLOW_NON_LOOPBACK", "").lower() != "true":
        raise ConfigError(
            f"dev authentication only binds to loopback; refusing host {host!r}. "
            "For a local container only, set INVENTORY_DEV_ALLOW_NON_LOOPBACK=true."
        )


def _guard_host_validation(env: Mapping[str, str], host: str, allowed_hosts: tuple[str, ...]) -> None:
    """Google mode off loopback (i.e. deployed) must pin the Host header.

    Without INVENTORY_ALLOWED_HOSTS the SDK disables DNS-rebinding protection for
    non-loopback binds, so require it rather than silently running without it.
    """
    deployed = host not in LOOPBACK_HOSTS or any(env.get(name) for name in CLOUD_RUN_ENV_MARKERS)
    if deployed and not allowed_hosts:
        raise ConfigError(
            "INVENTORY_ALLOWED_HOSTS is required in google mode off loopback "
            "(e.g. SERVICE-PROJECT_NUMBER.REGION.run.app)"
        )
    for allowed in allowed_hosts:
        if "/" in allowed or " " in allowed or allowed == "*":
            raise ConfigError(f"INVENTORY_ALLOWED_HOSTS entries must be bare host[:port] values; got {allowed!r}")


@dataclass(frozen=True)
class Settings:
    auth_mode: AuthMode
    policy_path: Path | None
    db_path: Path
    host: str
    port: int
    allowed_hosts: tuple[str, ...]
    log_level: str
    oidc_audience: str | None = None
    policy_json: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env

        raw_mode = env.get("INVENTORY_AUTH_MODE", "").strip().lower()
        try:
            auth_mode = AuthMode(raw_mode)
        except ValueError:
            raise ConfigError(
                f"INVENTORY_AUTH_MODE must be one of {[m.value for m in AuthMode]}; got {raw_mode!r}"
            ) from None

        host = env.get("INVENTORY_HOST", "127.0.0.1")
        allowed_hosts = tuple(h.strip() for h in env.get("INVENTORY_ALLOWED_HOSTS", "").split(",") if h.strip())
        oidc_audience: str | None = None
        if auth_mode is AuthMode.DEV:
            _guard_dev_mode(env, host)
        else:
            oidc_audience = _validate_audience(env.get("INVENTORY_OIDC_AUDIENCE", ""))
            _guard_host_validation(env, host, allowed_hosts)

        # Exactly one policy source: a file (local) or inline JSON (Cloud Run env var).
        policy_path = env.get("INVENTORY_POLICY_PATH", "").strip()
        policy_json = env.get("INVENTORY_POLICY_JSON", "").strip()
        if bool(policy_path) == bool(policy_json):
            raise ConfigError("Set exactly one of INVENTORY_POLICY_PATH or INVENTORY_POLICY_JSON.")

        raw_port = env.get("PORT", "8000")
        try:
            port = int(raw_port)
        except ValueError:
            raise ConfigError(f"PORT must be an integer; got {raw_port!r}") from None

        return cls(
            auth_mode=auth_mode,
            policy_path=Path(policy_path) if policy_path else None,
            policy_json=policy_json or None,
            db_path=Path(env.get("INVENTORY_DB_PATH", "var/restock_requests.db")),
            host=host,
            port=port,
            allowed_hosts=allowed_hosts,
            log_level=env.get("INVENTORY_LOG_LEVEL", "INFO").upper(),
            oidc_audience=oidc_audience,
        )
