"""Deterministic replay: the production execution path.

No model is consulted for any decision. Every branch the engine can take is one
of a fixed set, and which one it took is recorded:

  1. the step's locator resolved and the action succeeded            -> continue
  2. a declared **business outcome** matched the screen              -> return it
  3. a declared **recoverable** condition matched (interstitial,
     transient stall, expired session)                               -> handle, retry
  4. a declared **hard error** matched                               -> fail fast
  5. nothing matched and the step could not proceed                  -> fail, or
     escalate to a human if one is available

Order matters and is deliberate. Hard errors are checked before business
outcomes (an application crash on the not-found screen is still a crash);
business outcomes before recovery (a definite answer beats another retry); and
recovery before acting (never act on a screen you have not recognized).

The engine also enforces the guardrails on every step, re-checking the route the
surface reports rather than trusting where it thinks it is, and refusing
irreversible actions unless a human approved this specific run.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from ..agent.reporter import NullReporter, Reporter
from ..agent.trace import ScreenState
from ..artifact.locator import Resolution, resolve
from ..artifact.schema import PARSERS, Capability, Condition, Step
from ..escalation.broker import InterventionRequest, SessionBroker
from ..evidence.recorder import EvidenceRecorder
from ..perception.annotate import annotate
from ..policy.allowlist import PolicyEngine
from ..policy.redact import Redactor, mask_value
from ..surfaces.base import Surface
from ..surfaces.model import Action, Observation
from .outcomes import (
    APPLICATION_ERROR,
    APPROVAL_REQUIRED,
    CHECKPOINT_NOT_REACHED,
    ELEMENT_NOT_FOUND,
    INVALID_INPUT,
    POLICY_DENIED,
    SURFACE_ERROR,
    UNRECOGNIZED_SCREEN,
    FailureDetail,
    RecoveryEvent,
    ReplayResult,
    StepResult,
)


@dataclass
class ReplayOptions:
    escalate_on_failure: bool = False
    approve_irreversible: bool = False
    max_interstitials: int = 4
    max_transient_waits: int = 4
    transient_wait_ms: int = 1500
    allow_reauth: bool = True
    #: Type 2 only: consult the model to *interpret* an unrecognized screen.
    hybrid_classifier: Any = None


class ReplayEngine:
    def __init__(
        self,
        surface: Surface,
        policy: PolicyEngine,
        recorder: EvidenceRecorder,
        redactor: Redactor,
        *,
        broker: SessionBroker | None = None,
        options: ReplayOptions | None = None,
        capability_loader=None,
        reporter: Reporter | None = None,
    ) -> None:
        self.surface = surface
        self.policy = policy
        self.recorder = recorder
        self.redactor = redactor
        self.broker = broker
        self.options = options or ReplayOptions()
        #: Callable ``(capability_id) -> Capability``. Used for re-authentication,
        #: which is capability composition rather than a special case in here.
        self.capability_loader = capability_loader
        #: Where a parked run says so. Silence during a handoff is the difference
        #: between "waiting for you" and "hung", and only the caller knows whether
        #: anyone is watching a terminal.
        self.reporter = reporter or NullReporter()
        self._interstitials_seen = 0
        self._transient_waits = 0
        self._llm_calls = 0
        self._reauths = 0

    # -- perception helpers ---------------------------------------------------
    async def _observe(self, label: str, *, force_frame: bool = False) -> tuple[Observation, str | None]:
        obs = await self.surface.observe()
        frame = None
        try:
            png = annotate(await self.surface.screenshot_bytes(), obs.elements)
            frame = self.recorder.frame(png, label, force=force_frame)
        except Exception as exc:
            self.recorder.event("frame_error", error=str(exc))
        return obs, frame

    def _match_outcome(self, capability: Capability, obs: Observation):
        for outcome in capability.contract.outcomes:
            if outcome.code == "OK" or outcome.detect is None:
                continue
            hit, why = outcome.detect.evaluate(obs)
            if hit:
                return outcome, why
        return None, ""

    # -- the run --------------------------------------------------------------
    async def run(
        self,
        capability: Capability,
        raw_inputs: dict[str, Any],
        *,
        entrypoint_override: str | None = None,
    ) -> ReplayResult:
        started = time.time()
        # A public host over the internet is far slower than the localhost the
        # flow was recorded against. That is a property of *where* this runs, not
        # of what it does, so it scales the budget here rather than editing the
        # artifact -- see TenantOverlay.specialize.
        self._timing = capability.tenant.timing_multiplier if capability.tenant else 1.0
        result = ReplayResult(
            run_id=self.recorder.run_id,
            capability=capability.id,
            version=capability.version,
            digest=capability.digest(),
            tenant=capability.tenant.tenant_id if capability.tenant else None,
            execution_type=capability.crystallization.execution_type,
            status="failed",
            evidence_dir=str(self.recorder.dir),
        )

        # 1. Contract validation, before anything touches the surface.
        try:
            inputs = capability.contract.validate_inputs(raw_inputs)
        except ValueError as exc:
            result.outcome = INVALID_INPUT
            result.outcome_description = str(exc)
            result.remediation = "Correct the inputs; the flow was not started."
            result.failure = FailureDetail(
                code=INVALID_INPUT,
                expected="inputs satisfying the capability contract",
                observed=str(exc),
                hint="Rejected at the contract boundary -- the application was never touched.",
            )
            result.duration_ms = int((time.time() - started) * 1000)
            self.recorder.event("input_rejected", error=str(exc))
            self.recorder.finish(_manifest(result))
            return result

        for param in capability.contract.inputs:
            if param.sensitivity == "secret":
                self.redactor.add_literal(str(inputs.get(param.name) or ""))

        self.recorder.event(
            "run_start",
            mode="replay",
            capability=capability.ref(),
            digest=result.digest,
            execution_type=result.execution_type,
            tenant=result.tenant,
            inputs={
                p.name: mask_value(inputs.get(p.name), p.sensitivity)
                for p in capability.contract.inputs
            },
        )

        outputs: dict[str, Any] = {}
        entrypoint = entrypoint_override or capability.render(capability.surface.entrypoint, inputs, outputs)

        route_check = self.policy.check_route(entrypoint or "")
        if not route_check.allowed:
            return self._fail(
                result, POLICY_DENIED, route_check.detail,
                hint="The entry point is outside the allowlist.", started=started,
            )

        await self.surface.open(entrypoint or "")

        # 2. Session precondition. Handled by the same machinery as a mid-run
        #    expiry, so there is one code path for "we are not signed in".
        obs, _ = await self._observe("open")
        obs = await self._ensure_session(capability, obs, result, entrypoint or "")
        if isinstance(obs, ReplayResult):
            return obs

        # 3. Steps. Indexed rather than iterated, because a mid-flow
        #    re-authentication has to be able to send the flow back to the start.
        index = 0
        restarts = 0
        while index < len(capability.steps):
            step = capability.steps[index]
            step_started = time.time()
            step_result = StepResult(id=step.id, intent=step.intent, action=step.action)

            obs, frame, classified, restart = await self._stabilize(capability, step, obs, result)
            if classified is not None:
                return self._business(result, capability, classified, started, outputs)
            if isinstance(obs, ReplayResult):
                return obs
            if restart:
                verdict = self._restart_after_reauth(capability, result, restarts)
                if verdict is not None:
                    return verdict
                restarts += 1
                result.steps.clear()
                outputs.clear()
                index = 0
                await self.surface.open(entrypoint or "")
                obs, frame = await self._observe("restart-after-reauth")
                continue

            # -- policy, on the route the surface actually reports
            control_hint = step.target.description if step.target else ""
            decision = self.policy.check_action(
                step,
                route=obs.route,
                control_name=control_hint,
                approved=self.options.approve_irreversible,
                sub_routes=await self.surface.routes(),
            )
            self.recorder.event("policy", step=step.id, **decision.as_dict())
            if not decision.allowed:
                if decision.requires_approval and self.broker is not None:
                    approved = await self._ask_approval(capability, step, obs, frame, decision.detail)
                    if not approved:
                        result.status = "escalated"
                        return self._fail(
                            result, POLICY_DENIED,
                            f"human declined the irreversible step {step.id!r}",
                            step=step, obs=obs, frame=frame, started=started, status="escalated",
                        )
                elif decision.requires_approval:
                    return self._fail(
                        result, APPROVAL_REQUIRED, decision.detail,
                        step=step, obs=obs, frame=frame, started=started, status="escalated",
                        hint="This step is irreversible and no operator was available to approve "
                             "it. Re-invoke with an operator console attached, or pass an explicit "
                             "pre-approval. The run stopped before the step, not during it.",
                    )
                else:
                    return self._fail(
                        result, POLICY_DENIED, decision.detail,
                        step=step, obs=obs, frame=frame, started=started,
                        hint="Guardrail refused this step. Widen the capability or deployment "
                             "allowlist deliberately, or record a different flow.",
                    )

            # -- precondition
            if step.precondition is not None:
                ok, why = step.precondition.evaluate(obs)
                if not ok:
                    return self._fail(
                        result, UNRECOGNIZED_SCREEN, f"precondition failed: {why}",
                        step=step, obs=obs, frame=frame, started=started,
                    )

            # -- resolve + act, with bounded retries
            attempt = 0
            last_reason = ""
            resolution: Resolution | None = None
            while attempt < max(1, step.retry.attempts):
                attempt += 1
                element = None
                if step.target is not None:
                    resolution, element = resolve(step.target, obs)
                    step_result.resolution = resolution
                    if not resolution.ok:
                        last_reason = resolution.reason
                        self.recorder.event(
                            "resolve_failed", step=step.id, attempt=attempt, reason=resolution.reason
                        )
                        await self.surface.settle(timeout_ms=self._budget(step.retry.backoff_ms))
                        obs, frame = await self._observe(f"{step.id}-retry{attempt}")
                        continue
                    if resolution.degraded:
                        result.degraded_resolutions += 1
                        self.recorder.event(
                            "resolution_degraded",
                            step=step.id,
                            strategy=resolution.strategy_kind,
                            index=resolution.strategy_index,
                            note="fallback strategy fired -- drift signal",
                        )

                action, sent = self._build_action(capability, step, inputs, outputs)
                step_result.value_sent = self._mask_sent(capability, step, sent)
                outcome = await self.surface.act(action, element)
                if not outcome.ok:
                    last_reason = outcome.detail
                    self.recorder.event("act_failed", step=step.id, attempt=attempt, detail=outcome.detail)
                    await self.surface.settle(timeout_ms=self._budget(step.retry.backoff_ms))
                    obs, frame = await self._observe(f"{step.id}-retry{attempt}")
                    continue

                if step.action == "extract":
                    raw = outcome.extracted or ""
                    try:
                        value = PARSERS[step.parse](raw)
                    except Exception as exc:
                        return self._fail(
                            result, SURFACE_ERROR,
                            f"could not parse {step.parse} from {raw!r}: {exc}",
                            step=step, obs=obs, frame=frame, started=started,
                            hint="The control resolved but its content is not the expected shape.",
                        )
                    if step.bind:
                        outputs[step.bind] = value
                    step_result.extracted = value

                step_result.attempts = attempt
                step_result.duration_ms = int((time.time() - step_started) * 1000)
                self.recorder.event(
                    "act",
                    step=step.id,
                    action=action.summary(),
                    resolution=resolution.brief() if resolution else "n/a",
                    attempts=attempt,
                    ms=step_result.duration_ms,
                )
                break
            else:
                if step.optional:
                    step_result.status = "skipped"
                    step_result.detail = last_reason
                    result.steps.append(step_result)
                    self.recorder.event("step_skipped", step=step.id, reason=last_reason)
                    index += 1
                    continue
                obs, frame = await self._observe(f"{step.id}-failure", force_frame=True)
                # A step that will not resolve is often a business outcome in
                # disguise -- the screen changed because the app said no.
                outcome_match, why = self._match_outcome(capability, obs)
                if outcome_match is not None:
                    result.steps.append(step_result)
                    return self._business(result, capability, (outcome_match, why), started, outputs)
                return self._fail(
                    result, ELEMENT_NOT_FOUND, last_reason,
                    step=step, obs=obs, frame=frame, started=started, resolution=resolution,
                    hint="The control could not be identified by any recorded strategy and the "
                         "screen matches no declared outcome.",
                )

            before_signature = obs.signature()

            # -- post-action recognition
            #
            # The screen an action produces goes through exactly the same
            # pipeline as the screen it started from. An interstitial that
            # arrives *as the response to a click* is the common case -- the
            # maintenance banner is served in place of the page you asked for --
            # so recovery has to run before the expectation is judged, or every
            # recoverable condition reads as "unrecognized screen".
            await self.surface.settle(timeout_ms=self._budget(step.timeout_ms))
            obs, frame = await self._observe(f"{step.id}-after")
            obs, frame2, classified, restart = await self._stabilize(capability, step, obs, result)
            frame = frame2 or frame
            if classified is not None:
                result.steps.append(step_result)
                return self._business(result, capability, classified, started, outputs)
            if isinstance(obs, ReplayResult):
                result.steps.append(step_result)
                return obs
            if restart:
                verdict = self._restart_after_reauth(capability, result, restarts)
                if verdict is not None:
                    return verdict
                restarts += 1
                result.steps.clear()
                outputs.clear()
                index = 0
                await self.surface.open(entrypoint or "")
                obs, frame = await self._observe("restart-after-reauth")
                continue

            # Drift accounting: the recorded screen identity is compared, never
            # asserted. A mismatch means the screen is not the one recorded --
            # possibly a relabelled tenant, possibly a real change -- and it is
            # exactly the signal the crystallization ledger uses to decide a
            # capability has stopped being trustworthy.
            if step.signature_hint and obs.signature() != step.signature_hint:
                note = f"{step.id}: screen {obs.signature()} != recorded {step.signature_hint}"
                result.drift_signals.append(note)
                self.recorder.event("drift", step=step.id, detail=note)

            if step.expect_navigation and obs.signature() == before_signature:
                result.steps.append(step_result)
                return self._fail(
                    result, UNRECOGNIZED_SCREEN,
                    "the step ran but the screen did not change; the recorded run navigated here",
                    step=step, obs=obs, frame=frame, started=started,
                    hint="Usually the control was clicked but the application rejected the "
                         "input silently, or the page is still loading.",
                    snapshot=await self.surface.raw_snapshot(),
                )

            if step.expect is not None:
                ok, why = step.expect.evaluate(obs)
                step_result.expectation = why
                if not ok:
                    obs2, frame2 = await self._observe(f"{step.id}-expect-failed", force_frame=True)
                    outcome_match, why2 = self._match_outcome(capability, obs2)
                    if outcome_match is not None:
                        result.steps.append(step_result)
                        return self._business(result, capability, (outcome_match, why2), started, outputs)
                    step_result.status = "failed"
                    result.steps.append(step_result)
                    return self._fail(
                        result, UNRECOGNIZED_SCREEN,
                        f"expectation not met after {step.id}: {why}",
                        step=step, obs=obs2, frame=frame2, started=started,
                        hint="The step ran but the screen it produced is not the recorded one "
                             "and matches no declared outcome.",
                        snapshot=await self.surface.raw_snapshot(),
                    )

            result.steps.append(step_result)
            index += 1

        # 4. Checkpoint.
        if capability.checkpoint.signature_hint and obs.signature() != capability.checkpoint.signature_hint:
            note = f"checkpoint: screen {obs.signature()} != recorded {capability.checkpoint.signature_hint}"
            result.drift_signals.append(note)
            self.recorder.event("drift", step="checkpoint", detail=note)

        ok, why = capability.checkpoint.condition.evaluate(obs)
        self.recorder.event("checkpoint", id=capability.checkpoint.id, passed=ok, detail=why)
        if not ok:
            obs, frame = await self._observe("checkpoint-failed", force_frame=True)
            outcome_match, why2 = self._match_outcome(capability, obs)
            if outcome_match is not None:
                return self._business(result, capability, (outcome_match, why2), started, outputs)
            return self._fail(
                result, CHECKPOINT_NOT_REACHED, why, obs=obs, frame=frame, started=started,
                hint="Every step ran but the success condition is not satisfied.",
                snapshot=await self.surface.raw_snapshot(),
            )

        missing = [
            p.name for p in capability.contract.outputs if p.required and p.name not in outputs
        ]
        if missing:
            return self._fail(
                result, CHECKPOINT_NOT_REACHED,
                f"declared output(s) not captured: {missing}",
                obs=obs, frame=frame, started=started,
                hint="The checkpoint passed but the contract was not fulfilled.",
            )

        result.status = "success"
        result.outcome = "OK"
        result.outputs = outputs
        result.llm_calls = self._llm_calls
        result.duration_ms = int((time.time() - started) * 1000)
        self.recorder.event(
            "run_end",
            status=result.status,
            outcome=result.outcome,
            outputs={
                p.name: mask_value(outputs.get(p.name), p.sensitivity)
                for p in capability.contract.outputs
            },
            llm_calls=result.llm_calls,
            degraded_resolutions=result.degraded_resolutions,
            drift_signals=result.drift_signals,
            duration_ms=result.duration_ms,
        )
        self.recorder.finish(_manifest(result))
        return result

    # -- screen stabilization -------------------------------------------------
    async def _stabilize(self, capability: Capability, step: Step, obs: Observation, result: ReplayResult):
        """Bring the screen to a state the step can act on, or say why not.

        Returns ``(observation_or_failure, frame, business_outcome_or_None,
        restart_required)``. Ordering here is the heart of the error model: hard
        error, then business outcome, then recoverable, then act.
        """
        frame = None
        for _ in range(self.options.max_interstitials + self.options.max_transient_waits + 2):
            hard = self._hard_error(capability, obs)
            if hard:
                obs, frame = await self._observe(f"{step.id}-apperror", force_frame=True)
                return (
                    self._fail(
                        result, APPLICATION_ERROR, hard, step=step, obs=obs, frame=frame,
                        started=result.started_at,
                        hint="Application error page reached before the step could run. Do not "
                             "retry blindly; the state of any earlier step is unknown.",
                        snapshot=await self.surface.raw_snapshot(),
                    ),
                    frame,
                    None,
                    False,
                )

            outcome_match, why = self._match_outcome(capability, obs)
            if outcome_match is not None:
                return obs, frame, (outcome_match, why), False

            rule = self._interstitial(capability, obs)
            if rule is not None and self._interstitials_seen < self.options.max_interstitials:
                self._interstitials_seen += 1
                self.recorder.event("recovery", kind="interstitial", rule=rule.name, step=step.id)
                for dismiss_step in rule.dismiss:
                    res, element = resolve(dismiss_step.target, obs) if dismiss_step.target else (None, None)
                    if element is None:
                        break
                    await self.surface.act(Action(kind="click", target_ref=element.ref), element)
                    await self.surface.settle()
                result.recoveries.append(
                    RecoveryEvent(at_step=step.id, rule=rule.name, kind="interstitial",
                                  detail="dismissed", attempts=self._interstitials_seen)
                )
                obs, frame = await self._observe(f"{step.id}-post-interstitial")
                continue

            transient = self._transient(capability, obs)
            if transient is not None and self._transient_waits < self.options.max_transient_waits:
                self._transient_waits += 1
                self.recorder.event("recovery", kind="transient", rule=transient.name, step=step.id)
                await self.surface.act(Action(kind="wait", ms=self.options.transient_wait_ms), None)
                result.recoveries.append(
                    RecoveryEvent(at_step=step.id, rule=transient.name, kind="transient",
                                  detail=f"waited {self.options.transient_wait_ms}ms",
                                  attempts=self._transient_waits)
                )
                obs, frame = await self._observe(f"{step.id}-post-wait")
                continue

            if capability.recovery.session_expired is not None:
                expired, why = capability.recovery.session_expired.evaluate(obs)
                if expired:
                    restored = await self._ensure_session(capability, obs, result, None)
                    if isinstance(restored, ReplayResult):
                        return restored, frame, None, False
                    # Re-authenticating leaves the application at *its* landing
                    # screen, not where this flow was. Resuming from here would
                    # act on the wrong screen, so the flow restarts -- and the
                    # caller decides whether that is safe (see _restart_after_reauth).
                    return restored, frame, None, True

            return obs, frame, None, False

        return obs, frame, None, False

    def _restart_after_reauth(
        self, capability: Capability, result: ReplayResult, restarts: int
    ) -> ReplayResult | None:
        """Decide whether a re-authenticated flow may be replayed from the top.

        A read-only flow can be: re-running an inquiry costs nothing. A flow with
        side effects cannot -- the session may have expired *after* the write
        committed, and a blind restart is how an automation opens two
        sub-accounts or posts a transaction twice. Those escalate instead, which
        is the honest answer: only a human can see what actually landed.
        """
        if capability.contract.side_effect != "read_only" or not capability.contract.idempotent:
            return self._fail(
                result, "SESSION_EXPIRED",
                "the session expired part-way through a flow with side effects",
                started=result.started_at,
                hint=(
                    "Not restarted automatically: this capability is "
                    f"{capability.contract.side_effect} and part of it may already have "
                    "committed. Escalate so an operator can check the application state."
                ),
            )
        if restarts >= 1:
            return self._fail(
                result, "SESSION_EXPIRED",
                "the session expired twice in one run",
                started=result.started_at,
                hint="Credentials may be wrong, or the application is expiring sessions immediately.",
            )
        self.recorder.event(
            "recovery", kind="reauth_restart", detail="read-only flow restarted from step 1"
        )
        result.recoveries.append(
            RecoveryEvent(
                at_step="session", rule="reauth_restart", kind="reauth",
                detail="session expired mid-flow; read-only capability restarted from the first step",
            )
        )
        return None

    def _interstitial(self, capability: Capability, obs: Observation):
        for rule in capability.recovery.interstitials:
            hit, _ = rule.when.evaluate(obs)
            if hit:
                return rule
        return None

    def _transient(self, capability: Capability, obs: Observation):
        for rule in capability.recovery.transient:
            hit, _ = rule.when.evaluate(obs)
            if hit:
                return rule
        return None

    def _hard_error(self, capability: Capability, obs: Observation) -> str:
        if capability.recovery.hard_error is None:
            return ""
        hit, why = capability.recovery.hard_error.evaluate(obs)
        return why if hit else ""

    # -- session --------------------------------------------------------------
    async def _ensure_session(
        self, capability: Capability, obs: Observation, result: ReplayResult, entrypoint: str | None
    ):
        """If the surface is at sign-on, restore the session by *replaying* the
        re-auth capability. Composition, not a hard-coded login routine."""
        if capability.recovery.session_expired is None:
            return obs
        expired, why = capability.recovery.session_expired.evaluate(obs)
        if not expired:
            return obs

        reauth_id = capability.recovery.reauth_capability
        if not (self.options.allow_reauth and reauth_id and self.capability_loader):
            return self._fail(
                result, "SESSION_EXPIRED",
                f"session is not authenticated ({why}) and no re-auth capability is configured",
                obs=obs, started=result.started_at,
                hint="Set recovery.reauth_capability, or supply an authenticated session.",
            )
        if self._reauths >= 2:
            return self._fail(
                result, "SESSION_EXPIRED", "re-authentication looped twice without sticking",
                obs=obs, started=result.started_at,
                hint="Credentials may be wrong, or the account is locked.",
            )
        self._reauths += 1
        self.recorder.event("recovery", kind="reauth", capability=reauth_id, detail=why)

        # The profile names its re-auth capability as a forward reference, so the
        # sign-on flow may simply not have been recorded yet. That is a
        # configuration state, not a crash.
        try:
            reauth_cap = self.capability_loader(reauth_id)
        except FileNotFoundError:
            return self._fail(
                result, "SESSION_EXPIRED",
                f"session is not authenticated ({why}) and the configured re-auth "
                f"capability {reauth_id!r} has not been recorded",
                obs=obs, started=result.started_at,
                hint=f"Record it first: pcx discover --id {reauth_id} ...",
            )
        sub = ReplayEngine(
            self.surface,
            PolicyEngine(
                self.policy.deployment, reauth_cap.policy, tenant_host=self.policy.tenant_host
            ),
            self.recorder,
            self.redactor,
            broker=self.broker,
            options=self.options,
            capability_loader=self.capability_loader,
        )
        sub_result = await sub.run(reauth_cap, _credentials_for(reauth_cap))
        result.recoveries.append(
            RecoveryEvent(at_step="session", rule=reauth_id, kind="reauth",
                          detail=f"re-auth {sub_result.status}")
        )
        if sub_result.status != "success":
            return self._fail(
                result, "SESSION_EXPIRED",
                f"re-authentication failed: {sub_result.caller_summary()}",
                obs=obs, started=result.started_at,
                hint="See the same evidence directory for the nested sign-on run.",
            )
        if entrypoint:
            await self.surface.open(entrypoint)
        new_obs, _ = await self._observe("post-reauth")
        return new_obs

    # -- escalation -----------------------------------------------------------
    async def _ask_approval(self, capability, step, obs, frame, detail) -> bool:
        request = InterventionRequest(
            kind="approval",
            run_id=self.recorder.run_id,
            capability=capability.ref(),
            goal=capability.contract.summary,
            step_id=step.id,
            step_index=capability.steps.index(step) + 1,
            reason=detail,
            screen=ScreenState.of(obs),
            frame_path=frame,
            context={"mode": "replay", "step_intent": step.intent, "risk": step.risk},
        )
        self.recorder.event("approval_requested", step=step.id, detail=detail)
        outcome = await self.broker.raise_intervention(request)
        self.recorder.event("approval_resolved", disposition=outcome.disposition, operator=outcome.operator)
        return outcome.disposition == "approved"

    def _announce_wait(self, request: InterventionRequest) -> None:
        """Say that the run is parked, and what the person has to do.

        A handoff that prints nothing is indistinguishable from a hang: the run
        simply stops for up to fifteen minutes. And "take control of the live
        session" is not one action -- the broker will not accept an operator
        action until the request is *claimed*, so a person who opens the console
        and starts clicking the screen gets nothing and no explanation.
        """
        budget = int(self.broker.auto_timeout_s) if self.broker else 0
        where = self.broker.console_url if self.broker else None
        if not where:
            self.reporter.note(
                f"escalated, but no operator console is running -- nothing can answer this. "
                f"Aborting in {budget}s; re-run with --console.", "warn",
            )
            return
        self.reporter.note(f"escalated: {request.reason[:120]}", "warn")
        self.reporter.note(f"WAITING FOR A HUMAN -- open {where} in a web browser", "warn")
        self.reporter.note(f"  (this run aborts on its own after {budget}s)", "warn")
        self.reporter.note("  On that page, in the 'Intervention' card, top right:", "warn")
        self.reporter.note("  1. click 'Take control' -- until you do, the screen is read-only", "warn")
        self.reporter.note("  2. click the live screen on the left to focus a field, then use", "warn")
        self.reporter.note("     the 'Manual input' card: type text, then 'Type' / Enter / Tab", "warn")
        self.reporter.note("  3. click 'Resume' to hand control back", "warn")

    async def escalate_failure(self, capability: Capability, result: ReplayResult) -> ReplayResult:
        """Route a hard failure to a human, and let them finish on the live session."""
        if self.broker is None or result.failure is None:
            return result
        obs, frame = await self._observe("escalation", force_frame=True)
        request = InterventionRequest(
            kind="error",
            run_id=self.recorder.run_id,
            capability=capability.ref(),
            goal=capability.contract.summary,
            step_id=result.failure.step_id or "?",
            step_index=len(result.steps),
            reason=f"{result.outcome}: {result.failure.observed}",
            screen=ScreenState.of(obs),
            frame_path=frame,
            context={
                "mode": "replay",
                "expected": result.failure.expected,
                "hint": result.failure.hint,
                "steps_completed": [s.id for s in result.steps],
                "outputs_so_far": list(result.outputs),
            },
        )
        self.recorder.event("escalation_raised", request_id=request.id, reason=request.reason)
        self._announce_wait(request)
        outcome = await self.broker.raise_intervention(request)
        result.human_interventions += 1
        self.recorder.event(
            "escalation_resolved",
            disposition=outcome.disposition,
            operator=outcome.operator,
            note=outcome.note,
            human_actions=[a.model_dump() for a in outcome.human_actions],
            waited_s=outcome.waited_s,
        )
        result.status = "escalated"
        if result.failure:
            result.failure.hint = (
                f"handed to operator {outcome.operator}: {outcome.disposition} -- {outcome.note}"
            )
        if outcome.resume:
            # The human acted on the same live session; re-evaluate the checkpoint
            # rather than assuming either success or failure.
            obs, frame = await self._observe("post-handoff", force_frame=True)
            ok, why = capability.checkpoint.condition.evaluate(obs)
            self.recorder.event("checkpoint_after_handoff", passed=ok, detail=why)
            if ok:
                result.status = "success"
                result.outcome = "OK_AFTER_HANDOFF"
                result.outcome_description = (
                    f"Completed by operator {outcome.operator} on the live session."
                )
        self.recorder.finish(_manifest(result))
        return result

    # -- result builders ------------------------------------------------------
    def _business(self, result: ReplayResult, capability, matched, started, outputs) -> ReplayResult:
        outcome, why = matched
        result.status = "business_outcome"
        result.outcome = outcome.code
        result.outcome_description = outcome.description.strip()
        result.remediation = outcome.remediation
        result.outputs = outputs
        result.llm_calls = self._llm_calls
        result.duration_ms = int((time.time() - started) * 1000)
        self.recorder.event(
            "business_outcome", code=outcome.code, detector=why, duration_ms=result.duration_ms
        )
        self.recorder.finish(_manifest(result))
        return result

    def _fail(
        self,
        result: ReplayResult,
        code: str,
        observed: str,
        *,
        step: Step | None = None,
        obs: Observation | None = None,
        frame: str | None = None,
        started: float | None = None,
        resolution: Resolution | None = None,
        hint: str = "",
        snapshot: str | None = None,
        status: str = "failed",
    ) -> ReplayResult:
        result.status = status  # type: ignore[assignment]
        result.outcome = code
        result.llm_calls = self._llm_calls
        result.duration_ms = int((time.time() - (started or result.started_at)) * 1000)
        snapshot_path = None
        if snapshot:
            snapshot_path = self.recorder.snapshot(snapshot, code.lower())
        self.recorder.flush_deferred_frames()
        result.failure = FailureDetail(
            code=code,
            step_id=step.id if step else None,
            step_intent=step.intent if step else "",
            expected=(
                step.expect.model_dump_json(exclude_defaults=True)
                if step is not None and step.expect is not None
                else (step.target.description if step and step.target else "")
            ),
            observed=observed,
            resolution=resolution,
            route=obs.route if obs else "",
            screen_signature=obs.signature() if obs else "",
            frame=frame,
            snapshot=snapshot_path,
            hint=hint,
        )
        self.recorder.event(
            "run_end", status=result.status, outcome=code, failure=result.failure.model_dump(mode="json")
        )
        self.recorder.finish(_manifest(result))
        return result

    # -- misc -----------------------------------------------------------------
    def _build_action(self, capability: Capability, step: Step, inputs, outputs) -> tuple[Action, str | None]:
        text = capability.render(step.value, inputs, outputs)
        url = capability.render(step.url, inputs, outputs)
        return (
            Action(
                kind=step.action if step.action != "assert" else "wait",  # type: ignore[arg-type]
                text=text,
                keys=step.keys,
                url=url,
                ms=step.ms,
                bind=step.bind,
                rationale=step.intent,
            ),
            text,
        )

    def _budget(self, recorded_ms: int) -> int:
        """Scale a recorded wait by this tenant's timing multiplier."""
        return int(recorded_ms * getattr(self, "_timing", 1.0))

    def _mask_sent(self, capability: Capability, step: Step, sent: str | None) -> str | None:
        if sent is None:
            return None
        for param in capability.contract.inputs:
            if step.value and f"inputs.{param.name}" in step.value:
                return str(mask_value(sent, param.sensitivity))
        return self.redactor.scrub(sent)


def _credentials_for(capability: Capability) -> dict[str, Any]:
    """Resolve a sign-on capability's inputs from the environment, never the artifact."""
    from ..config import resolve_credential

    values: dict[str, Any] = {}
    for param in capability.contract.inputs:
        value, _ = resolve_credential(capability.id, param.name)
        if value is not None:
            values[param.name] = value
    return values


def missing_credentials_for(capability: Capability) -> list[str]:
    """Which required inputs of a capability the environment cannot supply.

    Reported as the variable names that *would* have worked, because "set one of
    these" is actionable and "input 'username' is required" is not.
    """
    from ..config import credential_env_candidates, resolve_credential

    missing: list[str] = []
    for param in capability.contract.inputs:
        if not param.required or param.default is not None:
            continue
        value, _ = resolve_credential(capability.id, param.name)
        if value is None:
            names = " or ".join(f"${n}" for n in credential_env_candidates(capability.id, param.name))
            missing.append(f"  {param.name}  needs {names}")
    return missing


def _manifest(result: ReplayResult) -> dict[str, Any]:
    data = result.model_dump(mode="json")
    data.pop("outputs", None)  # outputs go to the caller, not to the evidence file
    data["output_names"] = list(result.outputs)
    return data
