"""Use a phone (or anything else) as the accelerometer, over UDP.

Most laptops have no accelerometer, but every phone does — and a phone sitting
in the car's cradle is a better proxy for vehicle motion than a laptop on your
knees anyway. Any app that can stream accelerometer readings to a UDP port
works; the parser accepts the shapes those apps actually send.

Accepted payloads (one reading per datagram)::

    {"x": 0.1, "y": -0.3, "z": 9.8}
    {"ax": 0.1, "ay": -0.3, "az": 9.8}
    {"accelerometer": {"x": 0.1, "y": -0.3, "z": 9.8}}
    [0.1, -0.3, 9.8]
    0.1,-0.3,9.8
"""

from __future__ import annotations

import contextlib
import json
import socket
import time
from typing import Any

from motionless.motion import GRAVITY, MotionSample
from motionless.sources.base import Availability, MotionSource, SampleCallback

_TRIPLES = (("x", "y", "z"), ("ax", "ay", "az"), ("accelX", "accelY", "accelZ"))
_NESTED_KEYS = ("accelerometer", "accel", "acceleration", "motion")


class PacketError(ValueError):
    """Raised for a datagram that does not contain a usable reading."""


def parse_packet(payload: bytes) -> tuple[float, float, float]:
    """Extract an (x, y, z) triple from one datagram.

    Raises :class:`PacketError` for anything unrecognised, so a stray packet
    on the port is dropped rather than taken as a violent manoeuvre.
    """
    text = payload.decode("utf-8", errors="replace").strip()
    if not text:
        raise PacketError("empty datagram")
    if text[0] in "{[":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise PacketError(f"invalid JSON: {exc}") from exc
        return _from_json(data)
    return _from_csv(text)


def _from_json(data: Any) -> tuple[float, float, float]:
    if isinstance(data, list):
        if len(data) < 3:
            raise PacketError("array payload needs at least three numbers")
        return _as_triple(data[0], data[1], data[2])
    if not isinstance(data, dict):
        raise PacketError("expected an object or array")
    for key in _NESTED_KEYS:
        nested = data.get(key)
        if isinstance(nested, (dict, list)):
            return _from_json(nested)
    for keys in _TRIPLES:
        if all(key in data for key in keys):
            return _as_triple(*(data[key] for key in keys))
    raise PacketError("no x/y/z acceleration found in object")


def _from_csv(text: str) -> tuple[float, float, float]:
    parts = [part for part in text.replace(";", ",").split(",") if part.strip()]
    if len(parts) < 3:
        raise PacketError("expected at least three comma-separated numbers")
    return _as_triple(*parts[:3])


def _as_triple(x: Any, y: Any, z: Any) -> tuple[float, float, float]:
    try:
        return float(x), float(y), float(z)
    except (TypeError, ValueError) as exc:
        raise PacketError(f"non-numeric acceleration: {exc}") from exc


class UdpSource(MotionSource):
    """Listen for acceleration datagrams on a UDP port."""

    name = "udp"
    description = "Accelerometer streamed over UDP (use your phone as the sensor)"

    def __init__(
        self,
        on_sample: SampleCallback,
        *,
        host: str = "127.0.0.1",
        port: int = 5577,
        units: str = "m/s^2",
    ) -> None:
        super().__init__(on_sample)
        self._host = host
        self._port = port
        self._factor = GRAVITY if units == "g" else 1.0
        self._socket: socket.socket | None = None
        #: Number of datagrams dropped as unparsable, for ``status``.
        self.rejected = 0

    @classmethod
    def probe(cls) -> Availability:
        # Nothing to detect: the port is either bindable at start, or the
        # daemon reports a clear bind error. Not auto-selected, though, since
        # it needs a sender on the other end.
        return Availability.ok("always available; requires a sender")

    @property
    def address(self) -> tuple[str, int]:
        return self._host, self._port

    def wake(self) -> None:
        # recvfrom() blocks; closing the socket is what unblocks it.
        if self._socket is not None:
            with contextlib.suppress(OSError):  # close rarely fails
                self._socket.close()

    def _run(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self._host, self._port))
        sock.settimeout(0.5)
        self._socket = sock
        try:
            while not self.stopping:
                try:
                    payload, _ = sock.recvfrom(4096)
                except TimeoutError:
                    continue
                except OSError:
                    # stop() closes the socket to break us out of recvfrom();
                    # anything else is a genuine failure worth surfacing.
                    if self.stop_requested():
                        return
                    raise
                try:
                    x, y, z = parse_packet(payload)
                except PacketError:
                    self.rejected += 1
                    continue
                self.emit(
                    MotionSample(
                        x * self._factor,
                        y * self._factor,
                        z * self._factor,
                        time.monotonic(),
                    )
                )
        finally:
            self._socket = None
            sock.close()
