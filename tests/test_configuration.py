"""Fail-closed startup configuration (SPEC-AMENDMENT-1, A1.3 and A1.4)."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from inventory_mcp.config import AuthMode, ConfigError, Settings
from tests.conftest import DEV_POLICY, REPO_ROOT

AUDIENCE = "https://cresenta-inventory-123456789012.us-central1.run.app"
DEV = {"INVENTORY_AUTH_MODE": "dev", "INVENTORY_POLICY_PATH": str(DEV_POLICY)}
GOOGLE = {"INVENTORY_AUTH_MODE": "google", "INVENTORY_POLICY_PATH": "policy.json", "INVENTORY_OIDC_AUDIENCE": AUDIENCE}


@pytest.mark.parametrize("marker", ["K_SERVICE", "K_REVISION", "K_CONFIGURATION"])
def test_dev_auth_refused_when_cloud_run_marker_present(marker: str) -> None:
    with pytest.raises(ConfigError, match="refused on Cloud Run"):
        Settings.from_env({**DEV, marker: "cresenta-inventory"})


def test_cloud_run_marker_beats_the_non_loopback_override() -> None:
    env = {**DEV, "K_SERVICE": "x", "INVENTORY_HOST": "0.0.0.0", "INVENTORY_DEV_ALLOW_NON_LOOPBACK": "true"}
    with pytest.raises(ConfigError, match="refused on Cloud Run"):
        Settings.from_env(env)


def test_dev_auth_refused_on_non_loopback_bind() -> None:
    with pytest.raises(ConfigError, match="loopback"):
        Settings.from_env({**DEV, "INVENTORY_HOST": "0.0.0.0"})


def test_dev_auth_non_loopback_requires_explicit_override() -> None:
    settings = Settings.from_env({**DEV, "INVENTORY_HOST": "0.0.0.0", "INVENTORY_DEV_ALLOW_NON_LOOPBACK": "true"})
    assert settings.auth_mode is AuthMode.DEV


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_dev_auth_allowed_on_loopback(host: str) -> None:
    assert Settings.from_env({**DEV, "INVENTORY_HOST": host}).auth_mode is AuthMode.DEV


HOST = "cresenta-inventory-123456789012.us-central1.run.app"


def test_google_mode_is_allowed_on_cloud_run() -> None:
    settings = Settings.from_env(
        {**GOOGLE, "K_SERVICE": "cresenta-inventory", "INVENTORY_HOST": "0.0.0.0", "INVENTORY_ALLOWED_HOSTS": HOST}
    )
    assert settings.auth_mode is AuthMode.GOOGLE
    assert settings.oidc_audience == AUDIENCE
    assert settings.allowed_hosts == (HOST,)


@pytest.mark.parametrize(
    "extra",
    [{"INVENTORY_HOST": "0.0.0.0"}, {"K_SERVICE": "cresenta-inventory"}],
    ids=["non-loopback-bind", "cloud-run-marker"],
)
def test_google_mode_deployed_without_allowed_hosts_is_refused(extra: dict[str, str]) -> None:
    with pytest.raises(ConfigError, match="INVENTORY_ALLOWED_HOSTS"):
        Settings.from_env({**GOOGLE, **extra})


@pytest.mark.parametrize("hosts", ["*", "https://x.run.app", "x.run.app/mcp", "a b"])
def test_allowed_hosts_must_be_bare_hosts(hosts: str) -> None:
    with pytest.raises(ConfigError, match="bare host"):
        Settings.from_env({**GOOGLE, "INVENTORY_HOST": "0.0.0.0", "INVENTORY_ALLOWED_HOSTS": hosts})


def test_google_mode_on_loopback_does_not_require_allowed_hosts() -> None:
    assert Settings.from_env(GOOGLE).allowed_hosts == ()


def test_policy_may_come_from_inline_json() -> None:
    env = {k: v for k, v in DEV.items() if k != "INVENTORY_POLICY_PATH"}
    settings = Settings.from_env({**env, "INVENTORY_POLICY_JSON": '{"principals": []}'})
    assert settings.policy_path is None
    assert settings.policy_json == '{"principals": []}'


@pytest.mark.parametrize(
    "policy_env",
    [{}, {"INVENTORY_POLICY_PATH": "p.json", "INVENTORY_POLICY_JSON": '{"principals": []}'}],
    ids=["neither", "both"],
)
def test_exactly_one_policy_source_is_required(policy_env: dict[str, str]) -> None:
    with pytest.raises(ConfigError, match="exactly one"):
        Settings.from_env({"INVENTORY_AUTH_MODE": "dev", **policy_env})


@pytest.mark.parametrize(
    "audience",
    [
        "",
        "   ",
        "http://cresenta-inventory.run.app",
        "cresenta-inventory.run.app",
        "https://",
        "https://x.run.app?aud=other",
        "https://x.run.app#frag",
        "https://x.run.app extra",
    ],
)
def test_google_mode_requires_a_valid_https_audience(audience: str) -> None:
    with pytest.raises(ConfigError, match="INVENTORY_OIDC_AUDIENCE"):
        Settings.from_env({**GOOGLE, "INVENTORY_OIDC_AUDIENCE": audience})


def test_google_mode_without_audience_is_refused() -> None:
    env = {k: v for k, v in GOOGLE.items() if k != "INVENTORY_OIDC_AUDIENCE"}
    with pytest.raises(ConfigError):
        Settings.from_env(env)


def test_process_exits_nonzero_when_dev_auth_meets_cloud_run(tmp_path) -> None:
    """End to end: the real entry point refuses to start, before binding any port."""
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith(("INVENTORY_", "K_"))},
        **DEV,
        "INVENTORY_DB_PATH": str(tmp_path / "x.db"),
        "K_SERVICE": "cresenta-inventory",
        "PORT": "0",
    }
    proc = subprocess.run(
        [sys.executable, "-m", "inventory_mcp"], env=env, cwd=REPO_ROOT, capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 2
    assert "refusing to start" in proc.stdout
    assert "refused on Cloud Run" in proc.stdout
