"""Built-in accelerometer via the kernel's IIO sysfs interface.

Laptops, convertibles and tablets that report screen rotation expose an
accelerometer at ``/sys/bus/iio/devices/iio:deviceN``. Reading it directly
avoids depending on iio-sensor-proxy, which only publishes a coarse
orientation ("normal", "left-up", ...) rather than the continuous values a
motion cue needs.
"""

from __future__ import annotations

import time
from pathlib import Path

from motionless.motion import MotionSample
from motionless.sources.base import Availability, MotionSource, SampleCallback

IIO_ROOT = Path("/sys/bus/iio/devices")
_AXES = ("x", "y", "z")


def find_accelerometers(root: Path = IIO_ROOT) -> list[Path]:
    """Return every IIO device that exposes a three-axis accelerometer."""
    if not root.is_dir():
        return []
    found = [
        device
        for device in sorted(root.glob("iio:device*"))
        if all((device / f"in_accel_{axis}_raw").exists() for axis in _AXES)
    ]
    return found


def read_scale(device: Path, axis: str) -> float:
    """Scale factor converting a raw reading to m/s^2.

    The kernel exposes either a shared ``in_accel_scale`` or per-axis files;
    a device with neither reports in m/s^2 already, so 1.0 is the right
    fallback.
    """
    for name in (f"in_accel_{axis}_scale", "in_accel_scale"):
        candidate = device / name
        if candidate.exists():
            try:
                return float(candidate.read_text().strip())
            except (OSError, ValueError):
                continue
    return 1.0


def read_sample(device: Path, scales: dict[str, float]) -> MotionSample:
    """Read one three-axis sample, in m/s^2."""
    values = []
    for axis in _AXES:
        raw = (device / f"in_accel_{axis}_raw").read_text().strip()
        values.append(float(raw) * scales[axis])
    return MotionSample(values[0], values[1], values[2], time.monotonic())


class IioSource(MotionSource):
    """Poll a sysfs accelerometer at a fixed rate."""

    name = "iio"
    description = "Built-in accelerometer via /sys/bus/iio (laptops, convertibles, tablets)"

    def __init__(
        self,
        on_sample: SampleCallback,
        *,
        device: str = "",
        poll_hz: float = 50.0,
        root: Path = IIO_ROOT,
    ) -> None:
        super().__init__(on_sample)
        self._root = root
        self._device = Path(device) if device else None
        self._interval = 1.0 / max(1.0, poll_hz)

    @classmethod
    def probe(cls, root: Path = IIO_ROOT) -> Availability:
        devices = find_accelerometers(root)
        if not devices:
            return Availability.no(f"no accelerometer found under {root}")
        return Availability.ok(f"using {devices[0].name}")

    def resolve_device(self) -> Path:
        if self._device is not None:
            if not (self._device / "in_accel_x_raw").exists():
                raise FileNotFoundError(f"{self._device} does not look like an IIO accelerometer")
            return self._device
        devices = find_accelerometers(self._root)
        if not devices:
            raise FileNotFoundError(f"no IIO accelerometer found under {self._root}")
        return devices[0]

    def _run(self) -> None:
        device = self.resolve_device()
        scales = {axis: read_scale(device, axis) for axis in _AXES}
        while not self.stopping:
            started = time.monotonic()
            try:
                self.emit(read_sample(device, scales))
            except OSError:
                # Sensors can disappear on suspend/resume; back off and retry
                # rather than killing the overlay.
                if self.wait(1.0):
                    return
                continue
            elapsed = time.monotonic() - started
            if self.wait(max(0.0, self._interval - elapsed)):
                return
