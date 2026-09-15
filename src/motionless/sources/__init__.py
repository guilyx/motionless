"""Motion source registry and auto-detection."""

from __future__ import annotations

from motionless.config import MotionConfig
from motionless.sources.base import Availability, MotionSource, SampleCallback
from motionless.sources.demo import DemoSource
from motionless.sources.iio import IioSource
from motionless.sources.null import NullSource
from motionless.sources.phone import PhoneSource
from motionless.sources.udp import UdpSource

#: Every source that can be named in configuration, in registry order.
SOURCES: tuple[type[MotionSource], ...] = (
    IioSource,
    PhoneSource,
    UdpSource,
    DemoSource,
    NullSource,
)

#: Order tried by ``source = "auto"``.
#:
#: ``phone`` and ``udp`` are excluded because they are always "available" but
#: do nothing until a phone is paired or a sender starts. ``demo`` is excluded
#: for a more important reason: it replays a *synthetic* drive, and cues that
#: disagree with the vehicle you are actually in are worse than no cues at all.
#: Auto-detection that cannot find a real sensor says so instead of inventing
#: motion.
AUTO_ORDER: tuple[type[MotionSource], ...] = (IioSource,)


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
    """Pick the best real source for this machine.

    Returns :class:`NullSource` when there is no real sensor, rather than
    substituting synthetic motion. See :data:`AUTO_ORDER`.
    """
    for source in AUTO_ORDER:
        if source.probe():
            return source
    return NullSource


def build(config: MotionConfig, on_sample: SampleCallback) -> MotionSource:
    """Instantiate the source described by ``config``."""
    cls = detect() if config.source == "auto" else get_source_class(config.source)
    if cls is IioSource:
        return IioSource(on_sample, device=config.iio.device, poll_hz=config.iio.poll_hz)
    if cls is PhoneSource:
        return PhoneSource(
            on_sample,
            host=config.phone.host,
            port=config.phone.port,
            units=config.phone.units,
            token=config.phone.token,
            invert=config.phone.invert,
            tls_cert=config.phone.tls_cert,
            tls_key=config.phone.tls_key,
        )
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
    "NullSource",
    "PhoneSource",
    "UdpSource",
    "UnknownSourceError",
    "build",
    "detect",
    "get_source_class",
    "probe_all",
    "source_names",
]
