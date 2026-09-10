"""Contract validation, guardrails, redaction, tenancy and the condition language."""

from __future__ import annotations

import os

import pytest

from pcx.artifact.schema import (
    Capability,
    Checkpoint,
    Condition,
    Contract,
    Outcome,
    Param,
    Policy,
    Step,
    SurfaceBinding,
    TenantOverlay,
    parse_money,
)
from pcx.artifact.locator import Locator, Strategy
from pcx.policy.allowlist import DeploymentPolicy, PolicyEngine
from pcx.policy.redact import Redactor, mask_value
from pcx.surfaces.model import Observation, Rect, Role, UIElement


def make_capability(**overrides) -> Capability:
    base = dict(
        id="demo_capability",
        surface=SurfaceBinding(entrypoint="{{ tenant.base_url }}/desk"),
        contract=Contract(
            summary="demo",
            inputs=[Param(name="member_number", type="string", pattern=r"\d{8}", sensitivity="pii_reference")],
            outputs=[Param(name="savings_balance", type="money")],
            outcomes=[Outcome(code="OK", kind="success")],
        ),
        steps=[
            Step(
                id="s1", intent="click inquire", action="click",
                target=Locator(
                    description="INQUIRE button",
                    strategies=[Strategy(kind="role_name", role=Role.BUTTON, name="INQUIRE")],
                ),
            )
        ],
        checkpoint=Checkpoint(condition=Condition(text_present="MEMBER DETAIL")),
        policy=Policy(allowed_hosts=["127.0.0.1:8799"], allowed_routes=["^/desk$"]),
    )
    base.update(overrides)
    return Capability(**base)


# --------------------------------------------------------------------------- #
# Contract
# --------------------------------------------------------------------------- #


def test_inputs_are_validated_before_the_surface_is_touched():
    contract = make_capability().contract
    with pytest.raises(ValueError, match="does not match"):
        contract.validate_inputs({"member_number": "1234"})
    assert contract.validate_inputs({"member_number": "10000001"}) == {"member_number": "10000001"}


def test_unknown_inputs_are_rejected_not_ignored():
    contract = make_capability().contract
    with pytest.raises(ValueError, match="unknown input"):
        contract.validate_inputs({"member_number": "10000001", "sneaky": "x"})


def test_money_is_structured_not_a_string():
    assert parse_money("$18,425.63")["amount"] == 18425.63
    assert parse_money("-$11,208.44")["amount"] == -11208.44
    assert parse_money("(1,000.00)")["amount"] == -1000.0


def test_tool_schema_is_emitted_for_a_calling_agent():
    schema = make_capability().contract.to_tool_schema()
    assert schema["input_schema"]["properties"]["member_number"]["pattern"] == r"\d{8}"
    assert schema["input_schema"]["required"] == ["member_number"]


def test_templates_render_without_an_expression_engine():
    capability = make_capability()
    rendered = capability.render("id={{ inputs.member_number }}", {"member_number": "10000001"}, {})
    assert rendered == "id=10000001"
    # No expression evaluation is available to an artifact, by construction.
    assert capability.render("{{ __import__('os').getcwd() }}", {}, {}) == "{{ __import__('os').getcwd() }}"


# --------------------------------------------------------------------------- #
# Conditions
# --------------------------------------------------------------------------- #


def obs(text="", route="http://127.0.0.1:8799/desk", title="T", elements=None):
    return Observation(
        surface_kind="test", route=route, title=title,
        text_digest=text, elements=elements or [],
    )


def test_condition_explains_itself():
    ok, why = Condition(text_present="MBR-404").evaluate(obs("MBR-404 MEMBER NOT FOUND ON FILE"))
    assert ok and "MBR-404" in why


def test_route_pattern_matches_the_path_not_the_host():
    ok, _ = Condition(route_matches="^/desk$").evaluate(obs(route="http://any.host:9999/desk"))
    assert ok, "route patterns must be host-independent so they work for every tenant"


def test_composite_conditions():
    condition = Condition(
        all_of=[Condition(text_present="SYSTEM NOTICE"), Condition(text_present="ACKNOWLEDGE")]
    )
    assert condition.evaluate(obs("SYSTEM NOTICE ... ACKNOWLEDGE TO CONTINUE"))[0]
    assert not condition.evaluate(obs("SYSTEM NOTICE only"))[0]


def test_screen_signature_ignores_data_but_not_structure():
    def screen(balance):
        return obs(
            elements=[
                UIElement(ref="e1", role=Role.BUTTON, name="INQUIRE", rect=Rect(x=0, y=0, w=10, h=10)),
                UIElement(ref="e2", role=Role.CELL, name=balance, rect=Rect(x=0, y=20, w=10, h=10)),
            ]
        )
    assert screen("$1.00").signature() == screen("$99,999.00").signature()


# --------------------------------------------------------------------------- #
# Guardrails
# --------------------------------------------------------------------------- #


def deployment(**overrides) -> DeploymentPolicy:
    base = dict(
        allowed_hosts=["127.0.0.1:8799"],
        denied_routes=["^/admin"],
        irreversible_control_patterns=["POST TRANSACTION"],
    )
    base.update(overrides)
    return DeploymentPolicy(**base)


def test_host_outside_the_allowlist_is_refused():
    engine = PolicyEngine(deployment(), make_capability().policy)
    assert not engine.check_route("http://evil.example.com/desk").allowed


def test_denied_route_wins_over_capability_allowlist():
    engine = PolicyEngine(deployment(), Policy(allowed_routes=[".*"]))
    decision = engine.check_route("http://127.0.0.1:8799/admin/fault")
    assert not decision.allowed and decision.rule == "denied_routes"


def test_sub_frame_route_is_checked_too():
    """A frameset keeps its top-level URL while a child frame goes anywhere."""
    engine = PolicyEngine(deployment(), Policy(allowed_routes=["^/desk$"]))
    decision = engine.check_routes(
        "http://127.0.0.1:8799/desk", ["http://127.0.0.1:8799/admin/fault"]
    )
    assert not decision.allowed and decision.rule == "denied_routes"


def test_deployment_overrides_a_recording_that_claims_a_step_is_safe():
    engine = PolicyEngine(deployment(), make_capability().policy)
    step = Step(id="s9", intent="commit", action="click", risk="safe")
    assert engine.classify_risk(step, control_name="POST TRANSACTION button") == "irreversible"


def test_irreversible_step_requires_approval():
    engine = PolicyEngine(deployment(), Policy(allowed_routes=["^/desk$"], allowed_actions=["click"]))
    step = Step(id="s9", intent="commit", action="click", risk="irreversible")
    decision = engine.check_action(step, route="http://127.0.0.1:8799/desk", control_name="POST TRANSACTION")
    assert not decision.allowed and decision.requires_approval
    approved = engine.check_action(
        step, route="http://127.0.0.1:8799/desk", control_name="POST TRANSACTION", approved=True
    )
    assert approved.allowed


def test_capability_cannot_widen_the_deployment_policy():
    wide = Policy(allowed_hosts=["anything.example.com"], allowed_actions=["click", "navigate"])
    engine = PolicyEngine(deployment(allowed_actions=["click"]), wide)
    step = Step(id="s1", intent="go", action="navigate", url="http://anything.example.com/")
    assert not engine.check_action(step, route="http://127.0.0.1:8799/desk").allowed


# --------------------------------------------------------------------------- #
# Redaction
# --------------------------------------------------------------------------- #


def test_regulated_categories_are_scrubbed():
    redactor = Redactor()
    text = "TAX ID (SSN): 537-88-4821 for alice@example.com token=abc123xyz"
    scrubbed = redactor.scrub(text)
    assert "537-88-4821" not in scrubbed
    assert "alice@example.com" not in scrubbed
    assert "abc123xyz" not in scrubbed


def test_literal_secrets_are_scrubbed_wherever_they_appear():
    redactor = Redactor()
    redactor.add_literal("openSesame!42")
    assert "openSesame!42" not in redactor.scrub("typed openSesame!42 into the field")


def test_secret_keys_are_dropped_by_name():
    redactor = Redactor()
    out = redactor.scrub_obj({"password": "hunter2hunter2", "member": "10000001"})
    assert out["password"] == "[REDACTED]"


def test_sensitivity_masking_keeps_a_recognizable_tail_for_operators():
    assert mask_value("10000001", "pii_reference").endswith("0001")
    assert "10000001" not in mask_value("10000001", "pii_reference")
    assert mask_value("ALICE T. NGUYEN", "pii") == "[REDACTED:PII len=15]"
    assert mask_value("s3cret", "secret") == "[REDACTED]"


# --------------------------------------------------------------------------- #
# Tenancy
# --------------------------------------------------------------------------- #


def test_tenant_overlay_rebinds_without_mutating_the_base():
    base = make_capability()
    overlay = TenantOverlay(tenant_id="northgate_cu", base_url="https://cu2.example.com", timing_multiplier=2.0)
    specialized = base.specialize(overlay)
    assert specialized.resolved_entrypoint() == "https://cu2.example.com/desk"
    assert base.surface.entrypoint == "{{ tenant.base_url }}/desk", "base artifact must not be mutated"
    assert specialized.steps[0].timeout_ms == base.steps[0].timeout_ms * 2


def test_binding_a_tenant_does_not_change_the_artifact_identity():
    """One artifact, one track record, many institutions.

    Evidence is keyed on the digest. If binding a tenant changed the digest,
    every institution would start from zero accumulated evidence and no
    capability would ever be promoted.
    """
    base = make_capability()
    before = base.digest()
    bound = base.specialize(TenantOverlay(tenant_id="t", base_url="https://cu2.example.com"))
    assert bound.digest() == before


def test_tenant_vocabulary_override_rewrites_locator_anchors():
    base = make_capability(
        steps=[
            Step(
                id="s1", intent="type", action="type",
                target=Locator(
                    description="member field",
                    strategies=[Strategy(kind="label", role=Role.TEXTBOX, anchor_text="MEMBER NUMBER:")],
                ),
            )
        ]
    )
    specialized = base.specialize(
        TenantOverlay(tenant_id="t2", base_url="http://x", text_overrides={"MEMBER": "CUSTOMER"})
    )
    assert specialized.steps[0].target.strategies[0].anchor_text == "CUSTOMER NUMBER:"


def test_digest_ignores_provenance_and_evidence_but_tracks_the_flow():
    a = make_capability()
    before = a.digest()
    a.crystallization.evidence.successful_runs = 99
    a.steps[0].target.recorded = {"rect": {"x": 1}}
    assert a.digest() == before, "evidence and provenance are not part of the executable body"
    a.steps[0].action = "type"
    assert a.digest() != before, "a change to what the flow does must change the digest"


def test_digest_covers_step_intent_because_the_risk_classifier_reads_it():
    """``intent`` looks like a comment but is not one.

    ``PolicyEngine.classify_risk`` matches the deployment's irreversibility
    patterns against the control name *and the step intent*, so editing an intent
    can change whether a step needs approval. Anything that can move a guardrail
    belongs inside the digest.
    """
    a = make_capability()
    before = a.digest()
    a.steps[0].intent = "post the transaction to the ledger"
    assert a.digest() != before


def test_tenant_vocabulary_reaches_the_verification_pattern_too():
    """A rewritten anchor with an un-rewritten guard resolves and then rejects.

    This is the bug the second-tenant run actually hit: the locator found
    ``CUSTOMER INQUIRY`` and the verify block, still carrying ``MEMBER\\ INQUIRY``,
    threw it away. The override has to reach every field that names on-screen text.
    """
    from pcx.artifact.locator import Verify

    base = make_capability(
        steps=[
            Step(
                id="s1", intent="open inquiry", action="click",
                target=Locator(
                    description="MEMBER INQUIRY link",
                    strategies=[Strategy(kind="role_name", role=Role.LINK, name="MEMBER INQUIRY")],
                    verify=Verify(role=Role.LINK, name_pattern=r"MEMBER\ INQUIRY"),
                ),
                expect=Condition(text_present="MEMBER NUMBER:"),
            )
        ],
        checkpoint=Checkpoint(condition=Condition(text_present="MEMBER DETAIL")),
    )
    specialized = base.specialize(
        TenantOverlay(tenant_id="northgate_cu", base_url="http://x", text_overrides={"MEMBER": "CUSTOMER"})
    )
    target = specialized.steps[0].target
    assert target.strategies[0].name == "CUSTOMER INQUIRY"
    assert target.verify.name_pattern == r"CUSTOMER\ INQUIRY"
    assert specialized.steps[0].expect.text_present == "CUSTOMER NUMBER:"
    assert specialized.checkpoint.condition.text_present == "CUSTOMER DETAIL"


def test_outcome_detectors_are_keyed_on_codes_not_words():
    """Product error codes survive vocabulary drift; prose does not.

    ``MBR-404`` reads the same at every institution, which is why the profile
    detects outcomes by code rather than by the sentence next to it.
    """
    from pcx.artifact.profiles import AppProfile
    from pcx.config import settings

    profile = AppProfile.find("coreserv-7.2", settings.profiles_dir)
    assert profile is not None
    not_found = next(o for o in profile.outcomes if o.code == "MEMBER_NOT_FOUND")
    meridian = obs("MBR-404  MEMBER NOT FOUND ON FILE")
    northgate = obs("MBR-404  CUSTOMER NOT FOUND ON FILE")
    assert not_found.detect.evaluate(meridian)[0]
    assert not_found.detect.evaluate(northgate)[0]


# --------------------------------------------------------------------------- #
# .env loading
# --------------------------------------------------------------------------- #


def test_dotenv_parses_the_shapes_people_actually_write(tmp_path, monkeypatch):
    from pcx.config import load_dotenv

    env = tmp_path / ".env"
    env.write_text(
        "# a comment\n"
        "\n"
        "ANTHROPIC_API_KEY\n"                  # the common mistake: no '=' at all
        'PCX_OPERATOR_PASSWORD="openSesame!42"\n'
        "export PCX_MODEL=claude-x\n"
        "PCX_EMPTY=\n",
        encoding="utf-8",
    )
    for key in ("ANTHROPIC_API_KEY", "PCX_OPERATOR_PASSWORD", "PCX_MODEL", "PCX_EMPTY"):
        monkeypatch.delenv(key, raising=False)

    loaded = load_dotenv(env)

    assert "ANTHROPIC_API_KEY" not in loaded, (
        "a bare name with no value must not be set: an empty string would read as "
        "'a key is configured' to every check downstream"
    )
    assert "PCX_EMPTY" not in loaded
    assert os.environ["PCX_OPERATOR_PASSWORD"] == "openSesame!42", "quotes are stripped"
    assert os.environ["PCX_MODEL"] == "claude-x", "an `export ` prefix is tolerated"


def test_the_shell_wins_over_the_dotenv_file(tmp_path, monkeypatch):
    """A temporary override should not require editing the file."""
    from pcx.config import load_dotenv

    env = tmp_path / ".env"
    env.write_text("PCX_OPERATOR_ID=FROM_FILE\n", encoding="utf-8")
    monkeypatch.setenv("PCX_OPERATOR_ID", "FROM_SHELL")

    load_dotenv(env)
    assert os.environ["PCX_OPERATOR_ID"] == "FROM_SHELL"

    load_dotenv(env, override=True)
    assert os.environ["PCX_OPERATOR_ID"] == "FROM_FILE"


def test_missing_dotenv_is_not_an_error(tmp_path):
    from pcx.config import load_dotenv

    assert load_dotenv(tmp_path / "nope.env") == []
