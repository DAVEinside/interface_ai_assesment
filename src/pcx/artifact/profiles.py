"""Application profiles: what is known about a vendor product, authored once.

A single discovery run only ever sees the happy path. It cannot tell you that
``MBR-404`` means "no such member" or that ``CSV-440`` means the session died --
and waiting to *discover* every error screen would mean deliberately breaking a
production system dozens of times per capability.

So error vocabulary is not per-capability knowledge; it is per-**product**
knowledge, declared once in a profile and attached to every capability compiled
against that product. Two consequences that matter at the stated scale:

* a new capability for CoreServ inherits every known outcome, interstitial and
  session-expiry rule for CoreServ on the day it is recorded;
* hundreds of tenants running the same vendor product share one profile, and a
  tenant that renames things supplies a :class:`~pcx.artifact.schema.TenantOverlay`
  rather than a forked profile.

Profiles are plain YAML, reviewable by someone who knows the application and has
never read this codebase.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from .schema import AppIdentity, Condition, InterstitialRule, Outcome, Recovery

DEFAULT_PROFILE_DIR = Path(__file__).resolve().parents[3] / "profiles"


class AppProfile(BaseModel):
    """Product-level knowledge shared by every capability recorded against it."""

    app: AppIdentity
    description: str = ""
    outcomes: list[Outcome] = Field(default_factory=list)
    interstitials: list[InterstitialRule] = Field(default_factory=list)
    transient: list[InterstitialRule] = Field(default_factory=list)
    session_expired: Condition | None = None
    hard_error: Condition | None = None
    reauth_capability: str | None = None
    #: Control-name regexes that are irreversible in this product, whatever a
    #: recording claims. Merged into the deployment policy at load time.
    irreversible_controls: list[str] = Field(default_factory=list)
    redact_patterns: list[str] = Field(default_factory=list)
    #: Text on this product's screens that varies between sessions, members or
    #: days, and must therefore never be used as a locator anchor. A recording
    #: cannot tell volatile text from furniture -- it sees each screen once --
    #: so this is declared per product, alongside the error vocabulary.
    volatile_text: list[str] = Field(default_factory=list)
    #: Extra text that must never be treated as page content, e.g. chrome.
    ignore_text: list[str] = Field(default_factory=list)

    def recovery(self) -> Recovery:
        return Recovery(
            interstitials=list(self.interstitials),
            transient=list(self.transient),
            session_expired=self.session_expired,
            hard_error=self.hard_error,
            reauth_capability=self.reauth_capability,
        )

    @classmethod
    def load(cls, path: str | Path) -> "AppProfile":
        data: dict[str, Any] = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return cls.model_validate(data)

    @classmethod
    def find(cls, key: str, directory: Path | None = None) -> "AppProfile | None":
        """Look up a profile by ``vendor/product@version`` or by file stem."""
        directory = directory or DEFAULT_PROFILE_DIR
        if not directory.exists():
            return None
        for path in sorted(directory.glob("*.y*ml")):
            if path.stem == key:
                return cls.load(path)
            try:
                profile = cls.load(path)
            except Exception:
                continue
            if profile.app.key() == key.lower():
                return profile
        return None


def load_all(directory: Path | None = None) -> list[AppProfile]:
    directory = directory or DEFAULT_PROFILE_DIR
    if not directory.exists():
        return []
    profiles = []
    for path in sorted(directory.glob("*.y*ml")):
        try:
            profiles.append(AppProfile.load(path))
        except Exception:
            continue
    return profiles
