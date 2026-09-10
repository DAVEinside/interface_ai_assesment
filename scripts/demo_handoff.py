"""End-to-end demonstration of escalation and control handoff.

What this proves
----------------
A deterministic replay hits a condition it cannot recover from (an injected
CoreServ system error). Instead of just failing, it raises an intervention
request, parks, and waits. A human operator opens the console, takes control of
**the same live browser session**, finishes the work by hand, and hands control
back. The engine then re-evaluates the capability's checkpoint on the resulting
screen and reports success -- with the human's actions recorded in the run
evidence.

About the "operator"
--------------------
The console (``pcx.escalation.console``) is real: a real HTTP surface, a real
live screenshot, real click/type passthrough into the live page. What is
simulated here is only the *person* -- this script drives the console's HTTP API
the way a browser would when someone clicks. It finds the coordinates to click
by asking the surface where the controls are, which stands in for a person
looking at the screenshot. Everything downstream of the HTTP request is the
production path.

Run:
    python scripts/demo_handoff.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import urllib.request
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / "src"), str(Path(__file__).resolve().parents[1])]

import httpx

from pcx.config import Settings
from pcx.policy.allowlist import PolicyEngine
from pcx.replay.engine import ReplayEngine, ReplayOptions
from pcx.runner import Session, bind_default_tenant
from pcx.surfaces.model import normalize_text

CAPABILITY = "read_member_savings_balance"
MEMBER = "10000001"


def arm_fault(settings: Settings, name: str, count: int = 1) -> None:
    url = f"{settings.base_url}/admin/fault?name={name}&count={count}"
    with urllib.request.urlopen(url) as response:
        print(f"[harness] armed fault: {response.read().decode()}")


async def find_control(surface, name: str, role: str | None = None):
    """Where a person would point. Stands in for reading the screenshot."""
    obs = await surface.observe(screenshot=False)
    for element in obs.elements:
        if normalize_text(element.name).upper() == name.upper():
            if role is None or element.role.value == role:
                return element
    return None


async def simulated_operator(console_url: str, broker, surface) -> None:
    """Poll the console, claim the request, fix it by hand, hand control back."""
    async with httpx.AsyncClient(base_url=console_url, timeout=30) as client:
        request_id = None
        for _ in range(120):
            state = (await client.get("/api/state")).json()
            pending = [r for r in state["requests"] if r["status"] == "open"]
            if pending:
                request_id = pending[0]["id"]
                print(f"\n[operator] sees intervention {request_id}: {pending[0]['reason'][:90]}")
                break
            await asyncio.sleep(0.4)
        if request_id is None:
            print("[operator] no intervention appeared")
            return

        claimed = await client.post(
            "/api/claim", json={"request_id": request_id, "operator": "j.reyes"}
        )
        claimed.raise_for_status()
        print(f"[operator] took control -- broker state is now {broker.state.value!r}")

        # The screenshot the operator is looking at is served live from the same
        # session; a real person clicks it, this script posts the coordinates.
        frame = await client.get("/api/frame.png")
        print(f"[operator] live frame: {len(frame.content)} bytes of PNG")

        async def click(name: str) -> None:
            element = await find_control(surface, name)
            if element is None:
                print(f"[operator] could not see {name!r} on screen")
                return
            await client.post(
                "/api/act", json={"kind": "click", "x": element.rect.cx, "y": element.rect.cy}
            )
            print(f"[operator] clicked {name!r} at ({int(element.rect.cx)},{int(element.rect.cy)})")

        await click("MEMBER INQUIRY")
        field = await find_control(surface, "", role="textbox")
        if field is not None:
            await client.post(
                "/api/act", json={"kind": "click", "x": field.rect.cx, "y": field.rect.cy}
            )
            await client.post("/api/act", json={"kind": "type", "text": MEMBER})
            print(f"[operator] typed the member number into the inquiry field")
        await click("INQUIRE")

        released = await client.post(
            "/api/release",
            json={
                "request_id": request_id,
                "disposition": "resume",
                "note": "CoreServ threw CSV-500; re-ran the inquiry by hand, screen is good.",
            },
        )
        released.raise_for_status()
        print(f"[operator] released control back to the automation\n")


async def main() -> int:
    settings = Settings()
    async with Session(settings, "handoff") as session:
        console_url = await session.start_console()
        print(f"[console] operator console at {console_url}")

        capability = bind_default_tenant(session.store.load(CAPABILITY), settings)
        engine = ReplayEngine(
            session.surface,
            PolicyEngine(session.deployment, capability.policy),
            session.recorder,
            session.redactor,
            broker=session.broker,
            options=ReplayOptions(escalate_on_failure=True),
            capability_loader=lambda cid: bind_default_tenant(session.store.load(cid), settings),
        )

        arm_fault(settings, "app_error", 1)
        print(f"[replay] invoking {CAPABILITY} with member_number={MEMBER}\n")

        operator = asyncio.create_task(
            simulated_operator(console_url, session.broker, session.surface)
        )
        result = await engine.run(capability, {"member_number": MEMBER})
        print(f"[replay] first verdict: {result.status} / {result.outcome}")

        if result.status == "failed":
            result = await engine.escalate_failure(capability, result)

        await operator

        print(f"[replay] final verdict : {result.status} / {result.outcome}")
        print(f"          description   : {result.outcome_description}")
        print(f"          evidence      : {result.evidence_dir}")

        human_events = [
            e for e in session.recorder.events() if e.get("kind") in
            {"escalation_raised", "handoff_claimed", "human_action", "handoff_released",
             "escalation_resolved", "checkpoint_after_handoff"}
        ]
        print("\n[audit] control-transfer trail recorded in the run log:")
        for event in human_events:
            detail = {k: v for k, v in event.items() if k not in ("seq", "t", "kind", "actor")}
            print(f"  t={event['t']:>6}s {event['kind']:<24} {json.dumps(detail, default=str)[:120]}")
        return 0 if result.status == "success" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
