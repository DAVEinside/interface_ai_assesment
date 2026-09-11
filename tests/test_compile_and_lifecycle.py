"""Compiler invariants and the crystallization lifecycle."""

from __future__ import annotations

import pytest

from pcx.agent.trace import RunTrace, ScreenState, TraceStep
from pcx.artifact.compile import AnchorRules, CompileError, compile_capability, synthesize_locator
from pcx.artifact.schema import Capability, Checkpoint, Condition, Contract, Outcome, Param, Step, SurfaceBinding
from pcx.artifact.locator import Locator, Strategy, resolve
from pcx.crystallize import lifecycle
from pcx.surfaces.model import Action, ActionOutcome, Observation, Rect, Role, UIElement


def el(ref, role, name="", x=0, y=0, w=100, h=16, region="workframe", editable=False):
    return UIElement(
        ref=ref, role=role, name=name, editable=editable, region=region,
        rect=Rect(x=x, y=y, w=w, h=h),
    )


# --------------------------------------------------------------------------- #
# Locator synthesis
# --------------------------------------------------------------------------- #


def test_synthesized_strategies_all_resolve_to_the_recorded_element():
    elements = [
        el("e1", Role.CELL, "MEMBER NUMBER:", x=10, y=50, w=120),
        el("e2", Role.TEXTBOX, "", x=140, y=50, w=110, editable=True),
        el("e3", Role.BUTTON, "INQUIRE", x=270, y=50, w=80),
    ]
    locator = synthesize_locator(elements[1], elements)
    obs = Observation(surface_kind="t", route="/x", elements=elements)
    assert locator.strategies, "at least one hypothesis must survive validation"
    for i, strategy in enumerate(locator.strategies):
        probe = Locator(strategies=[strategy], verify=locator.verify)
        res, element = resolve(probe, obs)
        assert res.ok and element.ref == "e2", f"strategy {i} ({strategy.kind}) does not hold"


def test_recorded_input_values_are_never_used_as_anchors():
    """The bug this exists to prevent: anchoring on a value the agent just typed."""
    elements = [
        el("e1", Role.CELL, "TLR0042", x=10, y=50, w=120),   # a value, not furniture
        el("e2", Role.TEXTBOX, "", x=140, y=50, w=110, editable=True),
    ]
    rules = AnchorRules(forbidden={"TLR0042"})
    locator = synthesize_locator(elements[1], elements, rules=rules)
    assert not any(s.anchor_text for s in locator.strategies), (
        "the only text available to anchor on was a recorded value, so the "
        "compiler must fall through to a positional strategy rather than use it"
    )
    assert "TLR0042" not in locator.model_dump_json()


def test_volatile_product_text_is_never_used_as_an_anchor():
    import re

    elements = [
        el("e1", Role.CELL, "OPR: R. OKONKWO", x=10, y=50, w=120),
        el("e2", Role.LINK, "MEMBER INQUIRY", x=10, y=90, w=120),
    ]
    rules = AnchorRules(volatile=(re.compile("^OPR:"),))
    locator = synthesize_locator(elements[1], elements, rules=rules)
    anchors = [s.anchor_text for s in locator.strategies if s.anchor_text]
    assert not any("OKONKWO" in (a or "") for a in anchors)


def test_extraction_targets_are_not_identified_by_their_value():
    elements = [
        el("e1", Role.CELL, "ACCOUNT TYPE", x=100, y=10, w=140),
        el("e2", Role.CELL, "CURRENT BALANCE", x=260, y=10, w=120),
        el("e3", Role.CELL, "SHARE SAVINGS", x=100, y=40, w=140),
        el("e4", Role.CELL, "$18,425.63", x=260, y=40, w=120),
    ]
    locator = synthesize_locator(elements[3], elements, dynamic=True)
    serialized = locator.model_dump_json()
    assert "18,425.63" not in serialized, "the balance is the answer; it cannot be the address"
    assert any(s.kind == "row_cell" for s in locator.strategies)


# --------------------------------------------------------------------------- #
# Compilation from a trace
# --------------------------------------------------------------------------- #


def _trace() -> RunTrace:
    elements = [
        el("e1", Role.CELL, "MEMBER NUMBER:", x=10, y=50, w=120),
        el("e2", Role.TEXTBOX, "", x=140, y=50, w=110, editable=True),
        el("e3", Role.BUTTON, "INQUIRE", x=270, y=50, w=80),
        el("e4", Role.CELL, "CURRENT BALANCE", x=260, y=100, w=120),
        el("e5", Role.CELL, "SHARE SAVINGS", x=100, y=130, w=140),
        el("e6", Role.CELL, "$18,425.63", x=260, y=130, w=120),
    ]
    before = ScreenState(route="http://127.0.0.1:8799/desk", title="INQ", signature="sig_a")
    after = ScreenState(route="http://127.0.0.1:8799/desk", title="DETAIL", signature="sig_b")
    trace = RunTrace(
        run_id="t1", goal="read the savings balance", entrypoint="http://127.0.0.1:8799/desk",
        surface_kind="web", parameters={"member_number": "10000001"},
        wanted_outputs=["savings_balance"], status="succeeded",
        outputs={"savings_balance": "$18,425.63"},
    )
    trace.steps = [
        TraceStep(
            index=1, action=Action(kind="type", target_ref="e2", text="10000001"),
            before=before, after=before, target=elements[1], context_elements=elements,
            outcome=ActionOutcome(ok=True),
        ),
        TraceStep(
            index=2, action=Action(kind="click", target_ref="e3"),
            before=before, after=after, target=elements[2], context_elements=elements,
            outcome=ActionOutcome(ok=True, navigated=True),
        ),
        TraceStep(
            index=3, action=Action(kind="extract", target_ref="e6", bind="savings_balance"),
            before=after, after=after, target=elements[5], context_elements=elements,
            outcome=ActionOutcome(ok=True, extracted="$18,425.63"),
        ),
    ]
    trace.finished_at = trace.started_at + 4
    return trace


def test_compile_parameterizes_typed_values():
    capability = compile_capability(_trace(), capability_id="read_balance")
    assert capability.steps[0].value == "{{ inputs.member_number }}"
    assert "10000001" not in capability.model_dump_json()


def test_compile_refuses_a_literal_that_matches_no_declared_input():
    trace = _trace()
    trace.parameters = {}
    with pytest.raises(CompileError, match="matches no declared input"):
        compile_capability(trace, capability_id="read_balance")


def test_compile_refuses_an_unsuccessful_run():
    trace = _trace()
    trace.status = "failed"
    with pytest.raises(CompileError, match="only successful runs crystallize"):
        compile_capability(trace, capability_id="read_balance")


def test_compiled_policy_is_least_privilege():
    capability = compile_capability(_trace(), capability_id="read_balance")
    assert set(capability.policy.allowed_actions) == {"type", "click", "extract"}
    assert capability.policy.allowed_hosts == [], (
        "a capability names what it may do, never which institution's host it runs against"
    )
    assert capability.contract.side_effect == "reversible_write"  # a navigation occurred


def test_compiled_outputs_are_typed_from_what_was_read():
    capability = compile_capability(_trace(), capability_id="read_balance")
    output = capability.contract.outputs[0]
    assert output.name == "savings_balance" and output.type == "money"
    assert capability.steps[-1].parse == "money"


def test_compiled_capability_starts_at_type_3():
    capability = compile_capability(_trace(), capability_id="read_balance")
    assert capability.crystallization.execution_type == 3
    assert capability.crystallization.status == "candidate"
    assert capability.crystallization.acceptance_tests, "tests are generated from the trace"


# --------------------------------------------------------------------------- #
# Crystallization lifecycle
# --------------------------------------------------------------------------- #


def _capability() -> Capability:
    return compile_capability(_trace(), capability_id="read_balance")


def _ledger(n, digest, status="success", degraded=0, sequence="a|b|c"):
    return [
        {
            "digest": digest, "status": status, "outcome": "OK",
            "action_sequence": sequence, "input_key": f"m{i}",
            "degraded_resolutions": degraded, "human_interventions": 0,
            "safety_violation": False,
        }
        for i in range(n)
    ]


def test_gates_block_promotion_without_evidence():
    capability = _capability()
    gate = lifecycle.evaluate_promotion(capability)
    assert not gate.eligible
    assert any("successful runs 0/3" in r for r in gate.reasons)


def test_promotion_to_hybrid_after_enough_clean_runs():
    capability = _capability()
    capability.crystallization.evidence = lifecycle.summarize(_ledger(3, capability.digest()), capability.digest())
    changed, message = lifecycle.promote(capability)
    assert changed and capability.crystallization.execution_type == 2
    assert capability.crystallization.hybrid_steps, "hybrid marks which steps may consult the model"


def test_promotion_to_deterministic_requires_human_review():
    capability = _capability()
    capability.crystallization.execution_type = 2
    capability.crystallization.evidence = lifecycle.summarize(_ledger(6, capability.digest()), capability.digest())
    changed, message = lifecycle.promote(capability)
    assert not changed and "human review: PENDING" in message

    capability.crystallization.evidence.human_reviewed = True
    changed, _ = lifecycle.promote(capability)
    assert changed and capability.crystallization.execution_type == 1


def test_unstable_action_sequence_blocks_promotion():
    capability = _capability()
    rows = _ledger(2, capability.digest()) + _ledger(2, capability.digest(), sequence="x|y")
    capability.crystallization.evidence = lifecycle.summarize(rows, capability.digest())
    gate = lifecycle.evaluate_promotion(capability)
    assert not gate.eligible
    assert any("action-sequence stability" in r and r.startswith("FAIL") for r in gate.reasons)


def test_evidence_is_scoped_to_the_artifact_digest():
    capability = _capability()
    stale = _ledger(5, "sha256:someotherversion")
    evidence = lifecycle.summarize(stale, capability.digest())
    assert evidence.successful_runs == 0, "evidence for another version proves nothing about this one"


def test_business_outcomes_do_not_trip_the_breaker():
    from pcx.replay.outcomes import ReplayResult

    result = ReplayResult(
        run_id="r", capability="c", version=1, digest="d",
        status="business_outcome", outcome="MEMBER_NOT_FOUND",
    )
    assert lifecycle.should_demote(result) is None


def test_approval_required_does_not_trip_the_breaker():
    from pcx.replay.outcomes import ReplayResult

    result = ReplayResult(
        run_id="r", capability="c", version=1, digest="d",
        status="escalated", outcome="APPROVAL_REQUIRED",
    )
    assert lifecycle.should_demote(result) is None


def test_hard_failure_demotes_and_quarantines():
    from pcx.replay.outcomes import ReplayResult

    capability = _capability()
    capability.crystallization.execution_type = 1
    capability.crystallization.status = "active"
    result = ReplayResult(
        run_id="r", capability="c", version=1, digest="d",
        status="failed", outcome="ELEMENT_NOT_FOUND",
    )
    reason = lifecycle.should_demote(result)
    assert reason
    changed, message = lifecycle.demote(capability, reason)
    assert changed and capability.crystallization.execution_type == 2
    assert capability.crystallization.status == "quarantined"
    assert not lifecycle.evaluate_promotion(capability).eligible


def test_quarantine_clears_only_on_runs_after_the_demotion():
    """The failure that caused the demotion is in the history for ever.

    Counting it would mean a demoted capability could never recover, so recovery
    is judged on the runs that happened *after* the demotion timestamp.
    """
    capability = _capability()
    capability.crystallization.status = "quarantined"
    digest = capability.digest()
    rows = _ledger(3, digest, status="failed")            # before the demotion
    for row in rows:
        row["at"] = 100.0
    later = _ledger(3, digest)                            # after it
    for row in later:
        row["at"] = 300.0
    evidence = lifecycle.summarize(rows + later, digest, since=200.0)
    capability.crystallization.evidence = evidence
    assert evidence.failed_runs == 3, "history still records the failures"
    assert evidence.successful_runs_since_demotion == 3
    assert lifecycle.clear_quarantine(capability)
    assert capability.crystallization.status == "candidate"


def test_quarantine_does_not_clear_while_failures_continue():
    capability = _capability()
    capability.crystallization.status = "quarantined"
    digest = capability.digest()
    rows = _ledger(3, digest) + _ledger(1, digest, status="failed")
    for row in rows:
        row["at"] = 300.0
    capability.crystallization.evidence = lifecycle.summarize(rows, digest, since=200.0)
    assert not lifecycle.clear_quarantine(capability)


def test_entrypoint_is_parameterized_even_on_an_unfamiliar_host():
    """Recording against a host that is not the default deployment.

    The entry point must always come out as `{{ tenant.base_url }}/path`. If the
    compiler strips only the caller's default base URL, a capability recorded
    against a second tenant, a staging instance or a live site silently bakes
    that host into the artifact -- and every later `specialize()` is a no-op
    because there is no template left to substitute.
    """
    trace = _trace()
    trace.entrypoint = "https://parabank.parasoft.com/parabank/overview.htm"
    for step in trace.steps:
        step.before.route = trace.entrypoint
        if step.after:
            step.after.route = trace.entrypoint

    capability = compile_capability(
        trace, capability_id="read_balance", base_url="http://127.0.0.1:8799"
    )
    assert capability.surface.entrypoint == "{{ tenant.base_url }}/parabank/overview.htm"
    assert "parabank.parasoft.com" not in capability.model_dump_json()

    from pcx.artifact.schema import TenantOverlay

    bound = capability.specialize(
        TenantOverlay(tenant_id="parabank", base_url="https://parabank.parasoft.com")
    )
    assert bound.resolved_entrypoint() == "https://parabank.parasoft.com/parabank/overview.htm"


def test_entrypoint_on_the_default_host_still_strips_the_default():
    trace = _trace()
    capability = compile_capability(
        trace, capability_id="read_balance", base_url="http://127.0.0.1:8799"
    )
    assert capability.surface.entrypoint == "{{ tenant.base_url }}/desk"


# --------------------------------------------------------------------------- #
# Automatic promotion, and the gate that reads human interventions
# --------------------------------------------------------------------------- #


def test_a_run_that_needed_a_human_blocks_promotion():
    """`human_interventions == 0` is a T3 -> T2 gate, and it used to be
    vacuously true: ReplayResult had no such field, so every call to record_run
    passed the default 0. A flow that needed hands is not a flow that has earned
    fewer of them, and the gate has to be able to say so."""
    from pcx.artifact.schema import Evidence
    from pcx.crystallize.lifecycle import evaluate_promotion

    capability = _capability()
    capability.crystallization.evidence = Evidence(
        successful_runs=5, action_sequence_stability=1.0, human_interventions=0
    )
    assert evaluate_promotion(capability).eligible

    capability.crystallization.evidence.human_interventions = 1
    gate = evaluate_promotion(capability)
    assert not gate.eligible
    assert any("human interventions 1" in r for r in gate.reasons)


def test_the_replay_result_carries_the_intervention_count():
    from pcx.replay.outcomes import ReplayResult

    result = ReplayResult(run_id="r", capability="c", version=1, digest="d", status="success")
    assert result.human_interventions == 0
    result.human_interventions += 1
    assert result.human_interventions == 1


def test_auto_promotion_is_on_by_default_and_can_be_turned_off(monkeypatch):
    from pcx.config import Settings

    monkeypatch.delenv("PCX_AUTO_PROMOTE", raising=False)
    assert Settings().auto_promote is True

    monkeypatch.setenv("PCX_AUTO_PROMOTE", "0")
    assert Settings().auto_promote is False
