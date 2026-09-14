"""A source that reports no motion at all.

Selected when ``source = "auto"`` finds no real sensor. Falling back to the
synthetic ``demo`` source would be actively harmful in the situation this
program exists for: dots drifting to a script the vehicle is not following are
a *worse* sensory mismatch than no dots, which is the thing that makes people
ill. Better to draw nothing and say why.
"""

from __future__ import annotations

from motionless.sources.base import Availability, MotionSource


class NullSource(MotionSource):
    """Emits nothing. Present so "no sensor" is explicit rather than implied."""

    name = "none"
    description = "No motion source — cues stay hidden until one is configured"

    @classmethod
    def probe(cls) -> Availability:
        return Availability.ok("no motion, by design")

    def _run(self) -> None:
        # Nothing to read; park until stop() is called.
        while not self.stopping:
            if self.wait(0.5):
                return
