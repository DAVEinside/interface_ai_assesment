"""Progressive crystallization: promotion on evidence, demotion on regression.

This implements the lifecycle from Malik, *Progressive Crystallization* (2026),
against UI capabilities rather than IT-operations playbooks. The mapping:

    Type 3  agent-orchestrated   an LLM decides each action against the live UI
    Type 2  hybrid               fixed steps; the model is consulted only to
                                 *interpret* a screen it does not recognize,
                                 choosing from the capability's declared outcome
                                 codes -- never to choose an action
    Type 1  deterministic        fixed steps, no model, zero tokens

A capability enters at Type 3 the moment it is discovered, and moves down the
spectrum only as evidence accumulates: successful runs, a stable action
sequence, locators that keep resolving on their primary strategy, no safety
violations, and passing acceptance tests generated from its own traces.

The circuit breaker runs in the other direction. A hard failure, a safety
violation or an acceptance-test regression demotes the capability one level and
quarantines it, so the *next* invocation is served by a more capable, more
expensive execution type instead of failing again. That is the property that
makes automatic promotion safe to have at all: being wrong is recoverable
without a human noticing first.

One deliberate departure from the paper: promotion to Type 1 additionally
requires a human review flag. Locator resolution is a perception problem, not a
classification problem, and "the model agreed with itself 99 times" is weaker
evidence about a UI than it is about a log line.
"""

from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from ..artifact.schema import Capability, Evidence
from ..artifact.store import CapabilityStore
from ..replay.outcomes import ReplayResult

#: Outcomes that are *not* evidence of a working capability, but are not
#: regressions either -- the app said no, which is the capability working.
NEUTRAL_OUTCOMES = {
    "MEMBER_NOT_FOUND",
    "VALIDATION_ERROR",
    "PERMISSION_DENIED",
    "INVALID_INPUT",
    # The guardrail stopping an irreversible step is the design working. Treating
    # it as a regression would demote every capability that needs approval, every
    # time it is invoked without an operator.
    "APPROVAL_REQUIRED",
}


@dataclass
class LedgerRecord:
    run_id: str
    at: float
    capability: str
    version: int
    digest: str
    execution_type: int
    status: str
    outcome: str
    action_sequence: str
    input_key: str
    degraded_resolutions: int
    drift_signals: int
    llm_calls: int
    duration_ms: int
    human_interventions: int = 0
    safety_violation: bool = False
    tenant: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def record_run(
    store: CapabilityStore,
    capability: Capability,
    result: ReplayResult,
    *,
    action_sequence: str,
    input_key: str,
    human_interventions: int = 0,
) -> LedgerRecord:
    """Append one run to the capability's evidence ledger."""
    record = LedgerRecord(
        run_id=result.run_id,
        at=time.time(),
        capability=capability.id,
        version=capability.version,
        digest=result.digest,
        execution_type=result.execution_type,
        status=result.status,
        outcome=result.outcome,
        action_sequence=action_sequence,
        input_key=input_key,
        degraded_resolutions=result.degraded_resolutions,
        drift_signals=len(result.drift_signals),
        llm_calls=result.llm_calls,
        duration_ms=result.duration_ms,
        human_interventions=human_interventions,
        safety_violation=result.outcome == "POLICY_DENIED",
        tenant=result.tenant,
    )
    store.append_ledger(capability.id, record.as_dict())
    return record


def summarize(records: Iterable[dict[str, Any]], digest: str, *, since: float | None = None) -> Evidence:
    """Roll the ledger up into the evidence block, scoped to one artifact digest.

    Scoping by digest matters: evidence accumulated against v1 says nothing about
    v2. Editing a capability resets its track record, which is the correct and
    conservative behaviour.
    """
    rows = [r for r in records if r.get("digest") == digest]
    evidence = Evidence()
    if not rows:
        return evidence

    successes = [r for r in rows if r["status"] == "success"]
    business = [r for r in rows if r["status"] == "business_outcome"]
    failures = [r for r in rows if r["status"] == "failed"]

    evidence.successful_runs = len(successes)
    evidence.failed_runs = len(failures)
    evidence.distinct_input_sets = len({r["input_key"] for r in successes + business})
    evidence.safety_violations = sum(1 for r in rows if r.get("safety_violation"))
    evidence.human_interventions = sum(int(r.get("human_interventions") or 0) for r in rows)
    evidence.degraded_resolutions = sum(int(r.get("degraded_resolutions") or 0) for r in rows)
    evidence.drift_signals = sum(int(r.get("drift_signals") or 0) for r in rows)
    evidence.tenants_seen = sorted({r.get("tenant") or "default" for r in rows})

    # Recovery from quarantine is judged on what has happened *since* the
    # demotion, not on the capability's whole history -- the failure that caused
    # the demotion is in that history for ever, and would otherwise block
    # recovery permanently.
    if since is not None:
        after = [r for r in rows if float(r.get("at") or 0) > since]
        evidence.successful_runs_since_demotion = sum(1 for r in after if r["status"] == "success")
        evidence.failed_runs_since_demotion = sum(1 for r in after if r["status"] == "failed")

    # Sequence stability is measured over *completed* runs only. A run that
    # ended in a declared business outcome legitimately stops early -- counting
    # its shorter path as instability would mean a capability could never be
    # promoted in an environment where "no such member" happens, which is every
    # real environment.
    if successes:
        sequences = Counter(r["action_sequence"] for r in successes)
        evidence.action_sequence_stability = round(sequences.most_common(1)[0][1] / len(successes), 4)
    healthy = successes + business
    if healthy:
        clean = sum(1 for r in healthy if int(r.get("degraded_resolutions") or 0) == 0)
        evidence.resolution_stability = round(clean / len(healthy), 4)
    return evidence


@dataclass
class GateResult:
    eligible: bool
    target_type: int
    reasons: list[str]

    def explain(self) -> str:
        verdict = "ELIGIBLE" if self.eligible else "not eligible"
        return f"{verdict} for Type {self.target_type}: " + "; ".join(self.reasons)


def evaluate_promotion(capability: Capability) -> GateResult:
    """Check the gates for the next promotion, without applying it."""
    crystal = capability.crystallization
    gates = crystal.gates
    evidence = crystal.evidence
    reasons: list[str] = []

    if crystal.status == "quarantined":
        return GateResult(False, crystal.execution_type, ["capability is quarantined after a regression"])

    if crystal.execution_type == 3:
        target = 2
        checks = [
            (evidence.successful_runs >= gates.t3_to_t2_min_runs,
             f"successful runs {evidence.successful_runs}/{gates.t3_to_t2_min_runs}"),
            (evidence.action_sequence_stability >= gates.t3_to_t2_min_sequence_stability,
             f"action-sequence stability {evidence.action_sequence_stability:.2f}"
             f"/{gates.t3_to_t2_min_sequence_stability:.2f}"),
            (evidence.safety_violations <= gates.max_safety_violations,
             f"safety violations {evidence.safety_violations}/{gates.max_safety_violations}"),
            (evidence.human_interventions == 0,
             f"human interventions {evidence.human_interventions} (must be 0)"),
            (bool(crystal.acceptance_tests) or not gates.require_acceptance_tests,
             f"acceptance tests present: {len(crystal.acceptance_tests)}"),
        ]
    elif crystal.execution_type == 2:
        target = 1
        checks = [
            (evidence.successful_runs >= gates.t2_to_t1_min_runs,
             f"successful runs {evidence.successful_runs}/{gates.t2_to_t1_min_runs}"),
            (evidence.resolution_stability >= gates.t2_to_t1_min_resolution_stability,
             f"locator resolution stability {evidence.resolution_stability:.2f}"
             f"/{gates.t2_to_t1_min_resolution_stability:.2f}"),
            (evidence.distinct_input_sets >= 2,
             f"distinct input sets {evidence.distinct_input_sets}/2"),
            (evidence.safety_violations <= gates.max_safety_violations,
             f"safety violations {evidence.safety_violations}/{gates.max_safety_violations}"),
            (evidence.human_reviewed or not gates.require_human_review_for_t1,
             f"human review: {'done' if evidence.human_reviewed else 'PENDING'}"),
        ]
    else:
        return GateResult(False, 1, ["already fully deterministic"])

    reasons = [("PASS " if ok else "FAIL ") + why for ok, why in checks]
    return GateResult(all(ok for ok, _ in checks), target, reasons)


def promote(capability: Capability) -> tuple[bool, str]:
    """Apply a promotion if the gates allow it."""
    gate = evaluate_promotion(capability)
    if not gate.eligible:
        return False, gate.explain()
    crystal = capability.crystallization
    crystal.execution_type = gate.target_type  # type: ignore[assignment]
    crystal.status = "active"
    crystal.evidence.last_promoted_at = datetime.now(timezone.utc).isoformat()
    if gate.target_type == 2:
        # In hybrid, the model is allowed to interpret only the steps whose
        # expectation is a whole-screen assertion -- those are the places an
        # unforeseen screen actually shows up.
        crystal.hybrid_steps = [s.id for s in capability.steps if s.expect is not None]
    else:
        crystal.hybrid_steps = []
    return True, f"promoted to Type {gate.target_type}"


def demote(capability: Capability, reason: str) -> tuple[bool, str]:
    """Circuit breaker: step back up the spectrum and quarantine."""
    crystal = capability.crystallization
    if crystal.execution_type >= 3:
        crystal.status = "quarantined"
        crystal.evidence.last_demoted_at = datetime.now(timezone.utc).isoformat()
        crystal.evidence.last_demotion_reason = reason
        return False, "already Type 3; quarantined pending review"
    crystal.execution_type = crystal.execution_type + 1  # type: ignore[assignment]
    crystal.status = "quarantined"
    crystal.evidence.last_demoted_at = datetime.now(timezone.utc).isoformat()
    crystal.evidence.last_demotion_reason = reason
    if crystal.execution_type == 2:
        crystal.hybrid_steps = [s.id for s in capability.steps if s.expect is not None]
    return True, f"demoted to Type {crystal.execution_type}: {reason}"


def should_demote(result: ReplayResult) -> str | None:
    """Decide whether a completed run trips the breaker.

    A declared business outcome never does -- the capability worked. A hard
    failure, a policy violation or an unrecognized screen does.
    """
    if result.status in ("success", "business_outcome"):
        return None
    if result.outcome in NEUTRAL_OUTCOMES:
        return None
    if result.status == "escalated":
        return f"run escalated to a human ({result.outcome})"
    if result.outcome in NEUTRAL_OUTCOMES:
        return None
    return f"hard failure: {result.outcome}"


def clear_quarantine(capability: Capability) -> bool:
    """Return a quarantined capability to candidacy after a clean streak.

    The paper's production anecdote is exactly this shape: a deterministic
    playbook broke on a format change, was demoted so the model could handle it,
    and was re-promoted after a run of clean executions. Recovery is automatic
    and evidence-based in the same way demotion is -- otherwise every incident
    leaves a permanently degraded capability behind for a human to notice.
    """
    crystal = capability.crystallization
    if crystal.status != "quarantined":
        return False
    needed = crystal.gates.t3_to_t2_min_runs
    evidence = crystal.evidence
    clean = evidence.successful_runs_since_demotion >= needed and evidence.failed_runs_since_demotion == 0
    if clean:
        crystal.status = "candidate"
        return True
    return False


def refresh_evidence(store: CapabilityStore, capability: Capability) -> Capability:
    """Recompute the evidence block from the ledger and persist it.

    Counters are derived from the ledger; lifecycle facts (when it was promoted
    or demoted, whether a human has reviewed the logic) are *decisions*, not
    observations, so they are carried across rather than recomputed.
    """
    previous = capability.crystallization.evidence
    since = _epoch(previous.last_demoted_at)
    fresh = summarize(store.read_ledger(capability.id), capability.digest(), since=since)
    fresh.last_promoted_at = previous.last_promoted_at
    fresh.last_demoted_at = previous.last_demoted_at
    fresh.last_demotion_reason = previous.last_demotion_reason
    fresh.human_reviewed = previous.human_reviewed
    capability.crystallization.evidence = fresh
    store.update(capability)
    return capability


def _epoch(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return None
