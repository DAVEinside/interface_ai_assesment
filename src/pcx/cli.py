"""``pcx`` -- the command line.

    pcx target                 run the mock CoreServ application
    pcx discover               LLM-driven run against a live surface -> capability
    pcx replay                 deterministic execution of a saved capability
    pcx invoke                 production entry point: route to the cheapest type
    pcx list / show / status   inspect capabilities and their crystallization state
    pcx test                   run a capability's acceptance tests
    pcx promote / demote       apply the crystallization lifecycle by hand
    pcx tenant                 register a tenant overlay
    pcx fault                  arm a fault in the mock app (demo harness only)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from .artifact.store import CapabilityStore
from .config import settings
from .crystallize import lifecycle
from .runner import recompile_from_trace, run_acceptance_tests, run_discovery, run_replay


def _kv(pairs: list[str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in pairs or []:
        if "=" not in item:
            raise SystemExit(f"expected key=value, got {item!r}")
        key, _, value = item.partition("=")
        out[key.strip()] = value
    return out


def _print_json(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))


# --------------------------------------------------------------------------- #


def cmd_target(args) -> int:
    # `target_app` is a demo fixture, not part of the installed package, so it
    # lives beside the repo rather than under src/. A console-script entry point
    # does not put the working directory on sys.path, so point at the repo root
    # explicitly -- otherwise `pcx target` works from a source checkout and fails
    # after `pip install -e .`, which is a confusing way to greet a new user.
    from .config import REPO_ROOT

    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    try:
        from target_app.app import main as target_main
    except ModuleNotFoundError:  # pragma: no cover - depends on how it was installed
        print(
            "Could not import target_app. It ships with the source checkout, not the\n"
            f"installed package. Run this from the repository root (looked in {REPO_ROOT}).",
            file=sys.stderr,
        )
        return 2

    port = args.port or int(settings.base_url.rsplit(":", 1)[-1])
    print(f"CoreServ 7.2 mock listening on http://127.0.0.1:{port}  (Ctrl-C to stop)")
    if args.vocab:
        print(f"  tenant vocabulary: {args.vocab}")
    target_main(port=port, vocab=args.vocab, secret=args.secret)
    return 0


def cmd_fault(args) -> int:
    import urllib.request

    url = f"{settings.base_url}/admin/fault?name={args.name}&count={args.count}"
    with urllib.request.urlopen(url) as response:
        print(response.read().decode())
    return 0


def cmd_discover(args) -> int:
    result, capability = asyncio.run(
        run_discovery(
            settings,
            goal=args.goal,
            entrypoint=args.entrypoint or settings.base_url + "/",
            parameters=_kv(args.param),
            outputs=args.output or [],
            capability_id=args.id,
            profile_key=args.profile,
            setup_capability=args.setup,
            max_steps=args.max_steps,
            with_console=args.console,
            llm_backend=args.llm,
            verbose=not args.quiet,
        )
    )
    trace = result.trace
    print(
        f"\n[discovery] {trace.status}: {trace.stop_reason}\n"
        f"  steps        : {len(trace.steps)}\n"
        f"  model        : {trace.model}\n"
        f"  outputs bound: {list(trace.outputs)}\n"
        f"  usage        : {trace.usage}\n"
        f"  evidence     : {result.recorder.dir}"
    )
    if capability is not None:
        print(f"  capability   : {capability.ref()}  digest={capability.digest()}")
    return 0 if trace.status == "succeeded" else 1


def cmd_replay(args) -> int:
    result = asyncio.run(
        run_replay(
            settings,
            capability_id=args.id,
            inputs=_kv(args.input),
            version=args.version,
            tenant=args.tenant,
            escalate=args.escalate,
            with_console=args.console,
            approve_irreversible=args.approve,
        )
    )
    payload = result.model_dump(mode="json")
    if args.json:
        _print_json(payload)
    else:
        print(f"\n[replay] {result.status.upper()}  outcome={result.outcome}")
        if result.outcome_description:
            print(f"  {result.outcome_description}")
        if result.remediation:
            print(f"  remediation: {result.remediation}")
        if result.outputs:
            print("  outputs:")
            for key, value in result.outputs.items():
                print(f"    {key} = {value}")
        for step in result.steps:
            mark = {"ok": "ok  ", "skipped": "skip", "failed": "FAIL"}[step.status]
            res = step.resolution.brief() if step.resolution else "-"
            print(f"    [{mark}] {step.id:<4} {step.action:<8} {res}")
        for recovery in result.recoveries:
            print(f"    [rcvr] {recovery.kind}: {recovery.rule} ({recovery.detail})")
        for signal in result.drift_signals:
            print(f"    [drift] {signal}")
        if result.failure:
            f = result.failure
            print(f"  failure : {f.code} at {f.step_id} ({f.step_intent})")
            print(f"    expected: {f.expected[:200]}")
            print(f"    observed: {f.observed[:300]}")
            if f.hint:
                print(f"    hint    : {f.hint}")
            if f.frame:
                print(f"    frame   : {f.frame}")
            if f.snapshot:
                print(f"    snapshot: {f.snapshot}")
        print(
            f"  llm calls: {result.llm_calls}   degraded locators: {result.degraded_resolutions}"
            f"   {result.duration_ms} ms"
        )
        print(f"  evidence : {result.evidence_dir}")
    return 0 if result.status in ("success", "business_outcome") else 1


def cmd_invoke(args) -> int:
    """Production entry point: the router the paper describes.

    Picks the cheapest execution type the capability has earned, which today
    means: quarantined or Type 3 capabilities are refused rather than silently
    run without a model in the loop.
    """
    store = CapabilityStore(settings.capabilities_dir)
    capability = store.load(args.id)
    crystal = capability.crystallization
    if crystal.status == "quarantined":
        print(
            f"[router] {capability.ref()} is quarantined "
            f"({crystal.evidence.last_demotion_reason}); refusing to invoke deterministically."
        )
        return 2
    if crystal.execution_type == 3 and crystal.status != "candidate":
        print(f"[router] {capability.ref()} is Type 3 and has not been promoted; use `pcx discover`.")
        return 2
    print(f"[router] {capability.ref()} -> Type {crystal.execution_type} ({crystal.status})")
    return cmd_replay(args)


def cmd_list(args) -> int:
    store = CapabilityStore(settings.capabilities_dir)
    ids = store.ids()
    if not ids:
        print("no capabilities recorded yet")
        return 0
    print(f"{'capability':<34}{'ver':>4}  {'type':<6}{'status':<12}{'runs':>5}  digest")
    for cid in ids:
        cap = store.load(cid)
        ev = cap.crystallization.evidence
        print(
            f"{cid:<34}{cap.version:>4}  T{cap.crystallization.execution_type:<5}"
            f"{cap.crystallization.status:<12}{ev.successful_runs:>5}  {cap.digest()[:19]}"
        )
    return 0


def cmd_show(args) -> int:
    store = CapabilityStore(settings.capabilities_dir)
    capability = store.load(args.id, args.version)
    if args.tool_schema:
        schema = capability.contract.to_tool_schema()
        schema["name"] = capability.id
        _print_json(schema)
        return 0
    path = store.root / capability.id / f"v{capability.version}.yaml"
    print(path.read_text(encoding="utf-8"))
    return 0


def cmd_status(args) -> int:
    store = CapabilityStore(settings.capabilities_dir)
    capability = store.load(args.id, args.version)
    lifecycle.refresh_evidence(store, capability)
    crystal = capability.crystallization
    ev = crystal.evidence
    print(f"{capability.ref()}   digest {capability.digest()}")
    print(f"  execution type : Type {crystal.execution_type}   status: {crystal.status}")
    print(f"  side effect    : {capability.contract.side_effect}")
    print(f"  successful runs: {ev.successful_runs}   failed: {ev.failed_runs}")
    print(f"  distinct inputs: {ev.distinct_input_sets}")
    print(f"  seq stability  : {ev.action_sequence_stability:.2f}")
    print(f"  loc stability  : {ev.resolution_stability:.2f}   degraded: {ev.degraded_resolutions}")
    print(f"  drift signals  : {ev.drift_signals}   tenants seen: {', '.join(ev.tenants_seen) or '-'}")
    print(f"  safety viol.   : {ev.safety_violations}   human interventions: {ev.human_interventions}")
    if ev.last_demotion_reason:
        print(f"  last demotion  : {ev.last_demoted_at} -- {ev.last_demotion_reason}")
    gate = lifecycle.evaluate_promotion(capability)
    print(f"\n  promotion gate -> Type {gate.target_type}")
    for reason in gate.reasons:
        print(f"    {reason}")
    print(f"  => {'ELIGIBLE' if gate.eligible else 'not eligible'}")
    return 0


def cmd_promote(args) -> int:
    store = CapabilityStore(settings.capabilities_dir)
    capability = store.load(args.id)
    lifecycle.refresh_evidence(store, capability)
    if args.mark_reviewed:
        capability.crystallization.evidence.human_reviewed = True
    changed, message = lifecycle.promote(capability)
    store.update(capability)
    print(("[promote] " if changed else "[promote] refused: ") + message)
    return 0 if changed else 1


def cmd_demote(args) -> int:
    store = CapabilityStore(settings.capabilities_dir)
    capability = store.load(args.id)
    changed, message = lifecycle.demote(capability, args.reason)
    store.update(capability)
    print("[demote] " + message)
    return 0


def cmd_test(args) -> int:
    results = asyncio.run(run_acceptance_tests(settings, args.id))
    failed = 0
    for row in results:
        mark = "PASS" if row["passed"] else "FAIL"
        failed += 0 if row["passed"] else 1
        print(f"[{mark}] {row['test']}: expected {row['expected_outcome']}, got {row['actual_outcome']}")
        if row["missing_outputs"]:
            print(f"        missing outputs: {row['missing_outputs']}")
        for problem in row["shape_failures"]:
            print(f"        {problem}")
    print(f"\n{len(results) - failed}/{len(results)} acceptance tests passed")
    return 0 if failed == 0 else 1


def cmd_recompile(args) -> int:
    capability = recompile_from_trace(
        settings, args.trace, capability_id=args.id, profile_key=args.profile
    )
    print(f"[recompile] {capability.ref()}  digest={capability.digest()}")
    return 0


def cmd_tenant(args) -> int:
    from .artifact.schema import TenantOverlay

    store = CapabilityStore(settings.capabilities_dir)
    overlay = TenantOverlay(
        tenant_id=args.tenant_id,
        base_url=args.base_url,
        label=args.label or args.tenant_id,
        text_overrides=_kv(args.text_override),
        timing_multiplier=args.timing,
        notes=args.notes or "",
    )
    path = store.save_tenant(overlay)
    print(f"[tenant] {overlay.tenant_id} -> {path}")
    return 0


# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pcx", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    target = sub.add_parser("target", help="run the mock CoreServ application")
    target.add_argument("--port", type=int, help="listen port (default 8799)")
    target.add_argument(
        "--vocab", choices=["customer"],
        help="run as a differently-branded tenant of the same product",
    )
    target.add_argument("--secret", help="session secret, so two tenants do not share cookies")
    target.set_defaults(func=cmd_target)

    fault = sub.add_parser("fault", help="arm a fault in the mock app (demo harness)")
    fault.add_argument("name", choices=["none", "slow", "interstitial", "session_timeout", "app_error"])
    fault.add_argument("--count", type=int, default=1)
    fault.set_defaults(func=cmd_fault)

    discover = sub.add_parser("discover", help="LLM-driven discovery run")
    discover.add_argument("--goal", required=True)
    discover.add_argument("--id", required=True, help="capability id to emit")
    discover.add_argument("--entrypoint")
    discover.add_argument("--param", action="append", metavar="name=value",
                          help="a typed input, with the sample value the agent should use")
    discover.add_argument("--output", action="append", metavar="name",
                          help="an output the agent must capture with `extract`")
    discover.add_argument("--profile", default="coreserv-7.2")
    discover.add_argument("--setup", help="capability to replay first, e.g. sign_on_coreserv")
    discover.add_argument("--max-steps", type=int, default=24)
    discover.add_argument("--console", action="store_true", help="start the operator console")
    discover.add_argument("--llm", default=None, choices=["auto", "api", "cli"],
                          help="model backend: auto prefers ANTHROPIC_API_KEY, then the claude CLI")
    discover.add_argument("--quiet", action="store_true",
                          help="suppress the live turn-by-turn narration")
    discover.set_defaults(func=cmd_discover)

    for name, help_text in (("replay", "deterministic replay"), ("invoke", "route and execute")):
        cmd = sub.add_parser(name, help=help_text)
        cmd.add_argument("id")
        cmd.add_argument("--input", action="append", metavar="name=value")
        cmd.add_argument("--version", type=int)
        cmd.add_argument("--tenant")
        cmd.add_argument("--escalate", action="store_true", help="hand a hard failure to a human")
        cmd.add_argument("--console", action="store_true")
        cmd.add_argument("--approve", action="store_true", help="pre-approve irreversible steps")
        cmd.add_argument("--json", action="store_true")
        cmd.set_defaults(func=cmd_replay if name == "replay" else cmd_invoke)

    sub.add_parser("list", help="list capabilities").set_defaults(func=cmd_list)

    show = sub.add_parser("show", help="print a capability")
    show.add_argument("id")
    show.add_argument("--version", type=int)
    show.add_argument("--tool-schema", action="store_true", help="print the JSON-Schema tool view")
    show.set_defaults(func=cmd_show)

    status = sub.add_parser("status", help="crystallization state and promotion gates")
    status.add_argument("id")
    status.add_argument("--version", type=int)
    status.set_defaults(func=cmd_status)

    promote = sub.add_parser("promote", help="apply a promotion if the gates allow")
    promote.add_argument("id")
    promote.add_argument("--mark-reviewed", action="store_true", help="record human review of the logic")
    promote.set_defaults(func=cmd_promote)

    demote = sub.add_parser("demote", help="force a demotion")
    demote.add_argument("id")
    demote.add_argument("--reason", default="manual demotion")
    demote.set_defaults(func=cmd_demote)

    test = sub.add_parser("test", help="run acceptance tests")
    test.add_argument("id")
    test.set_defaults(func=cmd_test)

    recompile = sub.add_parser("recompile", help="re-crystallize a stored trace with the current compiler")
    recompile.add_argument("trace", help="path to evidence/<run>/trace.full.json")
    recompile.add_argument("--id", help="capability id to emit")
    recompile.add_argument("--profile", default="coreserv-7.2")
    recompile.set_defaults(func=cmd_recompile)

    tenant = sub.add_parser("tenant", help="register a tenant overlay")
    tenant.add_argument("tenant_id")
    tenant.add_argument("--base-url", required=True)
    tenant.add_argument("--label")
    tenant.add_argument("--text-override", action="append", metavar="OLD=NEW")
    tenant.add_argument("--timing", type=float, default=1.0)
    tenant.add_argument("--notes")
    tenant.set_defaults(func=cmd_tenant)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
