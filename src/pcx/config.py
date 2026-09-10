"""Runtime configuration. Everything overridable by environment, `.env`, or CLI flag."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def load_dotenv(path: Path | None = None, *, override: bool = False) -> list[str]:
    """Read ``.env`` into the process environment. Returns the names it set.

    Hand-rolled rather than a dependency: this needs to parse ``KEY=value`` and
    nothing else, and a credentials path is a bad place for surprises.

    A variable already present in the real environment wins unless ``override``
    is set — the usual dotenv rule, and the one that lets a shell export
    temporarily beat the file without editing it.

    ``.env`` is gitignored. It is the right home for an API key on a developer
    machine and the wrong one for a deployment, which should inject real
    environment variables instead.
    """
    path = path or REPO_ROOT / ".env"
    if not path.exists():
        return []

    applied: list[str] = []
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key:
            # A bare `ANTHROPIC_API_KEY` with no `=value` is the most common
            # mistake here; skip it rather than setting an empty string, which
            # would look like "a key is configured" to every check downstream.
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if not value:
            continue
        if key in os.environ and not override:
            continue
        os.environ[key] = value
        applied.append(key)
    return applied


#: Loaded once, at import, so every entry point sees it -- CLI, scripts, tests.
DOTENV_LOADED = load_dotenv()


@dataclass
class Settings:
    base_url: str = field(default_factory=lambda: os.environ.get("PCX_BASE_URL", "http://127.0.0.1:8799"))
    capabilities_dir: Path = field(
        default_factory=lambda: Path(os.environ.get("PCX_CAPABILITIES", REPO_ROOT / "capabilities"))
    )
    evidence_dir: Path = field(
        default_factory=lambda: Path(os.environ.get("PCX_EVIDENCE", REPO_ROOT / "evidence"))
    )
    profiles_dir: Path = field(
        default_factory=lambda: Path(os.environ.get("PCX_PROFILES", REPO_ROOT / "profiles"))
    )
    policy_file: Path = field(
        default_factory=lambda: Path(os.environ.get("PCX_POLICY", REPO_ROOT / "policy.yaml"))
    )
    headless: bool = field(default_factory=lambda: os.environ.get("PCX_HEADLESS", "1") != "0")
    console_port: int = field(default_factory=lambda: int(os.environ.get("PCX_CONSOLE_PORT", "8765")))
    #: "always" while developing, "on_failure" in production -- screenshots of a
    #: banking screen are regulated data and are not kept by default.
    capture_frames: str = field(default_factory=lambda: os.environ.get("PCX_FRAMES", "always"))
    llm_backend: str = field(default_factory=lambda: os.environ.get("PCX_LLM", "auto"))

    def operator_credentials(self) -> dict[str, str]:
        return {
            "operator_id": os.environ.get("PCX_OPERATOR_ID", ""),
            "operator_password": os.environ.get("PCX_OPERATOR_PASSWORD", ""),
        }


settings = Settings()
