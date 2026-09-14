"""Motion model: raw accelerometer samples in, stable screen-space cues out.

Everything here is pure Python with no GTK or hardware dependency so it can be
unit tested directly. The pipeline is:

    raw sample  ->  gravity estimate (low-pass)  ->  linear acceleration
                ->  axis remap  ->  smoothing (low-pass)  ->  dead zone
                ->  MotionState

Units are SI throughout: acceleration in m/s^2, time in seconds.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

#: Standard gravity, m/s^2.
GRAVITY = 9.80665


def _ema_alpha(dt: float, tau: float) -> float:
    """Frame-rate independent smoothing factor for an exponential filter.

    ``tau`` is the time constant in seconds: after ``tau`` seconds the filter
    has covered ~63% of a step change. A non-positive ``tau`` disables
    smoothing (alpha of 1.0, i.e. follow the input exactly).
    """
    if tau <= 0.0:
        return 1.0
    if dt <= 0.0:
        return 0.0
    return 1.0 - math.exp(-dt / tau)


@dataclass(frozen=True)
class MotionSample:
    """A raw accelerometer reading in the sensor's own frame, in m/s^2."""

    x: float
    y: float
    z: float
    timestamp: float = field(default_factory=time.monotonic)

    def scaled(self, factor: float) -> MotionSample:
        return MotionSample(self.x * factor, self.y * factor, self.z * factor, self.timestamp)


@dataclass(frozen=True)
class MotionState:
    """Gravity-compensated motion expressed in the viewer's frame.

    ``lateral`` is positive when the vehicle accelerates to the viewer's right,
    ``longitudinal`` is positive under forward acceleration (negative when
    braking), and ``vertical`` is positive when pushed upward (a bump).
    """

    lateral: float = 0.0
    longitudinal: float = 0.0
    vertical: float = 0.0
    timestamp: float = 0.0

    @property
    def magnitude(self) -> float:
        """Planar magnitude of the cue, in m/s^2.

        Vertical motion is deliberately excluded: it drives dot size rather
        than dot displacement, and including it would make bumps look like
        cornering.
        """
        return math.hypot(self.lateral, self.longitudinal)

    @property
    def is_still(self) -> bool:
        return self.magnitude == 0.0 and self.vertical == 0.0


#: A motion state with no movement at all.
STILL = MotionState()

#: Axis order accepted by :class:`AxisMap`, in the sensor's own frame.
_AXES = ("x", "y", "z")


@dataclass(frozen=True)
class AxisMap:
    """Maps sensor axes onto viewer axes.

    Accelerometer orientation varies wildly between laptops, tablets and phone
    bridges, so the mapping is configuration rather than a hardcoded guess.
    Each field names a sensor axis, optionally prefixed with ``-`` to invert
    it: ``lateral = "-x"`` means "the viewer's right is the sensor's -x".
    """

    lateral: str = "x"
    longitudinal: str = "y"
    vertical: str = "z"

    def __post_init__(self) -> None:
        for name in ("lateral", "longitudinal", "vertical"):
            spec = getattr(self, name)
            axis, _ = _parse_axis(spec, name)
            del axis

    def apply(self, x: float, y: float, z: float) -> tuple[float, float, float]:
        values = {"x": x, "y": y, "z": z}
        out = []
        for name in ("lateral", "longitudinal", "vertical"):
            axis, sign = _parse_axis(getattr(self, name), name)
            out.append(values[axis] * sign)
        return out[0], out[1], out[2]


def _parse_axis(spec: str, field_name: str) -> tuple[str, float]:
    text = spec.strip().lower()
    sign = 1.0
    if text.startswith("-"):
        sign = -1.0
        text = text[1:]
    elif text.startswith("+"):
        text = text[1:]
    if text not in _AXES:
        raise ValueError(
            f"invalid axis {spec!r} for {field_name}: expected one of "
            f"{', '.join(_AXES)}, optionally prefixed with '-'"
        )
    return text, sign


class MotionProcessor:
    """Turns a stream of raw samples into a smoothed, gravity-free cue.

    The gravity estimate is a slow low-pass of the raw signal; subtracting it
    leaves linear acceleration and — as a bonus — automatically adapts when the
    device is re-oriented (laptop on a lap, tablet in a mount) without needing
    a calibration step.
    """

    def __init__(
        self,
        *,
        axis_map: AxisMap | None = None,
        gravity_tau: float = 5.0,
        smoothing_tau: float = 0.12,
        dead_zone: float = 0.15,
        clamp: float = 6.0,
    ) -> None:
        if clamp <= 0.0:
            raise ValueError("clamp must be positive")
        if dead_zone < 0.0:
            raise ValueError("dead_zone must not be negative")
        self.axis_map = axis_map or AxisMap()
        self.gravity_tau = gravity_tau
        self.smoothing_tau = smoothing_tau
        self.dead_zone = dead_zone
        self.clamp = clamp

        self._gravity: tuple[float, float, float] | None = None
        self._smoothed: tuple[float, float, float] = (0.0, 0.0, 0.0)
        self._last_timestamp: float | None = None
        self.state = STILL

    def reset(self) -> None:
        """Forget the gravity estimate and smoothing history."""
        self._gravity = None
        self._smoothed = (0.0, 0.0, 0.0)
        self._last_timestamp = None
        self.state = STILL

    @property
    def has_gravity_estimate(self) -> bool:
        return self._gravity is not None

    def update(self, sample: MotionSample) -> MotionState:
        """Feed one raw sample and return the resulting motion state."""
        dt = 0.0
        if self._last_timestamp is not None:
            dt = max(0.0, sample.timestamp - self._last_timestamp)
        self._last_timestamp = sample.timestamp

        raw = (sample.x, sample.y, sample.z)
        previous = self._gravity
        if previous is None:
            # Seed gravity with the first sample so we do not spend the first
            # few seconds rendering a bogus 1g "acceleration".
            gravity = raw
        else:
            a = _ema_alpha(dt, self.gravity_tau)
            gravity = (
                previous[0] + (raw[0] - previous[0]) * a,
                previous[1] + (raw[1] - previous[1]) * a,
                previous[2] + (raw[2] - previous[2]) * a,
            )
        self._gravity = gravity

        linear = (raw[0] - gravity[0], raw[1] - gravity[1], raw[2] - gravity[2])
        mapped = self.axis_map.apply(*linear)

        a = _ema_alpha(dt, self.smoothing_tau) if dt > 0.0 else 1.0
        smoothed = self._smoothed
        smoothed = (
            smoothed[0] + (mapped[0] - smoothed[0]) * a,
            smoothed[1] + (mapped[1] - smoothed[1]) * a,
            smoothed[2] + (mapped[2] - smoothed[2]) * a,
        )
        self._smoothed = smoothed

        lateral, longitudinal, vertical = (
            _clamp(_dead_zone(value, self.dead_zone), self.clamp) for value in smoothed
        )
        self.state = MotionState(lateral, longitudinal, vertical, sample.timestamp)
        return self.state


def _dead_zone(value: float, threshold: float) -> float:
    """Suppress jitter below ``threshold`` without introducing a step.

    Values inside the dead zone become zero and values outside it are shifted
    toward zero by ``threshold``, so the output is continuous at the boundary.
    """
    if threshold <= 0.0:
        return value
    if abs(value) <= threshold:
        return 0.0
    return math.copysign(abs(value) - threshold, value)


def _clamp(value: float, limit: float) -> float:
    return max(-limit, min(limit, value))
