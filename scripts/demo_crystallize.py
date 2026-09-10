"""The crystallization lifecycle, end to end, on any platform.

A discovered capability earns its way down the execution-type spectrum on
evidence, gets knocked back up by the circuit breaker when it regresses, and
recovers after a clean streak. No model access needed -- every run here is a
deterministic replay.

    python scripts/demo_crystallize.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

from pcx.artifact.schema import Crystallization, Evidence  # noqa: E402
from pcx.artifact.store import CapabilityStore  # noqa: E402
from pcx.cli import main as pcx_main  # noqa: E402
from pcx.config import settings  # noqa: E402

CAP = "read_member_savings_balance"
BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"
if os.name == "nt" and not os.environ.get("WT_SESSION"):
    BOLD = DIM = RESET = ""


def banner(text: str) -> None:
    print(f"\n{BOLD}-- {text}{RESET}")


def run(*argv: str) -> int:
    return pcx_main(list(argv))


def reset_to_as_discovered() -> None:
    """Put the capability back where a fresh discovery run leaves it."""
    store = CapabilityStore(settings.capabilities_dir)
    capability = store.load(CAP)
    tests = capability.crystallization.acceptance_tests
    capability.crystallization = Crystallization(
        execution_type=3, status="candidate", evidence=Evidence(), acceptance_tests=tests
    )
    capability.tenant = None
    store.update(capability)
    ledger = store.ledger_path(CAP)
    if ledger.exists():
        ledger.unlink()


def main() -> int:
    os.environ.setdefault("PCX_OPERATOR_ID", "TLR0042")
    os.environ.setdefault("PCX_OPERATOR_PASSWORD", "openSesame!42")

    banner("starting point: freshly discovered, Type 3")
    reset_to_as_discovered()
    run("status", CAP)

    banner("three successful replays with distinct inputs")
    for member in ("10000001", "10000002", "10000005"):
        run("replay", CAP, "--input", f"member_number={member}")

    banner("promotion gate: Type 3 -> Type 2")
    run("status", CAP)
    run("promote", CAP)

    banner("five more runs, then the gate to fully deterministic")
    for member in ("10000001", "10000002", "10000004", "10000005", "10000001"):
        run("replay", CAP, "--input", f"member_number={member}")
    print(f"{DIM}# refused first: promotion to Type 1 also needs a human review flag{RESET}")
    run("promote", CAP)
    run("promote", CAP, "--mark-reviewed")
    run("status", CAP)

    banner("regression: the application throws CSV-500 under a promoted capability")
    run("fault", "app_error")
    run("replay", CAP, "--input", "member_number=10000001")
    run("status", CAP)

    banner("recovery: clean runs return it to candidacy")
    for member in ("10000001", "10000002", "10000005"):
        run("replay", CAP, "--input", f"member_number={member}")
    run("status", CAP)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
