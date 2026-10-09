"""Application-level authorization: explicit policy, deny by default.

The policy maps verified subjects to permissions. Nothing a caller sends can
add permissions: the only input is the subject established by authentication.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from inventory_mcp.authentication import Principal
from inventory_mcp.config import ConfigError


class Permission(StrEnum):
    INVENTORY_READ = "inventory.read"
    RESTOCK_CREATE = "restock.create"


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str


class Policy:
    def __init__(self, grants: Mapping[str, frozenset[Permission]]) -> None:
        self._grants = dict(grants)

    @classmethod
    def from_json(cls, raw: str) -> Policy:
        try:
            data = json.loads(raw)
            entries = data["principals"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise ConfigError("policy must be JSON with a 'principals' list") from exc
        grants: dict[str, frozenset[Permission]] = {}
        for entry in entries:
            subject = entry.get("subject")
            if not isinstance(subject, str) or not subject:
                raise ConfigError("every policy entry needs a non-empty 'subject'")
            if subject in grants:
                raise ConfigError(f"duplicate policy subject: {subject}")
            try:
                grants[subject] = frozenset(Permission(p) for p in entry.get("permissions", []))
            except ValueError as exc:
                raise ConfigError(f"unknown permission for subject {subject}: {exc}") from exc
        return cls(grants)

    @classmethod
    def from_path(cls, path: Path) -> Policy:
        try:
            raw = path.read_text()
        except OSError as exc:
            raise ConfigError(f"cannot read policy file {path}: {exc.strerror}") from exc
        return cls.from_json(raw)

    def permissions_for(self, subject: str) -> frozenset[Permission]:
        return self._grants.get(subject, frozenset())

    def authorize(self, principal: Principal | None, permission: Permission) -> Decision:
        if principal is None:
            return Decision(False, "no_authenticated_principal")
        if permission in self.permissions_for(principal.subject):
            return Decision(True, "granted")
        return Decision(False, "permission_not_granted")
