"""Control transfer between the automation and a human operator.

The seam
--------
There is exactly one live surface per run. Who may act on it at any moment is
decided by a single piece of state -- :class:`ControlState` -- held by the
:class:`SessionBroker`. Automation and operator both go through the broker, and
the broker refuses any action from whichever side does not currently hold
control. There is no second session, no replayed context, no "reconnect": the
human drives the same browser the agent was driving, with the same cookies, the
same half-filled form and the same server-side session.

Lifecycle
---------
    AUTOMATION ──raise()──► PENDING_HANDOFF ──claim()──► HUMAN
         ▲                        │                        │
         └──────release(resume)───┴────────────────────────┘
                       release(abort) ──► RELEASED

While ``PENDING_HANDOFF`` or ``HUMAN``, the automation task is parked on an
``asyncio.Event``; it holds its place in the step list and its accumulated
outputs, so ``resume`` continues the run rather than restarting it. Everything
the human does is appended to the same trace with ``actor="human"``, because a
run that a person finished by hand is still a run someone will have to audit.

Escalation is raised from three places, deliberately:
  * the discovery loop, when the model emits ``escalate`` or gets stuck;
  * the replay engine, when it hits a condition it has no rule for;
  * the policy engine, when a step is classified irreversible and needs approval.

The third is an approval request rather than a handoff -- same queue, same
console, different disposition -- because "a human must decide" and "a human
must do it" are different problems and conflating them makes both worse.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..agent.trace import ScreenState

InterventionKind = Literal["stuck", "approval", "error", "manual"]
Disposition = Literal["resume", "abort", "approved", "rejected"]


class ControlState(str, Enum):
    AUTOMATION = "automation"
    PENDING_HANDOFF = "pending_handoff"
    HUMAN = "human"
    RELEASED = "released"


class HumanAction(BaseModel):
    at: float = Field(default_factory=time.time)
    kind: str
    detail: str = ""


class InterventionRequest(BaseModel):
    id: str = Field(default_factory=lambda: "int_" + uuid.uuid4().hex[:10])
    created_at: float = Field(default_factory=time.time)
    kind: InterventionKind = "stuck"
    run_id: str = ""
    capability: str = ""
    goal: str = ""
    step_id: str = ""
    step_index: int = 0
    reason: str = ""
    #: Everything the operator needs to act without reading the source.
    context: dict[str, Any] = Field(default_factory=dict)
    screen: ScreenState | None = None
    frame_path: str | None = None

    status: Literal["open", "claimed", "resolved"] = "open"
    operator: str | None = None
    claimed_at: float | None = None
    resolved_at: float | None = None
    disposition: Disposition | None = None
    note: str = ""
    human_actions: list[HumanAction] = Field(default_factory=list)

    def summary(self) -> str:
        return f"[{self.kind}] {self.capability or self.goal[:48]} @ step {self.step_index} ({self.step_id}): {self.reason}"


class HandoffResult(BaseModel):
    disposition: Disposition
    note: str = ""
    operator: str | None = None
    human_actions: list[HumanAction] = Field(default_factory=list)
    waited_s: float = 0.0

    @property
    def resume(self) -> bool:
        return self.disposition in ("resume", "approved")


class ControlDenied(RuntimeError):
    """An actor tried to touch the surface while the other side held control."""


class SessionBroker:
    """Owns control of one live session and the queue of intervention requests."""

    def __init__(self, *, auto_timeout_s: float = 900.0) -> None:
        self.state = ControlState.AUTOMATION
        self.auto_timeout_s = auto_timeout_s
        self.requests: dict[str, InterventionRequest] = {}
        self.order: list[str] = []
        self._resolved: dict[str, asyncio.Event] = {}
        self._results: dict[str, HandoffResult] = {}
        self.surface = None  # set by the runner; the live surface under transfer
        self.run_id: str = ""
        self.listeners: list[asyncio.Queue] = []
        #: Set once an operator console is actually serving. Without one there is
        #: nobody who *can* answer an intervention, and a run that parks silently
        #: for the full timeout looks like a hang rather than a design decision.
        self.console_url: str | None = None

    # -- introspection --------------------------------------------------------
    def open_requests(self) -> list[InterventionRequest]:
        return [self.requests[i] for i in self.order if self.requests[i].status != "resolved"]

    def current(self) -> InterventionRequest | None:
        for req in self.open_requests():
            if req.status == "claimed":
                return req
        return next(iter(self.open_requests()), None)

    def snapshot(self) -> dict[str, Any]:
        return {
            "control": self.state.value,
            "run_id": self.run_id,
            "requests": [r.model_dump(mode="json") for r in [self.requests[i] for i in self.order]],
            "current": (self.current().id if self.current() else None),
        }

    def _notify(self) -> None:
        for queue in list(self.listeners):
            try:
                queue.put_nowait(self.snapshot())
            except Exception:
                pass

    # -- automation side ------------------------------------------------------
    async def raise_intervention(
        self, request: InterventionRequest, *, timeout_s: float | None = None
    ) -> HandoffResult:
        """Park the automation and wait for a human. Called from the run loop."""
        self.requests[request.id] = request
        self.order.append(request.id)
        self._resolved[request.id] = asyncio.Event()
        self.state = ControlState.PENDING_HANDOFF
        self._notify()

        started = time.time()
        budget = timeout_s if timeout_s is not None else self.auto_timeout_s
        try:
            await asyncio.wait_for(self._resolved[request.id].wait(), timeout=budget)
        except asyncio.TimeoutError:
            request.status = "resolved"
            request.resolved_at = time.time()
            request.disposition = "abort"
            request.note = f"no operator responded within {budget:.0f}s"
            self.state = ControlState.RELEASED
            self._notify()
            return HandoffResult(
                disposition="abort", note=request.note, waited_s=round(time.time() - started, 1)
            )

        result = self._results[request.id]
        result.waited_s = round(time.time() - started, 1)
        self.state = ControlState.AUTOMATION if result.resume else ControlState.RELEASED
        self._notify()
        return result

    def assert_automation_may_act(self) -> None:
        if self.state is not ControlState.AUTOMATION:
            raise ControlDenied(f"automation does not hold control (state={self.state.value})")

    # -- operator side --------------------------------------------------------
    def claim(self, request_id: str, operator: str) -> InterventionRequest:
        req = self.requests.get(request_id)
        if req is None:
            raise KeyError(request_id)
        if req.status == "resolved":
            raise ControlDenied("request already resolved")
        req.status = "claimed"
        req.operator = operator
        req.claimed_at = time.time()
        self.state = ControlState.HUMAN
        self._notify()
        return req

    def assert_human_may_act(self) -> InterventionRequest:
        if self.state is not ControlState.HUMAN:
            raise ControlDenied(
                f"operator does not hold control (state={self.state.value}); claim a request first"
            )
        req = self.current()
        if req is None:
            raise ControlDenied("no claimed request")
        return req

    def record_human_action(self, kind: str, detail: str = "") -> None:
        req = self.assert_human_may_act()
        req.human_actions.append(HumanAction(kind=kind, detail=detail))
        self._notify()

    def release(self, request_id: str, disposition: Disposition, note: str = "") -> HandoffResult:
        req = self.requests.get(request_id)
        if req is None:
            raise KeyError(request_id)
        req.status = "resolved"
        req.resolved_at = time.time()
        req.disposition = disposition
        req.note = note
        result = HandoffResult(
            disposition=disposition,
            note=note,
            operator=req.operator,
            human_actions=list(req.human_actions),
        )
        self._results[req.id] = result
        event = self._resolved.get(req.id)
        if event is not None:
            event.set()
        else:  # released without anyone waiting (e.g. manual takeover)
            self.state = ControlState.AUTOMATION if result.resume else ControlState.RELEASED
        self._notify()
        return result
