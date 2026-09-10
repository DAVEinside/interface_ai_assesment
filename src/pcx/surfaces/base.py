"""The Surface abstraction: the only thing that knows how to see and touch an app.

A ``Surface`` is deliberately narrow. It can:

  * ``open`` an entry point,
  * ``observe`` -- return a structured :class:`~pcx.surfaces.model.Observation`,
  * ``act`` -- perform one :class:`~pcx.surfaces.model.Action` against an element
    that the caller already resolved from the current observation,
  * hand out raw evidence (screenshot bytes, an adapter-defined state dump).

It cannot: interpret a goal, decide what to do next, or know anything about
capabilities. That asymmetry is what lets the same recorded flow run on a
Chromium page today and a Windows UI Automation tree later.

Contract notes for anyone writing a new adapter
-----------------------------------------------
* ``observe()`` must be *cheap enough to call before every action* and must
  return page-space geometry for every element it reports.
* ``act()`` must locate the element **by geometry**, not by re-querying a
  selector. Clicking is "put the pointer at this point and press"; typing is
  "focus this point, then send these keystrokes". Anything else quietly
  reintroduces the DOM dependency the whole design is trying to avoid.
* ``route()`` should return something coarse and stable (a URL path, a window
  class + caption). It is used for allowlisting, so it must not be spoofable
  by page content.
"""

from __future__ import annotations

import abc

from .model import Action, ActionOutcome, Observation, UIElement


class SurfaceError(RuntimeError):
    """Adapter-level failure: the surface itself is unusable or gone."""


class Surface(abc.ABC):
    """Abstract perceive/act port."""

    #: Short identifier recorded in artifacts, e.g. ``"web"`` or ``"windows_uia"``.
    kind: str = "abstract"

    @abc.abstractmethod
    async def start(self) -> None:
        """Bring the surface up (launch browser / attach to app)."""

    @abc.abstractmethod
    async def open(self, entrypoint: str) -> None:
        """Navigate to an entry point (URL, window, menu path)."""

    @abc.abstractmethod
    async def observe(self, *, screenshot: bool = True) -> Observation:
        """Perceive current state."""

    @abc.abstractmethod
    async def act(self, action: Action, element: UIElement | None) -> ActionOutcome:
        """Perform one action. ``element`` is pre-resolved by the caller."""

    @abc.abstractmethod
    async def route(self) -> str:
        """Coarse, non-spoofable location used for allowlist checks."""

    async def routes(self) -> list[str]:
        """Every location currently loaded, not just the primary one.

        A frameset keeps its top-level URL while navigating a child frame
        anywhere it likes, so allowlisting only :meth:`route` would leave a hole
        wide enough to drive a whole application through. Adapters that have
        sub-surfaces (frames, child windows, MDI panes) must report them here.
        """
        return [await self.route()]

    @abc.abstractmethod
    async def screenshot_bytes(self) -> bytes:
        """PNG bytes of the current surface, for evidence."""

    async def raw_snapshot(self) -> str:
        """Adapter-defined deep dump captured only on failure (DOM, UIA XML, ...)."""
        return ""

    @abc.abstractmethod
    async def close(self) -> None:
        """Tear down."""

    # -- optional capabilities ------------------------------------------------
    async def settle(self, timeout_ms: int = 8000) -> None:
        """Block until the surface looks quiescent. Best-effort by default."""
        return None
