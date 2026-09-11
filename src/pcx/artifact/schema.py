"""The capability artifact: a typed, versioned, agent-invocable flow.

What this is trying to be
-------------------------
Not a macro recording. A **tool definition with a body**. Everything a calling
agent needs to decide "should I call this, with what, and what will I get back"
lives in :class:`Contract`; everything the replay engine needs to actually do it
lives in :class:`Step` and :class:`Recovery`; everything a reviewer or a risk
officer needs lives in :class:`Policy` and :class:`Crystallization`.

Five decisions worth defending
------------------------------
1. **Outcomes are part of the contract, not exceptions.** ``MEMBER_NOT_FOUND``
   is a value the caller receives, with a documented meaning. Only genuinely
   broken things raise. A capability whose failure modes are undeclared cannot
   be safely invoked by another agent.
2. **One condition language.** Checkpoints, outcome detectors, recovery triggers
   and step expectations are all :class:`Condition`. There is exactly one thing
   to learn, one thing to test, and one thing to render in a review UI.
3. **Locators, not selectors, and always plural.** See :mod:`pcx.artifact.locator`.
4. **Tenant overlay instead of a copy.** ``Capability`` describes the vendor
   product; :class:`TenantOverlay` carries what one institution does differently.
   Hundreds of tenants on the same core platform share one artifact plus a small
   diff, and drift is measurable as "how big is the diff".
5. **Values are templates, never literals from the recording.** The compiler
   replaces every observed input value with ``{{ inputs.<name> }}``, so a
   recorded run cannot smuggle a real member number into a reusable artifact.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from urllib.parse import urlparse
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from ..surfaces.model import Observation, Role, normalize_text
from .locator import Locator

SCHEMA_VERSION = "pcx.capability/1.0"


# --------------------------------------------------------------------------- #
# Typed data
# --------------------------------------------------------------------------- #

ParamType = Literal["string", "integer", "number", "boolean", "money", "date", "enum"]

Sensitivity = Literal[
    "public",  # safe anywhere
    "internal",  # fine in logs, not in shared artifacts
    "pii_reference",  # an identifier for a person (member no.) -- masked in logs
    "pii",  # the person's actual data (name, SSN, DOB) -- never persisted raw
    "secret",  # credentials, tokens -- never persisted at all, in any form
]


class Param(BaseModel):
    """One typed input or output of the capability."""

    name: str
    type: ParamType = "string"
    description: str = ""
    required: bool = True
    pattern: str | None = Field(default=None, description="Regex the value must satisfy.")
    enum_values: list[str] | None = None
    default: Any = None
    sensitivity: Sensitivity = "internal"
    example: Any = Field(default=None, description="Non-sensitive example, for the calling agent.")

    @field_validator("name")
    @classmethod
    def _ident(cls, v: str) -> str:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,48}", v):
            raise ValueError(f"parameter name {v!r} must be snake_case")
        return v

    def coerce(self, raw: Any) -> Any:
        """Validate and normalize one supplied value. Raises ``ValueError``."""
        if raw is None:
            if self.required and self.default is None:
                raise ValueError(f"input {self.name!r} is required")
            return self.default
        text = str(raw).strip()
        if self.pattern and not re.fullmatch(self.pattern, text):
            raise ValueError(f"input {self.name!r} does not match /{self.pattern}/")
        if self.type == "integer":
            return int(text)
        if self.type == "number":
            return float(text)
        if self.type == "boolean":
            return text.lower() in {"1", "true", "yes", "y"}
        if self.type == "enum":
            allowed = self.enum_values or []
            if text not in allowed:
                raise ValueError(f"input {self.name!r} must be one of {allowed}")
            return text
        if self.type == "money":
            return parse_money(text)
        if self.type == "date":
            return text
        return text


def parse_money(text: str) -> dict[str, Any]:
    """``'-$11,208.44'`` -> ``{'amount': -11208.44, 'currency': 'USD', 'raw': ...}``.

    Money is a structured output type rather than a string because the caller is
    an agent that will do arithmetic or comparisons with it, and because
    ``$1,234.56`` vs ``1234.56`` is exactly the kind of formatting difference
    that varies between tenants of the same product.
    """
    raw = normalize_text(text)
    negative = raw.startswith("-") or (raw.startswith("(") and raw.endswith(")"))
    digits = re.sub(r"[^0-9.]", "", raw)
    if not digits:
        raise ValueError(f"cannot parse money from {text!r}")
    amount = float(digits)
    return {
        "amount": -amount if negative else amount,
        "currency": "USD",
        "raw": raw,
    }


PARSERS = {
    "raw": lambda s: normalize_text(s),
    "money": parse_money,
    "integer": lambda s: int(re.sub(r"[^0-9-]", "", s)),
    "date": lambda s: normalize_text(s),
}


# --------------------------------------------------------------------------- #
# Conditions -- one language for every predicate in the artifact
# --------------------------------------------------------------------------- #


class Condition(BaseModel):
    """A predicate over an :class:`~pcx.surfaces.model.Observation`.

    Composable (``all_of`` / ``any_of`` / ``none_of``) and, importantly,
    *explaining*: :meth:`evaluate` returns why it decided what it decided, which
    is what turns a failed checkpoint into a debuggable error rather than
    "assertion failed".
    """

    text_present: str | None = None
    text_absent: str | None = None
    text_matches: str | None = None
    title_matches: str | None = None
    route_matches: str | None = None
    screen_signature: str | None = None
    element_present: Locator | None = None
    element_absent: Locator | None = None
    all_of: list["Condition"] = Field(default_factory=list)
    any_of: list["Condition"] = Field(default_factory=list)
    none_of: list["Condition"] = Field(default_factory=list)

    def is_empty(self) -> bool:
        return not any(
            [
                self.text_present,
                self.text_absent,
                self.text_matches,
                self.title_matches,
                self.route_matches,
                self.screen_signature,
                self.element_present,
                self.element_absent,
                self.all_of,
                self.any_of,
                self.none_of,
            ]
        )

    def evaluate(self, obs: Observation) -> tuple[bool, str]:
        from .locator import resolve as _resolve  # local import: avoids a cycle

        checks: list[tuple[bool, str]] = []

        if self.text_present is not None:
            hit = obs.contains_text(self.text_present)
            checks.append((hit, f"text_present {self.text_present!r} -> {hit}"))
        if self.text_absent is not None:
            hit = not obs.contains_text(self.text_absent)
            checks.append((hit, f"text_absent {self.text_absent!r} -> {hit}"))
        if self.text_matches is not None:
            hit = bool(re.search(self.text_matches, obs.text_digest, re.I | re.S))
            checks.append((hit, f"text_matches /{self.text_matches}/ -> {hit}"))
        if self.title_matches is not None:
            hit = bool(re.search(self.title_matches, obs.title, re.I))
            checks.append((hit, f"title_matches /{self.title_matches}/ -> {hit}"))
        if self.route_matches is not None:
            # Patterns are written against the path (``^/desk$``) because that is
            # what is stable across tenants -- the host is per-institution. Test
            # the full location too, so an absolute pattern also works.
            path = urlparse(obs.route).path or "/"
            hit = bool(re.search(self.route_matches, path, re.I)) or bool(
                re.search(self.route_matches, obs.route, re.I)
            )
            checks.append(
                (hit, f"route_matches /{self.route_matches}/ -> {hit} (path={path})")
            )
        if self.screen_signature is not None:
            actual = obs.signature()
            hit = actual == self.screen_signature
            checks.append((hit, f"screen_signature {self.screen_signature} vs {actual} -> {hit}"))
        if self.element_present is not None:
            res, _ = _resolve(self.element_present, obs)
            checks.append((res.ok, f"element_present -> {res.brief()}"))
        if self.element_absent is not None:
            res, _ = _resolve(self.element_absent, obs)
            checks.append((not res.ok, f"element_absent -> {'absent' if not res.ok else 'PRESENT'}"))

        for sub in self.all_of:
            checks.append(sub.evaluate(obs))
        if self.any_of:
            results = [sub.evaluate(obs) for sub in self.any_of]
            checks.append(
                (
                    any(r[0] for r in results),
                    "any_of[" + "; ".join(r[1] for r in results) + "]",
                )
            )
        for sub in self.none_of:
            ok, why = sub.evaluate(obs)
            checks.append((not ok, f"none_of({why})"))

        if not checks:
            return True, "empty condition (vacuously true)"
        passed = all(c[0] for c in checks)
        return passed, "; ".join(c[1] for c in checks)


Condition.model_rebuild()


# --------------------------------------------------------------------------- #
# Contract
# --------------------------------------------------------------------------- #

SideEffect = Literal["read_only", "reversible_write", "irreversible_write"]
RiskClass = Literal["safe", "reversible", "irreversible"]


class Outcome(BaseModel):
    """A named, expected result the caller must be prepared to receive.

    ``terminal`` outcomes end the run. ``MEMBER_NOT_FOUND`` is terminal and is
    *not* a failure -- the distinction between "the system worked and the answer
    is no" and "the system broke" is the single most important thing a
    production caller needs, and it belongs in the contract.
    """

    code: str
    kind: Literal["success", "business", "denied"] = "business"
    description: str = ""
    detect: Condition | None = None
    terminal: bool = True
    retryable: bool = False
    remediation: str = Field(default="", description="What a caller (or human) should do about it.")

    @field_validator("code")
    @classmethod
    def _upper(cls, v: str) -> str:
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,40}", v):
            raise ValueError("outcome codes are SCREAMING_SNAKE_CASE")
        return v


class Contract(BaseModel):
    """What a calling agent sees. This is the tool signature."""

    summary: str = Field(description="One line, imperative: what invoking this does.")
    inputs: list[Param] = Field(default_factory=list)
    outputs: list[Param] = Field(default_factory=list)
    outcomes: list[Outcome] = Field(default_factory=list)
    side_effect: SideEffect = "read_only"
    idempotent: bool = True
    typical_duration_ms: int = 0

    def input_map(self) -> dict[str, Param]:
        return {p.name: p for p in self.inputs}

    def validate_inputs(self, supplied: dict[str, Any]) -> dict[str, Any]:
        """Coerce and check. Unknown keys are rejected, not ignored."""
        known = self.input_map()
        unknown = set(supplied) - set(known)
        if unknown:
            raise ValueError(f"unknown input(s): {sorted(unknown)}")
        return {name: p.coerce(supplied.get(name)) for name, p in known.items()}

    def to_tool_schema(self) -> dict[str, Any]:
        """JSON-Schema view, so this capability can be handed to an LLM as a tool."""
        json_types = {
            "string": "string",
            "integer": "integer",
            "number": "number",
            "boolean": "boolean",
            "money": "object",
            "date": "string",
            "enum": "string",
        }
        props: dict[str, Any] = {}
        required: list[str] = []
        for p in self.inputs:
            spec: dict[str, Any] = {"type": json_types[p.type], "description": p.description}
            if p.pattern:
                spec["pattern"] = p.pattern
            if p.enum_values:
                spec["enum"] = p.enum_values
            props[p.name] = spec
            if p.required:
                required.append(p.name)
        return {
            "name": "",
            "description": self.summary,
            "input_schema": {"type": "object", "properties": props, "required": required},
        }


# --------------------------------------------------------------------------- #
# Flow
# --------------------------------------------------------------------------- #

StepAction = Literal["click", "type", "select", "press", "navigate", "wait", "extract", "assert"]


class RetryPolicy(BaseModel):
    attempts: int = 2
    backoff_ms: int = 750
    settle_ms: int = 300


class Step(BaseModel):
    """One recorded action, plus what must be true before and after it."""

    id: str
    intent: str = Field(description="Plain-language purpose, for reviewers and escalation context.")
    action: StepAction
    target: Locator | None = None
    value: str | None = Field(
        default=None,
        description="Template, e.g. '{{ inputs.member_number }}'. Never a literal from the recording.",
    )
    keys: str | None = None
    url: str | None = None
    ms: int | None = None

    bind: str | None = Field(default=None, description="extract: output name to bind the value to.")
    parse: Literal["raw", "money", "integer", "date"] = "raw"

    precondition: Condition | None = Field(default=None, description="Checked before acting.")
    expect: Condition | None = Field(
        default=None,
        description=(
            "Checked after acting and settling. Must contain only *tenant-portable* "
            "assertions -- a route, a product error code, the presence of a control. "
            "See signature_hint for why the exact screen identity is not asserted."
        ),
    )
    expect_navigation: bool = Field(
        default=False,
        description="The recorded step changed the screen, so a replay that does not is wrong.",
    )
    signature_hint: str | None = Field(
        default=None,
        description=(
            "The screen signature observed when this step was recorded. NOT an "
            "assertion: a signature is built from control names, which differ "
            "between two institutions running the same product with different "
            "labels. Compared at replay time and reported as drift, never failed on."
        ),
    )
    risk: RiskClass = "safe"
    optional: bool = Field(default=False, description="A failure here is logged, not fatal.")
    retry: RetryPolicy = Field(default_factory=RetryPolicy)
    timeout_ms: int = 15000


class Checkpoint(BaseModel):
    """The success condition. A run that does not reach it did not succeed."""

    id: str = "final"
    description: str = ""
    condition: Condition
    signature_hint: str | None = Field(
        default=None, description="Recorded screen identity; a drift signal, not an assertion."
    )


class InterstitialRule(BaseModel):
    """A known screen that can legitimately appear at any point and be cleared."""

    name: str
    when: Condition
    dismiss: list[Step] = Field(default_factory=list)
    max_occurrences: int = 2
    note: str = ""


class Recovery(BaseModel):
    """Declarative handling for the runtime conditions this app actually produces."""

    interstitials: list[InterstitialRule] = Field(default_factory=list)
    transient: list[InterstitialRule] = Field(
        default_factory=list, description="Wait-and-retry rather than click-through."
    )
    session_expired: Condition | None = Field(
        default=None, description="Detects a bounce to sign-on; triggers re-auth or escalation."
    )
    reauth_capability: str | None = Field(
        default=None,
        description="Capability id to invoke to restore a session. Composition, not copy-paste.",
    )
    hard_error: Condition | None = Field(
        default=None, description="Application error page: stop immediately, do not retry."
    )


# --------------------------------------------------------------------------- #
# Policy
# --------------------------------------------------------------------------- #


class Policy(BaseModel):
    """Guardrails that travel *with* the capability.

    Shipping the allowlist inside the artifact rather than only in global config
    means a capability cannot be moved to a different deployment and quietly gain
    reach; the two are intersected at runtime, never unioned.
    """

    allowed_hosts: list[str] = Field(default_factory=list)
    allowed_routes: list[str] = Field(
        default_factory=lambda: [".*"], description="Regexes over the surface route."
    )
    allowed_actions: list[StepAction] = Field(
        default_factory=lambda: ["click", "type", "select", "press", "navigate", "wait", "extract", "assert"]
    )
    denied_routes: list[str] = Field(default_factory=list)
    max_steps: int = 40
    timeout_ms: int = 180_000
    irreversible_requires_approval: bool = True
    redact_patterns: list[str] = Field(
        default_factory=list, description="Extra tenant-specific regexes for the redactor."
    )


# --------------------------------------------------------------------------- #
# Crystallization
# --------------------------------------------------------------------------- #

ExecutionType = Literal[3, 2, 1]


class PromotionGates(BaseModel):
    """Evidence thresholds, mirroring the paper's Table II at demo scale.

    Defaults are deliberately smaller than the paper's (10 / 50 runs) so the
    lifecycle is observable in a demo. They are configuration, not design.
    """

    t3_to_t2_min_runs: int = 3
    t3_to_t2_min_sequence_stability: float = 0.90
    t2_to_t1_min_runs: int = 5
    t2_to_t1_min_resolution_stability: float = 0.99
    max_safety_violations: int = 0
    require_acceptance_tests: bool = True
    require_human_review_for_t1: bool = True


class AcceptanceTest(BaseModel):
    """Generated from successful traces; re-run before every promotion.

    Sensitive inputs are deliberately *not* stored here. A capability artifact is
    shared across tenants and lives in version control; a real member number in
    its acceptance tests would be regulated data checked into a git repository.
    Those values go to a local fixture file named by ``inputs_ref``, which stays
    on the machine that runs the tests.
    """

    name: str
    inputs: dict[str, Any] = Field(
        default_factory=dict, description="Non-sensitive inputs only."
    )
    inputs_ref: str | None = Field(
        default=None,
        description="Fixture key holding the sensitive inputs, resolved at test time.",
    )
    expect_outcome: str = "OK"
    expect_outputs_present: list[str] = Field(default_factory=list)
    expect_output_matches: dict[str, str] = Field(default_factory=dict)


class Evidence(BaseModel):
    successful_runs: int = 0
    failed_runs: int = 0
    distinct_input_sets: int = 0
    safety_violations: int = 0
    human_interventions: int = 0
    action_sequence_stability: float = 0.0
    resolution_stability: float = 0.0
    degraded_resolutions: int = 0
    drift_signals: int = Field(
        default=0,
        description=(
            "Screens whose identity differed from the recording. Expected and "
            "harmless across tenants with different labels; a spike within one "
            "tenant means the application changed."
        ),
    )
    tenants_seen: list[str] = Field(default_factory=list)
    successful_runs_since_demotion: int = 0
    failed_runs_since_demotion: int = 0
    last_promoted_at: str | None = None
    last_demoted_at: str | None = None
    last_demotion_reason: str | None = None
    human_reviewed: bool = False


class Crystallization(BaseModel):
    execution_type: ExecutionType = 3
    status: Literal["draft", "candidate", "active", "quarantined"] = "draft"
    gates: PromotionGates = Field(default_factory=PromotionGates)
    evidence: Evidence = Field(default_factory=Evidence)
    acceptance_tests: list[AcceptanceTest] = Field(default_factory=list)
    hybrid_steps: list[str] = Field(
        default_factory=list,
        description=(
            "Step ids that still consult the LLM in Type 2. The model is asked to "
            "*interpret* an unrecognized screen, never to choose an action."
        ),
    )


# --------------------------------------------------------------------------- #
# Surface binding and tenancy
# --------------------------------------------------------------------------- #


class AppIdentity(BaseModel):
    """Identifies the vendor product, which is what is actually shared at scale."""

    vendor: str = "unknown"
    product: str = "unknown"
    version: str = "unknown"

    def key(self) -> str:
        return f"{self.vendor}/{self.product}@{self.version}".lower()


class SurfaceBinding(BaseModel):
    kind: str = "web"
    adapter: str = "pcx.surfaces.web:WebSurface"
    app: AppIdentity = Field(default_factory=AppIdentity)
    entrypoint: str = Field(description="Template, e.g. '{{ tenant.base_url }}/inquiry'.")
    viewport: tuple[int, int] = (1280, 800)


class TenantOverlay(BaseModel):
    """Per-institution specialization of a shared capability.

    The reuse model: one artifact per *vendor product*, plus a small overlay per
    tenant. An overlay may change where the app lives, how long it takes, and how
    a handful of controls are named -- but it cannot add or reorder steps. If a
    tenant needs different steps, that is a genuinely different capability and
    should be recorded as one, which keeps the blast radius of a fork visible.
    """

    tenant_id: str
    base_url: str
    label: str = ""
    step_locator_overrides: dict[str, Locator] = Field(default_factory=dict)
    text_overrides: dict[str, str] = Field(
        default_factory=dict, description="Vocabulary swaps, e.g. 'MEMBER' -> 'CUSTOMER'."
    )
    timing_multiplier: float = 1.0
    disabled_steps: list[str] = Field(default_factory=list)
    notes: str = ""


# --------------------------------------------------------------------------- #
# The artifact
# --------------------------------------------------------------------------- #


class Capability(BaseModel):
    """A versioned, reviewable, agent-invocable flow."""

    schema_version: str = SCHEMA_VERSION
    id: str = Field(description="Stable slug, e.g. 'read_member_savings_balance'.")
    version: int = 1
    title: str = ""
    description: str = ""

    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    created_by_run: str | None = None
    discovered_with_model: str | None = None
    author: Literal["llm_discovery", "human", "compiler"] = "llm_discovery"

    surface: SurfaceBinding
    contract: Contract
    preconditions: list[Condition] = Field(default_factory=list)
    steps: list[Step] = Field(default_factory=list)
    checkpoint: Checkpoint
    recovery: Recovery = Field(default_factory=Recovery)
    policy: Policy = Field(default_factory=Policy)
    crystallization: Crystallization = Field(default_factory=Crystallization)
    tenant: TenantOverlay | None = None

    # -- identity -------------------------------------------------------------
    def digest(self) -> str:
        """Content hash over the executable parts only.

        Evidence counters and timestamps are excluded, so a capability that has
        merely accumulated successful runs keeps its digest -- which is what lets
        "did the flow change?" be answered independently of "how much do we trust
        it?".
        """
        payload = self.model_dump(
            mode="json",
            include={"id", "surface", "contract", "steps", "checkpoint", "recovery", "policy", "preconditions"},
        )
        blob = json.dumps(_strip_provenance(payload), sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(blob.encode()).hexdigest()[:32]

    def ref(self) -> str:
        return f"{self.id}@v{self.version}"

    def resolved_entrypoint(self, default_base_url: str = "") -> str:
        """Where this run actually starts, without editing the artifact."""
        base = self.tenant.base_url if self.tenant else default_base_url
        return (
            self.surface.entrypoint.replace("{{ tenant.base_url }}", base)
            .replace("{{tenant.base_url}}", base)
        )

    def step(self, step_id: str) -> Step | None:
        for s in self.steps:
            if s.id == step_id:
                return s
        return None

    def outcome(self, code: str) -> Outcome | None:
        for o in self.contract.outcomes:
            if o.code == code:
                return o
        return None

    # -- tenancy --------------------------------------------------------------
    def specialize(self, overlay: TenantOverlay) -> "Capability":
        """Return a tenant-bound copy. The base artifact is never mutated."""
        clone = self.model_copy(deep=True)
        clone.tenant = overlay
        # The entrypoint template is left as it is. `tenant` is outside the
        # digest, so binding an artifact to an institution does not change its
        # identity -- which is the whole point: one artifact, one track record,
        # many tenants. The template is resolved at run time by `render`.

        for step in clone.steps:
            if step.id in overlay.disabled_steps:
                step.optional = True
            override = overlay.step_locator_overrides.get(step.id)
            if override is not None:
                step.target = override
            if overlay.text_overrides:
                _apply_vocabulary(step.target, overlay.text_overrides)
                _apply_vocabulary(step.expect, overlay.text_overrides)
                _apply_vocabulary(step.precondition, overlay.text_overrides)
            # `timing_multiplier` is deliberately NOT applied here. Rewriting
            # step.timeout_ms would change the digest -- timings are inside it --
            # so a tenant that merely needs longer waits would look like a
            # different flow, and its runs would land in the ledger under a
            # digest the base artifact never queries. The multi-tenant claim is
            # "one artifact, one track record, many tenants"; splitting the track
            # record by latency budget silently breaks it. The engine scales the
            # budget at run time instead, where it belongs.

        if overlay.text_overrides:
            _apply_vocabulary(clone.checkpoint.condition, overlay.text_overrides)
            for rule in clone.recovery.interstitials + clone.recovery.transient:
                _apply_vocabulary(rule.when, overlay.text_overrides)
                for dismiss in rule.dismiss:
                    _apply_vocabulary(dismiss.target, overlay.text_overrides)

        # The tenant's host is NOT written into the policy block: doing so would
        # change the artifact's digest, and a capability's evidence is keyed on
        # its digest, so every tenant would start from zero track record. Host
        # restriction is applied at run time from `tenant.base_url` instead --
        # see PolicyEngine(tenant_host=...).
        return clone

    # -- templating -----------------------------------------------------------
    def render(self, template: str | None, inputs: dict[str, Any], outputs: dict[str, Any]) -> str | None:
        """Substitute ``{{ inputs.x }}`` / ``{{ outputs.y }}`` / ``{{ tenant.base_url }}``.

        Intentionally not Jinja: a capability artifact is untrusted-ish data that
        may have been produced by a model, and a template engine with expression
        evaluation would be an execution primitive hiding inside a data file.
        """
        if template is None:
            return None

        def sub(match: re.Match) -> str:
            scope, _, key = match.group(1).strip().partition(".")
            if scope == "inputs":
                return str(inputs.get(key, ""))
            if scope == "outputs":
                return str(outputs.get(key, ""))
            if scope == "tenant" and self.tenant is not None:
                return str(getattr(self.tenant, key, ""))
            return ""

        return re.sub(r"\{\{\s*([a-z_]+\.[a-z0-9_]+)\s*\}\}", sub, template)


#: Every string field in the schema that names something a screen says. A tenant
#: vocabulary override has to reach all of them: rewriting a locator's anchor but
#: not its verification pattern leaves the capability resolving the right control
#: and then rejecting it.
_VOCABULARY_FIELDS = {
    "name", "name_pattern", "text", "text_pattern", "anchor_text", "column_header",
    "text_present", "text_absent", "text_matches", "title_matches", "description",
}


def _apply_vocabulary(node: Any, overrides: dict[str, str]) -> None:
    """Rewrite institution-specific wording throughout a locator or condition tree."""
    if node is None or isinstance(node, (str, int, float, bool)):
        return
    if isinstance(node, (list, tuple)):
        for item in node:
            _apply_vocabulary(item, overrides)
        return
    if not isinstance(node, BaseModel):
        return
    for field in type(node).model_fields:
        value = getattr(node, field, None)
        if isinstance(value, str) and field in _VOCABULARY_FIELDS:
            rewritten = value
            for old, new in overrides.items():
                rewritten = re.sub(re.escape(old), new, rewritten, flags=re.I)
            if rewritten != value:
                setattr(node, field, rewritten)
        elif isinstance(value, (BaseModel, list, tuple)):
            _apply_vocabulary(value, overrides)


def _strip_provenance(value: Any) -> Any:
    """Remove ``Locator.recorded`` blocks before hashing.

    Provenance is for humans: the rect the recorder saw, which hypotheses it
    rejected. It has no effect on execution, so including it in the digest would
    mean an improvement to the *compiler's commentary* looked like a change to
    the flow -- bumping the version and discarding the capability's accumulated
    evidence for no behavioural reason.
    """
    if isinstance(value, dict):
        return {k: _strip_provenance(v) for k, v in value.items() if k != "recorded"}
    if isinstance(value, list):
        return [_strip_provenance(v) for v in value]
    return value


def money_like(text: str) -> bool:
    return bool(re.fullmatch(r"-?\(?\$?[0-9][0-9,]*\.[0-9]{2}\)?", normalize_text(text)))


__all__ = [
    "SCHEMA_VERSION",
    "AcceptanceTest",
    "AppIdentity",
    "Capability",
    "Checkpoint",
    "Condition",
    "Contract",
    "Crystallization",
    "Evidence",
    "InterstitialRule",
    "Outcome",
    "Param",
    "Policy",
    "PromotionGates",
    "Recovery",
    "RetryPolicy",
    "Role",
    "Step",
    "SurfaceBinding",
    "TenantOverlay",
    "money_like",
    "parse_money",
    "PARSERS",
]
