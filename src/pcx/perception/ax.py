"""Accessibility-tree perception for Chromium, via raw CDP.

Why the accessibility tree and not the DOM
------------------------------------------
The brief's environment is legacy back-office software: framesets, nested layout
tables, generated ids like ``ctl00_wf_txt3``, and inputs with no ``<label for>``.
CSS selectors over that markup encode *incidental structure*. The accessibility
tree encodes *what the control is* -- and it is the exact same abstraction a
Windows adapter gets from UI Automation and a macOS adapter gets from AXUIElement.
Building perception on it means the artifact schema does not have to change when
the surface does.

Two properties of Chromium's CDP make this practical:

* ``Accessibility.getFullAXTree`` accepts a ``frameId``, so a ``<frameset>`` is
  handled by walking ``Page.getFrameTree`` -- no frame-targeting selectors.
* ``DOM.getBoxModel`` returns geometry already in **top-level page coordinates**,
  even for nodes inside child frames, so no coordinate translation is needed and
  clicks can be issued as plain page-space mouse events.

What comes out is a flat inventory of :class:`~pcx.surfaces.model.UIElement`,
which is all any layer above this module ever sees.
"""

from __future__ import annotations

import asyncio
from typing import Any

from ..surfaces.model import Rect, Role, UIElement, normalize_text

#: Chromium AX role -> canonical role. Anything unmapped is dropped, except the
#: structural text roles handled explicitly below.
_ROLE_MAP = {
    "textbox": Role.TEXTBOX,
    "searchbox": Role.TEXTBOX,
    "spinbutton": Role.TEXTBOX,
    "button": Role.BUTTON,
    "link": Role.LINK,
    "combobox": Role.COMBOBOX,
    "listbox": Role.COMBOBOX,
    "menulistpopup": Role.COMBOBOX,
    "checkbox": Role.CHECKBOX,
    "radio": Role.RADIO,
    "heading": Role.HEADING,
    "cell": Role.CELL,
    "gridcell": Role.CELL,
    "columnheader": Role.CELL,
    "rowheader": Role.CELL,
    "LayoutTableCell": Role.CELL,
    "StaticText": Role.TEXT,
    "image": Role.IMAGE,
    "img": Role.IMAGE,
}

#: Roles whose live value we bother to read back from the page. Reading a value
#: costs two extra CDP round trips per node, so it is restricted to fields.
_VALUE_ROLES = {Role.TEXTBOX, Role.COMBOBOX, Role.CHECKBOX, Role.RADIO}

#: Cap on nodes we will geometry-resolve, to keep observation latency bounded on
#: pathological pages. Legacy screens are small; this is a guard, not a design.
MAX_NODES = 400


async def _box(cdp, backend_id: int) -> Rect | None:
    try:
        model = await cdp.send("DOM.getBoxModel", {"backendNodeId": backend_id})
    except Exception:
        return None
    quad = model["model"]["content"]
    xs = quad[0::2]
    ys = quad[1::2]
    x, y = min(xs), min(ys)
    w, h = max(xs) - x, max(ys) - y
    if w <= 0 or h <= 0:
        return None
    return Rect(x=x, y=y, w=w, h=h)


async def _live_value(cdp, backend_id: int) -> str | None:
    """Read ``el.value`` / checkedness for a field, via the object protocol.

    This is still not DOM *querying* -- we already have the node from the AX
    tree; we are only asking it what it currently contains, which every
    accessibility API (UIA ``ValuePattern``, AX ``AXValue``) also exposes.
    """
    try:
        resolved = await cdp.send("DOM.resolveNode", {"backendNodeId": backend_id})
        obj_id = resolved["object"]["objectId"]
        res = await cdp.send(
            "Runtime.callFunctionOn",
            {
                "objectId": obj_id,
                "functionDeclaration": (
                    "function(){"
                    "  if (this.tagName === 'SELECT') {"
                    "    return this.options[this.selectedIndex] ? this.options[this.selectedIndex].text : '';"
                    "  }"
                    "  if (this.type === 'checkbox' || this.type === 'radio') return this.checked ? 'checked' : '';"
                    "  return this.value === undefined ? null : String(this.value);"
                    "}"
                ),
                "returnByValue": True,
            },
        )
        await cdp.send("Runtime.releaseObject", {"objectId": obj_id})
        return res.get("result", {}).get("value")
    except Exception:
        return None


def _props(node: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for p in node.get("properties", []) or []:
        out[p.get("name")] = (p.get("value") or {}).get("value")
    return out


async def collect_frames(cdp) -> list[tuple[str, str]]:
    """Return ``[(frame_id, frame_name_or_url_tail), ...]`` depth-first."""
    tree = await cdp.send("Page.getFrameTree")
    found: list[tuple[str, str]] = []

    def walk(node, inherited=""):
        frame = node["frame"]
        name = frame.get("name") or inherited or frame["url"].rsplit("/", 1)[-1] or "main"
        found.append((frame["id"], name))
        for child in node.get("childFrames", []) or []:
            walk(child)

    walk(tree["frameTree"])
    return found


async def build_inventory(cdp, *, read_values: bool = True) -> list[UIElement]:
    """Walk every frame's AX tree and return a flat, deduplicated element list."""
    elements: list[UIElement] = []
    counter = 0
    budget = MAX_NODES

    for frame_id, region in await collect_frames(cdp):
        try:
            tree = await cdp.send("Accessibility.getFullAXTree", {"frameId": frame_id})
        except Exception:
            continue

        for node in tree.get("nodes", []):
            if budget <= 0:
                break
            raw_role = (node.get("role") or {}).get("value")
            role = _ROLE_MAP.get(raw_role)
            if role is None:
                continue
            if node.get("ignored"):
                continue
            name = normalize_text((node.get("name") or {}).get("value"))
            backend_id = node.get("backendDOMNodeId")
            if backend_id is None:
                continue
            rect = await _box(cdp, backend_id)
            budget -= 1
            if rect is None or rect.area < 1:
                continue
            if role is Role.TEXT and not name:
                continue

            props = _props(node)
            value = None
            if read_values and role in _VALUE_ROLES:
                value = await _live_value(cdp, backend_id)

            counter += 1
            elements.append(
                UIElement(
                    ref=f"e{counter}",
                    role=role,
                    name=name,
                    value=value,
                    rect=rect,
                    region=region,
                    enabled=props.get("disabled") is not True,
                    focusable=bool(props.get("focusable")),
                    editable=bool(props.get("editable")) or role is Role.TEXTBOX,
                    native={"ax_role": raw_role, "backend_node_id": backend_id},
                )
            )

    return _dedupe(elements)


def _dedupe(elements: list[UIElement]) -> list[UIElement]:
    """Drop text nodes that merely restate a cell or a control they sit inside.

    Chromium reports ``LayoutTableCell 'SHARE SAVINGS'`` *and* the
    ``StaticText 'SHARE SAVINGS'`` inside it, and reports a button's label twice.
    Keeping both would double every anchor and make ambiguity checks useless.
    """
    kept: list[UIElement] = []
    non_text = [e for e in elements if e.role is not Role.TEXT]

    for e in elements:
        if e.role is not Role.TEXT:
            kept.append(e)
            continue
        redundant = False
        for other in non_text:
            if other.role is Role.TEXT:
                continue
            same_place = other.rect.contains_point(e.rect.cx, e.rect.cy)
            same_words = e.name and (e.name in other.name or other.name in e.name)
            if same_place and same_words:
                redundant = True
                break
        if not redundant:
            kept.append(e)

    # Exact duplicates (same role, name and near-identical box) collapse to one.
    seen: set[tuple[str, str, int, int]] = set()
    unique: list[UIElement] = []
    for e in kept:
        key = (e.role.value, e.name, round(e.rect.x), round(e.rect.y))
        if key in seen:
            continue
        seen.add(key)
        unique.append(e)

    # Reading order: top-to-bottom, then left-to-right, in 6px bands. Stable refs
    # matter because the LLM addresses elements by ref within a single turn.
    unique.sort(key=lambda e: (round(e.rect.y / 6), e.rect.x))
    for i, e in enumerate(unique, start=1):
        e.ref = f"e{i}"
    return unique


async def visible_text(page) -> str:
    """Concatenate visible text across all frames, whitespace-normalized."""
    chunks: list[str] = []
    for frame in page.frames:
        try:
            txt = await frame.evaluate(
                "() => document.body ? document.body.innerText : ''"
            )
        except Exception:
            continue
        txt = normalize_text(txt)
        if txt:
            chunks.append(txt)
    return "\n".join(chunks)


async def gather(cdp, page, *, read_values: bool = True):
    """Convenience: inventory + text in parallel-ish."""
    inventory = await build_inventory(cdp, read_values=read_values)
    text = await visible_text(page)
    await asyncio.sleep(0)
    return inventory, text
