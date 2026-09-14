"""Typed, TOML-backed configuration.

The schema is a tree of dataclasses, which gives us three things for free:
documented defaults, dotted-path access for ``motionless config set``, and a
single place to validate values before the overlay ever starts.
"""

from __future__ import annotations

import dataclasses
import sys
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, cast

from motionless.motion import AxisMap
from motionless.paths import config_file

if sys.version_info >= (3, 11):  # pragma: no cover - trivial import branch
    import tomllib
else:  # pragma: no cover - trivial import branch
    import tomli as tomllib


class ConfigError(ValueError):
    """Raised when a configuration file or value is not usable."""


@dataclass
class UdpSourceConfig:
    """Settings for the ``udp`` source (phone or external sensor bridge)."""

    #: Bind address. Keep the loopback default unless you really do want to
    #: accept motion packets from the network.
    host: str = "127.0.0.1"
    port: int = 5577
    #: Units used by the sender: ``m/s^2`` or ``g``.
    units: str = "m/s^2"

    def validate(self) -> None:
        if not 1 <= self.port <= 65535:
            raise ConfigError(f"motion.udp.port must be within 1-65535, got {self.port}")
        if self.units not in ("m/s^2", "g"):
            raise ConfigError(f"motion.udp.units must be 'm/s^2' or 'g', got {self.units!r}")


@dataclass
class IioSourceConfig:
    """Settings for the ``iio`` source (built-in accelerometer via sysfs)."""

    #: Explicit ``/sys/bus/iio/devices/iio:deviceN`` path, or empty to detect.
    device: str = ""
    poll_hz: float = 50.0

    def validate(self) -> None:
        if not 1.0 <= self.poll_hz <= 200.0:
            raise ConfigError(f"motion.iio.poll_hz must be within 1-200, got {self.poll_hz}")


@dataclass
class MotionConfig:
    """How raw acceleration is acquired and conditioned."""

    #: ``auto`` picks the first source that reports itself available.
    source: str = "auto"
    #: Sensor-to-viewer axis mapping; prefix with ``-`` to invert.
    axis_lateral: str = "x"
    axis_longitudinal: str = "y"
    axis_vertical: str = "z"
    #: Time constant of the gravity estimate, in seconds.
    #:
    #: Gravity is separated from acceleration by a low-pass filter, so a
    #: *sustained* force eventually looks like gravity and the cue fades. This
    #: is the trade-off dial: larger keeps long sweeping bends visible for
    #: longer, smaller adapts faster when you re-seat the device. Beyond about
    #: 10s, picking the laptop up leaves a false cue for several seconds.
    gravity_tau: float = 5.0
    #: Time constant of the output smoothing, in seconds.
    smoothing_tau: float = 0.12
    #: Accelerations below this (m/s^2) are treated as sensor noise.
    dead_zone: float = 0.15
    #: Upper bound on the cue magnitude (m/s^2) so potholes do not fling dots.
    clamp: float = 6.0
    udp: UdpSourceConfig = field(default_factory=UdpSourceConfig)
    iio: IioSourceConfig = field(default_factory=IioSourceConfig)

    def axis_map(self) -> AxisMap:
        try:
            return AxisMap(self.axis_lateral, self.axis_longitudinal, self.axis_vertical)
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc

    def validate(self) -> None:
        self.axis_map()
        for name in ("gravity_tau", "smoothing_tau"):
            if getattr(self, name) < 0.0:
                raise ConfigError(f"motion.{name} must not be negative")
        if self.dead_zone < 0.0:
            raise ConfigError("motion.dead_zone must not be negative")
        if self.clamp <= 0.0:
            raise ConfigError("motion.clamp must be positive")
        self.udp.validate()
        self.iio.validate()


@dataclass
class OverlayConfig:
    """How the cue is drawn."""

    #: ``edges`` keeps dots in a peripheral band (closest to Apple's design);
    #: ``grid`` fills the whole screen.
    layout: str = "edges"
    #: ``all`` covers every monitor, ``primary`` only the primary one.
    monitors: str = "all"
    #: Distance between dot centres, in pixels.
    spacing: float = 46.0
    #: Dot radius at rest, in pixels.
    radius: float = 3.5
    #: Width of the peripheral band, as a fraction of the screen's short edge.
    margin: float = 0.16
    #: Dot colour as ``#rgb``, ``#rrggbb`` or ``#rrggbbaa``.
    color: str = "#ffffff"
    #: Peak dot opacity, 0-1.
    opacity: float = 0.55
    #: Pixels of travel per m/s^2 of acceleration.
    sensitivity: float = 9.0
    #: Hard cap on dot displacement, in pixels.
    travel: float = 30.0
    #: Acceleration (m/s^2) that wakes the dots up.
    activation: float = 0.35
    #: Seconds to fade the dots in once motion starts.
    fade_in: float = 0.3
    #: Seconds of stillness before the dots fade back out.
    fade_out: float = 1.5
    #: Redraw rate while cues are visible.
    fps: int = 60
    #: Redraw rate while idle, to keep the overlay off the CPU governor's back.
    idle_fps: int = 8
    #: Keep the dots visible even when the vehicle is still. Useful while
    #: tuning the look; wasteful in a car.
    always_on: bool = False

    def validate(self) -> None:
        if self.layout not in ("edges", "grid"):
            raise ConfigError(f"overlay.layout must be 'edges' or 'grid', got {self.layout!r}")
        if self.monitors not in ("all", "primary"):
            raise ConfigError(f"overlay.monitors must be 'all' or 'primary', got {self.monitors!r}")
        if self.spacing < 4.0:
            raise ConfigError("overlay.spacing must be at least 4")
        if self.radius <= 0.0:
            raise ConfigError("overlay.radius must be positive")
        if not 0.0 < self.margin <= 0.5:
            raise ConfigError("overlay.margin must be within (0, 0.5]")
        if not 0.0 <= self.opacity <= 1.0:
            raise ConfigError("overlay.opacity must be within 0-1")
        if self.sensitivity < 0.0:
            raise ConfigError("overlay.sensitivity must not be negative")
        if self.travel < 0.0:
            raise ConfigError("overlay.travel must not be negative")
        if self.activation < 0.0:
            raise ConfigError("overlay.activation must not be negative")
        if self.fade_in < 0.0 or self.fade_out < 0.0:
            raise ConfigError("overlay fade durations must not be negative")
        if not 1 <= self.fps <= 240:
            raise ConfigError("overlay.fps must be within 1-240")
        if not 1 <= self.idle_fps <= self.fps:
            raise ConfigError("overlay.idle_fps must be within 1 and overlay.fps")
        parse_color(self.color)


@dataclass
class Config:
    """Top-level configuration."""

    motion: MotionConfig = field(default_factory=MotionConfig)
    overlay: OverlayConfig = field(default_factory=OverlayConfig)
    #: Start with cues hidden and wait for ``motionless toggle``.
    start_hidden: bool = False

    def validate(self) -> Config:
        self.motion.validate()
        self.overlay.validate()
        return self

    # ---------------------------------------------------------------- loading

    @classmethod
    def load(cls, path: Path | None = None, *, missing_ok: bool = True) -> Config:
        """Read configuration from ``path`` (default: the XDG config file)."""
        target = path or config_file()
        if not target.exists():
            if missing_ok:
                return cls()
            raise ConfigError(f"no configuration file at {target}")
        try:
            with target.open("rb") as handle:
                data = tomllib.load(handle)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{target}: invalid TOML: {exc}") from exc
        return cls.from_dict(data).validate()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Config:
        return cast(Config, _from_dict(cls, data, prefix=""))

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    # ---------------------------------------------------------------- writing

    def save(self, path: Path | None = None) -> Path:
        """Write the configuration atomically and return the path written."""
        target = path or config_file()
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".tmp")
        temporary.write_text(self.to_toml(), encoding="utf-8")
        temporary.replace(target)
        return target

    def to_toml(self) -> str:
        return _to_toml(self)

    # ------------------------------------------------------- dotted accessors

    def get(self, dotted: str) -> Any:
        node, name = self._resolve(dotted)
        return getattr(node, name)

    def set(self, dotted: str, raw: str) -> Any:
        """Set ``dotted`` from a string, coercing to the declared field type.

        Either the whole configuration is valid afterwards, or nothing changed:
        a caller that catches :class:`ConfigError` and carries on must not be
        left holding a half-applied configuration.
        """
        node, name = self._resolve(dotted)
        declared = {f.name: f.type for f in fields(node)}[name]
        value = _coerce(raw, declared, dotted)
        previous = getattr(node, name)
        setattr(node, name, value)
        try:
            self.validate()
        except ConfigError:
            setattr(node, name, previous)
            raise
        return value

    def setting_names(self) -> list[str]:
        return _dotted_keys(self, prefix="")

    def _resolve(self, dotted: str) -> tuple[Any, str]:
        parts = dotted.split(".")
        node: Any = self
        for part in parts[:-1]:
            node = getattr(node, part, None)
            if not is_dataclass(node):
                raise ConfigError(f"unknown setting {dotted!r}")
        name = parts[-1]
        if not is_dataclass(node) or name not in {f.name for f in fields(node)}:
            raise ConfigError(f"unknown setting {dotted!r}")
        if is_dataclass(getattr(node, name)):
            raise ConfigError(f"{dotted!r} is a section, not a setting")
        return node, name


# --------------------------------------------------------------------- colour


def parse_color(text: str) -> tuple[float, float, float, float]:
    """Parse ``#rgb`` / ``#rrggbb`` / ``#rrggbbaa`` into RGBA floats in 0-1."""
    value = text.strip().lstrip("#")
    if len(value) == 3:
        value = "".join(channel * 2 for channel in value)
    if len(value) == 6:
        value += "ff"
    if len(value) != 8:
        raise ConfigError(f"invalid colour {text!r}: expected #rgb, #rrggbb or #rrggbbaa")
    try:
        channels = [int(value[i : i + 2], 16) / 255.0 for i in range(0, 8, 2)]
    except ValueError as exc:
        raise ConfigError(f"invalid colour {text!r}: {exc}") from exc
    return channels[0], channels[1], channels[2], channels[3]


# ------------------------------------------------------------------ internals


def _from_dict(cls: type[Any], data: dict[str, Any], *, prefix: str) -> Any:
    if not isinstance(data, dict):
        raise ConfigError(f"expected a table for {prefix or 'the document root'}")
    known = {f.name: f for f in fields(cls)}
    unknown = sorted(set(data) - set(known))
    if unknown:
        where = f"{prefix}." if prefix else ""
        raise ConfigError(f"unknown setting(s): {', '.join(where + k for k in unknown)}")

    kwargs: dict[str, Any] = {}
    for name, spec in known.items():
        if name not in data:
            continue
        factory = spec.default_factory
        default = factory() if factory is not dataclasses.MISSING else None
        child_prefix = f"{prefix}.{name}" if prefix else name
        if is_dataclass(default):
            kwargs[name] = _from_dict(type(default), data[name], prefix=child_prefix)
        else:
            kwargs[name] = _check_scalar(data[name], spec.type, child_prefix)
    return cls(**kwargs)


def _check_scalar(value: Any, declared: Any, dotted: str) -> Any:
    declared_name = declared if isinstance(declared, str) else getattr(declared, "__name__", "")
    if declared_name == "bool":
        if not isinstance(value, bool):
            raise ConfigError(f"{dotted} must be a boolean")
        return value
    if declared_name == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{dotted} must be an integer")
        return value
    if declared_name == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{dotted} must be a number")
        return float(value)
    if declared_name == "str":
        if not isinstance(value, str):
            raise ConfigError(f"{dotted} must be a string")
        return value
    return value


def _coerce(raw: str, declared: Any, dotted: str) -> Any:
    declared_name = declared if isinstance(declared, str) else getattr(declared, "__name__", "")
    text = raw.strip()
    if declared_name == "bool":
        lowered = text.lower()
        if lowered in ("1", "true", "yes", "on"):
            return True
        if lowered in ("0", "false", "no", "off"):
            return False
        raise ConfigError(f"{dotted} must be a boolean (true/false)")
    if declared_name == "int":
        try:
            return int(text, 10)
        except ValueError as exc:
            raise ConfigError(f"{dotted} must be an integer, got {raw!r}") from exc
    if declared_name == "float":
        try:
            return float(text)
        except ValueError as exc:
            raise ConfigError(f"{dotted} must be a number, got {raw!r}") from exc
    return text


def _dotted_keys(node: Any, *, prefix: str) -> list[str]:
    out: list[str] = []
    for spec in fields(node):
        value = getattr(node, spec.name)
        dotted = f"{prefix}.{spec.name}" if prefix else spec.name
        if is_dataclass(value):
            out.extend(_dotted_keys(value, prefix=dotted))
        else:
            out.append(dotted)
    return out


def _to_toml(node: Any, *, prefix: str = "") -> str:
    scalars: list[str] = []
    tables: list[str] = []
    for spec in fields(node):
        value = getattr(node, spec.name)
        if is_dataclass(value):
            child = f"{prefix}.{spec.name}" if prefix else spec.name
            tables.append(f"[{child}]\n{_to_toml(value, prefix=child)}")
        else:
            scalars.append(f"{spec.name} = {_toml_scalar(value)}")
    body = "\n".join(scalars)
    if scalars and tables:
        body += "\n"
    if tables:
        body += "\n" + "\n".join(tables)
    return body + "\n" if not body.endswith("\n") else body


def _toml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if isinstance(value, float):
        return repr(value)
    return str(value)
