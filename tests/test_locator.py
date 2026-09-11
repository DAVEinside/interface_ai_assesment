"""Locator resolution is pure, so it is tested against synthetic screens.

These are the cases that actually bite on a legacy surface: an input with no
accessible name, two controls that look alike, a value read out of a layout
table, and a screen that has moved under a recorded locator.
"""

from __future__ import annotations

import pytest

from pcx.artifact.locator import Locator, Strategy, Verify, resolve
from pcx.surfaces.model import Observation, Rect, Role, UIElement


def el(ref, role, name="", x=0, y=0, w=100, h=16, region="workframe", value=None, editable=False):
    return UIElement(
        ref=ref, role=role, name=name, value=value, editable=editable, region=region,
        rect=Rect(x=x, y=y, w=w, h=h),
    )


def screen(*elements) -> Observation:
    return Observation(surface_kind="test", route="http://x/y", title="T", elements=list(elements))


def test_label_finds_unnamed_input_to_the_right():
    obs = screen(
        el("e1", Role.CELL, "MEMBER NUMBER:", x=10, y=50, w=120),
        el("e2", Role.TEXTBOX, "", x=140, y=50, w=110, editable=True),
        el("e3", Role.TEXTBOX, "", x=140, y=90, w=110, editable=True),
    )
    loc = Locator(
        strategies=[Strategy(kind="label", role=Role.TEXTBOX, anchor_text="MEMBER NUMBER:")],
        verify=Verify(role=Role.TEXTBOX, editable=True),
    )
    res, element = resolve(loc, obs)
    assert res.ok and element.ref == "e2"
    assert not res.degraded


def test_role_name_ambiguity_fails_rather_than_guessing():
    obs = screen(
        el("e1", Role.BUTTON, "SUBMIT", x=10, y=10),
        el("e2", Role.BUTTON, "SUBMIT", x=200, y=10),
    )
    loc = Locator(strategies=[Strategy(kind="role_name", role=Role.BUTTON, name="SUBMIT")])
    res, element = resolve(loc, obs)
    assert not res.ok and element is None
    assert "ambiguous" in res.reason


def test_ambiguity_best_mode_acts_but_lowers_confidence():
    obs = screen(
        el("e1", Role.BUTTON, "SUBMIT", x=10, y=10),
        el("e2", Role.BUTTON, "SUBMIT", x=200, y=10),
    )
    loc = Locator(
        strategies=[Strategy(kind="role_name", role=Role.BUTTON, name="SUBMIT")],
        on_ambiguity="best",
    )
    res, element = resolve(loc, obs)
    assert res.ok and element.ref == "e1"
    assert res.confidence < 0.97


def test_row_cell_reads_the_intersection_of_row_and_column():
    obs = screen(
        el("e1", Role.CELL, "ACCOUNT TYPE", x=100, y=10, w=140),
        el("e2", Role.CELL, "CURRENT BALANCE", x=260, y=10, w=120),
        el("e3", Role.CELL, "SHARE SAVINGS", x=100, y=40, w=140),
        el("e4", Role.CELL, "$18,425.63", x=260, y=40, w=120),
        el("e5", Role.CELL, "SHARE DRAFT CHECKING", x=100, y=70, w=140),
        el("e6", Role.CELL, "$2,140.09", x=260, y=70, w=120),
    )
    loc = Locator(
        strategies=[
            Strategy(
                kind="row_cell", role=Role.CELL,
                anchor_text="SHARE SAVINGS", column_header="CURRENT BALANCE",
            )
        ],
        verify=Verify(role=Role.CELL),
    )
    res, element = resolve(loc, obs)
    assert res.ok and element.text == "$18,425.63"


def test_fallback_is_used_and_flagged_as_degraded():
    """A renamed button falls through to the positional strategy, and says so."""
    obs = screen(el("e1", Role.BUTTON, "SUBMIT INQUIRY", x=10, y=10))
    loc = Locator(
        strategies=[
            Strategy(kind="role_name", role=Role.BUTTON, name="INQUIRE"),
            Strategy(kind="ordinal", role=Role.BUTTON, region="workframe", index=0),
        ],
        verify=Verify(role=Role.BUTTON),
    )
    res, element = resolve(loc, obs)
    assert res.ok and element.ref == "e1"
    assert res.degraded, "a non-primary strategy firing must be reported as drift"
    assert res.strategy_kind == "ordinal"


def test_verify_rejects_a_plausible_but_wrong_match():
    """The 'best' candidate is discarded when it fails the guard."""
    obs = screen(el("e1", Role.BUTTON, "INQUIRE", x=10, y=10))
    loc = Locator(
        strategies=[Strategy(kind="role_name", role=Role.BUTTON, name="INQUIRE")],
        verify=Verify(role=Role.BUTTON, enabled=True, name_pattern="POST"),
    )
    res, _ = resolve(loc, obs)
    assert not res.ok
    assert "verify failed" in res.reason


def test_unresolved_locator_explains_every_attempt():
    obs = screen(el("e1", Role.LINK, "SOMETHING ELSE", x=10, y=10))
    loc = Locator(
        strategies=[
            Strategy(kind="role_name", role=Role.BUTTON, name="INQUIRE"),
            Strategy(kind="label", role=Role.TEXTBOX, anchor_text="MEMBER NUMBER:"),
        ]
    )
    res, _ = resolve(loc, obs)
    assert not res.ok
    assert "[0] role_name" in res.reason and "[1] label" in res.reason


@pytest.mark.parametrize("direction,expected", [("right", "e2"), ("below", "e3")])
def test_label_direction(direction, expected):
    obs = screen(
        el("e1", Role.CELL, "AMOUNT:", x=10, y=50, w=80),
        el("e2", Role.TEXTBOX, "", x=100, y=50, w=80, editable=True),
        el("e3", Role.TEXTBOX, "", x=10, y=90, w=80, editable=True),
    )
    loc = Locator(
        strategies=[
            Strategy(kind="label", role=Role.TEXTBOX, anchor_text="AMOUNT:", direction=direction)
        ],
        verify=Verify(role=Role.TEXTBOX, editable=True),
    )
    res, element = resolve(loc, obs)
    assert res.ok and element.ref == expected


def test_an_overridden_locator_that_matches_nothing_does_not_resolve():
    """The forcing mechanism in scripts/demo_live_handoff.py.

    Making a demo fail by handing the site a bad password makes it depend on
    that site's authentication behaviour -- ParaBank is deliberately insecure and
    accepts any password for a known username, so the run simply succeeds and
    there is no escalation to watch. A locator that no longer resolves is forced
    from inside this system, works anywhere, and is the failure the escalation
    path exists for: the flow was fine until the application moved.
    """
    page = screen(
        el("e20", Role.TEXTBOX, "Username", x=10, y=10, editable=True),
        el("e30", Role.BUTTON, "Log In", x=10, y=70),
    )
    recorded = Locator(
        description="Log In",
        strategies=[Strategy(kind="role_name", role=Role.BUTTON, name="Log In")],
    )
    moved = Locator(
        description="the sign-in control, as it was before the site was redesigned",
        strategies=[Strategy(kind="role_name", role=Role.BUTTON, name="Sign In To Your Account")],
    )

    res, element = resolve(recorded, page)
    assert res.ok and element.ref == "e30"

    res, element = resolve(moved, page)
    assert not res.ok and element is None


def test_a_broken_override_reaches_the_step_through_the_tenant_overlay():
    """It is applied with `step_locator_overrides` -- the same production
    mechanism that pins a tenant's differing control -- not a test-only hook."""
    from pcx.artifact.schema import (
        Capability, Checkpoint, Condition, Contract, Outcome, Param, Step,
        SurfaceBinding, TenantOverlay,
    )

    moved = Locator(
        description="gone",
        strategies=[Strategy(kind="role_name", role=Role.BUTTON, name="Sign In To Your Account")],
    )
    capability = Capability(
        id="sign_on_parabank",
        surface=SurfaceBinding(entrypoint="{{ tenant.base_url }}/parabank/index.htm"),
        contract=Contract(
            summary="sign on",
            inputs=[Param(name="username")],
            outputs=[Param(name="signed_in_customer")],
            outcomes=[Outcome(code="OK", kind="success")],
        ),
        steps=[
            Step(id="s3", intent="click Log In", action="click",
                 target=Locator(description="Log In",
                                strategies=[Strategy(kind="role_name", role=Role.BUTTON, name="Log In")]))
        ],
        checkpoint=Checkpoint(condition=Condition(text_present="Accounts Overview")),
    )
    overlay = TenantOverlay(
        tenant_id="parabank", base_url="https://parabank.parasoft.com",
        step_locator_overrides={"s3": moved},
    )
    bound = capability.specialize(overlay)

    assert bound.step("s3").target.description == "gone"
    assert capability.step("s3").target.description == "Log In", "the base artifact is untouched"
