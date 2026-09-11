"""Trace -> Capability.

The compiler is the crystallization step from the paper, made concrete: it reads
the *observed* record of a successful discovery run and emits a typed artifact
that can be executed without a model.

The interesting work is locator synthesis. For each action the compiler proposes
several independent hypotheses about how the control could be identified, then
**tests every one of them against the observation the action was taken in** and
keeps only those that resolve to exactly the element that was actually clicked.
A hypothesis that is ambiguous or wrong at record time is discarded rather than
written down and discovered to be broken in production.

Two rules keep recorded data out of the artifact:

* every value the agent typed that matches a declared input is replaced with
  ``{{ inputs.<name> }}``; a typed literal that matches nothing is a compile
  error, not something to bake in;
* locators for elements the flow *reads* (extraction targets, and the cells
  around them) must not be identified by their content, because that content is
  the answer and it changes per invocation. The compiler marks those targets
  dynamic and refuses to use value-bearing strategies for them.
"""

from __future__ import annotations

import re
from typing import Any

from ..agent.trace import RunTrace, TraceStep
from ..surfaces.model import Observation, Role, UIElement, normalize_text
from .locator import Locator, Strategy, Verify, resolve
from .profiles import AppProfile
from .schema import (
    AcceptanceTest,
    Capability,
    Checkpoint,
    Condition,
    Contract,
    Crystallization,
    Evidence,
    Outcome,
    Param,
    Policy,
    Recovery,
    RetryPolicy,
    Step,
    SurfaceBinding,
    money_like,
)

MONEY_SHAPE = r"^-?\(?\$?[0-9][0-9,]*\.[0-9]{2}\)?$"
DATE_SHAPE = r"^\d{4}-\d{2}-\d{2}$"

#: Characters a password field renders instead of its contents. Text made only
#: of these is a control's masked value, never a label.
MASK_CHARS = set("•*·●○•·")


def _is_usable_anchor(text: str, forbidden: set[str], volatile: tuple = ()) -> bool:
    """Reject anchors that are recorded data rather than page furniture.

    The first discovery run produced two locators anchored on ``TLR0042`` (an
    input value the agent had just typed) and on a row of bullet characters (the
    password field's mask). Both resolved perfectly at record time and both would
    be wrong on the very next invocation. Anchors have to be things the screen
    says on its own.
    """
    value = normalize_text(text)
    if not value or len(value) < 2:
        return False
    if set(value) <= MASK_CHARS | {" "}:
        return False
    upper = value.upper()
    if any(f and f.upper() in upper for f in forbidden):
        return False
    return not any(rx.search(value) for rx in volatile)


class AnchorRules:
    """What a locator anchor is allowed to be.

    ``forbidden``: literal values seen in this run (inputs typed, outputs read).
    ``volatile``: product-level patterns for text that changes between runs.
    A recording cannot distinguish furniture from data on its own -- it sees each
    screen exactly once -- so the second half is declared in the app profile.
    """

    def __init__(self, forbidden: set[str] | None = None, volatile: tuple = ()) -> None:
        self.forbidden = forbidden or set()
        self.volatile = volatile

    def usable(self, text: str) -> bool:
        return _is_usable_anchor(text, self.forbidden, self.volatile)


class CompileError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# Locator synthesis
# --------------------------------------------------------------------------- #


def _nearest_label(
    target: UIElement, elements: list[UIElement], direction: str, ctx: "AnchorRules"
) -> UIElement | None:
    """The text that a human would read as naming this control."""
    best: tuple[float, UIElement] | None = None
    for e in elements:
        if e.ref == target.ref or e.role not in (Role.TEXT, Role.CELL, Role.HEADING):
            continue
        if not ctx.usable(e.text):
            continue
        if direction == "right":  # label sits to the LEFT of the control
            if e.rect.x >= target.rect.x:
                continue
            if e.rect.vertical_overlap(target.rect) < 0.5:
                continue
            gap = target.rect.x - (e.rect.x + e.rect.w)
        else:  # label sits ABOVE the control
            if e.rect.y >= target.rect.y:
                continue
            if e.rect.horizontal_overlap(target.rect) < 0.3:
                continue
            gap = target.rect.y - (e.rect.y + e.rect.h)
        if gap < -4 or gap > 340:
            continue
        if best is None or gap < best[0]:
            best = (gap, e)
    return best[1] if best else None


def _row_anchor(target: UIElement, elements: list[UIElement], ctx: "AnchorRules") -> UIElement | None:
    """Leftmost cell sharing the target's row band -- the row's identity."""
    row = [
        e
        for e in elements
        if e.ref != target.ref
        and e.role in (Role.CELL, Role.TEXT)
        and e.rect.vertical_overlap(target.rect) >= 0.5
        and e.rect.x < target.rect.x
        and ctx.usable(e.text)
        and not money_like(e.text)
    ]
    if not row:
        return None
    # Prefer the most descriptive cell (longest label), not merely the leftmost:
    # on this product the row identity is 'SHARE SAVINGS', not the '0000' suffix.
    return max(row, key=lambda e: len(normalize_text(e.text)))


def _column_header(target: UIElement, elements: list[UIElement], ctx: "AnchorRules") -> UIElement | None:
    """Header cell above the target's column."""
    best: tuple[float, UIElement] | None = None
    for e in elements:
        if e.ref == target.ref or e.role not in (Role.CELL, Role.HEADING):
            continue
        if e.rect.y >= target.rect.y:
            continue
        if e.rect.horizontal_overlap(target.rect) < 0.3:
            continue
        if money_like(e.text) or not ctx.usable(e.text):
            continue
        gap = target.rect.y - e.rect.y
        if best is None or gap < best[0]:
            best = (gap, e)
    return best[1] if best else None


def _candidate_strategies(
    target: UIElement, elements: list[UIElement], *, dynamic: bool, ctx: "AnchorRules"
) -> list[Strategy]:
    """Propose hypotheses, most-semantic first. Nothing is validated yet."""
    proposals: list[Strategy] = []
    name = normalize_text(target.name)

    if name and not dynamic and ctx.usable(name):
        proposals.append(
            Strategy(
                kind="role_name",
                role=target.role,
                name=name,
                note="Accessible name -- the most portable identity available.",
            )
        )
    if target.role is Role.CELL and dynamic:
        anchor = _row_anchor(target, elements, ctx)
        header = _column_header(target, elements, ctx)
        if anchor is not None:
            proposals.append(
                Strategy(
                    kind="row_cell",
                    role=Role.CELL,
                    anchor_text=normalize_text(anchor.text),
                    column_header=normalize_text(header.text) if header else None,
                    region=target.region,
                    note=(
                        "Read the value at the intersection of the row identified by "
                        f"{normalize_text(anchor.text)!r} and the "
                        f"{normalize_text(header.text)!r} column."
                        if header
                        else "Read the value to the right of the row label."
                    ),
                )
            )
            proposals.append(
                Strategy(
                    kind="row_cell",
                    role=Role.CELL,
                    anchor_text=normalize_text(anchor.text),
                    text_pattern=MONEY_SHAPE if money_like(target.text) else None,
                    region=target.region,
                    rank=0,
                    note="Same row, first cell matching the expected value shape.",
                )
            )
    for direction in ("right", "below"):
        label = _nearest_label(target, elements, direction, ctx)
        if label is not None:
            proposals.append(
                Strategy(
                    kind="label",
                    role=target.role,
                    anchor_text=normalize_text(label.text),
                    direction=direction,
                    region=target.region,
                    note=(
                        f"The control immediately {direction} of the label "
                        f"{normalize_text(label.text)!r}. This product does not "
                        "associate labels with inputs, so adjacency is the only "
                        "semantic link available -- and it is the same link a "
                        "human uses."
                    ),
                )
            )
    if not dynamic and ctx.usable(target.text) and target.role in (Role.LINK, Role.BUTTON):
        proposals.append(
            Strategy(
                kind="text",
                role=target.role,
                text=normalize_text(target.text),
                note="Visible label text.",
            )
        )
    if target.role is Role.CELL and not dynamic:
        anchor = _row_anchor(target, elements, ctx)
        if anchor is not None:
            proposals.append(
                Strategy(
                    kind="row_cell",
                    role=Role.CELL,
                    anchor_text=normalize_text(anchor.text),
                    region=target.region,
                    note="Row-relative read.",
                )
            )

    # Last resort: position among peers of the same role in the same region.
    peers = sorted(
        [e for e in elements if e.role is target.role and e.region == target.region],
        key=lambda e: (round(e.rect.y / 6), e.rect.x),
    )
    if target.ref in [p.ref for p in peers]:
        proposals.append(
            Strategy(
                kind="ordinal",
                role=target.role,
                region=target.region,
                index=[p.ref for p in peers].index(target.ref),
                note=(
                    "Positional fallback. If this is the strategy that fires, the "
                    "screen has changed and the capability should be reviewed."
                ),
            )
        )
    return proposals


def _verify_for(target: UIElement, *, dynamic: bool) -> Verify:
    verify = Verify(role=target.role, enabled=True)
    if target.role is Role.TEXTBOX:
        verify.editable = True
    if dynamic:
        # Pin the *shape* of the value, never the value.
        if money_like(target.text):
            verify.text_pattern = MONEY_SHAPE
        elif re.fullmatch(DATE_SHAPE, normalize_text(target.text)):
            verify.text_pattern = DATE_SHAPE
    elif normalize_text(target.name):
        verify.name_pattern = re.escape(normalize_text(target.name))
    return verify


def synthesize_locator(
    target: UIElement,
    obs_elements: list[UIElement],
    *,
    dynamic: bool = False,
    description: str = "",
    rules: "AnchorRules | None" = None,
) -> Locator:
    """Build a locator and prove each strategy against the recorded screen."""
    recorded = Observation(
        surface_kind="recorded",
        route="",
        elements=obs_elements,
    )
    ctx = rules or AnchorRules()
    verify = _verify_for(target, dynamic=dynamic)
    kept: list[Strategy] = []
    rejected: list[str] = []

    for strategy in _candidate_strategies(target, obs_elements, dynamic=dynamic, ctx=ctx):
        probe = Locator(strategies=[strategy], verify=verify, on_ambiguity="fail")
        result, element = resolve(probe, recorded)
        if result.ok and element is not None and element.ref == target.ref:
            kept.append(strategy)
        else:
            reason = result.reason or (
                f"resolved to {element.label()} instead of the recorded control"
                if element is not None
                else "did not resolve"
            )
            rejected.append(f"{strategy.kind}: {_scrub_reason(reason, target, ctx, dynamic)}")

    if not kept:
        raise CompileError(
            f"no strategy uniquely identifies {target.label()} on the recorded screen; "
            f"rejected: {rejected}"
        )

    kept.sort(key=lambda s: -s.base_confidence())
    if not description:
        # A dynamic target's own text is the data being read, so it cannot be
        # part of how the control is described either.
        if dynamic:
            anchor = kept[0].anchor_text or kept[0].column_header or ""
            description = (
                f"{target.role.value} located by {kept[0].kind}"
                + (f" from {anchor!r}" if anchor else "")
            )
        else:
            description = target.label()
    return Locator(
        description=description,
        strategies=kept,
        verify=verify,
        on_ambiguity="fail",
        recorded={
            "role": target.role.value,
            # The *name* of a dynamic target is the data the flow reads back --
            # a member's balance or name. Provenance is for reviewers, not a
            # side door for regulated data into a file that gets committed.
            "name": None if dynamic else normalize_text(target.name),
            "region": target.region,
            "rect": target.rect.model_dump(),
            "rejected_strategies": rejected,
        },
    )


# --------------------------------------------------------------------------- #
# Parameterization and typing
# --------------------------------------------------------------------------- #


def _infer_param(name: str, value: str) -> Param:
    text = str(value)
    sensitivity = "internal"
    lowered = name.lower()
    if any(w in lowered for w in ("password", "secret", "token", "pin", "passphrase")):
        sensitivity = "secret"
    elif any(
        w in lowered
        for w in ("member", "account", "ssn", "tax", "customer", "operator", "teller", "user", "login")
    ):
        sensitivity = "pii_reference"

    if re.fullmatch(r"\d{4,}", text):
        return Param(
            name=name,
            type="string",
            pattern=rf"\d{{{len(text)}}}",
            description=f"{name.replace('_', ' ').title()} ({len(text)} digits).",
            sensitivity=sensitivity,
            example=None if sensitivity != "internal" else text,
        )
    if money_like(text):
        return Param(name=name, type="money", description=f"{name.replace('_', ' ')}.", sensitivity=sensitivity)
    return Param(
        name=name,
        type="string",
        description=f"{name.replace('_', ' ').title()}.",
        sensitivity=sensitivity,
        example=None if sensitivity != "internal" else text,
    )


def _infer_output(name: str, value: Any) -> tuple[Param, str]:
    text = normalize_text(str(value))
    lowered = name.lower()
    sensitivity = "pii" if any(w in lowered for w in ("name", "ssn", "dob", "birth", "address")) else "internal"
    if money_like(text):
        return Param(name=name, type="money", description=f"{name.replace('_', ' ').title()}.", sensitivity=sensitivity), "money"
    if re.fullmatch(DATE_SHAPE, text):
        return Param(name=name, type="date", description=f"{name.replace('_', ' ').title()}.", sensitivity=sensitivity), "date"
    return Param(name=name, type="string", description=f"{name.replace('_', ' ').title()}.", sensitivity=sensitivity), "raw"


def _parameterize(text: str | None, parameters: dict[str, str]) -> str | None:
    """Replace observed literals with input templates. Longest match first."""
    if text is None:
        return None
    out = text
    for name, value in sorted(parameters.items(), key=lambda kv: -len(str(kv[1]))):
        if value and str(value) in out:
            out = out.replace(str(value), f"{{{{ inputs.{name} }}}}")
    return out


# --------------------------------------------------------------------------- #
# Compilation
# --------------------------------------------------------------------------- #

_ACTION_MAP = {
    "click": "click",
    "type": "type",
    "select": "select",
    "press": "press",
    "navigate": "navigate",
    "wait": "wait",
    "extract": "extract",
}


def compile_capability(
    trace: RunTrace,
    *,
    capability_id: str,
    title: str = "",
    description: str = "",
    profile: AppProfile | None = None,
    base_url: str | None = None,
) -> Capability:
    """Turn a successful discovery trace into an executable capability."""
    if trace.status != "succeeded":
        raise CompileError(f"refusing to compile a {trace.status!r} run; only successful runs crystallize")

    actionable = [
        s
        for s in trace.steps
        if s.actor != "human"
        and s.action.kind in _ACTION_MAP
        and (s.outcome is None or s.outcome.ok)
        and not (s.action.kind == "wait" and (s.action.ms or 0) < 400)
    ]
    if not actionable:
        raise CompileError("trace contains no usable actions")

    # Values that appeared in this run and must never end up inside a locator:
    # the inputs the agent typed and the outputs it read back.
    rules = AnchorRules(
        forbidden={str(v) for v in trace.parameters.values() if str(v).strip()}
        | {str(v) for v in trace.outputs.values() if str(v).strip()},
        volatile=tuple(re.compile(p, re.I) for p in (profile.volatile_text if profile else [])),
    )

    steps: list[Step] = []
    used_actions: set[str] = set()
    routes: set[str] = set()
    output_parse: dict[str, str] = {}

    for n, tstep in enumerate(actionable, start=1):
        routes.add(_route_pattern(tstep.before.route))
        kind = _ACTION_MAP[tstep.action.kind]
        used_actions.add(kind)
        dynamic = tstep.action.kind == "extract"

        locator = None
        if tstep.target is not None:
            locator = synthesize_locator(
                tstep.target,
                tstep.context_elements,
                dynamic=dynamic,
                description=_describe(tstep),
                rules=rules,
            )

        value = _parameterize(tstep.action.text, trace.parameters)
        if kind in ("type", "select") and value is not None and value == tstep.action.text and tstep.action.text:
            raise CompileError(
                f"step {n} typed the literal {tstep.action.text!r}, which matches no declared input; "
                "declare it as a parameter or the capability would hard-code recorded data"
            )

        parse = "raw"
        if kind == "extract" and tstep.action.bind:
            _param, parse = _infer_output(tstep.action.bind, tstep.outcome.extracted if tstep.outcome else "")
            output_parse[tstep.action.bind] = parse

        step = Step(
            id=f"s{n}",
            intent=_clean_intent(tstep.thought or tstep.action.summary(), trace, rules),
            action=kind,  # type: ignore[arg-type]
            target=locator,
            value=value,
            keys=tstep.action.keys,
            url=tstep.action.url,
            ms=tstep.action.ms,
            bind=tstep.action.bind,
            parse=parse,  # type: ignore[arg-type]
            expect=_expectation(tstep),
            expect_navigation=bool(
                tstep.after and tstep.before and tstep.after.signature != tstep.before.signature
            ),
            signature_hint=tstep.after.signature if tstep.after else None,
            risk=_risk_of(tstep, profile),
            retry=RetryPolicy(),
        )
        steps.append(step)

    # The success condition is "I am on the right screen and the values I
    # promised to return are visible on it" -- expressed with the very locators
    # the extraction steps use, so a tenant overlay that renames a label fixes
    # the checkpoint at the same time it fixes the steps.
    final = actionable[-1].after or actionable[-1].before
    checks: list[Condition] = [Condition(route_matches=_route_pattern(final.route))]
    for step in steps:
        if step.action == "extract" and step.target is not None:
            checks.append(Condition(element_present=step.target))
    checkpoint = Checkpoint(
        id="final",
        description=(
            f"On {_route_pattern(final.route)} with every declared output visible."
        ),
        condition=Condition(all_of=checks),
        signature_hint=final.signature,
    )

    recovery = profile.recovery() if profile is not None else Recovery()
    first = actionable[0].before
    if recovery.session_expired is not None:
        starts_at_signon, _ = recovery.session_expired.evaluate(
            Observation(
                surface_kind=trace.surface_kind,
                route=first.route,
                title=first.title,
                text_digest=first.text_digest,
            )
        )
        if starts_at_signon:
            # This flow *begins* at the sign-on screen, so "we are at sign-on"
            # cannot mean the session expired -- and wiring re-auth in would make
            # the capability re-authenticate into itself.
            recovery = recovery.model_copy(update={"session_expired": None, "reauth_capability": None})

    inputs = [_infer_param(name, value) for name, value in trace.parameters.items()]
    outputs: list[Param] = []
    for name, value in trace.outputs.items():
        param, _parse = _infer_output(name, value)
        outputs.append(param)

    outcomes = [
        Outcome(
            code="OK",
            kind="success",
            description="The flow completed and every declared output was captured.",
            terminal=True,
        )
    ]
    if profile is not None:
        outcomes.extend(profile.outcomes)

    # The entry point must come out as `{{ tenant.base_url }}/path`, or the
    # artifact is bound to whichever institution it happened to be recorded
    # against -- which defeats the whole tenancy model.
    #
    # `base_url` is the caller's *default* deployment, and it is only the right
    # thing to strip when the run actually happened there. Recording against any
    # other host -- a second tenant, a staging instance, a live site -- has to
    # fall back to the entry point's own origin, or the host is silently baked in.
    entry_base = base_url if base_url and trace.entrypoint.startswith(base_url) else _origin(trace.entrypoint)
    entrypoint = trace.entrypoint.replace(entry_base, "{{ tenant.base_url }}")

    # The goal is free text a person typed, and people write "look up member
    # 10000001" -- so it goes through the same parameterization as everything
    # else before becoming the capability's public description.
    clean_goal = _clean_intent(trace.goal, trace, rules)

    capability = Capability(
        id=capability_id,
        version=1,
        title=title or capability_id.replace("_", " ").title(),
        description=description or clean_goal,
        created_by_run=trace.run_id,
        discovered_with_model=trace.model,
        author="llm_discovery",
        surface=SurfaceBinding(
            kind=trace.surface_kind,
            app=profile.app if profile is not None else _app_from(trace),
            entrypoint=entrypoint,
        ),
        contract=Contract(
            summary=clean_goal,
            inputs=inputs,
            outputs=outputs,
            outcomes=outcomes,
            side_effect=_side_effect(steps),
            idempotent=_side_effect(steps) == "read_only",
            typical_duration_ms=int(trace.duration_s() * 1000),
        ),
        preconditions=[],
        steps=steps,
        checkpoint=checkpoint,
        recovery=recovery,
        policy=Policy(
            # Deliberately empty. A capability describes what may be done and
            # where *within the application*; which host that application lives
            # on is a deployment and tenancy fact. Baking the recording host into
            # the artifact would make it one institution's artifact and would
            # change its digest every time it was bound to a tenant.
            allowed_hosts=[],
            allowed_routes=sorted(routes),
            allowed_actions=sorted(used_actions),  # least privilege: only what it used
            max_steps=len(steps) + 8,
            timeout_ms=max(60_000, int(trace.duration_s() * 3000)),
            irreversible_requires_approval=True,
            redact_patterns=list(profile.redact_patterns) if profile else [],
        ),
        crystallization=Crystallization(
            execution_type=3,
            status="candidate",
            evidence=Evidence(successful_runs=0, distinct_input_sets=0),
            acceptance_tests=[
                AcceptanceTest(
                    name="recorded_happy_path",
                    inputs={
                        name: value
                        for name, value in trace.parameters.items()
                        if _sensitivity_of(name, inputs) in ("public", "internal")
                    },
                    inputs_ref=(
                        "recorded_happy_path"
                        if any(
                            _sensitivity_of(name, inputs) not in ("public", "internal")
                            for name in trace.parameters
                        )
                        else None
                    ),
                    expect_outcome="OK",
                    expect_outputs_present=list(trace.outputs),
                    expect_output_matches={
                        name: MONEY_SHAPE
                        for name, parse in output_parse.items()
                        if parse == "money"
                    },
                )
            ],
        ),
    )
    return capability


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #


def _scrub_reason(reason: str, target: UIElement, ctx: "AnchorRules", dynamic: bool) -> str:
    """Keep rejected-hypothesis notes useful without persisting screen data.

    The notes are genuinely helpful in review -- they say why the compiler did
    not trust a strategy -- but they quote element labels, and on a data-bearing
    control the label *is* the member's balance.
    """
    cleaned = reason
    if dynamic:
        value = normalize_text(target.text)
        if value:
            cleaned = cleaned.replace(value, "<value>")
    for literal in sorted(ctx.forbidden, key=len, reverse=True):
        if literal and len(literal) >= 3:
            cleaned = cleaned.replace(literal, "<redacted>")
    return cleaned


def _clean_intent(text: str, trace: RunTrace, rules: "AnchorRules") -> str:
    """Sanitize the model's rationale before it becomes part of the artifact.

    The intent line is the most readable thing in a capability and the reason a
    reviewer can follow it -- but it is also free text a language model wrote
    while looking at a member's record. Input values become their template, and
    anything the run read back is replaced by the name of the output it was
    bound to. Nothing observed survives verbatim.
    """
    cleaned = _parameterize(text, trace.parameters) or ""
    for name, value in trace.outputs.items():
        literal = str(value).strip()
        if literal and len(literal) >= 3:
            cleaned = cleaned.replace(literal, f"<{name}>")
    for literal in sorted(rules.forbidden, key=len, reverse=True):
        if literal and len(literal) >= 3:
            cleaned = cleaned.replace(literal, "<redacted>")
    return cleaned.strip()


def _sensitivity_of(name: str, params: list[Param]) -> str:
    for param in params:
        if param.name == name:
            return param.sensitivity
    return "internal"


def _describe(tstep: TraceStep) -> str:
    if tstep.target is None:
        return tstep.action.kind
    if tstep.action.kind == "extract":
        # Naming an extraction target by its content would put the value in the
        # artifact. Let synthesize_locator describe it by how it is found.
        return ""
    name = normalize_text(tstep.target.name)
    return f"{tstep.target.role.value} {name!r}" if name else f"unlabelled {tstep.target.role.value}"


def _expectation(tstep: TraceStep) -> Condition | None:
    """Assert only what the action was supposed to achieve, portably.

    The obvious expectation -- "the screen signature must equal the one I
    recorded" -- is wrong for a multi-tenant artifact. A signature is built from
    control names, and two institutions running the same vendor build label
    those differently ("MEMBER INQUIRY" vs "CUSTOMER INQUIRY"), so the assertion
    would fail on tenant two for a reason that has nothing to do with the flow.

    What is portable is the *route* the step lands on and the fact that the
    screen changed at all. The recorded signature is kept as a hint and
    reported as drift.
    """
    if tstep.after is None or tstep.before is None:
        return None
    if tstep.after.signature == tstep.before.signature:
        return None
    return Condition(route_matches=_route_pattern(tstep.after.route))


def _risk_of(tstep: TraceStep, profile: AppProfile | None) -> str:
    name = f"{tstep.target.name if tstep.target else ''} {tstep.thought}".upper()
    if profile is not None:
        for pattern in profile.irreversible_controls:
            if re.search(pattern, name, re.I):
                return "irreversible"
    if tstep.action.kind in ("extract", "wait", "press"):
        return "safe"
    if tstep.action.kind in ("type", "select"):
        return "safe"
    if tstep.outcome is not None and tstep.outcome.navigated:
        return "reversible"
    return "safe"


def _side_effect(steps: list[Step]) -> str:
    if any(s.risk == "irreversible" for s in steps):
        return "irreversible_write"
    if any(s.risk == "reversible" for s in steps):
        return "reversible_write"
    return "read_only"


def _route_pattern(route: str) -> str:
    path = re.sub(r"^https?://[^/]+", "", route) or "/"
    return "^" + re.escape(path) + "$"


def _origin(url: str) -> str:
    match = re.match(r"^(https?://[^/]+)", url)
    return match.group(1) if match else url


def _host(origin: str) -> str:
    return re.sub(r"^https?://", "", origin).rstrip("/")


def _app_from(trace: RunTrace):
    from .schema import AppIdentity

    return AppIdentity(**{k: v for k, v in (trace.app or {}).items() if k in {"vendor", "product", "version"}})
