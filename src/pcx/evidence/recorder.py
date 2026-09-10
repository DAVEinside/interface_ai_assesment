"""Evidence: the structured record of what happened and why.

Every run -- discovery, replay, or an escalated one -- gets a directory:

    evidence/<run_id>/
      run.jsonl          append-only structured event log (redacted)
      manifest.json      run summary: mode, capability, outcome, timings, counts
      frames/NNN-*.png   annotated screenshots, one per observation
      snapshots/*.html   raw surface dump, written only on failure
      llm/NNN-*.json     model request/response pairs (discovery only, redacted)

Two properties this is built for:

* **Redaction at write time.** Nothing reaches disk without passing the
  redactor. A trace is not a place to "keep the real value just in case".
* **Reconstructability.** The event log alone is enough to say what the actor
  saw, what it chose, why, whether policy allowed it, and what changed --
  without needing the model transcript. That is what makes the artifact
  decoupled from the transcript rather than a rendering of it.

Screenshots are the exception that has to be named: they can contain regulated
data that regex redaction cannot reach. They are therefore written only for
failing or escalated runs by default (``capture_frames="on_failure"``), retained
separately, and never embedded in a capability artifact.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from ..policy.redact import Redactor

FramePolicy = Literal["always", "on_failure", "never"]


def new_run_id(prefix: str) -> str:
    return f"{prefix}-{time.strftime('%Y%m%dT%H%M%S')}-{os.urandom(2).hex()}"


@dataclass
class EvidenceRecorder:
    root: Path
    run_id: str
    mode: str
    redactor: Redactor = field(default_factory=Redactor)
    capture_frames: FramePolicy = "always"
    _events: list[dict[str, Any]] = field(default_factory=list)
    _started: float = field(default_factory=time.time)
    _seq: int = 0
    _deferred_frames: list[tuple[str, bytes]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "frames").mkdir(exist_ok=True)
        (self.dir / "snapshots").mkdir(exist_ok=True)
        (self.dir / "llm").mkdir(exist_ok=True)

    @property
    def dir(self) -> Path:
        return self.root / self.run_id

    # -- events ---------------------------------------------------------------
    def event(self, kind: str, /, **fields: Any) -> dict[str, Any]:
        self._seq += 1
        record = {
            "seq": self._seq,
            "t": round(time.time() - self._started, 3),
            "kind": kind,
            **self.redactor.scrub_obj(fields),
        }
        self._events.append(record)
        with (self.dir / "run.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
        return record

    # -- artifacts ------------------------------------------------------------
    def frame(self, png: bytes, label: str, *, force: bool = False) -> str | None:
        """Record an annotated screenshot, honouring the frame policy."""
        name = f"{self._seq:03d}-{label}.png"
        if self.capture_frames == "never" and not force:
            return None
        if self.capture_frames == "on_failure" and not force:
            # Hold the most recent few in memory; flush them if the run fails.
            self._deferred_frames.append((name, png))
            self._deferred_frames = self._deferred_frames[-4:]
            return None
        path = self.dir / "frames" / name
        path.write_bytes(png)
        return str(path.relative_to(self.root))

    def flush_deferred_frames(self) -> list[str]:
        written = []
        for name, png in self._deferred_frames:
            path = self.dir / "frames" / name
            path.write_bytes(png)
            written.append(str(path.relative_to(self.root)))
        self._deferred_frames.clear()
        return written

    def snapshot(self, content: str, label: str) -> str:
        path = self.dir / "snapshots" / f"{self._seq:03d}-{label}.html"
        path.write_text(self.redactor.scrub(content), encoding="utf-8")
        return str(path.relative_to(self.root))

    def llm_exchange(self, index: int, request: Any, response: Any) -> str:
        path = self.dir / "llm" / f"{index:03d}-exchange.json"
        path.write_text(
            json.dumps(
                {
                    "request": self.redactor.scrub_obj(request),
                    "response": self.redactor.scrub_obj(response),
                },
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )
        return str(path.relative_to(self.root))

    # -- summary --------------------------------------------------------------
    def finish(self, summary: dict[str, Any]) -> Path:
        manifest = {
            "run_id": self.run_id,
            "mode": self.mode,
            "started_at": self._started,
            "duration_s": round(time.time() - self._started, 2),
            "events": len(self._events),
            **self.redactor.scrub_obj(summary),
        }
        path = self.dir / "manifest.json"
        path.write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
        return path

    def events(self) -> list[dict[str, Any]]:
        return list(self._events)
