"""Set-of-marks annotation: draw the element inventory onto the screenshot.

The agent gets two views of the same perception -- a numbered list of controls
and a screenshot with those same numbers drawn on it. The numbering is what ties
them together, so a vision model can say "click e7" and mean something exact.
The annotated frames are also the most useful single piece of evidence a human
reviewer can look at when a run goes wrong.
"""

from __future__ import annotations

import io

from PIL import Image, ImageDraw, ImageFont

from ..surfaces.model import INTERACTIVE_ROLES, Role, UIElement

_PALETTE = {
    Role.TEXTBOX: (0, 122, 204),
    Role.BUTTON: (204, 51, 0),
    Role.LINK: (0, 128, 64),
    Role.COMBOBOX: (128, 0, 160),
    Role.CHECKBOX: (150, 90, 0),
    Role.RADIO: (150, 90, 0),
    Role.CELL: (110, 110, 110),
    Role.HEADING: (30, 30, 30),
}


def _font(size: int = 11):
    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ):
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


def annotate(
    png_bytes: bytes,
    elements: list[UIElement],
    *,
    include_cells: bool = True,
    highlight: str | None = None,
) -> bytes:
    """Return PNG bytes with a numbered box drawn around each element."""
    img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    draw = ImageDraw.Draw(img)
    font = _font(11)

    for e in elements:
        if e.role is Role.TEXT:
            continue
        if e.role is Role.CELL and not include_cells:
            continue
        colour = _PALETTE.get(e.role, (90, 90, 90))
        width = 1 if e.role in (Role.CELL, Role.HEADING) else 2
        if highlight and e.ref == highlight:
            colour = (255, 0, 255)
            width = 3
        box = (e.rect.x, e.rect.y, e.rect.x + e.rect.w, e.rect.y + e.rect.h)
        draw.rectangle(box, outline=colour, width=width)

        if e.role in INTERACTIVE_ROLES or highlight == e.ref:
            tag = e.ref
            tw = draw.textlength(tag, font=font)
            tx = max(0.0, e.rect.x)
            ty = max(0.0, e.rect.y - 13)
            draw.rectangle((tx, ty, tx + tw + 6, ty + 13), fill=colour)
            draw.text((tx + 3, ty + 1), tag, fill=(255, 255, 255), font=font)

    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()


def inventory_text(elements: list[UIElement], *, max_rows: int = 120) -> str:
    """The textual half of the set-of-marks view, as handed to the model."""
    lines = []
    for e in elements[:max_rows]:
        bits = [f"{e.ref:>5}", f"{e.role.value:<8}"]
        bits.append(f"@({int(e.rect.cx)},{int(e.rect.cy)})")
        if e.region:
            bits.append(f"[{e.region}]")
        if e.name:
            bits.append(f'name="{e.name[:70]}"')
        if e.value:
            bits.append(f'value="{e.value[:40]}"')
        if not e.enabled:
            bits.append("DISABLED")
        lines.append(" ".join(bits))
    if len(elements) > max_rows:
        lines.append(f"... {len(elements) - max_rows} more elements omitted")
    return "\n".join(lines)
