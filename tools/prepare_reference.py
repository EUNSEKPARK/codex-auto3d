#!/usr/bin/env python3
"""Turn character art you already have into reference images the pipeline can use.

`auto3d.py run --reference` feeds a supplied image straight into forge's admission gate, and that
gate wants what a generated reference gives it: one subject, opaque plain backdrop, sensible
margins. Art as delivered usually is not that — transparent PNGs, tight crops, or a whole
turnaround sheet in one file. This prepares it, using only the standard library plus the vendored
forge PNG/JPEG decoders, so it runs anywhere `auto3d.py` runs.

    # a turnaround sheet: cut it up, look at the contact sheet, then name the views
    python3 tools/prepare_reference.py sheet.png --split --out work/refs
    python3 tools/prepare_reference.py sheet.png --split --out work/refs \\
        --views front,hero,side,back,skip

    # a single image
    python3 tools/prepare_reference.py hero.png --out work/refs --view hero

Every figure from one sheet is scaled by the SAME factor and sits on a common baseline, so the
views stay comparable — a per-figure fit would silently change the subject's proportions between
views and the reconstruction would inherit the error. The suggested `auto3d.py run …` command is
printed at the end, and the same data is written to references.json.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from auto3d.util import SKILL_ROOT  # noqa: E402

sys.path.insert(0, str(SKILL_ROOT / "forge" / "stage4_review"))
sys.path.insert(0, str(SKILL_ROOT / "forge" / "_shared"))

from make_comparison_sheet import load_image, write_png_rgb  # noqa: E402

# view name -> (azimuth, elevation) — mirrors auto3d.config.VIEW_CAMERAS
VIEW_CAMERAS = {
    "hero": (35.0, 15.0),
    "front": (0.0, 0.0),
    "side": (90.0, 0.0),
    "back": (180.0, 0.0),
    "top": (0.0, 80.0),
}

Pixel = tuple[int, int, int, int]


# ---------------------------------------------------------------------------
# geometry
# ---------------------------------------------------------------------------


def parse_color(text: str) -> tuple[int, int, int]:
    value = text.strip().lstrip("#")
    if len(value) == 3:
        value = "".join(ch * 2 for ch in value)
    if len(value) != 6:
        raise SystemExit(f"--background expects #rrggbb, got {text!r}")
    return tuple(int(value[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def background_color(pixels: list[Pixel], width: int, height: int) -> tuple[int, int, int]:
    """The most common colour among the four corners — good enough for studio-plate art."""
    corners = [
        pixels[0],
        pixels[width - 1],
        pixels[(height - 1) * width],
        pixels[height * width - 1],
    ]
    counts: dict[tuple[int, int, int], int] = {}
    for r, g, b, _a in corners:
        counts[(r, g, b)] = counts.get((r, g, b), 0) + 1
    return max(counts.items(), key=lambda item: item[1])[0]


def foreground_mask(pixels: list[Pixel], width: int, height: int, *, alpha_threshold: int, tolerance: int) -> list[bool]:
    """True where the subject is. Alpha decides when the image has any; otherwise distance from
    the plate colour does."""
    if any(pixel[3] < 250 for pixel in pixels):
        return [pixel[3] > alpha_threshold for pixel in pixels]
    br, bg, bb = background_color(pixels, width, height)
    return [abs(r - br) + abs(g - bg) + abs(b - bb) > tolerance for r, g, b, _a in pixels]


def column_runs(mask: list[bool], width: int, height: int, *, min_width: int, gap: int) -> list[tuple[int, int]]:
    """Columns that contain subject pixels, grouped into figures. Gaps narrower than `gap` do not
    split a figure (a character's legs can leave a thin empty column)."""
    filled = [any(mask[y * width + x] for y in range(height)) for x in range(width)]
    runs: list[list[int]] = []
    empty = 0
    for x, value in enumerate(filled):
        if value:
            if runs and empty <= gap and runs[-1][1] + empty >= x - gap:
                runs[-1][1] = x + 1
            else:
                runs.append([x, x + 1])
            empty = 0
        else:
            empty += 1
    return [(a, b) for a, b in runs if b - a >= min_width]


def bbox(mask: list[bool], width: int, height: int, x0: int, x1: int) -> tuple[int, int, int, int] | None:
    top, bottom, left, right = height, -1, x1, x0 - 1
    for y in range(height):
        row = y * width
        for x in range(x0, x1):
            if mask[row + x]:
                if y < top:
                    top = y
                bottom = y
                if x < left:
                    left = x
                if x > right:
                    right = x
    if bottom < 0:
        return None
    return left, top, right + 1, bottom + 1


def crop(pixels: list[Pixel], width: int, box: tuple[int, int, int, int]) -> tuple[list[Pixel], int, int]:
    left, top, right, bottom = box
    out: list[Pixel] = []
    for y in range(top, bottom):
        row = y * width
        out.extend(pixels[row + left : row + right])
    return out, right - left, bottom - top


def resize(pixels: list[Pixel], width: int, height: int, out_w: int, out_h: int) -> list[Pixel]:
    """Bilinear, with an integer box pre-reduction when shrinking a lot (pure-python, so this is
    also the cheap path)."""
    while out_w * 2 <= width and out_h * 2 <= height:
        half_w, half_h = width // 2, height // 2
        reduced: list[Pixel] = []
        for y in range(half_h):
            row0, row1 = (2 * y) * width, (2 * y + 1) * width
            for x in range(half_w):
                a, b = pixels[row0 + 2 * x], pixels[row0 + 2 * x + 1]
                c, d = pixels[row1 + 2 * x], pixels[row1 + 2 * x + 1]
                reduced.append(tuple((a[i] + b[i] + c[i] + d[i]) // 4 for i in range(4)))  # type: ignore[arg-type]
        pixels, width, height = reduced, half_w, half_h
    out: list[Pixel] = []
    x_scale = (width - 1) / (out_w - 1) if out_w > 1 else 0.0
    y_scale = (height - 1) / (out_h - 1) if out_h > 1 else 0.0
    for oy in range(out_h):
        sy = oy * y_scale
        y0 = int(sy)
        y1 = min(y0 + 1, height - 1)
        fy = sy - y0
        for ox in range(out_w):
            sx = ox * x_scale
            x0 = int(sx)
            x1 = min(x0 + 1, width - 1)
            fx = sx - x0
            p00 = pixels[y0 * width + x0]
            p01 = pixels[y0 * width + x1]
            p10 = pixels[y1 * width + x0]
            p11 = pixels[y1 * width + x1]
            out.append(
                tuple(
                    int(
                        p00[i] * (1 - fx) * (1 - fy)
                        + p01[i] * fx * (1 - fy)
                        + p10[i] * (1 - fx) * fy
                        + p11[i] * fx * fy
                        + 0.5
                    )
                    for i in range(4)
                )  # type: ignore[arg-type]
            )
    return out


def frame(
    subject: list[Pixel],
    width: int,
    height: int,
    *,
    canvas: int,
    background: tuple[int, int, int],
    baseline: float,
) -> list[tuple[int, int, int]]:
    """Composite the subject onto an opaque square plate, centred, standing on `baseline`."""
    plate = [background] * (canvas * canvas)
    off_x = (canvas - width) // 2
    off_y = int(canvas * baseline) - height
    for y in range(height):
        target_y = off_y + y
        if not 0 <= target_y < canvas:
            continue
        row = y * width
        base = target_y * canvas
        for x in range(width):
            target_x = off_x + x
            if not 0 <= target_x < canvas:
                continue
            r, g, b, a = subject[row + x]
            if a >= 255:
                plate[base + target_x] = (r, g, b)
            elif a > 0:
                br, bg, bb = plate[base + target_x]
                plate[base + target_x] = (
                    (r * a + br * (255 - a)) // 255,
                    (g * a + bg * (255 - a)) // 255,
                    (b * a + bb * (255 - a)) // 255,
                )
    return plate


def contact_sheet(figures: list[tuple[list[Pixel], int, int]], out: Path, background: tuple[int, int, int]) -> None:
    """One strip of every figure found, so the operator can name the views by eye."""
    scale = 220 / max(h for _p, _w, h in figures)
    scaled = [(resize(p, w, h, max(1, int(w * scale)), max(1, int(h * scale))), max(1, int(w * scale)), max(1, int(h * scale))) for p, w, h in figures]
    pad = 20
    width = sum(w for _p, w, _h in scaled) + pad * (len(scaled) + 1)
    height = max(h for _p, _w, h in scaled) + pad * 2
    plate = [background] * (width * height)
    x = pad
    for pixels, w, h in scaled:
        off_y = height - pad - h
        for y in range(h):
            base = (off_y + y) * width
            row = y * w
            for col in range(w):
                r, g, b, a = pixels[row + col]
                if a >= 255:
                    plate[base + x + col] = (r, g, b)
                elif a > 0:
                    br, bg, bb = plate[base + x + col]
                    plate[base + x + col] = ((r * a + br * (255 - a)) // 255, (g * a + bg * (255 - a)) // 255, (b * a + bb * (255 - a)) // 255)
        x += w + pad
    write_png_rgb(out, width, height, plate)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def figure_list(path: Path, args: argparse.Namespace) -> list[tuple[list[Pixel], int, int]]:
    width, height, pixels = load_image(path)
    mask = foreground_mask(pixels, width, height, alpha_threshold=args.alpha_threshold, tolerance=args.tolerance)
    if not any(mask):
        raise SystemExit(f"{path}: no subject found (the whole image reads as background)")
    spans: Iterable[tuple[int, int]]
    if args.split:
        spans = column_runs(mask, width, height, min_width=max(8, width // 40), gap=max(2, width // 200))
        if not spans:
            raise SystemExit(f"{path}: --split found no figures")
    else:
        spans = [(0, width)]
    figures = []
    for x0, x1 in spans:
        box = bbox(mask, width, height, x0, x1)
        if box is None:
            continue
        sub, w, h = crop(pixels, width, box)
        if not any(pixel[3] < 250 for pixel in pixels):
            sub = [(r, g, b, 255) for r, g, b, _a in sub]
        figures.append((sub, w, h))
    return figures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("image", type=Path, help="PNG or JPEG: one subject, or a turnaround sheet with --split")
    parser.add_argument("--out", type=Path, required=True, help="directory for the prepared references")
    parser.add_argument("--split", action="store_true", help="cut a turnaround sheet into one image per figure")
    parser.add_argument("--views", help="comma-separated view names in left-to-right order; 'skip' drops a figure")
    parser.add_argument("--view", help="view name for a single (non-split) image (default: hero)")
    parser.add_argument("--canvas", type=int, default=1024, help="output size in pixels (square, default 1024)")
    parser.add_argument("--fill", type=float, default=0.90, help="fraction of the canvas height the tallest figure takes (default 0.90)")
    parser.add_argument("--baseline", type=float, default=0.97, help="where the figures stand, as a fraction of canvas height (default 0.97)")
    parser.add_argument("--background", default="#f2f2f2", help="plate colour (default #f2f2f2, the pipeline's own backdrop)")
    parser.add_argument("--alpha-threshold", type=int, default=8, dest="alpha_threshold", help="alpha above this counts as subject (default 8)")
    parser.add_argument("--tolerance", type=int, default=30, help="for opaque art: colour distance from the plate that counts as subject (default 30)")
    args = parser.parse_args(argv)

    background = parse_color(args.background)
    figures = figure_list(args.image, args)
    args.out.mkdir(parents=True, exist_ok=True)

    names: list[str]
    if args.views:
        names = [part.strip() for part in args.views.split(",")]
    elif args.split:
        contact = args.out / "contact.png"
        contact_sheet(figures, contact, background)
        print(f"found {len(figures)} figure(s) in {args.image}")
        print(f"contact sheet: {contact}")
        print("\nlook at it, then re-run naming each figure left to right, e.g.:")
        print(f"  python3 tools/prepare_reference.py {args.image} --split --out {args.out} \\")
        print("      --views " + ",".join(["front", "hero", "side", "back", "skip"][: len(figures)]))
        print(f"\nnames: {', '.join(name for name in VIEW_CAMERAS)} · 'skip' drops a figure")
        return 0
    else:
        names = [args.view or "hero"]

    if len(names) != len(figures):
        raise SystemExit(f"--views lists {len(names)} name(s) but {len(figures)} figure(s) were found")
    unknown = [name for name in names if name not in VIEW_CAMERAS and name not in {"skip", "-"}]
    if unknown:
        raise SystemExit(f"unknown view name(s): {unknown}; choose from {', '.join(VIEW_CAMERAS)} or 'skip'")

    # one scale for every figure on the sheet: they were drawn at a common scale, and keeping it
    # is what makes the views measure the same character
    tallest = max(h for _p, _w, h in figures)
    scale = (args.canvas * args.fill) / tallest

    manifest: dict[str, dict[str, object]] = {}
    for name, (pixels, width, height) in zip(names, figures):
        if name in {"skip", "-"}:
            continue
        out_w, out_h = max(1, round(width * scale)), max(1, round(height * scale))
        scaled = resize(pixels, width, height, out_w, out_h)
        plate = frame(scaled, out_w, out_h, canvas=args.canvas, background=background, baseline=args.baseline)
        target = args.out / f"{name}.png"
        write_png_rgb(target, args.canvas, args.canvas, plate)
        azimuth, elevation = VIEW_CAMERAS[name]
        manifest[name] = {"path": str(target), "subjectPx": [out_w, out_h], "azimuth": azimuth, "elevation": elevation}
        print(f"{name:6s} → {target}  (subject {out_w}x{out_h} on {args.canvas}x{args.canvas})")

    if "hero" not in manifest:
        print("\nno 'hero' view was named — auto3d.py run --reference needs one", file=sys.stderr)
        return 1
    (args.out / "references.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    hero = manifest["hero"]
    extra = " ".join(f"--view {name}={data['path']}" for name, data in manifest.items() if name != "hero")
    print("\nrun it with:")
    print(f"  python3 auto3d.py run --reference {hero['path']} {extra} \\")
    print(f"      --reference-camera {hero['azimuth']:.0f},{hero['elevation']:.0f} --profile character --quality standard")
    return 0


if __name__ == "__main__":
    sys.exit(main())
