"""The whole thread in one command, on any platform.

    goal -> LLM-driven discovery run -> capability artifact -> deterministic
    replay (inputs, outputs, business outcomes, recovered conditions, hard
    failures, and the irreversible-step guardrail).

Python rather than shell so Windows, macOS and Linux run the identical script --
the demo path should not be the one part of the project that needs a POSIX shell.

Requires the target app to be running (`pcx target`) and, for the discovery half,
a model backend (ANTHROPIC_API_KEY, or the `claude` CLI with PCX_LLM=cli).

    python scripts/demo.py                # everything
    python scripts/demo.py --replay-only  # skip discovery: no model, no tokens
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

from pcx.cli import main as pcx_main  # noqa: E402

BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"
if os.name == "nt" and not os.environ.get("WT_SESSION"):
    BOLD = DIM = RESET = ""  # legacy conhost does not render ANSI


def banner(text: str) -> None:
    print(f"\n{BOLD}== {text}{RESET}")


def run(*argv: str, expect_failure: bool = False) -> int:
    """Invoke the CLI in-process, so there is no shell quoting to get wrong."""
    print(f"{DIM}$ pcx {' '.join(argv)}{RESET}")
    code = pcx_main(list(argv))
    if code != 0 and not expect_failure:
        print(f"{DIM}  (exit {code}){RESET}")
    return code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--replay-only", action="store_true",
        help="skip the two discovery runs (no model access needed, no tokens spent)",
    )
    args = parser.parse_args()

    os.environ.setdefault("PCX_OPERATOR_ID", "TLR0042")
    os.environ.setdefault("PCX_OPERATOR_PASSWORD", "openSesame!42")

    if not args.replay_only:
        banner("1. discovery -- an LLM drives the live application to satisfy a goal")
        run(
            "discover", "--id", "sign_on_coreserv",
            "--goal",
            "Sign on to the CoreServ teller workstation with the supplied operator "
            "credentials. Stop once the workstation main screen with the left-hand "
            "function menu is displayed.",
            "--entrypoint", "http://127.0.0.1:8799/",
            "--param", f"operator_id={os.environ['PCX_OPERATOR_ID']}",
            "--param", f"operator_password={os.environ['PCX_OPERATOR_PASSWORD']}",
            "--output", "signed_on_operator",
            "--max-steps", "8",
        )
        run(
            "discover", "--id", "read_member_savings_balance",
            "--goal",
            "Look up member 10000001 in the CoreServ member inquiry screen and read "
            "back their current SHARE SAVINGS balance and their member name.",
            "--entrypoint", "http://127.0.0.1:8799/desk",
            "--setup", "sign_on_coreserv",
            "--param", "member_number=10000001",
            "--output", "savings_balance", "--output", "member_name",
            "--max-steps", "12",
        )

    banner("2. the artifact -- what a calling agent sees")
    run("show", "read_member_savings_balance", "--tool-schema")

    banner("3. deterministic replay -- same flow, new input, no model in the loop")
    run("replay", "read_member_savings_balance", "--input", "member_number=10000005")

    banner("4. a declared business outcome, not a crash")
    run("replay", "read_member_savings_balance", "--input", "member_number=99999999",
        expect_failure=True)

    banner("5. a bad input, rejected at the contract boundary")
    run("replay", "read_member_savings_balance", "--input", "member_number=1000000",
        expect_failure=True)

    banner("6. an injected application error -- hard failure with evidence")
    run("fault", "app_error")
    run("replay", "read_member_savings_balance", "--input", "member_number=10000001",
        expect_failure=True)

    banner("7. an interstitial the replay recovers from on its own")
    run("fault", "interstitial")
    run("replay", "read_member_savings_balance", "--input", "member_number=10000002")

    banner("8. a session that expires mid-flow -- re-auth by capability composition")
    run("fault", "session_timeout")
    run("replay", "read_member_savings_balance", "--input", "member_number=10000001")

    banner("9. an irreversible step, refused without a human")
    run("replay", "post_transaction_guarded",
        "--input", "account=10000001-0000", "--input", "amount=250.00",
        expect_failure=True)

    print(f"\n{BOLD}done{RESET} -- evidence in ./evidence, artifacts in ./capabilities")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
