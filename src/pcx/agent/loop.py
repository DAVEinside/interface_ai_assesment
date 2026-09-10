"""The discovery loop: observe -> decide -> act, with a model in the decision seat.

This is the expensive path, and it is meant to be. Its product is not the run --
it is the :class:`~pcx.agent.trace.RunTrace` that :mod:`pcx.artifact.compile`
turns into a capability, after which this loop never has to run for that flow
again.

Everything the model can do is bounded before it happens:

* only the action verbs in :mod:`pcx.agent.prompts` exist;
* every action is checked by the :class:`~pcx.policy.allowlist.PolicyEngine`
  against the surface's own reported route, before it reaches the surface;
* irreversible actions do not execute -- they raise an approval request;
* a step budget, a wall-clock budget and a no-progress detector all terminate
  the run;
* nothing sensitive reaches the trace or the log, because both go through the
  redactor.

Stopping conditions are explicit rather than emergent: the loop ends on
``finish``, ``escalate``, budget exhaustion, three consecutive surface errors,
or a stall (the screen signature has not changed for several steps and the model
keeps proposing the same action).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..artifact.schema import Step as ArtifactStep
from ..escalation.broker import InterventionRequest, SessionBroker
from ..evidence.recorder import EvidenceRecorder
from ..perception.annotate import annotate
from ..policy.allowlist import PolicyEngine
from ..policy.redact import Redactor
from ..surfaces.base import Surface
from ..surfaces.model import Action, Observation
from .llm import Decision, LLMClient, LLMError
from .prompts import SYSTEM, build_user_prompt
from .reporter import NullReporter, Reporter
from .trace import RunTrace, ScreenState, TraceStep


@dataclass
class DiscoveryRequest:
    """What an operator hands to the discovery loop."""

    goal: str
    entrypoint: str
    #: name -> sample value. Declared up front so the compiler can parameterize
    #: exactly, rather than guessing which literals in the recording were inputs.
    parameters: dict[str, str]
    wanted_outputs: list[str]
    capability_id: str
    max_steps: int = 24
    max_seconds: float = 600.0
    app: dict[str, str] | None = None


@dataclass
class DiscoveryResult:
    trace: RunTrace
    recorder: EvidenceRecorder


class DiscoveryLoop:
    def __init__(
        self,
        surface: Surface,
        llm: LLMClient,
        policy: PolicyEngine,
        recorder: EvidenceRecorder,
        redactor: Redactor,
        broker: SessionBroker | None = None,
        reporter: Reporter | None = None,
    ) -> None:
        self.surface = surface
        self.llm = llm
        self.policy = policy
        self.recorder = recorder
        self.redactor = redactor
        self.broker = broker
        #: Narrates the run as it happens. Silent by default -- see reporter.py.
        self.reporter = reporter or NullReporter()

    # -- helpers --------------------------------------------------------------
    async def _perceive(self, label: str) -> tuple[Observation, str | None]:
        obs = await self.surface.observe()
        frame_path = None
        try:
            png = annotate(await self.surface.screenshot_bytes(), obs.elements)
            frame_path = self.recorder.frame(png, label, force=True)
        except Exception as exc:  # evidence must never break the run
            self.recorder.event("frame_error", error=str(exc))
        if frame_path:
            obs.screenshot_path = str(self.recorder.root / frame_path)
        return obs, frame_path

    def _as_artifact_step(self, action: Action, index: int) -> ArtifactStep:
        """Project a live action into the schema type the policy engine checks."""
        kind = action.kind if action.kind in {
            "click", "type", "select", "press", "navigate", "wait", "extract",
        } else "assert"
        return ArtifactStep(
            id=f"d{index}",
            intent=action.rationale or action.kind,
            action=kind,  # type: ignore[arg-type]
            url=action.url,
            risk="safe",
        )

    # -- the loop -------------------------------------------------------------
    async def run(self, request: DiscoveryRequest) -> DiscoveryResult:
        trace = RunTrace(
            run_id=self.recorder.run_id,
            goal=request.goal,
            entrypoint=request.entrypoint,
            surface_kind=self.surface.kind,
            app=request.app or {},
            parameters=request.parameters,
            wanted_outputs=request.wanted_outputs,
            model=getattr(self.llm, "model", "") or self.llm.name,
        )
        for value in request.parameters.values():
            self.redactor.add_literal(value) if _looks_secret(value) else None

        self.recorder.event(
            "run_start",
            mode="discovery",
            goal=request.goal,
            entrypoint=request.entrypoint,
            parameters={k: _mask_param(k, v) for k, v in request.parameters.items()},
            wanted_outputs=request.wanted_outputs,
            model=trace.model,
            backend=self.llm.name,
        )

        self.reporter.run_start(
            request.goal, request.entrypoint, self.llm.name, getattr(self.llm, "model", "")
        )

        route_decision = self.policy.check_route(request.entrypoint)
        if not route_decision.allowed:
            trace.status = "aborted"
            trace.stop_reason = f"entrypoint refused by policy: {route_decision.detail}"
            self.recorder.event("policy_denied", **route_decision.as_dict())
            trace.finished_at = time.time()
            return DiscoveryResult(trace, self.recorder)

        await self.surface.open(request.entrypoint)

        deadline = time.time() + request.max_seconds
        consecutive_errors = 0
        last_error: str | None = None
        recent_signatures: list[str] = []
        exchange_no = 0

        for index in range(1, request.max_steps + 1):
            if time.time() > deadline:
                trace.status = "failed"
                trace.stop_reason = f"wall-clock budget of {request.max_seconds}s exhausted"
                break

            budget = self.policy.check_budget(index - 1)
            if not budget.allowed:
                trace.status = "failed"
                trace.stop_reason = budget.detail
                self.recorder.event("policy_denied", **budget.as_dict())
                break

            obs, frame_path = await self._perceive(f"step{index:02d}-observe")
            before = ScreenState.of(obs)
            recent_signatures.append(before.signature)
            self.recorder.event(
                "observe",
                step=index,
                route=obs.route,
                title=obs.title,
                signature=before.signature,
                elements=len(obs.elements),
                frame=frame_path,
            )
            self.reporter.observe(index, obs, frame_path)

            prompt = build_user_prompt(
                goal=request.goal,
                obs=obs,
                history=trace.history(),
                parameters=request.parameters,
                wanted_outputs=request.wanted_outputs,
                steps_left=request.max_steps - index + 1,
                last_error=last_error,
            )
            started = time.monotonic()
            try:
                decision: Decision = await self.llm.decide(
                    SYSTEM, prompt, image_path=obs.screenshot_path
                )
            except LLMError as exc:
                consecutive_errors += 1
                last_error = f"model backend error: {exc}"
                self.recorder.event("llm_error", step=index, error=str(exc))
                self.reporter.note(f"model backend error: {exc}", "error")
                if getattr(exc, "fatal", False):
                    trace.status = "failed"
                    trace.stop_reason = last_error
                    break
                if consecutive_errors >= 3:
                    trace.status = "failed"
                    trace.stop_reason = last_error
                    break
                continue

            exchange_no += 1
            self.recorder.llm_exchange(
                exchange_no,
                {"system": SYSTEM, "user": prompt, "image": obs.screenshot_path},
                {"thought": decision.thought, "action": decision.action, "usage": decision.usage},
            )
            _accumulate_usage(trace.usage, decision.usage)

            action = _to_action(decision)
            self.reporter.think(
                index, decision.thought, int((time.monotonic() - started) * 1000), decision.usage
            )
            self.recorder.event(
                "decide",
                step=index,
                thought=decision.thought,
                action=action.model_dump(exclude_none=True),
                latency_ms=int((time.monotonic() - started) * 1000),
                usage=decision.usage,
            )

            # -- terminal verbs ------------------------------------------------
            if action.kind == "finish":
                self.reporter.act(index, action, None, True, "")
                self.reporter.note(f"goal satisfied; bound outputs: {list(trace.outputs)}", "good")
                trace.status = "succeeded"
                trace.stop_reason = "model reported the goal satisfied"
                declared = action.outputs or {}
                for name in list(declared):
                    if name not in trace.outputs:
                        # A value the model asserted but never extracted is not
                        # trustworthy and is not allowed into the capability.
                        self.recorder.event(
                            "output_rejected",
                            name=name,
                            why="declared in finish but never bound by an extract step",
                        )
                trace.add(
                    TraceStep(
                        index=index,
                        thought=decision.thought,
                        action=action,
                        before=before,
                        after=before,
                        llm={"model": decision.model},
                    )
                )
                break

            if action.kind == "escalate":
                self.reporter.note(f"escalating: {action.reason}", "warn")
                trace.status = "escalated"
                trace.stop_reason = action.reason or "model escalated"
                await self._escalate(
                    trace, request, index, before, frame_path, action.reason or "model escalated"
                )
                break

            # -- policy --------------------------------------------------------
            element = obs.by_ref(action.target_ref) if action.target_ref else None
            if action.target_ref and element is None:
                last_error = f"element {action.target_ref!r} is not on this screen"
                consecutive_errors += 1
                self.recorder.event("bad_reference", step=index, ref=action.target_ref)
                if consecutive_errors >= 3:
                    trace.status = "failed"
                    trace.stop_reason = last_error
                    break
                continue

            artifact_step = self._as_artifact_step(action, index)
            decision_policy = self.policy.check_action(
                artifact_step,
                route=obs.route,
                control_name=(element.name if element else "") + " " + (element.value or "" if element else ""),
                sub_routes=await self.surface.routes(),
            )
            self.recorder.event("policy", step=index, **decision_policy.as_dict())
            if not decision_policy.allowed:
                self.reporter.note(
                    f"policy refused ({decision_policy.rule}): {decision_policy.detail}", "warn"
                )

            if not decision_policy.allowed:
                if decision_policy.requires_approval and self.broker is not None:
                    approved = await self._request_approval(
                        trace, request, index, before, frame_path, decision_policy.detail, action
                    )
                    if not approved:
                        trace.status = "escalated"
                        trace.stop_reason = "human declined an irreversible action"
                        break
                else:
                    last_error = f"policy refused this action: {decision_policy.detail}"
                    consecutive_errors += 1
                    if consecutive_errors >= 3:
                        trace.status = "failed"
                        trace.stop_reason = last_error
                        break
                    continue

            # -- act -----------------------------------------------------------
            if self.broker is not None:
                self.broker.assert_automation_may_act()
            outcome = await self.surface.act(action, element)
            after_obs = await self.surface.observe(screenshot=False)
            after = ScreenState.of(after_obs)

            step = trace.add(
                TraceStep(
                    index=index,
                    thought=decision.thought,
                    action=action,
                    before=before,
                    after=after,
                    target=element,
                    context_elements=obs.elements,
                    outcome=outcome,
                    policy=decision_policy.as_dict(),
                    frame=frame_path,
                    llm={"model": decision.model, "usage": decision.usage},
                    duration_ms=outcome.duration_ms,
                )
            )
            self.reporter.act(index, action, element, outcome.ok, outcome.detail)
            self.recorder.event(
                "act",
                step=index,
                action=action.summary(),
                target=element.label() if element else None,
                ok=outcome.ok,
                detail=outcome.detail,
                navigated=outcome.navigated,
                after_signature=after.signature,
            )

            if action.kind == "extract" and outcome.ok and action.bind:
                trace.outputs[action.bind] = outcome.extracted
                if element is not None:
                    trace.extraction_targets[action.bind] = element
                self.recorder.event(
                    "extract", step=index, bind=action.bind, value=outcome.extracted
                )
                self.reporter.note(
                    f"captured {action.bind} = {self.redactor.scrub(str(outcome.extracted))!r}", "good"
                )

            if not outcome.ok:
                consecutive_errors += 1
                last_error = outcome.detail
                if consecutive_errors >= 3:
                    trace.status = "failed"
                    trace.stop_reason = f"three consecutive surface errors; last: {outcome.detail}"
                    self.recorder.snapshot(await self.surface.raw_snapshot(), "surface-error")
                    break
            else:
                consecutive_errors = 0
                last_error = None

            if _stalled(recent_signatures, trace):
                trace.status = "failed"
                trace.stop_reason = "no progress: the screen stopped changing"
                self.recorder.snapshot(await self.surface.raw_snapshot(), "stalled")
                break
        else:
            trace.status = "failed"
            trace.stop_reason = f"step budget of {request.max_steps} exhausted"

        trace.finished_at = time.time()
        if trace.status != "succeeded":
            self.recorder.flush_deferred_frames()
        self.reporter.run_end(
            trace.status, trace.stop_reason, len(trace.steps), trace.usage, trace.duration_s()
        )
        self.recorder.event(
            "run_end",
            status=trace.status,
            stop_reason=trace.stop_reason,
            steps=len(trace.steps),
            outputs=list(trace.outputs),
            duration_s=trace.duration_s(),
            usage=trace.usage,
        )
        return DiscoveryResult(trace, self.recorder)

    # -- escalation -----------------------------------------------------------
    async def _escalate(
        self,
        trace: RunTrace,
        request: DiscoveryRequest,
        index: int,
        before: ScreenState,
        frame_path: str | None,
        reason: str,
    ) -> None:
        if self.broker is None:
            self.recorder.event("escalation_unavailable", step=index, reason=reason)
            return
        intervention = InterventionRequest(
            kind="stuck",
            run_id=trace.run_id,
            capability=request.capability_id,
            goal=request.goal,
            step_id=f"d{index}",
            step_index=index,
            reason=reason,
            screen=before,
            frame_path=frame_path,
            context={
                "mode": "discovery",
                "history": trace.history()[-6:],
                "outputs_so_far": list(trace.outputs),
            },
        )
        self.recorder.event("escalation_raised", **intervention.model_dump(mode="json", exclude={"screen"}))
        result = await self.broker.raise_intervention(intervention)
        trace.human_interventions += 1
        for act in result.human_actions:
            trace.add(
                TraceStep(
                    index=index,
                    actor="human",
                    thought=f"operator {result.operator}",
                    action=Action(kind="wait", rationale=f"{act.kind}: {act.detail}"),
                    before=before,
                )
            )
        self.recorder.event(
            "escalation_resolved",
            disposition=result.disposition,
            operator=result.operator,
            note=result.note,
            human_actions=[a.model_dump() for a in result.human_actions],
            waited_s=result.waited_s,
        )
        if result.resume:
            trace.status = "succeeded" if result.disposition == "resume" else trace.status
            trace.stop_reason = f"completed after human handoff: {result.note}"

    async def _request_approval(
        self,
        trace: RunTrace,
        request: DiscoveryRequest,
        index: int,
        before: ScreenState,
        frame_path: str | None,
        detail: str,
        action: Action,
    ) -> bool:
        if self.broker is None:
            return False
        intervention = InterventionRequest(
            kind="approval",
            run_id=trace.run_id,
            capability=request.capability_id,
            goal=request.goal,
            step_id=f"d{index}",
            step_index=index,
            reason=detail,
            screen=before,
            frame_path=frame_path,
            context={"proposed_action": action.summary()},
        )
        self.recorder.event("approval_requested", step=index, detail=detail, action=action.summary())
        result = await self.broker.raise_intervention(intervention)
        trace.human_interventions += 1
        self.recorder.event(
            "approval_resolved", disposition=result.disposition, operator=result.operator, note=result.note
        )
        return result.disposition == "approved"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _to_action(decision: Decision) -> Action:
    raw = dict(decision.action)
    kind = raw.pop("kind", "wait")
    allowed = {"target_ref", "text", "keys", "url", "ms", "bind", "outputs", "reason"}
    payload: dict[str, Any] = {k: v for k, v in raw.items() if k in allowed}
    return Action(kind=kind, rationale=decision.thought, **payload)  # type: ignore[arg-type]


def _stalled(signatures: list[str], trace: RunTrace) -> bool:
    """Three identical screens in a row *and* a repeated action."""
    if len(signatures) < 4 or len(trace.steps) < 3:
        return False
    if len(set(signatures[-4:])) != 1:
        return False
    recent = [s.action.summary() for s in trace.steps[-3:]]
    return len(set(recent)) == 1


def _looks_secret(value: str) -> bool:
    return len(value) >= 6 and any(c.islower() for c in value) and any(c.isdigit() or not c.isalnum() for c in value)


def _mask_param(name: str, value: str) -> str:
    if any(word in name.lower() for word in ("password", "secret", "token", "pin")):
        return "[REDACTED]"
    return value


def _accumulate_usage(total: dict[str, Any], usage: dict[str, Any]) -> None:
    for key, value in (usage or {}).items():
        if isinstance(value, (int, float)):
            total[key] = round(total.get(key, 0) + value, 6)
