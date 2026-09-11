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
import os
import sys
from typing import Any

from .artifact.store import CapabilityStore
from .config import credential_env_candidates, resolve_credential, settings
from .crystallize import lifecycle
from .runner import (
    SetupFailed,
    recompile_from_trace,
    run_acceptance_tests,
    run_discovery,
    run_replay,
)


def _kv(pairs: list[str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in pairs or []:
        if "=" not in item:
            raise SystemExit(f"expected key=value, got {item!r}")
        key, _, value = item.partition("=")
        out[key.strip()] = value
    return out


def _resolve_parameters(
    param: list[str] | None, secret: list[str] | None, *, capability_id: str = ""
) -> dict[str, str]:
    """Build a discovery run's input map, resolving from the environment what
    should not be typed on a command line.

    Three forms, in increasing order of how much you should prefer them for
    anything sensitive::

        --param member_number=10000001        a literal value
        --param username                      resolve from $PCX_<ID>_USERNAME,
                                              then $PCX_USERNAME
        --secret password=PCX_PARABANK_PW     resolve from a named variable

    ``--secret`` takes the *name* of an environment variable, never its value.
    A credential passed as ``--param password=$SECRET`` has already been expanded
    by the shell before this process starts: it is in the shell's history file and
    in the process list, where any other user on the machine can read it. Passing
    the variable's name instead keeps the secret in the environment -- which is
    where ``.env`` puts it, and which the replay engine already reads for
    re-authentication (``_credentials_for``).
    """
    values: dict[str, str] = {}
    missing: list[str] = []

    for item in param or []:
        name, sep, literal = item.partition("=")
        name = name.strip()
        if not name:
            raise SystemExit(f"--param needs a name, got {item!r}")
        if not sep:
            # Bare `--param username` -- declare the input, take the value from
            # the conventional variable. Same candidate list the replay engine
            # uses to re-authenticate, so a capability recorded this way can be
            # replayed without a second convention.
            value, _ = resolve_credential(capability_id, name)
            if value is not None:
                values[name] = value
            else:
                names = " or ".join(
                    f"${n}" for n in credential_env_candidates(capability_id, name)
                )
                missing.append(f"  --param {name}  needs {names} to be set")
            continue
        if literal == "":
            # Almost always a shell interpolation that produced nothing rather
            # than a deliberate empty input. Saying so here costs nothing; finding
            # out costs a browser launch and a model call.
            missing.append(
                f"  --param {name}=  resolved to an empty string"
                + ("\n     (in PowerShell, bash's $NAME is $env:NAME -- and neither shell can see .env,"
                   f"\n      which only this process reads. Use:  --secret {name}=PCX_...)"
                   if os.name == "nt" else
                   f"\n     (if this came from $SOMETHING, the variable is unset. Use:  --secret {name}=SOMETHING)")
            )
            continue
        values[name] = literal

    for item in secret or []:
        name, sep, env_key = item.partition("=")
        name, env_key = name.strip(), env_key.strip()
        if not sep or not env_key:
            raise SystemExit(
                f"--secret takes name=ENV_VAR_NAME (the *name* of a variable, not the secret), got {item!r}"
            )
        if os.environ.get(env_key):
            values[name] = os.environ[env_key]
        else:
            missing.append(f"  --secret {name}={env_key}  needs ${env_key} to be set")

    if missing:
        where = ".env is read automatically; an exported shell variable takes precedence."
        raise SystemExit(
            "cannot start: these inputs have no value\n"
            + "\n".join(missing)
            + f"\n\n{where}"
        )
    return values


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
    try:
        result, capability = asyncio.run(
            run_discovery(
                settings,
                goal=args.goal,
                entrypoint=args.entrypoint or settings.base_url + "/",
                parameters=_resolve_parameters(args.param, args.secret, capability_id=args.id),
                outputs=args.output or [],
                capability_id=args.id,
                profile_key=args.profile,
                setup_capability=args.setup,
                tenant=args.tenant,
                max_steps=args.max_steps,
                with_console=args.console,
                llm_backend=args.llm,
                verbose=not args.quiet,
            )
        )
    except KeyboardInterrupt:
        raise SystemExit("\ninterrupted; nothing was compiled") from None
    except SetupFailed as exc:
        # A precondition that could not be established is a configuration
        # problem, not a crash. The stack it came from is about asyncio.
        raise SystemExit(str(exc)) from None
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
            inputs=_resolve_parameters(args.input, args.secret, capability_id=args.id),
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


PROFILE_TEMPLATE = """\
# Application profile: {product}
#
# Product-level knowledge, shared by every capability recorded against this site.
# A recording sees each screen once and cannot tell an error banner from
# furniture, or a session-expiry screen from a login page it was meant to reach.
# That judgement lives here, and it is the thing you iterate on: when a run comes
# back UNRECOGNIZED_SCREEN, the fix is usually a detector in this file, not code.
#
# Nothing below is required. An empty profile is legal and the system runs
# without one -- each section you fill in makes replay better at telling a
# business outcome from a failure.

hosts:
  - "{host}"

app:
  vendor: "{vendor}"
  product: "{product}"
  version: "{version}"

description: >
  One paragraph on how this application reports conditions -- a banner, a code,
  a redirect. Written for a person reading the profile later.

# Things the application says that are ANSWERS, not failures. "No such record"
# is the system working. Give each a code your callers can branch on.
outcomes: []
#  - code: NOT_FOUND
#    kind: business          # business | denied | error
#    description: "No record matched the identifier."
#    detect:
#      text_present: "No results found"
#    terminal: true
#    retryable: false
#    remediation: "Check the identifier with the caller."

# Screens that appear in front of the one you wanted, and are dismissed.
interstitials: []
#  - name: cookie_banner
#    when: {{text_present: "We use cookies"}}
#    dismiss: [{{click_text: "Accept"}}]
#    max_occurrences: 1

# Conditions that mean "wait and look again" rather than "fail".
transient: []

# How this application shows that the session is gone. Replay uses it to decide
# whether to re-authenticate rather than reporting a confusing failure.
session_expired: null
#  all_of:
#    - {{text_present: "Sign in"}}
#    - {{text_absent: "Sign out"}}

# The capability that signs on here. A forward reference: name it before it
# exists, then record it with `pcx discover --id ...`.
reauth_capability: null

# The application has genuinely broken. Fail fast; do not retry.
hard_error: null
#  any_of:
#    - {{text_present: "Internal Server Error"}}

# Control names that are irreversible in this product, whatever a recording
# claims. Merged into the deployment policy. An empty list means "nothing here
# is risky", so say so deliberately.
irreversible_controls:
  - "\\bDELETE\\b"

# Text that changes between sessions, users or days, and must therefore never
# anchor a locator. Balances, dates, greetings, ids.
volatile_text: []

redact_patterns: []
"""


def cmd_profile(args) -> int:
    """Scaffold a profile for a site the system has never seen."""
    from .config import REPO_ROOT

    directory = settings.profiles_dir
    if args.profile_command == "list":
        from .artifact.profiles import load_all

        for profile in load_all(directory):
            hosts = ", ".join(profile.hosts) or "(no hosts declared)"
            print(f"{profile.app.key():48s} {hosts}")
        return 0

    path = directory / f"{args.key}.yaml"
    if path.exists() and not args.force:
        print(f"{path} already exists. Edit it, or pass --force to overwrite.")
        return 1
    host = args.host or ""
    directory.mkdir(parents=True, exist_ok=True)
    path.write_text(
        PROFILE_TEMPLATE.format(
            host=host,
            vendor=args.vendor or "Unknown",
            product=args.product or args.key,
            version=args.version or "1.0",
        ),
        encoding="utf-8",
    )
    print(f"[profile] {path}")
    print("\nTwo more things before a run can reach that site:")
    print(f"  1. add the host to the deployment allowlist -- {REPO_ROOT / 'policy.yaml'}")
    print(f"     allowed_hosts:\n       - \"{host}\"")
    print("     This is the security boundary, so it is deliberately not edited for you.")
    print(f"  2. register a tenant, which supplies the base URL and the timing budget:")
    print(f"     pcx tenant {args.key.replace('-', '_')} --base-url https://{host} --timing 2.0")
    print(f"\nThen record something:  pcx discover --id <capability> --goal \"...\" \\")
    print(f"                          --entrypoint https://{host}/... --output <name>")
    print("The profile is found from the entry point's host, so --profile is optional.")
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
    discover.add_argument("--param", action="append", metavar="name[=value]",
                          help="a typed input. `name=value` supplies it literally; a bare "
                               "`name` resolves it from $PCX_NAME")
    discover.add_argument("--secret", action="append", metavar="name=ENV_VAR",
                          help="a sensitive input, given as the NAME of an environment variable "
                               "(read from .env too) so the value never reaches your shell "
                               "history or the process list")
    discover.add_argument("--output", action="append", metavar="name",
                          help="an output the agent must capture with `extract`")
    discover.add_argument("--profile", default=None,
                          help="application profile. Omit it and the profile whose `hosts:` "
                               "match the entry point is used")
    discover.add_argument("--setup", help="capability to replay first, e.g. sign_on_coreserv")
    discover.add_argument("--tenant", help="tenant overlay to bind this run to: supplies the base URL, "
                                           "timing multiplier and vocabulary, and pins the run to that host")
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
        cmd.add_argument("--input", action="append", metavar="name[=value]",
                         help="a typed input. `name=value` supplies it literally; a bare "
                              "`name` resolves it from $PCX_<ID>_NAME. Anything required and "
                              "omitted is looked up there too.")
        cmd.add_argument("--secret", action="append", metavar="name=ENV_VAR",
                         help="a sensitive input, given as the NAME of an environment variable")
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

    profile = sub.add_parser("profile", help="application profiles: scaffold one, or list them")
    profile_sub = profile.add_subparsers(dest="profile_command", required=True)
    profile_new = profile_sub.add_parser("new", help="scaffold a profile for a new site")
    profile_new.add_argument("key", help="file stem, e.g. `acme-portal` -> profiles/acme-portal.yaml")
    profile_new.add_argument("--host", help="host this product is served from")
    profile_new.add_argument("--vendor")
    profile_new.add_argument("--product")
    profile_new.add_argument("--version")
    profile_new.add_argument("--force", action="store_true")
    profile_sub.add_parser("list", help="profiles on disk and the hosts they claim")
    profile.set_defaults(func=cmd_profile)

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
