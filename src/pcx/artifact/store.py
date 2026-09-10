"""Versioned, reviewable capability storage.

Layout::

    capabilities/
      read_member_savings_balance/
        v1.yaml            the artifact, as YAML, meant to be read in a diff
        v2.yaml
        HEAD               the version currently eligible for invocation
        ledger.jsonl       one line per run: the evidence promotion depends on
      tenants/
        meridian_cu.yaml   TenantOverlay

YAML rather than JSON because these are reviewed by people, and a capability
diff should be legible in a pull request. Versions are immutable: a change to
the executable body writes ``v(N+1)`` rather than editing ``vN`` in place, so
the run ledger can always be tied to the exact bytes that ran.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

import yaml

from .schema import Capability, TenantOverlay


def prune(value: Any) -> Any:
    """Drop empty containers and nulls from a dump.

    Purely for reviewability: an artifact that prints ``all_of: []`` on every
    condition buries the three lines that matter under forty that do not, and a
    reviewer who stops reading is a guardrail that stopped working.
    """
    if isinstance(value, dict):
        cleaned = {k: prune(v) for k, v in value.items()}
        return {k: v for k, v in cleaned.items() if v not in (None, [], {}, ())}
    if isinstance(value, list):
        return [prune(v) for v in value]
    return value


class CapabilityStore:
    def __init__(self, root: str | Path = "capabilities") -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "tenants").mkdir(exist_ok=True)

    # -- paths ----------------------------------------------------------------
    def _dir(self, capability_id: str) -> Path:
        path = self.root / capability_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def versions(self, capability_id: str) -> list[int]:
        directory = self.root / capability_id
        if not directory.exists():
            return []
        found = []
        for path in directory.glob("v*.yaml"):
            try:
                found.append(int(path.stem[1:]))
            except ValueError:
                continue
        return sorted(found)

    def ids(self) -> list[str]:
        return sorted(
            p.name
            for p in self.root.iterdir()
            if p.is_dir() and p.name != "tenants" and any(p.glob("v*.yaml"))
        )

    # -- read / write ---------------------------------------------------------
    def save(self, capability: Capability, *, bump_if_changed: bool = True) -> Path:
        """Write the capability. Bumps the version when the executable body changed."""
        existing = self.versions(capability.id)
        if existing and bump_if_changed:
            head = self.load(capability.id, existing[-1])
            if head.digest() == capability.digest():
                capability.version = head.version  # same body: overwrite in place
            else:
                capability.version = existing[-1] + 1
        elif existing:
            capability.version = existing[-1]

        path = self._dir(capability.id) / f"v{capability.version}.yaml"
        payload = prune(capability.model_dump(mode="json", exclude_none=True))
        payload["_digest"] = capability.digest()
        path.write_text(
            yaml.safe_dump(payload, sort_keys=False, width=100, allow_unicode=True),
            encoding="utf-8",
        )
        (self._dir(capability.id) / "HEAD").write_text(str(capability.version), encoding="utf-8")
        return path

    def load(self, capability_id: str, version: int | None = None) -> Capability:
        if version is None:
            head = self._dir(capability_id) / "HEAD"
            versions = self.versions(capability_id)
            if not versions:
                raise FileNotFoundError(f"no capability {capability_id!r} in {self.root}")
            version = int(head.read_text().strip()) if head.exists() else versions[-1]
        path = self.root / capability_id / f"v{version}.yaml"
        if not path.exists():
            raise FileNotFoundError(path)
        data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        data.pop("_digest", None)
        return Capability.model_validate(data)

    def update(self, capability: Capability) -> Path:
        """Persist non-executable changes (evidence, status) without a version bump."""
        path = self.root / capability.id / f"v{capability.version}.yaml"
        payload = prune(capability.model_dump(mode="json", exclude_none=True))
        payload["_digest"] = capability.digest()
        path.write_text(
            yaml.safe_dump(payload, sort_keys=False, width=100, allow_unicode=True), encoding="utf-8"
        )
        return path

    # -- tenants --------------------------------------------------------------
    def save_tenant(self, overlay: TenantOverlay) -> Path:
        path = self.root / "tenants" / f"{overlay.tenant_id}.yaml"
        path.write_text(
            yaml.safe_dump(overlay.model_dump(mode="json", exclude_none=True), sort_keys=False),
            encoding="utf-8",
        )
        return path

    def load_tenant(self, tenant_id: str) -> TenantOverlay:
        path = self.root / "tenants" / f"{tenant_id}.yaml"
        if not path.exists():
            raise FileNotFoundError(path)
        return TenantOverlay.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))

    def tenants(self) -> list[str]:
        return sorted(p.stem for p in (self.root / "tenants").glob("*.yaml"))

    # -- ledger ---------------------------------------------------------------
    def ledger_path(self, capability_id: str) -> Path:
        return self._dir(capability_id) / "ledger.jsonl"

    def append_ledger(self, capability_id: str, record: dict[str, Any]) -> None:
        with self.ledger_path(capability_id).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")

    def read_ledger(self, capability_id: str) -> Iterator[dict[str, Any]]:
        path = self.ledger_path(capability_id)
        if not path.exists():
            return iter(())
        def _iter():
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        yield json.loads(line)
        return _iter()
