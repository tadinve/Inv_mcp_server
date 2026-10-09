"""The verify script's helpers must actually detect misconfiguration and leaked tokens."""

from __future__ import annotations

import base64
import copy
import json
import subprocess
import sys

import pytest

from tests.conftest import REPO_ROOT

TOOLS = REPO_ROOT / "deployment" / "tools"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


# A dummy-signed JWT built at runtime (not a literal) so secret scanners only flag real tokens.
FAKE_JWT = ".".join([_b64url(b'{"alg":"RS256"}'), _b64url(b'{"sub":"1234567890"}'), _b64url(b"signature-not-real")])
POLICY = json.dumps({"principals": [{"subject": "1", "permissions": []}, {"subject": "2", "permissions": []}]})
GOOD = {
    "metadata": {
        "labels": {"purpose": "mcp-inventory-demo", "data": "synthetic"},
        "annotations": {"run.googleapis.com/ingress": "all"},
    },
    "spec": {
        "template": {
            "metadata": {"annotations": {"autoscaling.knative.dev/maxScale": "1"}},
            "spec": {
                "containers": [
                    {
                        "env": [
                            {"name": "INVENTORY_AUTH_MODE", "value": "google"},
                            {"name": "INVENTORY_OIDC_AUDIENCE", "value": "https://x-1.us-central1.run.app"},
                            {"name": "INVENTORY_ALLOWED_HOSTS", "value": "x-1.us-central1.run.app"},
                            {"name": "INVENTORY_POLICY_JSON", "value": POLICY},
                        ]
                    }
                ]
            },
        }
    },
}


def _run(script: str, payload: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(TOOLS / script)], input=json.dumps(payload), capture_output=True, text=True, check=False
    )


def _env(doc: dict) -> list[dict]:
    return doc["spec"]["template"]["spec"]["containers"][0]["env"]


def test_check_service_passes_the_intended_configuration() -> None:
    result = _run("check_service.py", GOOD)
    assert result.returncode == 0, result.stdout
    assert "FAIL" not in result.stdout


def _mutations() -> list[tuple[str, dict]]:
    cases = []
    doc = copy.deepcopy(GOOD)
    doc["metadata"]["annotations"]["run.googleapis.com/invoker-iam-disabled"] = "true"
    cases.append(("invoker IAM check enabled", doc))
    doc = copy.deepcopy(GOOD)
    doc["spec"]["template"]["metadata"]["annotations"]["autoscaling.knative.dev/minScale"] = "1"
    cases.append(("min instances 0", doc))
    doc = copy.deepcopy(GOOD)
    doc["spec"]["template"]["metadata"]["annotations"]["autoscaling.knative.dev/maxScale"] = "3"
    cases.append(("max instances 1", doc))
    doc = copy.deepcopy(GOOD)
    _env(doc)[0]["value"] = "dev"
    cases.append(("auth mode google", doc))
    doc = copy.deepcopy(GOOD)
    _env(doc).append({"name": "INVENTORY_DEV_ALLOW_NON_LOOPBACK", "value": "true"})
    cases.append(("no dev override", doc))
    doc = copy.deepcopy(GOOD)
    del _env(doc)[2]
    cases.append(("allowed hosts set", doc))
    doc = copy.deepcopy(GOOD)
    doc["metadata"]["labels"]["data"] = "production"
    cases.append(("label data=synthetic", doc))
    return cases


@pytest.mark.parametrize(("check", "doc"), _mutations(), ids=[c for c, _ in _mutations()])
def test_check_service_detects_misconfiguration(check: str, doc: dict) -> None:
    result = _run("check_service.py", doc)
    assert result.returncode == 1
    assert f"[FAIL] {check}" in result.stdout


def test_summarize_logs_counts_events_and_finds_no_tokens() -> None:
    entries = [
        {"jsonPayload": {"event": "tool_call", "tool": "get_inventory", "outcome": "success",
                         "authorization": "allow"}},
        {"jsonPayload": {"event": "tool_call", "tool": "create_restock_request", "outcome": "denied",
                         "authorization": "deny", "error_category": "forbidden"}},
    ]  # fmt: skip
    result = _run("summarize_logs.py", entries)
    assert result.returncode == 0
    assert "forbidden" in result.stdout
    assert "JWT-like string: 0" in result.stdout


def test_summarize_logs_fails_when_a_token_is_logged() -> None:
    leaked = FAKE_JWT
    result = _run("summarize_logs.py", [{"textPayload": f"Authorization: Bearer {leaked}"}])
    assert result.returncode == 1
    assert "JWT-like string: 1" in result.stdout


# --- JUnit gate for the real-identity run -------------------------------------------------------

REQUIRED = REPO_ROOT / "deployment" / "required_integration_tests.txt"


def _junit(tmp_path, cases: list[tuple[str, str | None]]) -> str:
    body = "".join(
        f'<testcase classname="t" name="{name}">' + (f"<{tag}/>" if tag else "") + "</testcase>" for name, tag in cases
    )
    path = tmp_path / "junit.xml"
    path.write_text(f'<testsuites><testsuite name="pytest">{body}</testsuite></testsuites>')
    return str(path)


def _required() -> list[str]:
    return [line.strip() for line in REQUIRED.read_text().splitlines() if line.strip() and not line.startswith("#")]


def _gate(report: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(TOOLS / "check_junit.py"), report, str(REQUIRED)],
        capture_output=True, text=True, check=False,
    )  # fmt: skip


def test_required_list_matches_the_integration_test_module() -> None:
    import ast

    module = REPO_ROOT / "tests" / "integration" / "test_cloud_run.py"
    tree = ast.parse(module.read_text())
    defined = [
        n.name for n in tree.body
        if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and n.name.startswith("test_")
    ]  # fmt: skip
    assert _required() == defined


def test_gate_passes_when_every_required_test_passed(tmp_path) -> None:
    result = _gate(_junit(tmp_path, [(name, None) for name in _required()]))
    assert result.returncode == 0, result.stdout


@pytest.mark.parametrize("tag", ["skipped", "failure", "error"])
def test_gate_fails_on_any_skip_failure_or_error(tmp_path, tag: str) -> None:
    names = _required()
    cases = [(names[0] + "[reader]", tag)] + [(name, None) for name in names[1:]]
    result = _gate(_junit(tmp_path, cases))
    assert result.returncode == 1
    assert tag in result.stdout


def test_gate_fails_when_a_required_test_did_not_run(tmp_path) -> None:
    result = _gate(_junit(tmp_path, [(name, None) for name in _required()[1:]]))
    assert result.returncode == 1
    assert "required but did not run" in result.stdout


def test_gate_fails_when_nothing_was_collected(tmp_path) -> None:
    result = _gate(_junit(tmp_path, []))
    assert result.returncode == 1
    assert "no integration tests were collected" in result.stdout


def test_gate_fails_without_a_report(tmp_path) -> None:
    result = _gate(str(tmp_path / "missing.xml"))
    assert result.returncode == 1


# --- log verification requirements ---------------------------------------------------------------


def _logs(entries: list[dict], *flags: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(TOOLS / "summarize_logs.py"), *flags],
        input=json.dumps(entries), capture_output=True, text=True, check=False,
    )  # fmt: skip


def test_log_check_fails_on_empty_logs_when_tool_calls_required() -> None:
    assert _logs([], "--require-tool-calls").returncode == 1


def test_log_check_fails_without_a_denial_when_required() -> None:
    allow = {"jsonPayload": {"event": "tool_call", "tool": "get_inventory", "outcome": "success",
                             "authorization": "allow"}}  # fmt: skip
    assert _logs([allow], "--require-tool-calls", "--require-denial").returncode == 1
    deny = {"jsonPayload": {"event": "tool_call", "tool": "create_restock_request", "outcome": "denied",
                            "authorization": "deny", "error_category": "forbidden"}}  # fmt: skip
    assert _logs([allow, deny], "--require-tool-calls", "--require-denial").returncode == 0


def test_log_check_scans_platform_entries_for_tokens() -> None:
    leaked = FAKE_JWT
    platform = {"httpRequest": {"status": 200}, "textPayload": f"Bearer {leaked}"}
    assert _logs([platform]).returncode == 1
