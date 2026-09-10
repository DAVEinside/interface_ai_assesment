"""Live narration of a discovery run.

A discovery run takes 30-90 seconds and, until this existed, printed nothing
until it was over -- which looks like a hang and hides the only part anyone
actually wants to watch: the model looking at a screen, saying why, and acting.

The loop stays clean: it calls a reporter, and the reporter decides whether to
print. :class:`NullReporter` is the default so library callers and tests get no
output at all.
"""

from __future__ import annotations

import os
import sys
import time
from typing import Protocol

from ..surfaces.model import Action, Observation, UIElement

#: Windows consoles older than Windows Terminal do not render ANSI.
_COLOUR = sys.stdout.isatty() and (os.name != "nt" or bool(os.environ.get("WT_SESSION")))


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOUR else text


DIM = lambda s: _c("2", s)  # noqa: E731
BOLD = lambda s: _c("1", s)  # noqa: E731
CYAN = lambda s: _c("36", s)  # noqa: E731
GREEN = lambda s: _c("32", s)  # noqa: E731
YELLOW = lambda s: _c("33", s)  # noqa: E731
RED = lambda s: _c("31", s)  # noqa: E731


class Reporter(Protocol):
    def run_start(self, goal: str, entrypoint: str, backend: str, model: str) -> None: ...
    def observe(self, step: int, obs: Observation, frame: str | None) -> None: ...
    def think(self, step: int, thought: str, latency_ms: int, usage: dict) -> None: ...
    def act(self, step: int, action: Action, element: UIElement | None, ok: bool, detail: str) -> None: ...
    def note(self, text: str, level: str = "info") -> None: ...
    def run_end(self, status: str, reason: str, steps: int, usage: dict, seconds: float) -> None: ...


class NullReporter:
    """Says nothing. The default, so importing the loop never prints."""

    def run_start(self, *a, **k) -> None: ...
    def observe(self, *a, **k) -> None: ...
    def think(self, *a, **k) -> None: ...
    def act(self, *a, **k) -> None: ...
    def note(self, *a, **k) -> None: ...
    def run_end(self, *a, **k) -> None: ...


class ConsoleReporter:
    """Prints one block per turn: what was seen, what was decided, what happened."""

    def __init__(self, show_frames: bool = True) -> None:
        self.show_frames = show_frames
        self._t0 = time.time()

    # -- lifecycle ------------------------------------------------------------
    def run_start(self, goal: str, entrypoint: str, backend: str, model: str) -> None:
        self._t0 = time.time()
        print()
        print(BOLD("discovery run") + DIM(f"  ({backend} / {model})"))
        print(DIM("  goal  ") + goal)
        print(DIM("  start ") + entrypoint)
        print(DIM("  " + "-" * 72))

    def observe(self, step: int, obs: Observation, frame: str | None) -> None:
        controls = len(obs.interactive())
        print(
            f"\n{BOLD(f'step {step}')}  {CYAN('see')}    "
            f"{obs.title or obs.route}  "
            + DIM(f"{controls} controls, {len(obs.elements)} elements")
        )
        if frame and self.show_frames:
            print(DIM(f"          screenshot: {frame}"))

    def think(self, step: int, thought: str, latency_ms: int, usage: dict) -> None:
        tokens = usage.get("output_tokens")
        cost = usage.get("cost_usd")
        meta = f"{latency_ms / 1000:.1f}s"
        if tokens:
            meta += f", {tokens} out"
        if cost:
            meta += f", ${cost:.3f}"
        print(f"        {YELLOW('think')}  {_wrap(thought)}  " + DIM(f"({meta})"))

    def act(self, step: int, action: Action, element: UIElement | None, ok: bool, detail: str) -> None:
        target = ""
        if element is not None:
            name = element.name or "(unlabelled)"
            target = DIM(f"  -> {element.role.value} {name!r}")
        verb = action.kind.upper().ljust(8)
        value = ""
        if action.text is not None:
            value = f" {action.text!r}"
        if action.bind:
            value += DIM(f" -> {action.bind}")
        mark = GREEN("act") if ok else RED("act")
        print(f"        {mark}    {verb}{action.target_ref or ''}{value}{target}")
        if not ok:
            print(RED(f"               failed: {detail}"))

    def note(self, text: str, level: str = "info") -> None:
        paint = {"info": DIM, "warn": YELLOW, "error": RED, "good": GREEN}.get(level, DIM)
        print(f"        {paint(text)}")

    def run_end(self, status: str, reason: str, steps: int, usage: dict, seconds: float) -> None:
        paint = GREEN if status == "succeeded" else (YELLOW if status == "escalated" else RED)
        print(DIM("  " + "-" * 72))
        print(f"  {paint(status)}  {reason}")
        bits = [f"{steps} steps", f"{seconds:.0f}s"]
        if usage.get("cost_usd"):
            bits.append(f"${usage['cost_usd']:.2f}")
        if usage.get("output_tokens"):
            bits.append(f"{usage['output_tokens']} output tokens")
        print(DIM("  " + "  ".join(bits)))


def _wrap(text: str, width: int = 96) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= width else text[: width - 1] + "…"
