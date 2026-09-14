#!/usr/bin/env python3
"""Draw the mock desktop used as a backdrop for ``docs/screenshot.png``.

A synthetic desktop rather than a real screen capture: it keeps the image
reproducible and free of anyone's private window contents. The dots in the
final screenshot are drawn by the real overlay on top of this — see
``docs/capture_screenshot.sh``.

Usage: python3 docs/make_backdrop.py [output.png]
"""

from __future__ import annotations

import sys
from pathlib import Path

import cairo

WIDTH, HEIGHT = 1520, 950
TAU = 6.283185307179586

DESKTOP = (0.055, 0.06, 0.078)
PANEL = (0.09, 0.095, 0.12)
WINDOW = (0.113, 0.12, 0.15)
TITLEBAR = (0.145, 0.153, 0.19)
TEXT = (0.62, 0.66, 0.74)
ACCENT = (0.42, 0.62, 0.92)
MUTED = (0.29, 0.32, 0.4)


def rounded(context: cairo.Context, x: float, y: float, w: float, h: float, r: float) -> None:
    context.new_sub_path()
    context.arc(x + w - r, y + r, r, -TAU / 4, 0)
    context.arc(x + w - r, y + h - r, r, 0, TAU / 4)
    context.arc(x + r, y + h - r, r, TAU / 4, TAU / 2)
    context.arc(x + r, y + r, r, TAU / 2, 3 * TAU / 4)
    context.close_path()


def bar(context: cairo.Context, x: float, y: float, w: float, h: float, rgb, alpha=1.0) -> None:
    context.set_source_rgba(*rgb, alpha)
    rounded(context, x, y, w, h, h / 2)
    context.fill()


def draw_desktop(context: cairo.Context) -> None:
    context.set_source_rgb(*DESKTOP)
    context.paint()

    # Top panel with a clock and indicators.
    context.set_source_rgb(*PANEL)
    context.rectangle(0, 0, WIDTH, 36)
    context.fill()
    bar(context, 24, 14, 90, 8, MUTED)
    bar(context, WIDTH / 2 - 34, 14, 68, 8, TEXT, 0.8)
    for index in range(3):
        bar(context, WIDTH - 40 - index * 26, 14, 14, 8, MUTED)

    # An editor window, off-centre so the composition is not symmetric.
    wx, wy, ww, wh = 300, 140, 920, 660
    context.set_source_rgba(0, 0, 0, 0.45)
    rounded(context, wx + 6, wy + 10, ww, wh, 12)
    context.fill()
    context.set_source_rgb(*WINDOW)
    rounded(context, wx, wy, ww, wh, 12)
    context.fill()
    context.set_source_rgb(*TITLEBAR)
    rounded(context, wx, wy, ww, 44, 12)
    context.fill()
    context.rectangle(wx, wy + 32, ww, 12)
    context.fill()
    for index, colour in enumerate(((0.85, 0.4, 0.38), (0.88, 0.72, 0.35), (0.45, 0.76, 0.45))):
        context.set_source_rgb(*colour)
        context.arc(wx + 24 + index * 22, wy + 22, 6.5, 0, TAU)
        context.fill()
    bar(context, wx + 110, wy + 18, 160, 8, MUTED)

    # Sidebar.
    context.set_source_rgba(0, 0, 0, 0.18)
    context.rectangle(wx, wy + 44, 190, wh - 44)
    context.fill()
    for index in range(11):
        width = 70 + (index * 37) % 80
        bar(context, wx + 24, wy + 74 + index * 30, width, 7, MUTED, 0.85)

    # Text lines, with a highlighted "current" line.
    lines = [
        (0, 340),
        (1, 260),
        (1, 420),
        (2, 300),
        (2, 380),
        (1, 240),
        (0, 460),
        (1, 300),
        (2, 350),
        (2, 220),
        (1, 400),
        (0, 320),
        (1, 280),
        (2, 430),
        (1, 260),
        (0, 380),
    ]
    context.set_source_rgba(*ACCENT, 0.1)
    context.rectangle(wx + 190, wy + 44 + 8 * 34, ww - 190, 34)
    context.fill()
    for index, (indent, width) in enumerate(lines):
        y = wy + 70 + index * 34
        x = wx + 226 + indent * 28
        rgb = ACCENT if index % 5 == 0 else TEXT
        bar(context, x, y, width, 8, rgb, 0.55 if index % 3 else 0.8)

    # A terminal peeking out behind, to suggest a real session.
    tx, ty, tw, th = 1150, 470, 330, 400
    context.set_source_rgba(0, 0, 0, 0.4)
    rounded(context, tx + 5, ty + 8, tw, th, 10)
    context.fill()
    context.set_source_rgb(0.07, 0.075, 0.095)
    rounded(context, tx, ty, tw, th, 10)
    context.fill()
    for index in range(9):
        bar(
            context, tx + 22, ty + 32 + index * 30, 60 + (index * 53) % 190, 7, (0.4, 0.7, 0.5), 0.6
        )


def main() -> int:
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, WIDTH, HEIGHT)
    context = cairo.Context(surface)
    draw_desktop(context)
    default = Path(__file__).resolve().parent / "backdrop.png"
    output = Path(sys.argv[1]) if len(sys.argv) > 1 else default
    surface.write_to_png(str(output))
    print(f"wrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
