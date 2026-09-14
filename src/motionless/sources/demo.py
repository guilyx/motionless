"""Synthetic motion, for trying the overlay out without leaving the house.

The waveform is a deterministic function of time so it can be unit tested and
so screenshots are reproducible. It loops through the manoeuvres that actually
trigger motion sickness: sustained cornering, braking, acceleration, and the
low-amplitude road noise underneath all of it.
"""

from __future__ import annotations

import math
import time

from motionless.motion import GRAVITY, MotionSample
from motionless.sources.base import Availability, MotionSource, SampleCallback

#: Length of one full synthetic drive, in seconds.
CYCLE_SECONDS = 24.0


def demo_acceleration(t: float, *, intensity: float = 1.0) -> tuple[float, float, float]:
    """Acceleration in m/s^2 at time ``t``, gravity included on the z axis."""
    phase = (t % CYCLE_SECONDS) / CYCLE_SECONDS * math.tau
    # Cornering: one long sweep per cycle, plus a shorter counter-steer.
    lateral = 2.6 * math.sin(phase) + 0.9 * math.sin(3.0 * phase + 0.7)
    # Traction and braking: braking is sharper than acceleration, as in traffic.
    longitudinal = 2.0 * math.sin(2.0 * phase) - 1.1 * math.sin(phase + 1.9)
    # Road noise: fast, small, and never quite periodic with the manoeuvres.
    vertical = 0.45 * math.sin(11.3 * phase) + 0.25 * math.sin(19.7 * phase + 1.1)
    return (
        lateral * intensity,
        longitudinal * intensity,
        GRAVITY + vertical * intensity,
    )


class DemoSource(MotionSource):
    """Emit :func:`demo_acceleration` at a fixed rate."""

    name = "demo"
    description = "Synthetic drive loop for previewing and tuning the overlay"

    def __init__(
        self,
        on_sample: SampleCallback,
        *,
        rate_hz: float = 60.0,
        intensity: float = 1.0,
    ) -> None:
        super().__init__(on_sample)
        self._interval = 1.0 / max(1.0, rate_hz)
        self._intensity = intensity

    @classmethod
    def probe(cls) -> Availability:
        return Availability.ok("always available")

    def _run(self) -> None:
        started = time.monotonic()
        while not self.stopping:
            now = time.monotonic()
            x, y, z = demo_acceleration(now - started, intensity=self._intensity)
            self.emit(MotionSample(x, y, z, now))
            if self.wait(self._interval):
                return
