"""The guardrail engine: what the agent is permitted to do, checked every step.

Two layers, intersected and never unioned:

* a **deployment policy** (``policy.yaml``) -- what this installation permits at
  all, controlled by whoever runs the system;
* the **capability policy** carried inside each artifact -- what this particular
  flow needs.

An action is allowed only if both agree. That means a capability recorded
somewhere permissive cannot gain reach by being copied into a stricter
deployment, and a deployment cannot silently widen what a reviewed capability
does.

Checks happen *before* the action reaches the surface, against the surface's own
reported route rather than anything page content claims, and every decision --
allow or deny -- is written to the run log.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import yaml

from ..artifact.schema import Policy, RiskClass, Step


class PolicyViolation(RuntimeError):
    """Raised when an action is refused. Never caught and retried."""

    def __init__(self, message: str, *, rule: str) -> None:
        super().__init__(message)
        self.rule = rule


@dataclass
class Decision:
    allowed: bool
    rule: str
    detail: str = ""
    requires_approval: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "rule": self.rule,
            "detail": self.detail,
            "requires_approval": self.requires_approval,
        }


@dataclass
class DeploymentPolicy:
    """Installation-wide limits. Loaded from ``policy.yaml``."""

    allowed_hosts: list[str] = field(default_factory=list)
    denied_routes: list[str] = field(default_factory=list)
    allowed_actions: list[str] = field(
        default_factory=lambda: [
            "click", "type", "select", "press", "navigate", "wait", "extract", "assert",
        ]
    )
    max_steps: int = 60
    irreversible_requires_approval: bool = True
    #: Route regexes that are irreversible regardless of what a capability claims.
    irreversible_routes: list[str] = field(default_factory=list)
    #: Control names that are irreversible wherever they appear.
    irreversible_control_patterns: list[str] = field(default_factory=list)
    redact_patterns: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: str) -> "DeploymentPolicy":
        with open(path, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        unknown = set(raw) - known
        if unknown:
            raise ValueError(f"policy.yaml has unknown key(s): {sorted(unknown)}")
        return cls(**raw)


def _host_of(route: str) -> str:
    parsed = urlparse(route)
    return parsed.netloc or route


def _host_allowed(host: str, allowed: list[str]) -> bool:
    for entry in allowed:
        if host == entry:
            return True
        if entry.startswith("*.") and host.endswith(entry[1:]):
            return True
    return False


class PolicyEngine:
    """Evaluates every proposed action against both policy layers."""

    def __init__(
        self,
        deployment: DeploymentPolicy,
        capability: Policy | None = None,
        tenant_host: str | None = None,
    ) -> None:
        self.deployment = deployment
        self.capability = capability or Policy()
        #: When a run is bound to one tenant, that tenant's host is the ONLY host
        #: it may touch -- narrower than the deployment allowlist, which has to
        #: list every tenant. This is what stops a capability bound to one
        #: institution from following a link into another's instance.
        self.tenant_host = tenant_host

    # -- risk -----------------------------------------------------------------
    def classify_risk(self, step: Step, control_name: str = "") -> RiskClass:
        """Decide how dangerous an action is, taking the *stricter* of the views.

        A recorded step carries the recorder's opinion. The deployment carries
        the institution's. Where they disagree the institution wins, because the
        recorder was a language model.
        """
        declared: RiskClass = step.risk
        haystack = f"{control_name} {step.intent}".upper()
        for pattern in self.deployment.irreversible_control_patterns:
            if re.search(pattern, haystack, re.I):
                return "irreversible"
        for pattern in self.deployment.irreversible_routes:
            if step.url and re.search(pattern, step.url, re.I):
                return "irreversible"
        return declared

    # -- checks ---------------------------------------------------------------
    def check_routes(self, primary: str, others: list[str] | None = None) -> Decision:
        """Host and deny rules apply to every loaded location; the capability's
        own route whitelist applies to the primary document only.

        The asymmetry is deliberate. A capability records where it *worked*,
        which on a frameset application is one top-level URL for the whole flow;
        pinning every frame it happened to load would make the artifact brittle
        for no safety gain. Host and deny rules are the ones that actually
        contain the agent, so those are enforced everywhere.
        """
        for location in others or []:
            host = _host_of(location)
            if self.tenant_host and host != self.tenant_host:
                return Decision(
                    False, "tenant.host", f"sub-surface host {host!r} is not this run's tenant"
                )
            if self.deployment.allowed_hosts and not _host_allowed(host, self.deployment.allowed_hosts):
                return Decision(False, "deployment.allowed_hosts", f"sub-surface host {host!r} not permitted")
            path = urlparse(location).path or "/"
            for pattern in self.deployment.denied_routes + self.capability.denied_routes:
                if re.search(pattern, path):
                    return Decision(
                        False, "denied_routes", f"sub-surface route {path!r} matches deny /{pattern}/"
                    )
        return self.check_route(primary)

    def check_route(self, route: str) -> Decision:
        host = _host_of(route)
        if self.tenant_host and host != self.tenant_host:
            return Decision(
                False, "tenant.host", f"host {host!r} is not this run's tenant ({self.tenant_host})"
            )
        if self.deployment.allowed_hosts and not _host_allowed(host, self.deployment.allowed_hosts):
            return Decision(False, "deployment.allowed_hosts", f"host {host!r} not permitted")
        if self.capability.allowed_hosts and not _host_allowed(host, self.capability.allowed_hosts):
            return Decision(False, "capability.allowed_hosts", f"host {host!r} not in capability allowlist")

        path = urlparse(route).path or "/"
        for pattern in self.deployment.denied_routes + self.capability.denied_routes:
            if re.search(pattern, path):
                return Decision(False, "denied_routes", f"route {path!r} matches deny /{pattern}/")

        patterns = self.capability.allowed_routes or [".*"]
        if not any(re.search(p, path) for p in patterns):
            return Decision(
                False,
                "capability.allowed_routes",
                f"route {path!r} matches none of {patterns}",
            )
        return Decision(True, "route", f"{host}{path}")

    def check_action(
        self,
        step: Step,
        *,
        route: str,
        control_name: str = "",
        approved: bool = False,
        sub_routes: list[str] | None = None,
    ) -> Decision:
        if step.action not in self.deployment.allowed_actions:
            return Decision(False, "deployment.allowed_actions", f"action {step.action!r} not permitted")
        if step.action not in self.capability.allowed_actions:
            return Decision(False, "capability.allowed_actions", f"action {step.action!r} not in capability allowlist")

        route_decision = self.check_routes(route, sub_routes)
        if not route_decision.allowed:
            return route_decision

        if step.action == "navigate" and step.url:
            nav_decision = self.check_route(step.url)
            if not nav_decision.allowed:
                return Decision(False, f"navigate/{nav_decision.rule}", nav_decision.detail)

        risk = self.classify_risk(step, control_name)
        needs_approval = risk == "irreversible" and (
            self.deployment.irreversible_requires_approval
            or self.capability.irreversible_requires_approval
        )
        if needs_approval and not approved:
            return Decision(
                False,
                "risk.irreversible",
                f"step {step.id!r} is classified irreversible ({control_name or step.intent!r}) "
                "and requires human approval",
                requires_approval=True,
            )
        return Decision(True, "action", f"{step.action} risk={risk}")

    def check_budget(self, steps_taken: int) -> Decision:
        limit = min(self.deployment.max_steps, self.capability.max_steps)
        if steps_taken >= limit:
            return Decision(False, "max_steps", f"step budget of {limit} exhausted")
        return Decision(True, "max_steps", f"{steps_taken}/{limit}")

    def redact_patterns(self) -> list[str]:
        return list(self.deployment.redact_patterns) + list(self.capability.redact_patterns)
