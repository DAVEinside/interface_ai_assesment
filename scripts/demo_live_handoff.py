"""The escalation path, on a live public website, ending in a promotion.

This is the demo_handoff story against ParaBank instead of the mock app, and it
runs the consequence out to the end: after the human hands control back, the run
finishes, its evidence is recorded, and the lifecycle acts on it.

    1. replay sign_on_parabank with one recorded locator DELIBERATELY BROKEN,
       standing in for the app having moved a control since the recording
    2. the step cannot resolve, the retries are exhausted, and the replay
       escalates instead of guessing at a control it cannot see
    3. a human opens the operator console, takes control of the LIVE browser,
       finishes the step by hand, and hands control back
    4. the run completes, and the ledger records that it needed a human
    5. `pcx status` shows the promotion gate refusing on exactly that count

Point 5 is the part worth watching. "Human interventions must be 0" is a
promotion gate: a flow that needed hands has not earned fewer of them. Run this
once and the capability cannot promote until three clean runs outweigh it.

    python scripts/demo_live_handoff.py              # you are the operator
    python scripts/demo_live_handoff.py --simulated  # a script plays the operator
    python scripts/demo_live_handoff.py --wrong-password   # fail on credentials instead

The default forcing mechanism is a broken locator rather than a bad password,
and that is deliberate. A demo that depends on the target site *rejecting* a bad
credential is a demo that depends on a third party's authentication behaviour:
ParaBank is a deliberately insecure teaching application and signs you in with
any password for a known username, so the wrong-password version simply
succeeds. A locator that no longer resolves is forced from inside this system,
works on any site, and is the failure this whole design exists for -- the
recorded flow was fine until the application moved.

It is applied through `TenantOverlay.step_locator_overrides`, a real production
mechanism for pinning a tenant's differing control to a step, not a test hook.

Requires PCX_SIGN_ON_PARABANK_USERNAME and PCX_SIGN_ON_PARABANK_PASSWORD, and
`pcx discover --id sign_on_parabank ...` to have been run first
(see docs/LIVE_SITE_DEMO.md). No model is called: replay never uses one, and the
whole point is that the *human* is the intelligence in the loop here.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import webbrowser
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT))

if os.name == "nt" and not os.environ.get("WT_SESSION"):
    os.system("")  # enable ANSI on legacy consoles

import httpx  # noqa: E402

from pcx.artifact.locator import Locator, Strategy  # noqa: E402
from pcx.config import Settings, resolve_credential  # noqa: E402
from pcx.agent.reporter import ConsoleReporter  # noqa: E402
from pcx.crystallize import lifecycle  # noqa: E402
from pcx.policy.allowlist import PolicyEngine  # noqa: E402
from pcx.replay.engine import ReplayEngine, ReplayOptions  # noqa: E402
from pcx.runner import Session, host_of  # noqa: E402
from pcx.surfaces.model import Role  # noqa: E402

CAPABILITY = "sign_on_parabank"
TENANT = "parabank"
WRONG_PASSWORD = "not-the-real-password"

#: What the broken step is told to look for. Nothing on the page answers to it,
#: which is the point: every ranked strategy misses, the retries are spent, and
#: the engine reports ELEMENT_NOT_FOUND rather than clicking something else.
GONE = Locator(
    description="the sign-in control, as it was before the site was redesigned",
    strategies=[Strategy(kind="role_name", role=Role.BUTTON, name="Sign In To Your Account")],
)


async def find_control(surface, name: str, role: str | None = None):
    obs = await surface.observe()
    for element in obs.elements:
        if name.upper() in (element.name or "").upper():
            if role is None or element.role.value == role:
                return element
    return None


async def simulated_operator(console_url: str, broker, surface, password: str) -> None:
    """What a person would do, driven over the console's own HTTP API.

    Nothing here is a private back door: every call is one the browser UI makes.
    The only thing simulated is the person.
    """
    async with httpx.AsyncClient(base_url=console_url, timeout=60) as client:
        request_id = None
        for _ in range(150):
            state = (await client.get("/api/state")).json()
            pending = [r for r in state["requests"] if r["status"] == "open"]
            if pending:
                request_id = pending[0]["id"]
                print(f"\n[operator] sees intervention {request_id}")
                print(f"[operator]   reason: {pending[0]['reason'][:100]}")
                break
            await asyncio.sleep(0.4)
        if request_id is None:
            print("[operator] no intervention appeared -- did the sign-on succeed?")
            return

        (await client.post("/api/claim", json={"request_id": request_id, "operator": "n.dave"})).raise_for_status()
        print(f"[operator] took control -- broker state is now {broker.state.value!r}")

        frame = await client.get("/api/frame.png")
        print(f"[operator] live frame: {len(frame.content)} bytes of PNG")

        # Sign in by hand on the live page. The password is typed into the
        # browser, never stored in the artifact -- which is the same rule the
        # automation follows.
        for label, value in (("Username", os.environ.get("PCX_SIGN_ON_PARABANK_USERNAME", "")),
                             ("Password", password)):
            field = await find_control(surface, label, role="textbox")
            if field is None:
                print(f"[operator] could not see the {label} field")
                continue
            await client.post("/api/act", json={"kind": "click", "x": field.rect.cx, "y": field.rect.cy})
            await client.post("/api/act", json={"kind": "type", "text": value})
            print(f"[operator] typed the {label.lower()}")

        button = await find_control(surface, "Log In")
        if button is not None:
            await client.post("/api/act", json={"kind": "click", "x": button.rect.cx, "y": button.rect.cy})
            print("[operator] clicked 'Log In'")
        await asyncio.sleep(3)

        (await client.post("/api/release", json={
            "request_id": request_id,
            "disposition": "resume",
            "note": "Recorded locator no longer resolves; completed the step by hand.",
        })).raise_for_status()
        print("[operator] released control back to the automation\n")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--simulated", action="store_true",
                        help="a script plays the operator instead of you")
    parser.add_argument("--wrong-password", action="store_true",
                        help="force the failure with a bad credential instead of a broken "
                             "locator. Only works on a site that actually rejects one -- "
                             "ParaBank does not.")
    args = parser.parse_args()

    settings = Settings()
    username, _ = resolve_credential(CAPABILITY, "username")
    password, var = resolve_credential(CAPABILITY, "password")
    if not (username and password):
        print("Set PCX_SIGN_ON_PARABANK_USERNAME and PCX_SIGN_ON_PARABANK_PASSWORD in .env first.")
        return 2

    async with Session(settings, "handoff") as session:
        try:
            base = session.store.load(CAPABILITY)
            overlay = session.store.load_tenant(TENANT)
        except FileNotFoundError:
            print(f"No {CAPABILITY} capability yet. Record it first -- docs/LIVE_SITE_DEMO.md step 1.")
            return 2

        broken_step = None
        if not args.wrong_password:
            broken_step = next((s.id for s in base.steps if s.action == "click"), None)
            if broken_step is None:
                print("This capability has no click step to break; use --wrong-password.")
                return 2
            overlay = overlay.model_copy(
                update={"step_locator_overrides": {**overlay.step_locator_overrides, broken_step: GONE}}
            )
        capability = base.specialize(overlay)

        console_url = await session.start_console()
        print(f"[console] operator console at {console_url}")
        if not args.simulated:
            # Telling someone to "open the console" is only useful if they know it
            # is a web page. Open it for them; say where it is either way, because
            # a headless box or an unusual default browser will not.
            opened = False
            try:
                opened = webbrowser.open(console_url)
            except Exception:
                opened = False
            print(f"[console] {'opened in your browser' if opened else 'OPEN THIS IN YOUR BROWSER'}: {console_url}")
            print("[console] It is a web page. Left half is the live browser this run is")
            print("[console] driving; right half is a column of cards.")
            print("[console] Everything is greyed out until the run parks -- that is expected.")
            print("[console] When it does, in the 'Intervention' card at the top right:")
            print("[console]   1. click the blue 'Take control' button")
            print("[console]   2. click on the live screen (left) to focus a field, then use the")
            print("[console]      'Manual input' card below it: type text, then 'Type' / Enter / Tab")
            print("[console]   3. click 'Resume', next to 'Take control', to hand back\n")

        engine = ReplayEngine(
            session.surface,
            PolicyEngine(session.deployment, capability.policy,
                         tenant_host=host_of(capability.resolved_entrypoint())),
            session.recorder,
            session.redactor,
            broker=session.broker,
            options=ReplayOptions(escalate_on_failure=True),
            reporter=ConsoleReporter(),
            capability_loader=lambda cid: session.store.load(cid).specialize(
                session.store.load_tenant(TENANT)
            ),
        )

        print(f"[replay] {capability.ref()} against {capability.resolved_entrypoint()}")
        if broken_step:
            print(f"[replay] step {broken_step} is looking for a control that is not there:")
            print(f"[replay]   {GONE.description}")
            print("[replay] the credentials are correct -- the recorded flow is what broke.\n")
            inputs = {"username": username, "password": password}
        else:
            print(f"[replay] password deliberately wrong (the real one is in ${var})")
            print("[replay] note: ParaBank accepts any password for a known username,")
            print("[replay] so this mode will simply succeed there.\n")
            inputs = {"username": username, "password": WRONG_PASSWORD}
        operator = (
            asyncio.create_task(simulated_operator(console_url, session.broker, session.surface, password))
            if args.simulated else None
        )

        result = await engine.run(capability, inputs)
        print(f"[replay] first verdict : {result.status} / {result.outcome}")

        if result.status != "success":
            result = await engine.escalate_failure(capability, result)
        if operator is not None:
            await operator

        if result.human_interventions == 0:
            print("\n[!] Nothing escalated -- the run succeeded on its own, so there is no")
            print("[!] handoff to watch. On --wrong-password against ParaBank that is")
            print("[!] expected: it accepts any password for a known username. Run without")
            print("[!] the flag to force the failure from inside this system instead.")

        print(f"[replay] final verdict : {result.status} / {result.outcome}")
        print(f"[replay] human interventions: {result.human_interventions}")

        # 4 -- the run goes into the ledger like any other, interventions included.
        action_sequence = "|".join(f"{s.action}:{s.id}" for s in result.steps)
        lifecycle.record_run(
            session.store, capability, result,
            action_sequence=action_sequence,
            input_key="handoff-demo",
            human_interventions=result.human_interventions,
        )
        stored = lifecycle.refresh_evidence(session.store, session.store.load(CAPABILITY))
        session.store.update(stored)

        # 5 -- and the gate reads it back.
        gate = lifecycle.evaluate_promotion(stored)
        print(f"\n[crystallize] {CAPABILITY} is Type {stored.crystallization.execution_type}, "
              f"status {stored.crystallization.status}")
        print(f"[crystallize] promotion gate -> Type {gate.target_type}")
        for reason in gate.reasons:
            print(f"    {reason}")
        print(f"  => {'ELIGIBLE' if gate.eligible else 'NOT eligible'}")
        print("\nThe 'human interventions' line is the one this demo exists to produce:")
        print("a flow that needed a person is not a flow ready to run without one.")
        print(f"\nEvidence: {session.recorder.dir}")
        return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        # Ctrl-C during a handoff is a normal way to stop waiting. A traceback
        # about asyncio futures is not a useful thing to show for it.
        print("\n[abort] stopped while waiting for an operator. Nothing was recorded.")
        raise SystemExit(130) from None
