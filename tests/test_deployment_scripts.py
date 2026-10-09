"""Static audit of the Cloud Run scripts: every mutating gcloud command must target only
resources these scripts own, and nothing project-wide or shared may be modified or deleted.

The A2A services, their service accounts and the a2a-demo Artifact Registry repository share
the project. These tests make accidental changes to them a test failure, not a code-review catch.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.conftest import REPO_ROOT

SCRIPTS = sorted([*REPO_ROOT.glob("deployment/*.sh"), *REPO_ROOT.glob("deployment/poc/*.sh")])
VERBS = {"delete", "deploy", "create", "add-iam-policy-binding", "remove-iam-policy-binding", "set-iam-policy",
         "update", "enable"}  # fmt: skip
OUR_SA_VARS = {"${sa}", "${RUNTIME_SA}", "${READER_SA}", "${WRITER_SA}", "${OUTSIDER_SA}", "${CALLER_SA}"}
OUR_SA_NAMES = {
    "mcp-inventory-runtime", "mcp-reader", "mcp-writer", "mcp-outsider",
    "mcp-poc-runtime", "mcp-poc-caller", "mcp-poc-outsider",
}  # fmt: skip


def _logical_lines(path: Path) -> list[str]:
    """Join backslash continuations; drop comments and heredoc bodies (plans and Python)."""
    lines, current, heredoc_end = [], "", None
    for raw in path.read_text().splitlines():
        if heredoc_end is not None:
            if raw.strip() == heredoc_end:
                heredoc_end = None
            continue
        match = re.search(r"<<-?'?(\w+)'?", raw)
        if match:
            heredoc_end = match.group(1)
        line = raw.split(" #", 1)[0] if not raw.lstrip().startswith("#") else ""
        if line.rstrip().endswith("\\"):
            current += line.rstrip()[:-1] + " "
            continue
        lines.append(current + line)
        current = ""
    return [line.strip() for line in lines if line.strip()]


def _gcloud_commands(path: Path) -> list[str]:
    """Every gcloud invocation, one per entry, split out of `||`/`&&`/`;`/`|` chains."""
    commands = []
    for line in _logical_lines(path):
        for segment in re.split(r"\|\||&&|;|\|", line):
            if "gcloud " in segment:
                commands.append("gcloud " + segment.split("gcloud ", 1)[1].strip())
    return commands


def _command_words(command: str) -> list[str]:
    """`gcloud run services delete "${SERVICE}" --x` -> ["run", "services", "delete"]."""
    words = []
    for token in command.split()[1:]:
        if token.startswith(("-", '"', "$")):
            break
        words.append(token)
    return words


def _mutations(path: Path) -> list[str]:
    return [c for c in _gcloud_commands(path) if VERBS & set(_command_words(c))]


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_no_project_wide_or_shared_resource_changes(script: Path) -> None:
    for line in _mutations(script):
        assert "projects add-iam-policy-binding" not in line and "projects set-iam-policy" not in line, line
        assert "repositories delete" not in line, line  # shared repos (cloud-run-source-deploy, a2a-demo)
        assert "docker images delete" not in line, line  # path-based deletes can over-match
        assert "--filter" not in line, line  # mutations name exact resources, never a query
        assert "a2a" not in line.lower(), line


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_every_mutation_targets_an_owned_resource(script: Path) -> None:
    for line in _mutations(script):
        command = line.split("gcloud ", 1)[1]
        if command.startswith("services enable"):
            continue  # enabling APIs is additive and idempotent
        if command.startswith(("run deploy", "run services delete", "run services add-iam-policy-binding")):
            assert '"${SERVICE}"' in command, line
        elif command.startswith("artifacts packages delete"):
            assert '"${SERVICE}"' in command and "--repository cloud-run-source-deploy" in command, line
        elif command.startswith(("iam service-accounts delete", "iam service-accounts add-iam-policy-binding")):
            target = command.split()[3]
            assert target in {f'"{v}"' for v in OUR_SA_VARS}, line
        elif command.startswith("iam service-accounts create"):
            assert command.split()[3] in {'"${sa}"'}, line
        else:
            pytest.fail(f"unreviewed mutating gcloud command: {line}")


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_service_account_loops_only_iterate_owned_accounts(script: Path) -> None:
    for line in _logical_lines(script):
        match = re.match(r"for sa in (.+?); do", line)
        if not match:
            continue
        for item in match.group(1).split():
            item = item.strip('"')
            assert item in OUR_SA_VARS or item in OUR_SA_NAMES, f"{script.name}: {line}"


@pytest.mark.parametrize(
    "script",
    [p for p in SCRIPTS if p.name in {"deploy_cloud_run.sh", "cleanup.sh", "deploy_poc.sh", "cleanup_poc.sh"}],
    ids=lambda p: p.name,
)
def test_ownership_guard_runs_after_confirmation_and_before_any_mutation(script: Path) -> None:
    lines = _logical_lines(script)
    confirm = next(i for i, line in enumerate(lines) if line.startswith("confirm "))
    guard = next(i for i, line in enumerate(lines) if line.startswith("assert_all_ours"))
    first_mutation = next(
        i for i, line in enumerate(lines) if "gcloud " in line and any(c in line for c in _mutations(script))
    )
    assert confirm < guard < first_mutation


def test_service_names_are_fixed_and_not_shared() -> None:
    for common in (REPO_ROOT / "deployment" / "common.sh", REPO_ROOT / "deployment" / "poc" / "common.sh"):
        name = re.search(r'^SERVICE="([a-z0-9-]+)"$', common.read_text(), re.MULTILINE).group(1)
        assert name in {"cresenta-inventory", "mcp-auth-poc"}
