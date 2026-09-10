"""Environment check: tells you what works, what does not, and what to run next.

Every check is independent and none of them mutate anything, so this is safe to
run at any point. Exit code is 0 when everything required for the offline test
suite passes, 1 otherwise.

    python scripts/doctor.py
"""

from __future__ import annotations

import importlib
import os
import platform
import shutil
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

OK, WARN, FAIL = "  ok  ", " warn ", " FAIL "
_results: list[tuple[str, str, str]] = []


def check(status: str, label: str, detail: str = "") -> None:
    _results.append((status, label, detail))
    colour = {OK: "\033[32m", WARN: "\033[33m", FAIL: "\033[31m"}.get(status, "")
    print(f"[{colour}{status}\033[0m] {label}" + (f"  — {detail}" if detail else ""))


def port_open(host: str, port: int, timeout: float = 0.6) -> bool:
    with socket.socket() as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0


def main() -> int:
    print(f"pcx doctor — {platform.system()} {platform.release()}, Python {platform.python_version()}")
    if "microsoft" in platform.release().lower() or "WSL_DISTRO_NAME" in os.environ:
        print(f"           running under WSL ({os.environ.get('WSL_DISTRO_NAME', 'unknown distro')})")
    print(f"           repo: {ROOT}\n")

    # -- required for the offline test suite ---------------------------------
    if sys.version_info >= (3, 11):
        check(OK, "Python >= 3.11", platform.python_version())
    else:
        check(FAIL, "Python >= 3.11", f"found {platform.python_version()}; 3.11+ required")

    for module, why in [
        ("pydantic", "artifact schema"),
        ("yaml", "capability + policy files"),
        ("PIL", "screenshot annotation"),
        ("httpx", "Anthropic API backend"),
        ("pytest", "test suite"),
    ]:
        try:
            importlib.import_module(module)
            check(OK, f"import {module}", why)
        except ImportError:
            check(FAIL, f"import {module}", "run: pip install -r requirements.txt")

    try:
        import pcx  # noqa: F401

        check(OK, "import pcx", "the package resolves")
    except ImportError as exc:
        check(FAIL, "import pcx", f"{exc} — run from the repo root, or pip install -e .")

    # -- required for replay (browser, no model) ------------------------------
    try:
        import playwright  # noqa: F401

        check(OK, "import playwright", "browser automation")
    except ImportError:
        check(FAIL, "import playwright", "pip install -r requirements.txt")

    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            path = p.chromium.executable_path
            if path and Path(path).exists():
                check(OK, "Chromium installed", path)
            else:
                check(FAIL, "Chromium installed", "run: python -m playwright install chromium")
    except Exception as exc:
        check(FAIL, "Chromium installed", f"{exc}")

    # Launching is the check that actually catches missing Linux/WSL system
    # libraries, which is the single most common first-run problem.
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            browser.close()
        check(OK, "Chromium launches headless", "system libraries present")
    except Exception as exc:
        hint = "on WSL/Linux run: sudo python3 -m playwright install-deps chromium"
        check(FAIL, "Chromium launches headless", f"{str(exc)[:120]} — {hint}")

    # -- optional: target app, model backend, second tenant -------------------
    for port, label in [(8799, "target app (meridian_cu)"), (8798, "target app (northgate_cu)")]:
        if port_open("127.0.0.1", port):
            check(OK, f"{label} on :{port}", "reachable")
        else:
            cmd = "pcx target" if port == 8799 else "make target2"
            check(WARN, f"{label} on :{port}", f"not running — start it with: {cmd}")

    from pcx.config import DOTENV_LOADED  # importing pcx already loaded .env

    dotenv = ROOT / ".env"
    if dotenv.exists():
        if DOTENV_LOADED:
            check(OK, ".env", f"loaded: {', '.join(sorted(DOTENV_LOADED))}")
        else:
            check(
                WARN, ".env",
                "found but nothing was loaded — every line needs KEY=value "
                "(a bare name with no '=' is skipped), or the values were already "
                "set in this shell",
            )

    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if key:
        source = ".env" if "ANTHROPIC_API_KEY" in DOTENV_LOADED else "shell environment"
        shape = OK if key.startswith("sk-ant-") else WARN
        detail = f"{key[:11]}…{key[-4:]} from the {source}"
        if shape is WARN:
            detail += " — does not look like an Anthropic key (expected sk-ant-…)"
        check(shape, "model backend", detail)
    elif shutil.which("claude"):
        check(OK, "model backend", "claude CLI on PATH — set PCX_LLM=cli")
    else:
        check(WARN, "model backend", "none found — replay still works; `pcx discover` does not")

    creds = [k for k in ("PCX_OPERATOR_ID", "PCX_OPERATOR_PASSWORD") if not os.environ.get(k)]
    if creds:
        check(WARN, "demo credentials", f"unset: {', '.join(creds)} — needed for replay sign-on")
    else:
        check(OK, "demo credentials", "PCX_OPERATOR_ID / PCX_OPERATOR_PASSWORD set")

    # -- repo state -----------------------------------------------------------
    caps = sorted(p.name for p in (ROOT / "capabilities").iterdir() if p.is_dir() and p.name != "tenants")
    check(OK if caps else WARN, "recorded capabilities", ", ".join(caps) or "none — run `pcx discover`")

    print()
    failures = [r for r in _results if r[0] == FAIL]
    warnings = [r for r in _results if r[0] == WARN]
    if failures:
        print(f"\033[31m{len(failures)} blocking problem(s).\033[0m Fix those first:")
        for _s, label, detail in failures:
            print(f"  - {label}: {detail}")
        return 1
    if warnings:
        print(f"\033[33mReady, with {len(warnings)} optional thing(s) not set up.\033[0m")
    else:
        print("\033[32mEverything is ready.\033[0m")
    print("\nNext:  python -m pytest tests/ -q          (offline, no browser or model)")
    print("       pcx target                          (terminal 1)")
    print("       pcx replay read_member_savings_balance --input member_number=10000001")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
