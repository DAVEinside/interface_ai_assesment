"""The replay result contract.

The single most important thing a production caller needs from an automated
flow is the difference between these three sentences:

  * "The system worked and the answer is $18,425.63."     -> ``success``
  * "The system worked and the answer is: no such member." -> ``business_outcome``
  * "The system did not work."                             -> ``failed``

Conflating the second with the third is what makes UI automation untrustworthy:
a caller that treats "record not found" as a crash retries forever, and a caller
that treats a crash as "not found" reports a wrong answer with confidence. So
the outcome is a declared, closed set on the capability, and this module is the
vocabulary for reporting which one happened -- plus enough detail on the failure
path (which step, what was expected, what was observed, where the screenshot is)
to debug without re-running.

``escalated`` is the fourth state, and it is not a failure either: it means the
run stopped deliberately and a human was asked.
"""

from __future__ import annotations

import time
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..artifact.locator import Resolution

ReplayStatus = Literal["success", "business_outcome", "escalated", "failed"]

#: Reserved outcome codes produced by the engine itself rather than the app.
INVALID_INPUT = "INVALID_INPUT"
CHECKPOINT_NOT_REACHED = "CHECKPOINT_NOT_REACHED"
ELEMENT_NOT_FOUND = "ELEMENT_NOT_FOUND"
POLICY_DENIED = "POLICY_DENIED"
#: Distinct from POLICY_DENIED on purpose. "A human must approve this step" is an
#: actionable result for the caller and a sign the guardrail worked; "this step is
#: outside the allowlist" is a sign something tried to do what it must not. Only
#: the second is a safety signal, and only the second should trip the breaker.
APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
APPLICATION_ERROR = "APPLICATION_ERROR"
TIMEOUT = "TIMEOUT"
SURFACE_ERROR = "SURFACE_ERROR"
UNRECOGNIZED_SCREEN = "UNRECOGNIZED_SCREEN"


class RecoveryEvent(BaseModel):
    """A condition the engine handled without the caller needing to know."""

    at_step: str
    rule: str
    kind: Literal["interstitial", "transient", "reauth", "retry"]
    detail: str = ""
    attempts: int = 1


class StepResult(BaseModel):
    id: str
    intent: str
    action: str
    status: Literal["ok", "skipped", "failed"] = "ok"
    resolution: Resolution | None = None
    value_sent: str | None = Field(default=None, description="Already masked when sensitive.")
    extracted: Any = None
    duration_ms: int = 0
    attempts: int = 1
    expectation: str | None = None
    detail: str = ""


class FailureDetail(BaseModel):
    """Everything needed to debug a failed replay without re-running it."""

    code: str
    step_id: str | None = None
    step_intent: str = ""
    expected: str = ""
    observed: str = ""
    resolution: Resolution | None = None
    route: str = ""
    screen_signature: str = ""
    frame: str | None = None
    snapshot: str | None = None
    hint: str = ""


class ReplayResult(BaseModel):
    """What ``pcx replay`` returns, and what a calling agent receives."""

    run_id: str
    capability: str
    version: int
    digest: str
    tenant: str | None = None
    execution_type: int = 1

    status: ReplayStatus
    outcome: str = "OK"
    outcome_description: str = ""
    remediation: str = ""
    outputs: dict[str, Any] = Field(default_factory=dict)

    steps: list[StepResult] = Field(default_factory=list)
    recoveries: list[RecoveryEvent] = Field(default_factory=list)
    failure: FailureDetail | None = None

    started_at: float = Field(default_factory=time.time)
    duration_ms: int = 0
    llm_calls: int = Field(default=0, description="0 for a Type 1 replay. Non-zero is a defect there.")
    llm_tokens: int = 0
    degraded_resolutions: int = 0
    drift_signals: list[str] = Field(
        default_factory=list,
        description="Screens that did not match their recorded signature. Not failures.",
    )
    evidence_dir: str = ""

    def ok(self) -> bool:
        return self.status == "success"

    def caller_summary(self) -> str:
        if self.status == "success":
            return f"OK ({len(self.outputs)} output(s))"
        if self.status == "business_outcome":
            return f"{self.outcome}: {self.outcome_description}"
        if self.status == "escalated":
            return f"escalated: {self.failure.hint if self.failure else ''}"
        step = self.failure.step_id if self.failure else "?"
        return f"FAILED {self.outcome} at step {step}"
