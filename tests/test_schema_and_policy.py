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
    # The overlay no longer rewrites the step's recorded timeout. It used to, and
    # that put the timing multiplier inside the digest -- see
    # test_a_timing_multiplier_does_not_change_the_digest for why that was wrong.
    # The multiplier is applied to the wait the engine actually performs.
    assert specialized.steps[0].timeout_ms == base.steps[0].timeout_ms


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


# --------------------------------------------------------------------------- #
# Resolving a discovery run's inputs
#
# The failure this guards against is specific and was hit for real: a credential
# interpolated by the shell (`--param password="$VAR"`) is empty when the variable
# is unset, and it is *always* unset for a value that lives in .env, because .env
# is read by this process and not by the shell. The old code passed the empty
# string through, launched a browser and spent a model call before the agent
# reported it could not sign in.
# --------------------------------------------------------------------------- #


def test_a_literal_parameter_is_taken_as_given():
    from pcx.cli import _resolve_parameters

    assert _resolve_parameters(["member_number=10000001"], None) == {"member_number": "10000001"}


def test_an_empty_parameter_is_refused_before_anything_is_launched():
    from pcx.cli import _resolve_parameters

    with pytest.raises(SystemExit) as excinfo:
        _resolve_parameters(["password="], None)
    message = str(excinfo.value)
    assert "empty string" in message
    assert "--secret password=" in message  # the message must name the fix


def test_a_bare_parameter_resolves_from_the_conventional_variable(monkeypatch):
    """`--param username` means "the value is $PCX_USERNAME" -- the same
    convention the replay engine already uses to re-authenticate."""
    from pcx.cli import _resolve_parameters

    monkeypatch.setenv("PCX_USERNAME", "tomsmith")
    assert _resolve_parameters(["username"], None) == {"username": "tomsmith"}


def test_a_bare_parameter_with_no_variable_names_the_variable_it_wanted(monkeypatch):
    from pcx.cli import _resolve_parameters

    monkeypatch.delenv("PCX_USERNAME", raising=False)
    with pytest.raises(SystemExit) as excinfo:
        _resolve_parameters(["username"], None)
    assert "$PCX_USERNAME" in str(excinfo.value)


def test_a_secret_is_passed_by_the_name_of_a_variable_not_by_value(monkeypatch):
    from pcx.cli import _resolve_parameters

    monkeypatch.setenv("PCX_PARABANK_PASSWORD", "s3cret")
    assert _resolve_parameters(None, ["password=PCX_PARABANK_PASSWORD"]) == {"password": "s3cret"}


def test_a_secret_without_a_variable_name_is_refused():
    """`--secret password` is a plausible typo for `--param password`, and
    silently accepting it would be the one case where a secret *does* end up on
    the command line."""
    from pcx.cli import _resolve_parameters

    with pytest.raises(SystemExit) as excinfo:
        _resolve_parameters(None, ["password"])
    assert "not the secret" in str(excinfo.value)


def test_an_unset_secret_variable_is_reported_by_name(monkeypatch):
    from pcx.cli import _resolve_parameters

    monkeypatch.delenv("PCX_PARABANK_PASSWORD", raising=False)
    with pytest.raises(SystemExit) as excinfo:
        _resolve_parameters(None, ["password=PCX_PARABANK_PASSWORD"])
    assert "$PCX_PARABANK_PASSWORD" in str(excinfo.value)


def test_every_missing_input_is_reported_at_once(monkeypatch):
    """Reporting one at a time turns a two-variable mistake into two round trips
    through a browser launch."""
    from pcx.cli import _resolve_parameters

    monkeypatch.delenv("PCX_PARABANK_USER", raising=False)
    monkeypatch.delenv("PCX_PARABANK_PASSWORD", raising=False)
    with pytest.raises(SystemExit) as excinfo:
        _resolve_parameters(
            ["last_name=Conway"],
            ["username=PCX_PARABANK_USER", "password=PCX_PARABANK_PASSWORD"],
        )
    message = str(excinfo.value)
    assert "PCX_PARABANK_USER" in message and "PCX_PARABANK_PASSWORD" in message


# --------------------------------------------------------------------------- #
# Where a capability's credentials come from at run time
#
# `--setup sign_on_x` used to resolve its inputs from two hard-coded CoreServ
# variables, so it worked for exactly one application and failed INVALID_INPUT
# for every other -- after driving a browser to the sign-on screen.
# --------------------------------------------------------------------------- #


def _sign_on(capability_id: str = "sign_on_parabank") -> Capability:
    return make_capability(
        id=capability_id,
        contract=Contract(
            summary="sign on",
            inputs=[
                Param(name="username", sensitivity="pii_reference"),
                Param(name="password", sensitivity="secret"),
            ],
            outputs=[Param(name="signed_in_customer")],
            outcomes=[Outcome(code="OK", kind="success")],
        ),
    )


def test_a_credential_is_looked_up_scoped_before_generic():
    from pcx.config import credential_env_candidates

    assert credential_env_candidates("sign_on_parabank", "username") == [
        "PCX_SIGN_ON_PARABANK_USERNAME",
        "PCX_USERNAME",
    ]


def test_two_sign_ons_do_not_share_one_credential(monkeypatch):
    """The reason the scoped name exists: two institutions both have a
    `username`, and the generic variable would hand one site's credential to the
    other without anything looking wrong."""
    from pcx.replay.engine import _credentials_for

    monkeypatch.setenv("PCX_SIGN_ON_PARABANK_USERNAME", "parabank_user")
    monkeypatch.setenv("PCX_SIGN_ON_THE_INTERNET_USERNAME", "tomsmith")
    monkeypatch.delenv("PCX_USERNAME", raising=False)

    assert _credentials_for(_sign_on("sign_on_parabank"))["username"] == "parabank_user"
    assert _credentials_for(_sign_on("sign_on_the_internet"))["username"] == "tomsmith"


def test_the_generic_name_still_works_when_nothing_is_scoped(monkeypatch):
    """`PCX_OPERATOR_ID` has to keep working for sign_on_coreserv."""
    from pcx.replay.engine import _credentials_for

    monkeypatch.delenv("PCX_SIGN_ON_PARABANK_USERNAME", raising=False)
    monkeypatch.setenv("PCX_USERNAME", "shared_identity")

    assert _credentials_for(_sign_on())["username"] == "shared_identity"


def test_a_scoped_name_beats_the_generic_one(monkeypatch):
    from pcx.replay.engine import _credentials_for

    monkeypatch.setenv("PCX_SIGN_ON_PARABANK_USERNAME", "specific")
    monkeypatch.setenv("PCX_USERNAME", "generic")

    assert _credentials_for(_sign_on())["username"] == "specific"


def test_missing_credentials_are_named_as_variables_not_as_inputs(monkeypatch):
    """"input 'username' is required" tells the reader nothing they can act on.
    The names of the variables that would have worked do."""
    from pcx.replay.engine import missing_credentials_for

    for name in ("PCX_SIGN_ON_PARABANK_USERNAME", "PCX_USERNAME",
                 "PCX_SIGN_ON_PARABANK_PASSWORD", "PCX_PASSWORD"):
        monkeypatch.delenv(name, raising=False)

    report = "\n".join(missing_credentials_for(_sign_on()))
    assert "$PCX_SIGN_ON_PARABANK_USERNAME" in report
    assert "$PCX_SIGN_ON_PARABANK_PASSWORD" in report
    assert "$PCX_USERNAME" in report  # the fallback is offered too


def test_nothing_is_missing_once_the_variables_are_set(monkeypatch):
    from pcx.replay.engine import missing_credentials_for

    monkeypatch.setenv("PCX_SIGN_ON_PARABANK_USERNAME", "u")
    monkeypatch.setenv("PCX_SIGN_ON_PARABANK_PASSWORD", "p")
    assert missing_credentials_for(_sign_on()) == []


def test_the_short_param_form_uses_the_same_lookup_as_replay(monkeypatch):
    """A capability recorded with `--param username` must be replayable from the
    same variable. Two conventions would mean discovery and replay disagree about
    where a credential lives."""
    from pcx.cli import _resolve_parameters

    monkeypatch.setenv("PCX_SIGN_ON_PARABANK_USERNAME", "parabank_user")
    monkeypatch.delenv("PCX_USERNAME", raising=False)

    assert _resolve_parameters(["username"], None, capability_id="sign_on_parabank") == {
        "username": "parabank_user"
    }


# --------------------------------------------------------------------------- #
# Which host a `--setup` capability runs against
#
# A setup capability prepares the surface the discovery run is about to use, so
# it belongs on that run's host. Binding it to the default deployment meant
# `--setup sign_on_parabank --entrypoint https://parabank.parasoft.com/...`
# navigated to http://127.0.0.1:8799, and the only symptom was a connection
# refused to a port that appears nowhere in the command.
# --------------------------------------------------------------------------- #


def test_origin_keeps_scheme_and_host_and_drops_the_path():
    from pcx.runner import origin_of

    assert origin_of("https://parabank.parasoft.com/parabank/overview.htm") == "https://parabank.parasoft.com"
    assert origin_of("http://127.0.0.1:8799/desk") == "http://127.0.0.1:8799"


def test_setup_binds_to_the_runs_host_not_the_default_deployment():
    from pcx.config import Settings
    from pcx.runner import bind_default_tenant, origin_of

    capability = make_capability(
        id="sign_on_parabank",
        surface=SurfaceBinding(entrypoint="{{ tenant.base_url }}/parabank/index.htm"),
    )
    entrypoint = "https://parabank.parasoft.com/parabank/overview.htm"
    bound = bind_default_tenant(capability, Settings(), origin_of(entrypoint))

    assert bound.resolved_entrypoint() == "https://parabank.parasoft.com/parabank/index.htm"


def test_setup_still_falls_back_to_the_default_deployment():
    """With a localhost entry point the behaviour is unchanged."""
    from pcx.config import Settings
    from pcx.runner import bind_default_tenant

    settings = Settings()
    bound = bind_default_tenant(make_capability(), settings)
    assert bound.resolved_entrypoint().startswith(settings.base_url)


def test_a_tenant_overlay_supplies_the_host_without_changing_the_artifact():
    capability = make_capability(
        surface=SurfaceBinding(entrypoint="{{ tenant.base_url }}/parabank/index.htm"),
    )
    bound = capability.specialize(
        TenantOverlay(tenant_id="parabank", base_url="https://parabank.parasoft.com",
                      timing_multiplier=2.5)
    )
    assert bound.resolved_entrypoint() == "https://parabank.parasoft.com/parabank/index.htm"
    assert bound.digest() == capability.digest()  # binding is not a change to the flow


def test_a_timing_multiplier_does_not_change_the_digest():
    """It used to. step.timeout_ms is inside the digest, so scaling it made a
    tenant that merely needs longer waits look like a different capability --
    and since the replay engine stamps each ledger row with the *specialized*
    digest while the lifecycle queries the base one, every run against such a
    tenant was written somewhere the promotion gates never look. The
    multi-tenant claim is one artifact and one track record across tenants; a
    latency budget must not split it."""
    capability = make_capability()
    slow = capability.specialize(
        TenantOverlay(tenant_id="parabank", base_url="https://parabank.parasoft.com",
                      timing_multiplier=2.5)
    )
    fast = capability.specialize(
        TenantOverlay(tenant_id="local", base_url="http://127.0.0.1:8799", timing_multiplier=1.0)
    )
    assert slow.digest() == fast.digest() == capability.digest()
    assert slow.steps[0].timeout_ms == capability.steps[0].timeout_ms


def test_the_engine_scales_the_wait_it_actually_uses():
    """The budget still has to grow -- it is applied at run time instead."""
    from pcx.replay.engine import ReplayEngine

    engine = ReplayEngine.__new__(ReplayEngine)
    engine._timing = 2.5
    assert engine._budget(15000) == 37500

    engine._timing = 1.0
    assert engine._budget(15000) == 15000


# --------------------------------------------------------------------------- #
# Finding a profile from the run's own entry point
#
# `--profile` defaulted to "coreserv-7.2". Pointing the system at any other site
# and forgetting the flag silently applied a mock credit union's error
# vocabulary, interstitials, session-expiry detectors and irreversible-control
# patterns to a site they describe nothing about -- and a profile that describes
# the wrong application is worse than no profile, because replay believes it.
# --------------------------------------------------------------------------- #


def _profiles(tmp_path, **by_name):
    import yaml

    for name, data in by_name.items():
        (tmp_path / f"{name}.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    return tmp_path


def test_a_profile_is_found_by_the_host_it_claims(tmp_path):
    from pcx.artifact.profiles import for_host

    _profiles(
        tmp_path,
        bank={"hosts": ["parabank.parasoft.com"],
              "app": {"vendor": "Parasoft", "product": "ParaBank", "version": "3.0"}},
        local={"hosts": ["127.0.0.1:8799"],
               "app": {"vendor": "Meridian", "product": "CoreServ", "version": "7.2"}},
    )
    assert for_host("parabank.parasoft.com", tmp_path).app.product == "ParaBank"
    assert for_host("127.0.0.1:8799", tmp_path).app.product == "CoreServ"


def test_an_unclaimed_host_gets_no_profile_rather_than_someone_elses(tmp_path):
    from pcx.artifact.profiles import for_host

    _profiles(
        tmp_path,
        bank={"hosts": ["parabank.parasoft.com"],
              "app": {"vendor": "Parasoft", "product": "ParaBank", "version": "3.0"}},
    )
    assert for_host("shop.example.com", tmp_path) is None


def test_a_host_matches_a_parent_domain_only_on_a_dot_boundary(tmp_path):
    """`parasoft.com` should claim `parabank.parasoft.com`. It must not claim
    `notparasoft.com`, and a profile listing `com` must not claim everything."""
    from pcx.artifact.profiles import for_host

    _profiles(
        tmp_path,
        vendor={"hosts": ["parasoft.com"],
                "app": {"vendor": "Parasoft", "product": "Everything", "version": "1"}},
    )
    assert for_host("parabank.parasoft.com", tmp_path) is not None
    assert for_host("notparasoft.com", tmp_path) is None
    assert for_host("parasoft.com.evil.example", tmp_path) is None


def test_the_port_is_part_of_a_local_host_but_a_bare_name_still_matches(tmp_path):
    from pcx.artifact.profiles import for_host

    _profiles(
        tmp_path,
        local={"hosts": ["127.0.0.1:8799"],
               "app": {"vendor": "Meridian", "product": "CoreServ", "version": "7.2"}},
    )
    assert for_host("127.0.0.1:8799", tmp_path) is not None
    assert for_host("127.0.0.1:9999", tmp_path) is None


def test_every_shipped_profile_declares_its_hosts():
    """A profile with no hosts can only be reached by remembering a flag."""
    from pcx.artifact.profiles import load_all

    for profile in load_all():
        assert profile.hosts, f"{profile.app.key()} declares no hosts"


def test_replay_fills_a_missing_required_input_from_the_environment(monkeypatch):
    """`pcx replay sign_on_parabank` should not need the password on the command
    line. Re-auth and `--setup` already take it from the environment; making the
    top-level call the one place it has to be typed puts it in shell history for
    no reason."""
    from pcx.runner import _fill_from_environment

    monkeypatch.setenv("PCX_SIGN_ON_PARABANK_PASSWORD", "from-dotenv")
    capability = make_capability(
        id="sign_on_parabank",
        contract=Contract(
            summary="sign on",
            inputs=[Param(name="username"), Param(name="password", sensitivity="secret")],
            outputs=[Param(name="signed_in_customer")],
            outcomes=[Outcome(code="OK", kind="success")],
        ),
    )
    filled = _fill_from_environment(capability, {"username": "typed-explicitly"})
    assert filled["password"] == "from-dotenv"
    assert filled["username"] == "typed-explicitly", "an explicit input always wins"


def test_replay_does_not_invent_values_for_optional_inputs(monkeypatch):
    from pcx.runner import _fill_from_environment

    monkeypatch.setenv("PCX_DEMO_CAPABILITY_NOTE", "surprise")
    capability = make_capability(
        contract=Contract(
            summary="demo",
            inputs=[Param(name="note", required=False)],
            outputs=[Param(name="savings_balance", type="money")],
            outcomes=[Outcome(code="OK", kind="success")],
        ),
    )
    assert "note" not in _fill_from_environment(capability, {})
