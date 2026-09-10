"""Redaction. Applied on the way *out* of the process, not on the way in.

Rule of the house: the agent may see whatever the operator's screen shows -- it
has to, to do the job -- but nothing sensitive survives into a log line, a trace
file, a capability artifact or an escalation payload. So redaction sits at the
serialization boundary, in exactly two places: :func:`scrub` (every string that
gets written) and :func:`scrub_obj` (every structure).

The patterns below are the regulated categories that appear on this surface:
US SSN/TIN, account and card numbers, credentials, bearer tokens, and the
operator's own password field. ``Sensitivity``-tagged contract values get a
second, type-aware pass in :func:`mask_value`, which is the one that knows a
member number should keep its last four digits so an operator can still
recognize the record in an escalation.

Limits, stated plainly: this is regex redaction over text the agent chose to
capture. It cannot catch a balance that is sensitive only in context, and it
cannot un-see a screenshot. Screenshots are therefore treated as a separate
class -- see :mod:`pcx.evidence.recorder`.
"""

from __future__ import annotations

import re
from typing import Any

REDACTED = "[REDACTED]"

#: (name, pattern, replacement). Order matters: the most specific first.
BUILTIN_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[REDACTED:SSN]"),
    ("ssn_flat", re.compile(r"\b(?<!\d)\d{9}(?!\d)\b"), "[REDACTED:TIN?]"),
    ("card", re.compile(r"\b(?:\d[ -]?){13,19}\b"), "[REDACTED:PAN]"),
    (
        "bearer",
        re.compile(r"\b(?:bearer|token|api[_-]?key|authorization)\b\s*[:=]\s*\S+", re.I),
        "[REDACTED:CREDENTIAL]",
    ),
    (
        "password_kv",
        re.compile(r"\b(?:password|passwd|pwd|secret|passphrase)\b\s*[:=]\s*\S+", re.I),
        "[REDACTED:CREDENTIAL]",
    ),
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]{2,}\b"), "[REDACTED:EMAIL]"),
    ("dob", re.compile(r"\bDATE OF BIRTH\b\s*:?\s*\d{4}-\d{2}-\d{2}", re.I), "DATE OF BIRTH: [REDACTED:DOB]"),
]

#: Field names whose *values* are dropped wholesale wherever they appear.
SECRET_KEYS = {
    "password",
    "passwd",
    "pwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "set-cookie",
    "session",
    "credential",
    "credentials",
}


class Redactor:
    """Stateful redactor: builtin patterns + per-capability extras + literals."""

    def __init__(
        self,
        extra_patterns: list[str] | None = None,
        literals: list[str] | None = None,
    ) -> None:
        self._extra = [re.compile(p) for p in (extra_patterns or [])]
        #: Exact strings known to be secret (e.g. the password just typed).
        self._literals = [s for s in (literals or []) if s and len(s) >= 4]

    def add_literal(self, value: str | None) -> None:
        if value and len(value) >= 4 and value not in self._literals:
            self._literals.append(value)

    def scrub(self, text: str | None) -> str:
        if not text:
            return ""
        out = text
        for literal in self._literals:
            out = out.replace(literal, REDACTED)
        for _name, pattern, replacement in BUILTIN_PATTERNS:
            out = pattern.sub(replacement, out)
        for pattern in self._extra:
            out = pattern.sub(REDACTED, out)
        return out

    def scrub_obj(self, obj: Any) -> Any:
        """Recursively redact a JSON-ish structure, keys included."""
        if isinstance(obj, dict):
            clean: dict[str, Any] = {}
            for key, value in obj.items():
                if str(key).lower() in SECRET_KEYS:
                    clean[key] = REDACTED
                else:
                    clean[key] = self.scrub_obj(value)
            return clean
        if isinstance(obj, (list, tuple)):
            return [self.scrub_obj(v) for v in obj]
        if isinstance(obj, str):
            return self.scrub(obj)
        return obj


def mask_value(value: Any, sensitivity: str) -> Any:
    """Type-aware masking for contract-tagged values.

    ``pii_reference`` keeps a tail so a human operator can still recognize the
    record during an escalation; ``pii`` keeps only shape; ``secret`` keeps
    nothing.
    """
    if value is None:
        return None
    if sensitivity == "secret":
        return REDACTED
    text = str(value)
    if sensitivity == "pii_reference":
        return ("*" * max(0, len(text) - 4)) + text[-4:] if len(text) > 4 else REDACTED
    if sensitivity == "pii":
        return f"[REDACTED:PII len={len(text)}]"
    return value


def mask_outputs(outputs: dict[str, Any], params) -> dict[str, Any]:
    """Mask a capability's outputs for logging, honouring each param's sensitivity."""
    by_name = {p.name: p for p in params}
    masked: dict[str, Any] = {}
    for key, value in outputs.items():
        param = by_name.get(key)
        masked[key] = mask_value(value, param.sensitivity) if param else value
    return masked


#: A module-level default for code paths that have no capability context.
default_redactor = Redactor()
