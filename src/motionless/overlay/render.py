"""Dot layout and animation maths.

Deliberately free of GTK and cairo: given a screen size and a motion state
this module says exactly which circles to paint, which means the visual
behaviour is unit-testable and can be previewed headlessly.

Screen coordinates are the usual ones — x grows right, y grows *down* — while
:class:`~motionless.motion.MotionState` uses the viewer's frame, where forward
is "into the screen". The translation between the two lives in
:func:`displacement` and is the one place signs matter.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from motionless.config import OverlayConfig, parse_color
from motionless.motion import MotionState

#: How much a full-scale vertical jolt grows a dot, as a fraction of its radius.
VERTICAL_RADIUS_GAIN = 0.55


@dataclass(frozen=True)
class Anchor:
    """A dot's resting position and its peripheral weight (0-1)."""

    x: float
    y: float
    weight: float


@dataclass(frozen=True)
class Dot:
    """A circle to paint, in screen coordinates."""

    x: float
    y: float
    radius: float
    alpha: float


@dataclass(frozen=True)
class Style:
    """Rendering parameters resolved from :class:`OverlayConfig`."""

    layout: str
    spacing: float
    radius: float
    margin: float
    rgb: tuple[float, float, float]
    opacity: float
    sensitivity: float
    travel: float
    clamp: float

    @classmethod
    def from_config(cls, overlay: OverlayConfig, *, clamp: float) -> Style:
        red, green, blue, alpha = parse_color(overlay.color)
        return cls(
            layout=overlay.layout,
            spacing=overlay.spacing,
            radius=overlay.radius,
            margin=overlay.margin,
            rgb=(red, green, blue),
            # An alpha channel in the colour scales the configured opacity
            # rather than fighting with it.
            opacity=overlay.opacity * alpha,
            sensitivity=overlay.sensitivity,
            travel=overlay.travel,
            clamp=clamp,
        )


def smoothstep(t: float) -> float:
    """Hermite ease on 0-1, clamped outside that range."""
    t = max(0.0, min(1.0, t))
    return t * t * (3.0 - 2.0 * t)


def anchors(width: int, height: int, style: Style) -> list[Anchor]:
    """Lay out resting dot positions for a screen of ``width`` x ``height``.

    Dots are distributed from a small inset at one edge to the same inset at
    the other, rather than being centred with whatever margin is left over.
    That guarantees the outermost ring sits in the peripheral band on any
    screen size and at any spacing — a centred grid can miss the band
    entirely when ``spacing`` is large, leaving a blank overlay and no clue
    why.

    In ``edges`` layout only dots inside the band are kept: motion cues work
    through peripheral vision, and dots over your text do not.
    """
    if width <= 0 or height <= 0 or style.spacing <= 0:
        return []

    inset = min(style.spacing / 2.0, width / 4.0, height / 4.0)
    span_x = width - 2.0 * inset
    span_y = height - 2.0 * inset
    columns = max(2, round(span_x / style.spacing) + 1)
    rows = max(2, round(span_y / style.spacing) + 1)
    step_x = span_x / (columns - 1)
    step_y = span_y / (rows - 1)
    # The band is never narrower than the outer ring's inset, so that ring
    # always survives the filter below.
    band = max(inset + 1.0, style.margin * min(width, height))

    out: list[Anchor] = []
    for row in range(rows):
        y = inset + row * step_y
        for column in range(columns):
            x = inset + column * step_x
            if style.layout == "grid":
                out.append(Anchor(x, y, 1.0))
                continue
            edge_distance = min(x, y, width - x, height - y)
            if edge_distance > band:
                continue
            out.append(Anchor(x, y, smoothstep(1.0 - edge_distance / band)))
    return out


def displacement(motion: MotionState, style: Style) -> tuple[float, float]:
    """Screen-space offset applied to every dot, in pixels.

    The dots behave like something loose in the cabin: under rightward
    acceleration they slide left, under braking they slide forward (up the
    screen). That is the cue your vestibular system is already predicting.
    """
    dx = -motion.lateral * style.sensitivity
    dy = motion.longitudinal * style.sensitivity
    length = math.hypot(dx, dy)
    if style.travel > 0.0 and length > style.travel:
        scale = style.travel / length
        dx *= scale
        dy *= scale
    return dx, dy


def dots(
    anchor_list: list[Anchor],
    motion: MotionState,
    style: Style,
    visibility: float,
) -> list[Dot]:
    """Build the frame's circles. Returns empty when nothing would be seen."""
    visibility = max(0.0, min(1.0, visibility))
    if visibility <= 0.0 or style.opacity <= 0.0:
        return []

    dx, dy = displacement(motion, style)
    vertical = motion.vertical / style.clamp if style.clamp > 0.0 else 0.0
    radius = style.radius * (1.0 + VERTICAL_RADIUS_GAIN * max(-0.9, min(1.0, vertical)))
    peak = style.opacity * visibility

    return [
        Dot(anchor.x + dx, anchor.y + dy, radius, peak * anchor.weight)
        for anchor in anchor_list
        if peak * anchor.weight > 0.002  # below this nothing is visible anyway
    ]


class CueAnimator:
    """Fades the cue in when the vehicle moves and out when it settles."""

    def __init__(
        self,
        *,
        activation: float = 0.35,
        fade_in: float = 0.3,
        fade_out: float = 1.5,
        always_on: bool = False,
    ) -> None:
        self.activation = activation
        self.fade_in = fade_in
        self.fade_out = fade_out
        self.always_on = always_on
        self.visibility = 1.0 if always_on else 0.0

    def is_active(self, motion: MotionState) -> bool:
        return (
            self.always_on
            or motion.magnitude >= self.activation
            or abs(motion.vertical) >= self.activation
        )

    def update(self, motion: MotionState, dt: float) -> float:
        """Advance the fade by ``dt`` seconds and return the new visibility."""
        dt = max(0.0, dt)
        if self.is_active(motion):
            target, duration = 1.0, self.fade_in
        else:
            target, duration = 0.0, self.fade_out
        if duration <= 0.0:
            self.visibility = target
        else:
            step = dt / duration
            if target > self.visibility:
                self.visibility = min(target, self.visibility + step)
            else:
                self.visibility = max(target, self.visibility - step)
        return self.visibility
