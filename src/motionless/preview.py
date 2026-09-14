"""Render the cue to a PNG, without a display.

Tuning ``sensitivity``, ``spacing`` and ``opacity`` by driving around is a poor
feedback loop. This renders exactly what the overlay would paint for a given
acceleration, so the loop becomes a command instead of a car journey. It needs
only pycairo — no GTK, no X server.
"""

from __future__ import annotations

from pathlib import Path

from motionless.config import Config, ConfigError, parse_color
from motionless.motion import MotionState
from motionless.overlay.render import Style, anchors, dots

TAU = 6.283185307179586


def render_png(
    path: Path,
    config: Config,
    motion: MotionState,
    *,
    width: int = 1280,
    height: int = 800,
    background: str | None = "#1b1d23",
    visibility: float = 1.0,
) -> Path:
    """Write a preview of the overlay to ``path`` and return it.

    ``background`` may be ``None`` for a transparent PNG, which is what you
    want when compositing the preview over a real screenshot.
    """
    try:
        import cairo
    except ImportError as exc:  # pragma: no cover - depends on the host
        raise RuntimeError(
            "preview needs pycairo: sudo apt install python3-gi-cairo (or pip install pycairo)"
        ) from exc

    if width <= 0 or height <= 0:
        raise ConfigError("preview width and height must be positive")

    style = Style.from_config(config.overlay, clamp=config.motion.clamp)
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
    context = cairo.Context(surface)

    if background is not None:
        red, green, blue, alpha = parse_color(background)
        context.set_source_rgba(red, green, blue, alpha)
        context.paint()

    red, green, blue = style.rgb
    for dot in dots(anchors(width, height, style), motion, style, visibility):
        context.set_source_rgba(red, green, blue, dot.alpha)
        context.arc(dot.x, dot.y, dot.radius, 0.0, TAU)
        context.fill()

    path.parent.mkdir(parents=True, exist_ok=True)
    surface.write_to_png(str(path))
    return path
