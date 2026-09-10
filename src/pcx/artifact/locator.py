"""How a recorded step finds its control again.

Design position
---------------
A locator is **a ranked list of independent hypotheses about what the control
is**, not a single path to where it was. Each strategy is evaluated against the
current observation; the first one that identifies exactly one element wins, the
result is checked against a ``verify`` guard, and the index of the strategy that
fired is reported back to the caller.

Three things follow from that shape, and they are the whole argument for it:

1. **Ordering encodes stability, not preference.** Strategies are written
   most-semantic first (what the control *is*) and most-positional last (where it
   *sits*). A capability that still resolves on strategy 0 is healthy; one that
   has started resolving on strategy 3 is telling you the surface moved under it.
2. **Fallback is observable.** ``Resolution.degraded`` is a first-class drift
   signal. The crystallization ledger consumes it, and a capability that
   degrades repeatedly gets demoted before it starts failing.
3. **Nothing here is web-specific.** Every strategy is expressed over role, name,
   text and geometry -- the four things a Windows UIA tree, a macOS AX tree and a
   screen-scraped terminal all provide. Porting a capability to a new surface
   does not mean rewriting its locators.

Deliberately absent: CSS, XPath, DOM ids. On the target application those exist
(``ctl00_wf_txt3``) and would in fact work today -- and would be worthless on the
native desktop apps in the same estate, and misleading the day the vendor's
control tree regenerates its ids.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, model_serializer

from ..surfaces.model import Observation, Rect, Role, UIElement, normalize_text

StrategyKind = Literal[
    "role_name",  # role + accessible name -- the most portable identity there is
    "label",  # a text label physically adjacent to the control (legacy forms)
    "row_cell",  # a cell picked out by its row anchor and column header (grids)
    "text",  # the element's own visible text (links, buttons)
    "anchor_offset",  # nth element of a role in a direction from an anchor
    "ordinal",  # nth element of a role within a region -- last resort
]

Direction = Literal["right", "below", "left", "above"]

#: Base confidence per strategy kind, before penalties. Used to decide whether a
#: resolution is trustworthy enough to act on and to rank ambiguous candidates.
_BASE_CONFIDENCE: dict[str, float] = {
    "role_name": 0.97,
    "label": 0.90,
    "row_cell": 0.88,
    "text": 0.85,
    "anchor_offset": 0.70,
    "ordinal": 0.50,
}


class Strategy(BaseModel):
    """One hypothesis about how to identify a control."""

    kind: StrategyKind
    role: Role | None = Field(default=None, description="Canonical role the target must have.")
    name: str | None = Field(default=None, description="Exact accessible name (normalized compare).")
    name_pattern: str | None = Field(default=None, description="Regex over the accessible name.")
    text: str | None = Field(default=None, description="Exact visible text of the element itself.")
    text_pattern: str | None = None

    # label / anchor_offset / row_cell
    anchor_text: str | None = Field(default=None, description="Visible text of the anchoring element.")
    anchor_role: Role | None = None
    direction: Direction = "right"
    max_distance: float = Field(default=420.0, description="Px budget from anchor to target.")
    rank: int = Field(default=0, description="0-based index among candidates in `direction`.")
    column_header: str | None = Field(
        default=None, description="row_cell: header text whose column the target sits under."
    )

    region: str | None = Field(default=None, description="Restrict to one frame/window/pane.")
    index: int = Field(default=0, description="ordinal: 0-based index in reading order.")

    note: str = Field(default="", description="Human-readable rationale, kept for reviewers.")

    def base_confidence(self) -> float:
        return _BASE_CONFIDENCE.get(self.kind, 0.4)

    @model_serializer(mode="wrap")
    def _serialize(self, handler):
        """Emit only the fields this strategy kind actually uses.

        A saved capability is a review artifact. Printing ``direction: right`` on
        an ordinal strategy is not neutral clutter -- it invites a reviewer to
        believe a field is doing something it is not.
        """
        data = handler(self)
        keep = _RELEVANT_FIELDS.get(self.kind)
        if keep is None:
            return data
        return {k: v for k, v in data.items() if k in keep and v not in (None, "")}


#: Which fields each strategy kind consults. Everything else is inert for it.
_RELEVANT_FIELDS: dict[str, set[str]] = {
    "role_name": {"kind", "role", "name", "name_pattern", "region", "note"},
    "text": {"kind", "role", "text", "text_pattern", "region", "note"},
    "label": {"kind", "role", "anchor_text", "anchor_role", "direction", "max_distance", "region", "note"},
    "anchor_offset": {
        "kind", "role", "anchor_text", "anchor_role", "direction", "max_distance", "rank", "region", "note",
    },
    "row_cell": {
        "kind", "role", "anchor_text", "anchor_role", "column_header", "text_pattern", "rank", "region", "note",
    },
    "ordinal": {"kind", "role", "region", "index", "note"},
}


class Verify(BaseModel):
    """Post-resolution guard. A resolved element that fails this is discarded.

    This is what stops a 'wrong but plausible' match from being clicked. On a
    banking surface, clicking the wrong control is materially worse than
    failing, so verification is mandatory and conservative.
    """

    role: Role | None = None
    name_pattern: str | None = None
    text_pattern: str | None = None
    editable: bool | None = None
    enabled: bool | None = True
    min_confidence: float = 0.45

    def check(self, element: UIElement, confidence: float) -> tuple[bool, str]:
        if self.role is not None and element.role is not self.role:
            return False, f"role {element.role.value} != expected {self.role.value}"
        if self.enabled is not None and element.enabled != self.enabled:
            return False, f"enabled={element.enabled}, expected {self.enabled}"
        if self.editable is not None and element.editable != self.editable:
            return False, f"editable={element.editable}, expected {self.editable}"
        if self.name_pattern and not re.search(self.name_pattern, element.name, re.I):
            return False, f"name {element.name!r} does not match /{self.name_pattern}/"
        if self.text_pattern and not re.search(self.text_pattern, element.text, re.I):
            return False, f"text {element.text!r} does not match /{self.text_pattern}/"
        if confidence < self.min_confidence:
            return False, f"confidence {confidence:.2f} < {self.min_confidence:.2f}"
        return True, "verified"


class Locator(BaseModel):
    """A ranked set of strategies plus the guard that validates the winner."""

    description: str = Field(default="", description="What a human calls this control.")
    strategies: list[Strategy] = Field(min_length=1)
    verify: Verify = Field(default_factory=Verify)
    on_ambiguity: Literal["fail", "best", "escalate"] = Field(
        default="fail",
        description=(
            "What to do when a strategy matches more than one element. 'fail' is "
            "the default because acting on the wrong control in a core banking "
            "system is not recoverable by retrying."
        ),
    )
    recorded: dict = Field(
        default_factory=dict,
        description=(
            "Provenance from the discovery run (rect, screen signature, resolved "
            "name). Never used to locate anything -- it exists so a human can see "
            "what the recorder was looking at."
        ),
    )


class Resolution(BaseModel):
    """Outcome of resolving one locator against one observation."""

    ok: bool
    ref: str | None = None
    strategy_index: int = -1
    strategy_kind: str = ""
    confidence: float = 0.0
    candidates: int = 0
    degraded: bool = Field(
        default=False,
        description="True when a non-primary strategy fired -- the drift signal.",
    )
    reason: str = ""

    def brief(self) -> str:
        if not self.ok:
            return f"unresolved ({self.reason})"
        flag = " DEGRADED" if self.degraded else ""
        return f"{self.ref} via {self.strategy_kind}[{self.strategy_index}] p={self.confidence:.2f}{flag}"


# --------------------------------------------------------------------------- #
# Matching primitives
# --------------------------------------------------------------------------- #


def _eq(a: str | None, b: str | None) -> bool:
    return normalize_text(a).casefold() == normalize_text(b).casefold()


def _matches_shape(element: UIElement, s: Strategy) -> bool:
    if s.region and element.region != s.region:
        return False
    if s.role is not None and element.role is not s.role:
        return False
    return True


def _find_anchor(obs: Observation, s: Strategy) -> UIElement | None:
    """Locate the anchoring element for label / row_cell / anchor_offset."""
    wanted = normalize_text(s.anchor_text or s.name)
    if not wanted:
        return None
    exact: list[UIElement] = []
    loose: list[UIElement] = []
    for e in obs.elements:
        if s.anchor_role is not None and e.role is not s.anchor_role:
            continue
        if s.region and e.region != s.region:
            continue
        name = normalize_text(e.text)
        if _eq(name, wanted):
            exact.append(e)
        elif wanted.casefold() in name.casefold() and len(name) < len(wanted) + 24:
            loose.append(e)
    pool = exact or loose
    if not pool:
        return None
    # Prefer the smallest box: on a layout table the tightest node is the label
    # cell itself rather than the row or table that also contains the text.
    return min(pool, key=lambda e: e.rect.area)


def _directional(anchor: Rect, candidate: Rect, direction: Direction) -> float | None:
    """Distance from anchor to candidate in ``direction``, or None if not in it."""
    if direction == "right":
        if candidate.x < anchor.x + anchor.w * 0.5:
            return None
        if anchor.vertical_overlap(candidate) < 0.4:
            return None
        return candidate.x - (anchor.x + anchor.w)
    if direction == "left":
        if candidate.x + candidate.w > anchor.x + anchor.w * 0.5:
            return None
        if anchor.vertical_overlap(candidate) < 0.4:
            return None
        return anchor.x - (candidate.x + candidate.w)
    if direction == "below":
        if candidate.y < anchor.y + anchor.h * 0.5:
            return None
        if anchor.horizontal_overlap(candidate) < 0.25:
            return None
        return candidate.y - (anchor.y + anchor.h)
    if direction == "above":
        if candidate.y + candidate.h > anchor.y + anchor.h * 0.5:
            return None
        if anchor.horizontal_overlap(candidate) < 0.25:
            return None
        return anchor.y - (candidate.y + candidate.h)
    return None


# --------------------------------------------------------------------------- #
# Strategy evaluation
# --------------------------------------------------------------------------- #


def _candidates_for(obs: Observation, s: Strategy) -> list[UIElement]:
    pool = [e for e in obs.elements if _matches_shape(e, s)]

    if s.kind == "role_name":
        if s.name is not None:
            return [e for e in pool if _eq(e.name, s.name)]
        if s.name_pattern:
            rx = re.compile(s.name_pattern, re.I)
            return [e for e in pool if rx.search(e.name)]
        return []

    if s.kind == "text":
        if s.text is not None:
            return [e for e in pool if _eq(e.text, s.text)]
        if s.text_pattern:
            rx = re.compile(s.text_pattern, re.I)
            return [e for e in pool if rx.search(e.text)]
        return []

    if s.kind in ("label", "anchor_offset"):
        anchor = _find_anchor(obs, s)
        if anchor is None:
            return []
        scored: list[tuple[float, UIElement]] = []
        for e in pool:
            if e.ref == anchor.ref:
                continue
            dist = _directional(anchor.rect, e.rect, s.direction)
            if dist is None or dist < -2 or dist > s.max_distance:
                continue
            scored.append((dist, e))
        scored.sort(key=lambda t: t[0])
        if s.kind == "label":
            # A label addresses the single nearest control in that direction.
            return [scored[0][1]] if scored else []
        return [scored[s.rank][1]] if len(scored) > s.rank else []

    if s.kind == "row_cell":
        anchor = _find_anchor(obs, s)
        if anchor is None:
            return []
        row = [
            e
            for e in pool
            if e.ref != anchor.ref and anchor.rect.vertical_overlap(e.rect) >= 0.5
        ]
        if s.column_header:
            header = _find_anchor(
                obs,
                Strategy(kind="text", anchor_text=s.column_header, region=s.region),
            )
            if header is not None:
                under = [
                    e for e in row if header.rect.horizontal_overlap(e.rect) >= 0.3
                ]
                if under:
                    return under[:1]
            if s.text_pattern is None:
                return []
        if s.text_pattern:
            rx = re.compile(s.text_pattern)
            hits = [e for e in row if rx.search(normalize_text(e.text))]
            return hits[:1] if hits else []
        right = [
            (e.rect.x, e) for e in row if e.rect.x > anchor.rect.x + anchor.rect.w * 0.5
        ]
        right.sort(key=lambda t: t[0])
        return [right[s.rank][1]] if len(right) > s.rank else []

    if s.kind == "ordinal":
        ordered = sorted(pool, key=lambda e: (round(e.rect.y / 6), e.rect.x))
        return [ordered[s.index]] if len(ordered) > s.index else []

    return []


def resolve(locator: Locator, obs: Observation) -> tuple[Resolution, UIElement | None]:
    """Resolve ``locator`` against ``obs``. Pure; no I/O, so it is trivially testable."""
    attempts: list[str] = []

    for idx, strategy in enumerate(locator.strategies):
        try:
            matches = _candidates_for(obs, strategy)
        except re.error as exc:
            attempts.append(f"[{idx}] {strategy.kind}: bad pattern ({exc})")
            continue

        if not matches:
            attempts.append(f"[{idx}] {strategy.kind}: 0 candidates")
            continue

        if len(matches) > 1 and locator.on_ambiguity == "fail":
            attempts.append(
                f"[{idx}] {strategy.kind}: ambiguous ({len(matches)} candidates: "
                + ", ".join(m.label() for m in matches[:4])
                + ")"
            )
            continue

        element = matches[0]
        confidence = strategy.base_confidence()
        if len(matches) > 1:
            confidence -= 0.15  # 'best' mode: acted on, but flagged
        if idx > 0:
            confidence -= 0.05 * idx  # later hypotheses are weaker evidence

        ok, why = locator.verify.check(element, confidence)
        if not ok:
            attempts.append(f"[{idx}] {strategy.kind}: verify failed ({why})")
            continue

        return (
            Resolution(
                ok=True,
                ref=element.ref,
                strategy_index=idx,
                strategy_kind=strategy.kind,
                confidence=round(confidence, 3),
                candidates=len(matches),
                degraded=idx > 0,
                reason=why,
            ),
            element,
        )

    return (
        Resolution(
            ok=False,
            reason="; ".join(attempts) or "no strategies defined",
            candidates=0,
        ),
        None,
    )
