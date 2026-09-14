"""Motion source registry and auto-detection."""

from __future__ import annotations

from motionless.config import MotionConfig
from motionless.sources.base import Availability, MotionSource, SampleCallback
from motionless.sources.demo import DemoSource
from motionless.sources.iio import IioSource
from motionless.sources.udp import UdpSource

#: Every source that can be named in configuration, in registry order.
SOURCES: tuple[type[MotionSource], ...] = (IioSource, UdpSource, DemoSource)

#: Order tried by ``source = "auto"``. ``udp`` is excluded on purpose: it is
#: always "available" but does nothing without a sender, so choosing it
#: automatically would look like a silent failure.
AUTO_ORDER: tuple[type[MotionSource], ...] = (IioSource, DemoSource)


class UnknownSourceError(ValueError):
    """Raised when configuration names a source that does not exist."""


def source_names() -> list[str]:
    return [source.name for source in SOURCES]


def get_source_class(name: str) -> type[MotionSource]:
    for source in SOURCES:
        if source.name == name:
            return source
    raise UnknownSourceError(
        f"unknown motion source {name!r}; available: {', '.join(source_names())}"
    )


def probe_all() -> dict[str, Availability]:
    """Availability of every registered source, keyed by name."""
    return {source.name: source.probe() for source in SOURCES}


def detect() -> type[MotionSource]:
    """Pick the best source for this machine.

    Falls back to the demo source, which always works — an overlay that
    visibly does something is easier to debug than one that silently does not.
    """
    for source in AUTO_ORDER:
        if source.probe():
            return source
    return DemoSource


def build(config: MotionConfig, on_sample: SampleCallback) -> MotionSource:
    """Instantiate the source described by ``config``."""
    cls = detect() if config.source == "auto" else get_source_class(config.source)
    if cls is IioSource:
        return IioSource(on_sample, device=config.iio.device, poll_hz=config.iio.poll_hz)
    if cls is UdpSource:
        return UdpSource(
            on_sample, host=config.udp.host, port=config.udp.port, units=config.udp.units
        )
    return cls(on_sample)


__all__ = [
    "AUTO_ORDER",
    "SOURCES",
    "Availability",
    "DemoSource",
    "IioSource",
    "MotionSource",
    "UdpSource",
    "UnknownSourceError",
    "build",
    "detect",
    "get_source_class",
    "probe_all",
    "source_names",
]
