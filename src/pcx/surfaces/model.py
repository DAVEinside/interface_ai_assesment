"""Surface-independent perception and action vocabulary.

Everything above this module -- the agent loop, the capability artifact, the
replay engine -- is written against these types and *only* these types. Nothing
here mentions a DOM, a CSS selector, an HTTP request or a Playwright object.
That is the seam: a new surface (legacy web, Windows UIA, terminal emulator)
is added by producing :class:`Observation` objects and consuming
:class:`Action` objects, and every recorded capability keeps working.
"""

from __future__ import annotations

import hashlib
import re
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field

# --------------------------------------------------------------------------- #
# Geometry
# --------------------------------------------------------------------------- #


class Rect(BaseModel):
    """Axis-aligned box in surface coordinates (CSS px for web, DIP for desktop)."""

    x: float
    y: float
    w: float
    h: float

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2

    @property
    def area(self) -> float:
        return max(0.0, self.w) * max(0.0, self.h)

    def contains_point(self, px: float, py: float) -> bool:
        return self.x <= px <= self.x + self.w and self.y <= py <= self.y + self.h

    def vertical_overlap(self, other: "Rect") -> float:
        """Fraction of the shorter box's height shared with ``other``.

        This is how "same row" is decided on surfaces that have no real table
        semantics -- which is most legacy surfaces.
        """
        top = max(self.y, other.y)
        bottom = min(self.y + self.h, other.y + other.h)
        shared = max(0.0, bottom - top)
        shortest = max(1e-6, min(self.h, other.h))
        return shared / shortest

    def horizontal_overlap(self, other: "Rect") -> float:
        left = max(self.x, other.x)
        right = min(self.x + self.w, other.x + other.w)
        shared = max(0.0, right - left)
        narrowest = max(1e-6, min(self.w, other.w))
        return shared / narrowest


# --------------------------------------------------------------------------- #
# Perception
# --------------------------------------------------------------------------- #


class Role(str, Enum):
    """Canonical control vocabulary.

    Deliberately small. Every surface adapter maps its native taxonomy (ARIA
    roles for web, ``UIA_ControlTypeId`` for Windows, curses widget kinds for a
    terminal) down to this set, so a capability recorded on one adapter is at
    least *describable* on another.
    """

    TEXTBOX = "textbox"
    BUTTON = "button"
    LINK = "link"
    COMBOBOX = "combobox"
    CHECKBOX = "checkbox"
    RADIO = "radio"
    CELL = "cell"
    TEXT = "text"
    HEADING = "heading"
    IMAGE = "image"
    OTHER = "other"


#: Roles a human could click or type into. Used to decide what the agent is
#: even allowed to target, and to build the screen signature.
INTERACTIVE_ROLES = {
    Role.TEXTBOX,
    Role.BUTTON,
    Role.LINK,
    Role.COMBOBOX,
    Role.CHECKBOX,
    Role.RADIO,
}


class UIElement(BaseModel):
    """One perceivable control or piece of on-screen text."""

    ref: str = Field(description="Stable only within one observation, e.g. 'e12'.")
    role: Role
    name: str = Field(default="", description="Accessible name; often EMPTY on legacy surfaces.")
    value: str | None = None
    rect: Rect
    region: str = Field(default="", description="Adapter-defined container, e.g. a frame name or window title.")
    enabled: bool = True
    focusable: bool = False
    editable: bool = False
    native: dict[str, Any] = Field(
        default_factory=dict,
        description="Adapter-private debug detail. Never consulted by the replay engine.",
    )

    @property
    def text(self) -> str:
        return self.value or self.name

    def label(self) -> str:
        return f"{self.ref} {self.role.value} {self.name!r}".strip()


def normalize_text(s: str | None) -> str:
    """Collapse the whitespace noise legacy markup is full of (&nbsp;, runs of spaces)."""
    if not s:
        return ""
    s = s.replace("\xa0", " ").replace("​", "")
    return re.sub(r"\s+", " ", s).strip()


class Observation(BaseModel):
    """A single perception of the surface: what a human would see, structured."""

    surface_kind: str
    route: str = Field(description="Normalized location: a URL path for web, a window/screen id elsewhere.")
    title: str = ""
    elements: list[UIElement] = Field(default_factory=list)
    text_digest: str = Field(default="", description="Visible text, whitespace-normalized and length-capped.")
    screenshot_path: str | None = None
    captured_at: float = 0.0

    def by_ref(self, ref: str) -> UIElement | None:
        for e in self.elements:
            if e.ref == ref:
                return e
        return None

    def interactive(self) -> list[UIElement]:
        return [e for e in self.elements if e.role in INTERACTIVE_ROLES and e.enabled]

    def signature(self) -> str:
        """Identity of the *screen*, not of the data on it.

        Built from the multiset of interactive controls plus headings, so
        ``member 10000001`` and ``member 10000002`` produce the same signature
        while ``member detail`` and ``not found`` do not. Replay uses this to
        answer "am I where I expect to be?" before acting, and the
        crystallization ledger uses changes in it as a drift signal.
        """
        parts = sorted(
            f"{e.role.value}:{normalize_text(e.name).upper()}"
            for e in self.elements
            if e.role in INTERACTIVE_ROLES or e.role is Role.HEADING
        )
        parts.append(f"title:{normalize_text(self.title).upper()}")
        return "sig_" + hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]

    def contains_text(self, needle: str) -> bool:
        return normalize_text(needle).upper() in normalize_text(self.text_digest).upper()


# --------------------------------------------------------------------------- #
# Action
# --------------------------------------------------------------------------- #

ActionKind = Literal[
    "click",
    "type",
    "press",
    "select",
    "navigate",
    "wait",
    "extract",
    "finish",
    "escalate",
]

#: Actions that cannot change server state on their own. The policy engine
#: treats everything else as a candidate for the risk classifier.
READ_ONLY_ACTIONS = {"wait", "extract", "finish", "escalate"}


class Action(BaseModel):
    """One step the agent or the replay engine wants performed.

    ``target_ref`` addresses an element in the *current* observation. Recorded
    capabilities never store a ref -- they store a :class:`~pcx.artifact.locator.Locator`
    and re-resolve it to a ref at replay time.
    """

    kind: ActionKind
    target_ref: str | None = None
    text: str | None = None
    keys: str | None = None
    url: str | None = None
    ms: int | None = None
    bind: str | None = Field(default=None, description="For 'extract': the output name to bind to.")
    rationale: str = Field(default="", description="Why the actor chose this. Recorded as evidence.")
    outputs: dict[str, Any] | None = Field(default=None, description="For 'finish'.")
    reason: str | None = Field(default=None, description="For 'escalate'.")

    def summary(self) -> str:
        bits = [self.kind]
        if self.target_ref:
            bits.append(f"@{self.target_ref}")
        if self.text is not None:
            bits.append(f"text={self.text!r}")
        if self.keys:
            bits.append(f"keys={self.keys}")
        if self.url:
            bits.append(f"url={self.url}")
        if self.bind:
            bits.append(f"->{self.bind}")
        return " ".join(bits)


class ActionOutcome(BaseModel):
    """What the surface reports back after attempting an action."""

    ok: bool
    detail: str = ""
    extracted: str | None = None
    duration_ms: int = 0
    navigated: bool = False
