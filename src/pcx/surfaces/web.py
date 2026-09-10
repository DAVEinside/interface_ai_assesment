"""Chromium surface adapter.

Perceives through the accessibility tree (see :mod:`pcx.perception.ax`) and acts
through **page-space mouse and keyboard events only**. There is exactly one
place in this file that touches a selector -- :meth:`WebSurface.open`, which
takes a URL because "go to an entry point" is the one thing every surface has to
express somehow. No ``page.click(css)``, no ``locator()``, no ``eval_on_selector``
in the action path.

That restriction is not purity for its own sake. It is what makes the recorded
artifact portable: the same ``click(Locator)`` step is executed by a Windows UIA
adapter as "find the control, get its bounding rectangle, send a click at its
centre", which is precisely what happens here.
"""

from __future__ import annotations

import asyncio
import time
from urllib.parse import urlparse

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from ..perception import ax
from .base import Surface, SurfaceError
from .model import Action, ActionOutcome, Observation, UIElement, normalize_text

#: Keys the agent may press, mapped to Playwright key names. Anything outside
#: this table is refused -- it keeps "press" from becoming an escape hatch
#: (e.g. Ctrl+Shift+J opening devtools, or OS-level shortcuts).
KEY_ALIASES = {
    "enter": "Enter",
    "tab": "Tab",
    "shift+tab": "Shift+Tab",
    "escape": "Escape",
    "esc": "Escape",
    "backspace": "Backspace",
    "delete": "Delete",
    "arrowdown": "ArrowDown",
    "arrowup": "ArrowUp",
    "arrowleft": "ArrowLeft",
    "arrowright": "ArrowRight",
    "pagedown": "PageDown",
    "pageup": "PageUp",
    "home": "Home",
    "end": "End",
    "f3": "F3",
    "f5": "F5",
    "f12": "F12",
}


class WebSurface(Surface):
    kind = "web"

    def __init__(
        self,
        *,
        headless: bool = True,
        viewport: tuple[int, int] = (1280, 800),
        slow_mo_ms: int = 0,
        settle_ms: int = 250,
    ) -> None:
        self._headless = headless
        self._viewport = viewport
        self._slow_mo = slow_mo_ms
        self._settle_ms = settle_ms
        self._pw = None
        self._browser: Browser | None = None
        self._ctx: BrowserContext | None = None
        self._page: Page | None = None
        self._cdp = None
        self._inflight = 0
        self._last_observation: Observation | None = None

    # -- lifecycle ------------------------------------------------------------
    async def start(self) -> None:
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=self._headless,
            slow_mo=self._slow_mo,
            args=["--force-color-profile=srgb", "--disable-lcd-text"],
        )
        self._ctx = await self._browser.new_context(
            viewport={"width": self._viewport[0], "height": self._viewport[1]},
            ignore_https_errors=False,
        )
        self._page = await self._ctx.new_page()
        self._page.set_default_timeout(15_000)
        # A legacy app may throw a native dialog at any moment. Auto-dismissing
        # would hide a real exceptional state, so record and surface it instead.
        self._dialogs: list[str] = []
        self._inflight = 0
        self._page.on("dialog", self._on_dialog)
        # In-flight request accounting across every frame. This is what `settle`
        # waits on; see the note there about why networkidle is not enough.
        self._page.on("request", lambda _r: self._bump(1))
        self._page.on("requestfinished", lambda _r: self._bump(-1))
        self._page.on("requestfailed", lambda _r: self._bump(-1))
        self._cdp = await self._ctx.new_cdp_session(self._page)
        await self._cdp.send("Accessibility.enable")
        await self._cdp.send("DOM.enable")
        await self._cdp.send("Page.enable")

    def _bump(self, delta: int) -> None:
        self._inflight = max(0, self._inflight + delta)

    def _on_dialog(self, dialog) -> None:
        self._dialogs.append(f"{dialog.type}: {dialog.message}")
        asyncio.ensure_future(dialog.dismiss())

    async def close(self) -> None:
        for closer in (self._ctx, self._browser):
            try:
                if closer:
                    await closer.close()
            except Exception:
                pass
        if self._pw:
            await self._pw.stop()

    @property
    def page(self) -> Page:
        if self._page is None:
            raise SurfaceError("surface not started")
        return self._page

    # -- perception -----------------------------------------------------------
    async def open(self, entrypoint: str) -> None:
        await self.page.goto(entrypoint, wait_until="domcontentloaded")
        await self.settle()

    async def route(self) -> str:
        parsed = urlparse(self.page.url)
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"

    async def routes(self) -> list[str]:
        """Top-level document plus every loaded frame."""
        seen: list[str] = []
        for frame in self.page.frames:
            parsed = urlparse(frame.url)
            if not parsed.scheme.startswith("http"):
                continue
            location = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
            if location not in seen:
                seen.append(location)
        primary = await self.route()
        return [primary] + [r for r in seen if r != primary]

    async def settle(self, timeout_ms: int = 12000) -> None:
        """Wait until nothing is in flight, counting sub-frame requests.

        ``wait_for_load_state("networkidle")`` watches the main frame. On a
        ``<frameset>`` application almost every navigation happens in a child
        frame, so relying on it alone means observing the *previous* screen
        whenever the host is slow -- which reads as "the app returned the wrong
        page" rather than "we did not wait". Counting requests across every
        frame is what actually answers "has the screen finished changing".
        """
        deadline = time.monotonic() + timeout_ms / 1000
        quiet_since: float | None = None
        while time.monotonic() < deadline:
            if self._inflight <= 0:
                quiet_since = quiet_since or time.monotonic()
                if (time.monotonic() - quiet_since) * 1000 >= self._settle_ms:
                    break
            else:
                quiet_since = None
            await self.page.wait_for_timeout(60)
        try:
            await self.page.wait_for_load_state("domcontentloaded", timeout=1500)
        except Exception:
            pass
        await self.page.wait_for_timeout(60)

    async def observe(self, *, screenshot: bool = True) -> Observation:
        elements = await ax.build_inventory(self._cdp)
        text = await ax.visible_text(self.page)
        obs = Observation(
            surface_kind=self.kind,
            route=await self.route(),
            title=await self.page.title(),
            elements=elements,
            text_digest=text[:6000],
            captured_at=time.time(),
        )
        if self._dialogs:
            obs.text_digest = (
                "[NATIVE DIALOG] " + " | ".join(self._dialogs) + "\n" + obs.text_digest
            )
        self._last_observation = obs
        return obs

    async def screenshot_bytes(self) -> bytes:
        return await self.page.screenshot(full_page=False)

    async def raw_snapshot(self) -> str:
        """Full HTML of every frame -- only ever written on failure."""
        parts = []
        for frame in self.page.frames:
            try:
                html = await frame.content()
            except Exception:
                continue
            parts.append(f"<!-- frame: {frame.name or 'main'} {frame.url} -->\n{html}")
        return "\n\n".join(parts)

    # -- action ---------------------------------------------------------------
    async def act(self, action: Action, element: UIElement | None) -> ActionOutcome:
        started = time.monotonic()
        url_before = self.page.url

        try:
            detail, extracted = await self._dispatch(action, element)
        except Exception as exc:  # surface-level failure, not a business outcome
            return ActionOutcome(
                ok=False,
                detail=f"{type(exc).__name__}: {exc}",
                duration_ms=int((time.monotonic() - started) * 1000),
            )

        return ActionOutcome(
            ok=True,
            detail=detail,
            extracted=extracted,
            duration_ms=int((time.monotonic() - started) * 1000),
            navigated=self.page.url != url_before,
        )

    async def _dispatch(self, action: Action, element: UIElement | None):
        kind = action.kind

        if kind == "wait":
            await self.page.wait_for_timeout(action.ms or 500)
            return f"waited {action.ms or 500}ms", None

        if kind == "navigate":
            if not action.url:
                raise ValueError("navigate requires a url")
            await self.page.goto(action.url, wait_until="domcontentloaded")
            await self.settle()
            return f"navigated to {action.url}", None

        if kind == "press":
            key = KEY_ALIASES.get((action.keys or "").lower())
            if key is None:
                raise ValueError(f"key {action.keys!r} is not in the permitted key table")
            await self.page.keyboard.press(key)
            await self.settle()
            return f"pressed {key}", None

        if element is None:
            raise ValueError(f"action {kind!r} requires a resolved element")

        if kind == "extract":
            return f"read {element.label()}", (element.value or element.name or "")

        if kind == "click":
            await self._click_element(element)
            await self.settle()
            return f"clicked {element.label()}", None

        if kind == "type":
            await self._type_into(element, action.text or "")
            return f"typed into {element.label()}", None

        if kind == "select":
            await self._select_option(element, action.text or "")
            await self.settle()
            return f"selected {action.text!r} in {element.label()}", None

        raise ValueError(f"web surface cannot perform {kind!r}")

    async def _click_element(self, element: UIElement) -> None:
        """Move the pointer to the element's centre and click. Nothing else."""
        x, y = element.rect.cx, element.rect.cy
        await self.page.mouse.move(x, y)
        await self.page.mouse.click(x, y, delay=25)

    async def _type_into(self, element: UIElement, text: str) -> None:
        """Focus by clicking, clear by keyboard, then type character by character.

        Clearing with select-all + Delete rather than a DOM ``value = ''`` write
        is what a human does and what a desktop adapter can also do.
        """
        await self._click_element(element)
        await self.page.keyboard.press("ControlOrMeta+a")
        await self.page.keyboard.press("Delete")
        if text:
            await self.page.keyboard.type(text, delay=12)

    async def _select_option(self, element: UIElement, text: str) -> None:
        """Drive a native combobox from the keyboard.

        Opening the popup and clicking an option is not reliably screenshot-able
        (Chromium renders the popup outside the page), so the portable technique
        is: focus, then type the option's leading characters, which every
        platform's list control honours.
        """
        await self._click_element(element)
        await self.page.keyboard.press("Escape")
        target = normalize_text(text)
        for _ in range(30):
            current = normalize_text(await self._read_value(element))
            if current.upper().startswith(target.upper()) or target.upper() in current.upper():
                return
            await self.page.keyboard.press("ArrowDown")
            await self.page.wait_for_timeout(30)
        raise ValueError(f"option {text!r} not reachable in combobox {element.label()}")

    async def _read_value(self, element: UIElement) -> str:
        backend_id = element.native.get("backend_node_id")
        if backend_id is None:
            return ""
        return (await ax._live_value(self._cdp, backend_id)) or ""

    # -- convenience used by the operator console -----------------------------
    async def human_click(self, x: float, y: float) -> None:
        await self.page.mouse.click(x, y, delay=25)
        await self.settle()

    async def human_type(self, text: str) -> None:
        await self.page.keyboard.type(text, delay=15)

    async def human_press(self, key: str) -> None:
        await self.page.keyboard.press(KEY_ALIASES.get(key.lower(), key))
        await self.settle()
