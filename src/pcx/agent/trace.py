"""The discovery trace: what the run did, in a form the compiler can read.

A trace is not a model transcript. It is the *observed* record: for each step,
the screen as perceived, the action taken, who took it, whether policy allowed
it, and what the screen became. The model's own words are kept only as a
one-line rationale, and the full exchanges live separately under
``evidence/<run>/llm/``.

Keeping these apart is what makes the capability artifact reviewable. The
compiler in :mod:`pcx.artifact.compile` reads the trace and never touches the
transcript, so nothing the model said can leak into a capability except through
a field that was explicitly modelled here.
"""

from __future__ import annotations

import time
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..surfaces.model import Action, ActionOutcome, Observation, UIElement

Actor = Literal["llm", "human", "system"]


class ScreenState(BaseModel):
    """A compact, storable projection of an observation."""

    route: str
    title: str
    signature: str
    text_digest: str = ""
    element_count: int = 0

    @classmethod
    def of(cls, obs: Observation, *, text_chars: int = 900) -> "ScreenState":
        return cls(
            route=obs.route,
            title=obs.title,
            signature=obs.signature(),
            text_digest=obs.text_digest[:text_chars],
            element_count=len(obs.elements),
        )


class TraceStep(BaseModel):
    index: int
    actor: Actor = "llm"
    thought: str = ""
    action: Action
    before: ScreenState
    after: ScreenState | None = None
    target: UIElement | None = Field(
        default=None, description="The element the action was applied to, as perceived."
    )
    context_elements: list[UIElement] = Field(
        default_factory=list,
        description="The full inventory at decision time. The compiler needs it to "
        "synthesize locators and to prove they are unambiguous.",
    )
    outcome: ActionOutcome | None = None
    policy: dict[str, Any] = Field(default_factory=dict)
    frame: str | None = None
    llm: dict[str, Any] = Field(default_factory=dict)
    duration_ms: int = 0

    def as_history_line(self) -> str:
        bits = f"{self.index}. [{self.actor}] {self.action.summary()}"
        if self.target is not None:
            bits += f"  -> {self.target.role.value} {self.target.name!r}"
        if self.outcome and not self.outcome.ok:
            bits += f"  !! {self.outcome.detail}"
        elif self.after is not None:
            bits += f"  => {self.after.title!r}"
        return bits


class RunTrace(BaseModel):
    run_id: str
    goal: str
    entrypoint: str
    surface_kind: str
    app: dict[str, str] = Field(default_factory=dict)
    parameters: dict[str, str] = Field(default_factory=dict)
    wanted_outputs: list[str] = Field(default_factory=list)
    started_at: float = Field(default_factory=time.time)
    finished_at: float | None = None
    steps: list[TraceStep] = Field(default_factory=list)
    status: Literal["running", "succeeded", "failed", "escalated", "aborted"] = "running"
    stop_reason: str = ""
    outputs: dict[str, Any] = Field(default_factory=dict)
    extraction_targets: dict[str, UIElement] = Field(
        default_factory=dict,
        description="output name -> the element it was read from. Becomes an extract step.",
    )
    model: str = ""
    usage: dict[str, Any] = Field(default_factory=dict)
    human_interventions: int = 0

    def add(self, step: TraceStep) -> TraceStep:
        self.steps.append(step)
        return step

    def history(self) -> list[str]:
        return [s.as_history_line() for s in self.steps]

    def duration_s(self) -> float:
        return round((self.finished_at or time.time()) - self.started_at, 2)

    def action_sequence(self) -> list[str]:
        """The canonical shape of the run, used for stability comparisons."""
        out = []
        for s in self.steps:
            if s.action.kind in ("wait",):
                continue
            target = ""
            if s.target is not None:
                target = f"{s.target.role.value}:{s.target.name or '?'}"
            out.append(f"{s.action.kind}/{target}")
        return out

    def redacted_dump(self) -> dict[str, Any]:
        """Model dump with the heavy per-step inventories dropped."""
        data = self.model_dump(mode="json")
        for step in data.get("steps", []):
            step.pop("context_elements", None)
        return data
