"""Model backends for the discovery loop.

The loop needs exactly one thing from a model: given a rendered observation,
return the next action as JSON. That narrow interface is :class:`LLMClient`, and
two backends implement it.

``AnthropicMessagesClient``
    The production path. Talks to the Messages API over HTTPS with an
    ``ANTHROPIC_API_KEY``, and sends the annotated screenshot as a real image
    block alongside the element inventory -- so the model is doing genuine
    vision-plus-structure perception, the same way a human operator would.

``ClaudeCliClient``
    A local backend that shells out to the ``claude`` CLI in headless JSON mode.
    Same prompt, same JSON contract, same loop; it exists so the project can be
    demonstrated on a machine that has a Claude Code login but no raw API key,
    which is how the committed evidence run in ``evidence/`` was produced.
    It is a real model call, not a stub -- there is no simulated backend in this
    repository, because a simulated discovery run would prove nothing.

Both backends return the same :class:`Decision`. Neither is allowed to be
consulted during replay.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import shutil
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

DEFAULT_API_MODEL = os.environ.get("PCX_MODEL", "claude-sonnet-4-5-20250929")


class LLMError(RuntimeError):
    """A model backend failed.

    ``fatal`` marks the failures that retrying cannot fix -- a bad or missing
    API key, a model the account cannot reach. The discovery loop stops on those
    immediately instead of spending two more calls to learn the same thing.
    """

    def __init__(self, message: str, *, fatal: bool = False) -> None:
        super().__init__(message)
        self.fatal = fatal


@dataclass
class Decision:
    """One model turn, parsed."""

    raw: str
    thought: str
    action: dict[str, Any]
    usage: dict[str, Any] = field(default_factory=dict)
    model: str = ""


class LLMClient(Protocol):
    name: str
    model: str

    async def decide(self, system: str, user: str, image_path: str | None = None) -> Decision: ...


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #

_JSON_BLOCK = re.compile(r"\{.*\}", re.S)


def parse_decision(text: str) -> tuple[str, dict[str, Any]]:
    """Pull ``{"thought": ..., "action": {...}}`` out of a model reply.

    Tolerant on the outside (fenced code, prose around it), strict on the inside:
    the action must be an object with a ``kind``. A model that will not produce a
    parseable action is a loop failure, not something to guess around -- guessing
    is how an agent ends up clicking a random control on a banking screen.
    """
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", candidate, flags=re.S).strip()
    match = _JSON_BLOCK.search(candidate)
    if not match:
        raise LLMError(f"no JSON object in model reply: {text[:400]!r}")
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise LLMError(f"model reply was not valid JSON ({exc}): {match.group(0)[:400]!r}") from exc
    action = payload.get("action")
    if not isinstance(action, dict) or "kind" not in action:
        raise LLMError(f"model reply has no usable action: {payload!r}")
    return str(payload.get("thought", "")), action


# --------------------------------------------------------------------------- #
# Anthropic Messages API
# --------------------------------------------------------------------------- #


class AnthropicMessagesClient:
    """Direct Messages API client. Multimodal, no SDK dependency."""

    name = "anthropic-messages"

    def __init__(
        self,
        api_key: str | None = None,
        model: str = DEFAULT_API_MODEL,
        *,
        base_url: str | None = None,
        max_tokens: int = 1024,
        timeout_s: float = 120.0,
    ) -> None:
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        if not self.api_key:
            raise LLMError(
                "ANTHROPIC_API_KEY is not set. Export it, or use --llm cli with the "
                "Claude Code CLI installed.",
                fatal=True,
            )
        self.model = model
        self.base_url = (base_url or os.environ.get("ANTHROPIC_API_URL") or "https://api.anthropic.com").rstrip("/")
        self.max_tokens = max_tokens
        self.timeout_s = timeout_s

    async def decide(self, system: str, user: str, image_path: str | None = None) -> Decision:
        content: list[dict[str, Any]] = []
        if image_path and os.path.exists(image_path):
            data = base64.b64encode(open(image_path, "rb").read()).decode()
            content.append(
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": data},
                }
            )
        content.append({"type": "text", "text": user})

        body = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": content}],
        }
        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            resp = await client.post(
                f"{self.base_url}/v1/messages",
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json=body,
            )
        if resp.status_code in (401, 403):
            raise LLMError(
                f"messages API {resp.status_code}: the ANTHROPIC_API_KEY was rejected. "
                f"Check the key and that it has access to {self.model!r}. "
                f"({resp.text[:200]})",
                fatal=True,
            )
        if resp.status_code == 404:
            raise LLMError(
                f"messages API 404: model {self.model!r} not found for this account. "
                "Set PCX_MODEL to one you can use.",
                fatal=True,
            )
        if resp.status_code >= 400:
            raise LLMError(f"messages API {resp.status_code}: {resp.text[:500]}")
        payload = resp.json()
        text = "".join(block.get("text", "") for block in payload.get("content", []))
        thought, action = parse_decision(text)
        return Decision(
            raw=text,
            thought=thought,
            action=action,
            usage=payload.get("usage", {}),
            model=payload.get("model", self.model),
        )


# --------------------------------------------------------------------------- #
# Claude CLI (headless)
# --------------------------------------------------------------------------- #


class ClaudeCliClient:
    """Headless ``claude -p`` backend.

    The screenshot is handed over as a filesystem path with the ``Read`` tool
    allowed, which is how the CLI ingests images. Everything else -- the system
    prompt, the observation rendering, the JSON contract -- is identical to the
    API backend, so a run produced here and a run produced there are comparable.
    """

    name = "claude-cli"

    def __init__(
        self,
        model: str = os.environ.get("PCX_CLI_MODEL", "sonnet"),
        *,
        binary: str = "claude",
        timeout_s: float = 180.0,
        vision: bool = True,
    ) -> None:
        resolved = shutil.which(binary)
        if resolved is None:
            raise LLMError(f"{binary!r} is not on PATH")
        # On Windows the CLI is installed as a .cmd shim, which
        # create_subprocess_exec cannot execute directly -- it needs the command
        # processor. Resolving through `which` first also avoids a PATH lookup
        # difference between the parent shell and the child process.
        if os.name == "nt" and resolved.lower().endswith((".cmd", ".bat")):
            self._argv0 = [os.environ.get("COMSPEC", "cmd.exe"), "/c", resolved]
        else:
            self._argv0 = [resolved]
        self.binary = resolved
        self.model = model
        self.timeout_s = timeout_s
        self.vision = vision

    async def decide(self, system: str, user: str, image_path: str | None = None) -> Decision:
        prompt_parts = [system, "", user]
        args = [
            *self._argv0,
            "-p",
            "--output-format",
            "json",
            "--model",
            self.model,
        ]
        if self.vision and image_path and os.path.exists(image_path):
            prompt_parts.insert(
                2,
                f"An annotated screenshot of the current screen is at {image_path} -- "
                "read it with the Read tool before deciding.",
            )
            args += ["--allowed-tools", "Read"]

        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate("\n".join(prompt_parts).encode()), timeout=self.timeout_s
            )
        except asyncio.TimeoutError as exc:
            proc.kill()
            raise LLMError(f"claude CLI timed out after {self.timeout_s}s") from exc

        if proc.returncode != 0:
            raise LLMError(f"claude CLI exited {proc.returncode}: {stderr.decode()[:500]}")

        try:
            envelope = json.loads(stdout.decode())
        except json.JSONDecodeError as exc:
            raise LLMError(f"claude CLI did not return JSON: {stdout.decode()[:400]!r}") from exc

        text = envelope.get("result") or ""
        thought, action = parse_decision(text)
        usage = envelope.get("usage", {}) or {}
        return Decision(
            raw=text,
            thought=thought,
            action=action,
            usage={
                "input_tokens": usage.get("input_tokens"),
                "output_tokens": usage.get("output_tokens"),
                "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
                "cost_usd": envelope.get("total_cost_usd"),
            },
            model=next(iter(envelope.get("modelUsage", {}) or {}), self.model),
        )


def build_client(backend: str = "auto", *, vision: bool = True) -> LLMClient:
    """Pick a backend. ``auto`` prefers the API key, then the CLI."""
    if backend in ("api", "anthropic"):
        return AnthropicMessagesClient()
    if backend in ("cli", "claude-cli"):
        return ClaudeCliClient(vision=vision)
    if os.environ.get("ANTHROPIC_API_KEY"):
        return AnthropicMessagesClient()
    return ClaudeCliClient(vision=vision)
