"""Turn runs into GIFs for the README.

Two sources, because the two things worth showing are captured differently.

**From recorded evidence.** Every run already writes an annotated screenshot of
every screen it saw, and ``run.jsonl`` records what the model was thinking when
it saw each one. That is a captioned GIF waiting to happen, and it needs no
screen recorder, no live site and no second run -- the evidence already in the
repository is enough::

    python scripts/capture.py evidence --run discovery-20260909T034714-4663
    python scripts/capture.py evidence --all          # the standard set

**From the desktop.** Some of it is not on one screen: the operator console
beside the browser it is driving, a terminal narrating while a page moves. For
those, record the actual desktop while a scenario runs::

    pip install mss
    python scripts/capture.py desktop --name handoff -- python scripts/demo_handoff.py

Output lands in ``docs/media/``. Sizes are held down deliberately -- a README
that autoplays forty megabytes of GIF is worse than one with no pictures. Frames
that barely differ from their predecessor are dropped, the palette is quantized,
and the last frame is held so the final state is readable before it loops.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

try:
    from PIL import Image, ImageChops, ImageDraw, ImageFont
except ImportError:  # pragma: no cover
    raise SystemExit("This needs Pillow:  pip install pillow")

MEDIA = REPO / "docs" / "media"
EVIDENCE = REPO / "evidence"

#: Caption bar, in the same ink as the operator console, so the media and the
#: product look like one thing rather than two.
BAR_BG = (0x17, 0x1B, 0x22)
BAR_INK = (0xD9, 0xDD, 0xE4)
BAR_DIM = (0x7E, 0x87, 0x98)
ACCENT = (0xE9, 0xBA, 0x6B)
GOOD = (0x7F, 0xBF, 0x9C)
BAD = (0xE0, 0x8A, 0x72)

FONT_CANDIDATES = {
    "sans": [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "C:/Windows/Fonts/segoeui.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ],
    "sans_bold": [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "C:/Windows/Fonts/segoeuib.ttf",
    ],
    "mono": [
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "C:/Windows/Fonts/consola.ttf",
        "/System/Library/Fonts/Menlo.ttc",
    ],
}


def font(kind: str, size: int) -> ImageFont.FreeTypeFont:
    for path in FONT_CANDIDATES[kind]:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


# --------------------------------------------------------------------------- #
# GIF assembly
# --------------------------------------------------------------------------- #


def _difference(a: Image.Image, b: Image.Image) -> float:
    """Fraction of pixels that changed, cheaply, on a downscale."""
    small = (160, 100)
    diff = ImageChops.difference(a.convert("L").resize(small), b.convert("L").resize(small))
    changed = sum(1 for px in diff.getdata() if px > 12)
    return changed / (small[0] * small[1])


def write_gif(
    frames: list[Image.Image],
    out: Path,
    *,
    ms_per_frame: int = 900,
    hold_last_ms: int = 2600,
    width: int = 900,
    min_change: float = 0.0,
    colors: int = 128,
) -> Path:
    """Write frames as a looping GIF, with the last one held.

    The hold matters more than it sounds. Without it a reader sees the outcome --
    the balance, the verdict, the `llm calls: 0` -- for a single frame before the
    loop restarts, which is exactly the frame they came to read.
    """
    if not frames:
        raise SystemExit("nothing to write -- no frames")

    kept: list[Image.Image] = []
    for frame in frames:
        if kept and min_change and _difference(kept[-1], frame) < min_change:
            continue
        kept.append(frame)

    scaled = []
    for frame in kept:
        if frame.width != width:
            height = round(frame.height * width / frame.width)
            frame = frame.resize((width, height), Image.LANCZOS)
        scaled.append(frame.convert("RGB").quantize(colors=colors, method=Image.MEDIANCUT))

    durations = [ms_per_frame] * len(scaled)
    durations[-1] = hold_last_ms

    out.parent.mkdir(parents=True, exist_ok=True)
    scaled[0].save(
        out, save_all=True, append_images=scaled[1:], duration=durations,
        loop=0, optimize=True, disposal=2,
    )
    size_kb = out.stat().st_size / 1024
    print(f"  {out.relative_to(REPO)}  {len(scaled)} frames, {size_kb:.0f} KB")
    if size_kb > 3500:
        print("    (over 3.5 MB -- consider --width 760 or a higher --min-change)")
    return out


def common_crop(paths: list[Path], *, floor: int = 300, pad: int = 26,
                min_colours: int = 6) -> tuple[int, int, int, int] | None:
    """One crop box that fits the content of every frame in a run.

    A legacy screen is mostly empty: the app paints a few hundred pixels and
    leaves the rest of an 800px viewport blank. Cropping to content makes the
    picture about twice as legible at README width.

    A bounding box does not work here, because the frameset's sidebar is a solid
    fill that runs the full height -- every row has "content" by that measure. So
    rows are judged by how many distinct colours they hold: a dead row is the
    sidebar's grey plus the workframe's white and little else, while a row with
    text or a control has many. Scanning up from the bottom for the last row
    above that threshold finds where the application actually stops.

    The box is computed once across the whole run and applied to every frame,
    which is what keeps the GIF from jittering as content grows between steps.
    """
    import numpy as np

    lowest = 0
    width = height = 0
    for path in paths:
        pixels = np.asarray(Image.open(path).convert("RGB"), dtype=np.uint8)
        height, width = pixels.shape[:2]
        packed = (pixels[:, :, 0].astype(np.uint32) << 16
                  | pixels[:, :, 1].astype(np.uint32) << 8
                  | pixels[:, :, 2].astype(np.uint32))
        for row in range(height - 1, lowest, -1):
            if len(np.unique(packed[row])) >= min_colours:
                lowest = max(lowest, row)
                break
    if not width:
        return None
    return (0, 0, width, min(height, max(floor, lowest + pad)))


def caption(frame: Image.Image, lines: list[tuple[str, tuple[int, int, int]]],
            *, title: str = "") -> Image.Image:
    """Put a caption bar under a frame. `lines` is (text, colour) pairs."""
    pad, leading = 16, 22
    bar_h = pad * 2 + leading * len(lines) + (24 if title else 0)
    canvas = Image.new("RGB", (frame.width, frame.height + bar_h), BAR_BG)
    canvas.paste(frame.convert("RGB"), (0, 0))
    draw = ImageDraw.Draw(canvas)

    y = frame.height + pad
    if title:
        draw.text((pad, y), title, font=font("sans_bold", 15), fill=ACCENT)
        y += 24
    for text, colour in lines:
        draw.text((pad, y), text, font=font("mono", 14), fill=colour)
        y += leading
    return canvas


def title_card(width: int, height: int, heading: str, sub: str, note: str = "") -> Image.Image:
    card = Image.new("RGB", (width, height), BAR_BG)
    draw = ImageDraw.Draw(card)
    x, y = 54, int(height * 0.32)
    draw.text((x, y), heading, font=font("sans_bold", 38), fill=BAR_INK)
    y += 54
    for line in textwrap.wrap(sub, 58):
        draw.text((x, y), line, font=font("sans", 21), fill=BAR_DIM)
        y += 30
    if note:
        draw.text((x, y + 14), note, font=font("mono", 16), fill=ACCENT)
    return card


# --------------------------------------------------------------------------- #
# Source 1: a recorded run's own frames
# --------------------------------------------------------------------------- #


def _events(run_dir: Path) -> list[dict]:
    log = run_dir / "run.jsonl"
    if not log.exists():
        raise SystemExit(f"{log} not found")
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


def _kind(event: dict) -> str:
    return event.get("event") or event.get("kind") or ""


def from_evidence(run_id: str, *, out_name: str | None = None, heading: str = "",
                  sub: str = "", **gif) -> Path:
    """Build a captioned GIF from one recorded run.

    The caption is not invented. Each frame is paired with the observation that
    produced it, and with whatever the run recorded next -- the model's own
    one-line rationale on a discovery run, the resolved locator on a replay.
    """
    run_dir = EVIDENCE / run_id
    if not run_dir.exists():
        raise SystemExit(f"no such run: {run_dir}")

    events = _events(run_dir)
    frames: list[Image.Image] = []
    crop = common_crop(sorted((run_dir / "frames").glob("*.png")))

    # Pair each frame with the text that belongs to it.
    for i, event in enumerate(events):
        frame_rel = event.get("frame")
        if not frame_rel:
            continue
        path = EVIDENCE / frame_rel
        if not path.exists():
            continue

        lines: list[tuple[str, tuple[int, int, int]]] = []
        head = ""
        if _kind(event) == "observe":
            head = f"step {event.get('step')}  ·  {event.get('title', '')}"
            lines.append((f"{event.get('elements', '?')} elements  {event.get('route', '')}"[:96], BAR_DIM))
            # the decision this observation led to
            for later in events[i + 1: i + 6]:
                if _kind(later) == "decide" and later.get("thought"):
                    for line in textwrap.wrap(str(later["thought"]), 88)[:2]:
                        lines.append((line, BAR_INK))
                    break
        else:
            head = _kind(event).replace("_", " ")
            for key in ("action", "detail", "reason", "resolution"):
                if event.get(key):
                    lines.append((f"{key}: {event[key]}"[:96], BAR_INK))
        if not lines:
            lines = [("", BAR_DIM)]
        frames.append(caption(_open(path, crop), lines, title=head))

    if not frames:
        # A replay saves its frames but does not name them in the log. Both are
        # numbered by the recorder's own sequence, though, so a frame called
        # `009-s3-after.png` belongs to whatever event was seq 9 or just before
        # it. Pairing on that gets replays captioned without changing what the
        # recorder writes.
        frames = _pair_by_sequence(run_dir, events, crop)
    if not frames:
        raise SystemExit(f"{run_id} has no usable frames")

    # A closing card with the verdict, so the GIF ends on the result.
    end = next((e for e in reversed(events) if _kind(e) in ("run_end", "finish")), None)
    if end:
        verdict = f"{end.get('status', '')}  {end.get('outcome', '')}".strip()
        detail = []
        if "llm_calls" in end:
            detail.append(f"llm calls: {end['llm_calls']}")
        if end.get("outputs"):
            detail.append(f"outputs: {', '.join(end['outputs'])}")
        frames.append(
            title_card(frames[0].width, frames[0].height, verdict or "done",
                       "; ".join(detail), run_id)
        )

    if heading:
        frames.insert(0, title_card(frames[0].width, frames[0].height, heading, sub, run_id))

    return write_gif(frames, MEDIA / f"{out_name or run_id}.gif", **gif)


def _open(path: Path, crop: tuple[int, int, int, int] | None) -> Image.Image:
    image = Image.open(path)
    return image.crop(crop) if crop else image


def _pair_by_sequence(run_dir: Path, events: list[dict],
                      crop: tuple[int, int, int, int] | None = None) -> list[Image.Image]:
    numbered = []
    for event in events:
        seq = event.get("seq")
        if isinstance(seq, int):
            numbered.append((seq, event))
    numbered.sort()

    out: list[Image.Image] = []
    for path in sorted((run_dir / "frames").glob("*.png")):
        stem = path.stem.split("-", 1)[0]
        if not stem.isdigit():
            continue
        seq = int(stem)
        prior = [e for n, e in numbered if n <= seq]
        event = prior[-1] if prior else {}

        head = path.stem.split("-", 1)[-1].replace("-", " ")
        lines: list[tuple[str, tuple[int, int, int]]] = []
        kind = _kind(event)
        if kind == "act":
            head = f"{event.get('step', '')}  ·  {event.get('action', '')}"[:90]
            lines.append((f"resolved {event.get('resolution', '')}", BAR_INK))
        elif kind == "business_outcome":
            head = f"business outcome: {event.get('code', '')}"
            lines.append((str(event.get("detector", ""))[:96], GOOD))
        elif kind in ("reauth", "recovery"):
            head = f"recovery: {event.get('kind')}"
            lines.append((str(event.get("detail", ""))[:96], ACCENT))
        elif kind == "checkpoint":
            head = "checkpoint"
            lines.append((str(event.get("detail", ""))[:96],
                          GOOD if event.get("passed") else BAD))
        else:
            lines.append((kind or "", BAR_DIM))
        out.append(caption(_open(path, crop), lines or [("", BAR_DIM)], title=head))
    return out


# --------------------------------------------------------------------------- #
# Source 2: the desktop, while a scenario runs
# --------------------------------------------------------------------------- #


def from_desktop(name: str, command: list[str], *, fps: float = 4.0,
                 region: tuple[int, int, int, int] | None = None,
                 settle: float = 1.5, **gif) -> Path:
    """Record the screen while `command` runs, then write it as a GIF."""
    try:
        import mss  # noqa: F401
    except ImportError:
        raise SystemExit(
            "Desktop capture needs mss:  pip install mss\n"
            "(Or use `capture.py evidence`, which needs nothing extra.)"
        )
    import mss as mss_mod

    shots: list[Image.Image] = []
    stop = threading.Event()

    def record() -> None:
        with mss_mod.mss() as sct:
            monitor = (
                {"left": region[0], "top": region[1], "width": region[2], "height": region[3]}
                if region else sct.monitors[1]
            )
            while not stop.is_set():
                raw = sct.grab(monitor)
                shots.append(Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX"))
                stop.wait(1.0 / fps)

    print(f"[capture] recording the desktop while: {' '.join(command)}")
    thread = threading.Thread(target=record, daemon=True)
    thread.start()
    try:
        subprocess.run(command, cwd=REPO)
    finally:
        time.sleep(settle)          # let the final screen land before stopping
        stop.set()
        thread.join(timeout=5)

    print(f"[capture] {len(shots)} raw frames")
    gif.setdefault("ms_per_frame", int(1000 / fps))
    gif.setdefault("min_change", 0.004)   # drop frames where nothing moved
    return write_gif(shots, MEDIA / f"{name}.gif", **gif)


# --------------------------------------------------------------------------- #
# The standard set
# --------------------------------------------------------------------------- #

#: run-id prefix -> (output name, heading, subtitle). Prefixes rather than exact
#: ids so this keeps working after the evidence is regenerated.
STANDARD = [
    ("discovery-20260909T034714", "discovery", "Discovery",
     "A goal in English. An LLM drives the live screen, one action at a time, "
     "deciding from what it can see."),
    ("discovery-20260909T034502", "discovery-signon", "Discovery: sign-on",
     "The first capability. Every later run establishes its session by replaying "
     "this one, with no model."),
    ("replay-20260909T042026", "replay-business-outcome", "A declared business outcome",
     "No such member is not a failure -- it is the system working, returned as a "
     "typed outcome with a remediation."),
    ("handoff-20260909T042202", "handoff", "Escalation",
     "A replay cannot proceed, raises an intervention, and an operator takes over "
     "the live session and finishes it by hand."),
]


#: Deliberately absent: the contract-boundary rejection
#: (`--input member_number=1000000`). It produces no frames, because no browser
#: is ever opened -- which is the entire point of that scenario, so a GIF of it
#: would have to be staged. It belongs in the gallery as terminal output and a
#: one-millisecond duration, not as a picture.
#:
#: The gallery: one scenario per thing worth proving. Each runs against the local
#: target app, then its own fresh evidence becomes a GIF -- so the media is
#: regenerated from real runs rather than curated by hand, and cannot drift away
#: from what the system actually does.
SCENARIOS = [
    ("replay-success", "Deterministic replay", None,
     ["replay", "read_member_savings_balance", "--input", "member_number=10000005"],
     "The recorded flow, a member it was not recorded against, and no model in the loop."),
    ("outcome-not-found", "Business outcome: no such member", None,
     ["replay", "read_member_savings_balance", "--input", "member_number=99999999"],
     "Not a failure. A definite answer, returned as a typed outcome with a remediation."),
    ("outcome-denied", "Business outcome: permission denied", None,
     ["replay", "read_member_savings_balance", "--input", "member_number=10000003"],
     "The record exists and this operator may not see it -- distinct from not-found."),
    ("hard-failure", "Hard failure, with evidence", "app_error",
     ["replay", "read_member_savings_balance", "--input", "member_number=10000001"],
     "An unhandled application error. Fail fast, keep a screenshot and a DOM snapshot."),
    ("recover-interstitial", "Recovered: a maintenance banner", "interstitial",
     ["replay", "read_member_savings_balance", "--input", "member_number=10000002"],
     "A screen in the way, dismissed by a declared rule, without the caller noticing."),
    ("recover-session", "Recovered: session expiry mid-flow", "session_timeout",
     ["replay", "read_member_savings_balance", "--input", "member_number=10000001"],
     "Re-authentication by replaying the sign-on capability. Composition, not a login routine."),
    ("guardrail-refused", "Irreversible step, no operator", None,
     ["replay", "post_transaction_guarded",
      "--input", "account=10000001-0000", "--input", "amount=250.00"],
     "The run stops BEFORE the step, not during it."),
    ("guardrail-approved", "The same step, with approval", None,
     ["replay", "post_transaction_guarded",
      "--input", "account=10000001-0000", "--input", "amount=250.00", "--approve"],
     "Same capability, same inputs. The only difference is that a human said yes."),
]


def run_scenarios(only: list[str] | None = None, **gif) -> None:
    """Run each scenario against the local target app and film its own evidence."""
    import urllib.error
    import urllib.request

    try:
        urllib.request.urlopen("http://127.0.0.1:8799/desk", timeout=3)
    except urllib.error.HTTPError:
        pass                      # a redirect to sign-on is the app being up
    except Exception:
        raise SystemExit("The target app is not running. In another terminal:  pcx target")

    before = {d.name for d in EVIDENCE.iterdir() if d.is_dir()}
    for name, heading, fault, command, sub in SCENARIOS:
        if only and name not in only:
            continue
        print(f"\n[{name}] {heading}")
        if fault:
            subprocess.run([sys.executable, "-m", "pcx.cli", "fault", fault],
                           cwd=REPO, capture_output=True)
        subprocess.run([sys.executable, "-m", "pcx.cli", *command], cwd=REPO,
                       capture_output=True)
        fresh = sorted({d.name for d in EVIDENCE.iterdir() if d.is_dir()} - before)
        if not fresh:
            print("  (no new evidence directory -- did the run start?)")
            continue
        before |= set(fresh)
        try:
            from_evidence(fresh[-1], out_name=name, heading=heading, sub=sub, **gif)
        except SystemExit as exc:
            print(f"  (skipped: {exc})")


def standard_set(**gif) -> None:
    runs = {d.name: d for d in EVIDENCE.iterdir() if d.is_dir()}
    for prefix, name, heading, sub in STANDARD:
        match = next((r for r in sorted(runs) if r.startswith(prefix)), None)
        if match is None:
            print(f"  (skipped {name}: no run matching {prefix}*)")
            continue
        from_evidence(match, out_name=name, heading=heading, sub=sub, **gif)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="source", required=True)

    ev = sub.add_parser("evidence", help="build a GIF from a recorded run's own frames")
    ev.add_argument("--run", help="run id under evidence/, or a prefix")
    ev.add_argument("--all", action="store_true", help="build the standard README set")
    ev.add_argument("--name", help="output file stem")
    ev.add_argument("--width", type=int, default=900)
    ev.add_argument("--ms", type=int, default=1400, dest="ms_per_frame")

    sc = sub.add_parser("scenarios",
                        help="run the gallery scenarios against the local app and film each")
    sc.add_argument("--only", nargs="*", help="scenario names to build; default all")
    sc.add_argument("--width", type=int, default=900)
    sc.add_argument("--ms", type=int, default=1100, dest="ms_per_frame")
    sc.add_argument("--list", action="store_true", help="print the scenario names and exit")

    dk = sub.add_parser("desktop", help="record the screen while a scenario runs")
    dk.add_argument("--name", required=True)
    dk.add_argument("--fps", type=float, default=4.0)
    dk.add_argument("--width", type=int, default=900)
    dk.add_argument("--region", help="left,top,width,height -- omit for the whole screen")
    dk.add_argument("command", nargs=argparse.REMAINDER,
                    help="-- then the command to run while recording")

    args = parser.parse_args()
    MEDIA.mkdir(parents=True, exist_ok=True)

    if args.source == "evidence":
        if args.all:
            standard_set(width=args.width, ms_per_frame=args.ms_per_frame)
            return 0
        if not args.run:
            raise SystemExit("give --run <id> or --all")
        runs = [d.name for d in EVIDENCE.iterdir() if d.is_dir()]
        match = args.run if args.run in runs else next((r for r in sorted(runs) if r.startswith(args.run)), None)
        if match is None:
            raise SystemExit(f"no run under evidence/ matching {args.run!r}")
        from_evidence(match, out_name=args.name, width=args.width, ms_per_frame=args.ms_per_frame)
        return 0

    if args.source == "scenarios":
        if args.list:
            for name, heading, *_ in SCENARIOS:
                print(f"  {name:22s} {heading}")
            return 0
        run_scenarios(args.only, width=args.width, ms_per_frame=args.ms_per_frame)
        return 0

    command = [c for c in args.command if c != "--"]
    if not command:
        raise SystemExit("give the command after --, e.g.\n"
                         "  python scripts/capture.py desktop --name handoff -- "
                         "python scripts/demo_handoff.py")
    region = tuple(int(n) for n in args.region.split(",")) if args.region else None
    from_desktop(args.name, command, fps=args.fps, width=args.width, region=region)  # type: ignore[arg-type]
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
