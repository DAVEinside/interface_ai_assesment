"""Wiring. Builds a session (surface + policy + evidence + broker) and runs things.

Kept separate from :mod:`pcx.cli` so the same entry points can be called from a
service, a test, or another agent, without going through argument parsing.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from .agent.llm import build_client
from .agent.loop import DiscoveryLoop, DiscoveryRequest, DiscoveryResult
from .agent.reporter import ConsoleReporter, NullReporter
from .artifact.compile import compile_capability
from .artifact import profiles
from .artifact.profiles import AppProfile
from .artifact.schema import Capability, TenantOverlay as TenantOverlayT
from .artifact.store import CapabilityStore
from .config import Settings
from .crystallize import lifecycle
from .escalation.broker import SessionBroker
from .escalation.console import build_app, serve
from .evidence.recorder import EvidenceRecorder, new_run_id
from .policy.allowlist import DeploymentPolicy, PolicyEngine
from .policy.redact import Redactor
from .replay.engine import ReplayEngine, ReplayOptions, _credentials_for, missing_credentials_for
from .replay.outcomes import ReplayResult
from .surfaces.web import WebSurface


class SetupFailed(RuntimeError):
    """`--setup` could not establish the precondition.

    Carried as its own type so the CLI can print it as an explanation. A
    traceback here tells the reader about asyncio internals; what they need to
    know is which capability failed and what to set.
    """


class Session:
    """One live surface plus everything attached to it."""

    def __init__(self, settings: Settings, mode: str, *, run_id: str | None = None) -> None:
        self.settings = settings
        self.deployment = DeploymentPolicy.load(settings.policy_file)
        self.store = CapabilityStore(settings.capabilities_dir)
        self.redactor = Redactor(extra_patterns=self.deployment.redact_patterns)
        self.recorder = EvidenceRecorder(
            root=settings.evidence_dir,
            run_id=run_id or new_run_id(mode),
            mode=mode,
            redactor=self.redactor,
            capture_frames=settings.capture_frames,  # type: ignore[arg-type]
        )
        self.surface = WebSurface(headless=settings.headless)
        self.broker = SessionBroker()
        self.broker.run_id = self.recorder.run_id
        self._console_server = None
        self._console_task = None

    async def __aenter__(self) -> "Session":
        await self.surface.start()
        self.broker.surface = self.surface
        return self

    async def __aexit__(self, *exc) -> None:
        if self._console_server is not None:
            self._console_server.should_exit = True
            try:
                await asyncio.wait_for(self._console_task, timeout=5)
            except Exception:
                pass
        await self.surface.close()

    async def start_console(self) -> str:
        app = build_app(self.broker, self.recorder)
        self._console_server, self._console_task = await serve(
            app, port=self.settings.console_port
        )
        self.broker.console_url = f"http://127.0.0.1:{self.settings.console_port}/"
        return self.broker.console_url

    def profile(self, key: str | None) -> AppProfile | None:
        if not key:
            return None
        return AppProfile.find(key, self.settings.profiles_dir)


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #


async def run_discovery(
    settings: Settings,
    *,
    goal: str,
    entrypoint: str,
    parameters: dict[str, str],
    outputs: list[str],
    capability_id: str,
    profile_key: str | None,
    setup_capability: str | None = None,
    tenant: str | None = None,
    max_steps: int = 24,
    with_console: bool = False,
    llm_backend: str | None = None,
    compile_result: bool = True,
    verbose: bool = True,
) -> tuple[DiscoveryResult, Capability | None]:
    async with Session(settings, "discovery") as session:
        if with_console:
            url = await session.start_console()
            print(f"[console] operator console at {url}")

        # `--profile` used to default to "coreserv-7.2", which meant pointing the
        # system at any other site and forgetting the flag silently applied a
        # mock credit union's error vocabulary, interstitials, session-expiry
        # detectors and irreversible-control patterns to a site they describe
        # nothing about. The entry point already says which product this is.
        profile = session.profile(profile_key)
        if profile is None and not profile_key:
            profile = profiles.for_host(host_of(entrypoint), settings.profiles_dir)
            if profile is not None:
                print(f"[profile] {profile.app.key()} (matched on host)")
            else:
                print(f"[profile] none matches {host_of(entrypoint)} -- running without one. "
                      f"`pcx profile new <key> --host {host_of(entrypoint)}` starts one.")
        deployment_policy = session.deployment
        if profile is not None:
            deployment_policy.irreversible_control_patterns = list(
                dict.fromkeys(deployment_policy.irreversible_control_patterns + profile.irreversible_controls)
            )

        # An explicit --tenant supplies the overlay; otherwise the run's own
        # entry point says which deployment this is. Either way it is *this*
        # host, never the default one.
        overlay = session.store.load_tenant(tenant) if tenant else None
        run_base_url = overlay.base_url if overlay else origin_of(entrypoint)

        # Establish preconditions deterministically, by replaying an existing
        # capability rather than teaching the model to log in every time.
        if setup_capability:
            await _run_setup(
                session, setup_capability, settings, base_url=run_base_url, overlay=overlay
            )

        llm = build_client(llm_backend or settings.llm_backend)
        loop = DiscoveryLoop(
            surface=session.surface,
            llm=llm,
            # Declaring a tenant declares that this run belongs to it, so the
            # same cross-tenant pin the replay engine applies applies here. Left
            # unpinned without --tenant, where discovery is exploratory.
            policy=PolicyEngine(
                deployment_policy, tenant_host=host_of(run_base_url) if overlay else None
            ),
            recorder=session.recorder,
            redactor=session.redactor,
            broker=session.broker,
            reporter=ConsoleReporter() if verbose else NullReporter(),
        )
        request = DiscoveryRequest(
            goal=goal,
            entrypoint=entrypoint,
            parameters=parameters,
            wanted_outputs=outputs,
            capability_id=capability_id,
            max_steps=max_steps,
            app=(profile.app.model_dump() if profile else None),
        )
        result = await loop.run(request)

        trace_path = session.recorder.dir / "trace.json"
        trace_path.write_text(
            json.dumps(session.redactor.scrub_obj(result.trace.redacted_dump()), indent=2, default=str),
            encoding="utf-8",
        )
        # The full trace keeps each step's perceived inventory, which is what the
        # compiler needs. Keeping it means a capability can be re-crystallized
        # after the compiler improves, without paying for the model run again.
        (session.recorder.dir / "trace.full.json").write_text(
            json.dumps(
                session.redactor.scrub_obj(result.trace.model_dump(mode="json")), indent=2, default=str
            ),
            encoding="utf-8",
        )
        session.recorder.finish(
            {
                "capability_id": capability_id,
                "status": result.trace.status,
                "stop_reason": result.trace.stop_reason,
                "steps": len(result.trace.steps),
                "model": result.trace.model,
                "usage": result.trace.usage,
                "outputs_bound": list(result.trace.outputs),
            }
        )

        capability = None
        if compile_result and result.trace.status == "succeeded":
            capability = compile_capability(
                result.trace,
                capability_id=capability_id,
                profile=profile,
                base_url=run_base_url,
            )
            path = session.store.save(capability)
            _write_fixtures(session.store, capability, result.trace.parameters)
            print(f"[compile] {capability.ref()} -> {path}")
        return result, capability


def _write_fixtures(store: CapabilityStore, capability: Capability, parameters: dict) -> None:
    """Persist sensitive acceptance-test inputs outside the shared artifact.

    The fixture directory is gitignored: these are real identifiers from the
    system the capability was recorded against, and they belong on the machine
    that recorded it, not in the repository the artifact is shared through.
    """
    for test in capability.crystallization.acceptance_tests:
        if not test.inputs_ref:
            continue
        directory = store.root / capability.id / "fixtures"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{test.inputs_ref}.json").write_text(
            json.dumps(dict(parameters), indent=2), encoding="utf-8"
        )


def load_fixture(store: CapabilityStore, capability_id: str, ref: str) -> dict:
    path = store.root / capability_id / "fixtures" / f"{ref}.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def bind_default_tenant(
    capability: Capability, settings: Settings, base_url: str | None = None
) -> Capability:
    """Point a capability at a deployment when no overlay is supplied.

    Artifacts store ``{{ tenant.base_url }}`` rather than a host, so an artifact
    is never bound to one institution by accident. Something has to supply the
    binding at run time; with no ``--tenant`` that is ``base_url``, defaulting to
    the local deployment.
    """
    from .artifact.schema import TenantOverlay

    resolved = base_url or settings.base_url
    capability.tenant = TenantOverlay(
        tenant_id="default",
        base_url=resolved,
        label="local deployment" if resolved == settings.base_url else host_of(resolved),
        notes="Implicit binding used when no --tenant was supplied.",
    )
    return capability


def host_of(url: str) -> str:
    return url.split("//", 1)[-1].split("/", 1)[0]


def origin_of(url: str) -> str:
    """Scheme and host, no path -- the base a `{{ tenant.base_url }}` resolves to."""
    if "//" not in url:
        return url
    scheme, _, rest = url.partition("//")
    return f"{scheme}//{rest.split('/', 1)[0]}"


async def _run_setup(
    session: Session,
    capability_id: str,
    settings: Settings,
    *,
    base_url: str | None = None,
    overlay: "TenantOverlayT | None" = None,
) -> None:
    capability = session.store.load(capability_id)
    if overlay is not None:
        capability = capability.specialize(overlay)
    else:
        # A setup capability exists to prepare *this run's* surface, so it belongs
        # on the host the run is about to use -- not on whatever the default
        # deployment happens to be. Binding it to settings.base_url meant
        # `--setup sign_on_parabank --entrypoint https://parabank...` signed on to
        # localhost, and the only symptom was a connection refused to a port the
        # user had no reason to expect in the command they typed.
        capability = bind_default_tenant(capability, settings, base_url)
    resolved_base = base_url or (overlay.base_url if overlay else settings.base_url)
    engine = ReplayEngine(
        session.surface,
        PolicyEngine(
            session.deployment,
            capability.policy,
            tenant_host=host_of(capability.resolved_entrypoint(resolved_base)),
        ),
        session.recorder,
        session.redactor,
        broker=session.broker,
        options=ReplayOptions(),
        # A re-auth raised *inside* setup has to land on the same host, for the
        # same reason the top-level replay binds its loader.
        capability_loader=(
            (lambda cid: session.store.load(cid).specialize(overlay))
            if overlay is not None
            else (lambda cid: bind_default_tenant(session.store.load(cid), settings, base_url))
        ),
    )
    # The setup capability is whatever the caller named, so its inputs are
    # resolved the same generic way the replay engine re-authenticates -- not from
    # a hard-coded pair of CoreServ operator variables, which is what this used to
    # do and which made `--setup` silently unusable for every other application.
    missing = missing_credentials_for(capability)
    if missing:
        raise SetupFailed(
            f"cannot run --setup {capability_id}: its credentials are not in the environment\n"
            + "\n".join(missing)
            + "\n\n.env is read automatically; an exported shell variable takes precedence."
        )
    result = await engine.run(capability, _credentials_for(capability))
    print(f"[setup] {capability.ref()} -> {result.caller_summary()}")
    if result.status != "success":
        raise SetupFailed(
            f"--setup {capability_id} did not complete: {result.caller_summary()}\n"
            f"The run it was preparing for has not started. Evidence: {session.recorder.dir}"
        )


# --------------------------------------------------------------------------- #
# Replay
# --------------------------------------------------------------------------- #


async def run_replay(
    settings: Settings,
    *,
    capability_id: str,
    inputs: dict[str, Any],
    version: int | None = None,
    tenant: str | None = None,
    escalate: bool = False,
    with_console: bool = False,
    approve_irreversible: bool = False,
    record_evidence: bool = True,
) -> ReplayResult:
    async with Session(settings, "replay") as session:
        if with_console:
            url = await session.start_console()
            print(f"[console] operator console at {url}")

        capability = session.store.load(capability_id, version)
        if tenant:
            overlay = session.store.load_tenant(tenant)
            capability = capability.specialize(overlay)
            # Composed capabilities (re-authentication, sub-flows) must be bound
            # to the *same* tenant. Signing on to a different institution's
            # instance is the kind of cross-tenant mistake that has to be
            # impossible by construction, not caught in review.
            loader = lambda cid: session.store.load(cid).specialize(overlay)  # noqa: E731
        else:
            capability = bind_default_tenant(capability, settings)
            loader = lambda cid: bind_default_tenant(session.store.load(cid), settings)  # noqa: E731

        engine = ReplayEngine(
            session.surface,
            PolicyEngine(
                session.deployment,
                capability.policy,
                tenant_host=host_of(capability.resolved_entrypoint(settings.base_url)),
            ),
            session.recorder,
            session.redactor,
            # No console means no operator is listening, so a risky step is
            # refused outright rather than parking the run on a request nobody
            # will ever see. "Ask a human" is only a strategy when there is one.
            broker=session.broker if (with_console or escalate) else None,
            # If a handoff is possible at all, the run has to be able to say it is
            # waiting for one.
            reporter=ConsoleReporter() if (with_console or escalate) else NullReporter(),
            options=ReplayOptions(approve_irreversible=approve_irreversible),
            capability_loader=loader,
        )
        # A capability's own credentials are never in the artifact and should not
        # have to be on the command line either -- that is where re-auth and
        # `--setup` already get them from, and a password typed into a shell is a
        # password in that shell's history. Anything the caller did not supply is
        # filled in from the environment, by the same candidate variables.
        inputs = _fill_from_environment(capability, dict(inputs))
        result = await engine.run(capability, inputs)

        if escalate and result.status == "failed":
            result = await engine.escalate_failure(capability, result)

        if record_evidence:
            _record_and_apply_lifecycle(session, capability, result, inputs)
        return result


def _fill_from_environment(capability: Capability, inputs: dict[str, Any]) -> dict[str, Any]:
    """Supply any required input the caller omitted, from $PCX_<ID>_<NAME>.

    Only fills what is missing, so an explicit ``--input`` always wins, and only
    required inputs, so an optional parameter is not silently given a value
    nobody asked for.
    """
    from .config import resolve_credential

    for param in capability.contract.inputs:
        if param.name in inputs or not param.required or param.default is not None:
            continue
        value, env_key = resolve_credential(capability.id, param.name)
        if value is not None:
            inputs[param.name] = value
            shown = env_key if param.sensitivity in ("secret", "pii") else f"{env_key}={value}"
            print(f"[input] {param.name} <- ${shown}")
    return inputs


def _record_and_apply_lifecycle(session: Session, capability: Capability, result: ReplayResult, inputs) -> None:
    action_sequence = "|".join(f"{s.action}:{s.id}" for s in result.steps)
    input_key = "|".join(f"{k}={_key_of(k, v, capability)}" for k, v in sorted(inputs.items()))
    lifecycle.record_run(
        session.store,
        capability,
        result,
        action_sequence=action_sequence,
        input_key=input_key,
        # Was defaulted to 0 at every call site, which made the "human
        # interventions must be 0" promotion gate vacuously true -- it read as a
        # safety property and could never fire.
        human_interventions=result.human_interventions,
    )
    stored = session.store.load(capability.id, capability.version)
    stored = lifecycle.refresh_evidence(session.store, stored)

    # Promotion is the other half of the lifecycle and was manual-only: the
    # breaker demoted automatically while nothing ever moved a capability the
    # other way without someone typing `pcx promote`. The paper's whole claim is
    # that a flow crystallizes as evidence accumulates, and the breaker is what
    # makes doing it automatically safe -- being wrong is recoverable before a
    # human notices. The T2 -> T1 gate still requires a human review flag, so
    # this promotes as far as Type 2 on its own and no further.
    reason = lifecycle.should_demote(result)
    if not reason and session.settings.auto_promote and stored.crystallization.status != "quarantined":
        changed, message = lifecycle.promote(stored)
        if changed:
            session.store.update(stored)
            print(f"[crystallize] {message}")

    if reason and stored.crystallization.status == "active":
        # The breaker exists to protect a capability that has been *promoted*.
        # A candidate that fails simply fails to accumulate evidence: the gates
        # already refuse to promote it, so quarantining as well would only make
        # the first bad replay of a new capability look like a regression.
        changed, message = lifecycle.demote(stored, reason)
        session.store.update(stored)
        print(f"[circuit-breaker] {message}")
    elif lifecycle.clear_quarantine(stored):
        session.store.update(stored)
        print(f"[circuit-breaker] quarantine cleared for {stored.ref()} after clean runs")


def _key_of(name: str, value: Any, capability: Capability) -> str:
    """Ledger keys must distinguish input sets without recording their values."""
    import hashlib

    param = next((p for p in capability.contract.inputs if p.name == name), None)
    if param is not None and param.sensitivity in ("pii", "pii_reference", "secret"):
        return "h:" + hashlib.sha256(str(value).encode()).hexdigest()[:10]
    return str(value)


# --------------------------------------------------------------------------- #
# Acceptance tests
# --------------------------------------------------------------------------- #


async def run_acceptance_tests(settings: Settings, capability_id: str) -> list[dict[str, Any]]:
    store = CapabilityStore(settings.capabilities_dir)
    capability = store.load(capability_id)
    results: list[dict[str, Any]] = []
    for test in capability.crystallization.acceptance_tests:
        supplied = dict(test.inputs)
        if test.inputs_ref:
            supplied.update(load_fixture(store, capability_id, test.inputs_ref))
        # Secrets are never in a fixture either; they come from the environment
        # of the machine running the test, like any other credential.
        for param in capability.contract.inputs:
            if param.sensitivity == "secret" or supplied.get(param.name) == "[REDACTED]":
                supplied[param.name] = os.environ.get(f"PCX_{param.name.upper()}", "")
        result = await run_replay(
            settings,
            capability_id=capability_id,
            inputs=supplied,
            record_evidence=False,
        )
        passed = result.outcome == test.expect_outcome
        missing = [name for name in test.expect_outputs_present if name not in result.outputs]
        shape_failures = []
        import re as _re

        for name, pattern in test.expect_output_matches.items():
            value = result.outputs.get(name)
            raw = value.get("raw") if isinstance(value, dict) else value
            if raw is None or not _re.search(pattern, str(raw)):
                shape_failures.append(f"{name} !~ /{pattern}/ (got {raw!r})")
        passed = passed and not missing and not shape_failures
        results.append(
            {
                "test": test.name,
                "passed": passed,
                "expected_outcome": test.expect_outcome,
                "actual_outcome": result.outcome,
                "missing_outputs": missing,
                "shape_failures": shape_failures,
                "run_id": result.run_id,
            }
        )
    return results


# --------------------------------------------------------------------------- #
# Recompilation
# --------------------------------------------------------------------------- #


def recompile_from_trace(
    settings: Settings,
    trace_path: str | Path,
    *,
    capability_id: str | None = None,
    profile_key: str | None = "coreserv-7.2",
) -> Capability:
    """Re-crystallize a stored discovery trace with the current compiler.

    Compilers improve; model runs are expensive and, on a production system, not
    freely repeatable. A capability is therefore a *derived* artifact, and the
    trace is the source. Recompiling an old trace and diffing the result is also
    how a compiler change gets reviewed.
    """
    from .agent.trace import RunTrace

    data = json.loads(Path(trace_path).read_text(encoding="utf-8"))
    trace = RunTrace.model_validate(data)
    if not trace.steps or not any(s.context_elements for s in trace.steps):
        raise ValueError(
            f"{trace_path} has no per-step inventories; recompile needs trace.full.json"
        )
    profile = AppProfile.find(profile_key, settings.profiles_dir) if profile_key else None
    capability = compile_capability(
        trace,
        capability_id=capability_id or trace.run_id,
        profile=profile,
        base_url=settings.base_url,
    )
    store = CapabilityStore(settings.capabilities_dir)
    store.save(capability)
    _write_fixtures(store, capability, trace.parameters)
    return capability
