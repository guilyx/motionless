"""Use a phone as the accelerometer, over a USB cable or Wi-Fi.

Most laptops have no accelerometer. Every phone does, and a phone in the car's
cradle is a better proxy for vehicle motion than a laptop on your knees anyway.

This source serves a small web page (:mod:`motionless.sources.sender`) that the
phone opens in its normal browser. The page reads ``DeviceMotionEvent`` and
posts batches of readings back. Nothing is installed on the phone.

Why a browser and not an app:

* nothing to install, nothing to trust, nothing to keep updated;
* the same page works on Android and iOS;
* ``adb reverse tcp:5577 tcp:5577`` makes the laptop reachable at the phone's
  own ``localhost``, which browsers treat as a secure context — so the USB path
  needs no certificate and no network at all.

``motionless pair`` sets all of that up; see :mod:`motionless.pair`.

Accepted request body on ``POST /motion`` (``Content-Type: application/json``)::

    {"samples": [[t_ms, x, y, z], ...], "units": "m/s^2", "invert": false}
    {"samples": [[x, y, z], ...]}
    {"samples": [{"t": 12.0, "x": 0.1, "y": -0.3, "z": 9.8}]}
    {"x": 0.1, "y": -0.3, "z": 9.8}
"""

from __future__ import annotations

import contextlib
import itertools
import json
import logging
import math
import secrets
import socket
import ssl
import threading
import time
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from motionless import __version__
from motionless.motion import GRAVITY, MotionSample
from motionless.sources.base import Availability, MotionSource, SampleCallback
from motionless.sources.sender import render_page

log = logging.getLogger("motionless.phone")

#: Refuse bodies larger than this. A full batch is a couple of kilobytes.
MAX_BODY_BYTES = 64 * 1024

#: Batches whose timestamps span longer than this are treated as unusable, and
#: every reading in them is stamped on arrival instead. Guards against a sender
#: whose clock is in seconds, or in epoch milliseconds, rather than page time.
MAX_BATCH_SPAN_SECONDS = 5.0

#: Readings beyond this (m/s^2) are physically implausible for a road vehicle —
#: about 10 g — and are far more likely to be a unit mix-up than a real event.
SANE_LIMIT = 100.0

_LOOPBACK = ("127.0.0.1", "::1", "localhost")


class ReportError(ValueError):
    """Raised for a request body that does not contain usable readings."""


@dataclass(frozen=True)
class Report:
    """One posted batch, parsed but not yet placed on our clock."""

    #: ``(t_ms, x, y, z)`` per reading; ``t_ms`` is ``None`` when unstamped.
    readings: tuple[tuple[float | None, float, float, float], ...]
    #: Sender's units, already resolved to a multiplier onto m/s^2.
    factor: float = 1.0
    #: Sender believes its platform reports gravity with inverted sign (iOS).
    invert: bool = False


@dataclass
class PhoneStats:
    """Counters surfaced by ``motionless status`` and ``GET /health``."""

    accepted: int = 0
    rejected: int = 0
    batches: int = 0
    last_seen: float = 0.0
    last_error: str = ""
    page_views: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record_batch(self, accepted: int) -> None:
        with self._lock:
            self.accepted += accepted
            self.batches += 1
            self.last_seen = time.monotonic()
            self.last_error = ""

    def record_rejection(self, detail: str) -> None:
        with self._lock:
            self.rejected += 1
            self.last_error = detail

    def record_page_view(self) -> None:
        with self._lock:
            self.page_views += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "accepted": self.accepted,
                "rejected": self.rejected,
                "batches": self.batches,
                "page_views": self.page_views,
                "seconds_since_sample": (
                    round(time.monotonic() - self.last_seen, 1) if self.last_seen else None
                ),
                "last_error": self.last_error,
            }


# ------------------------------------------------------------------- parsing


def parse_report(payload: bytes, *, default_units: str = "m/s^2") -> Report:
    """Parse a posted body into a :class:`Report`.

    Raises :class:`ReportError` for anything unusable, so a stray request is
    refused rather than taken as a violent manoeuvre.
    """
    if not payload.strip():
        raise ReportError("empty body")
    try:
        data = json.loads(payload.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise ReportError(f"invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ReportError("expected a JSON object")

    units = data.get("units", default_units)
    if units not in ("m/s^2", "m/s2", "g"):
        raise ReportError(f"unknown units {units!r}")
    factor = GRAVITY if units == "g" else 1.0
    invert = bool(data.get("invert", False))

    raw = data.get("samples")
    if raw is None:
        return Report((_reading(data),), factor, invert)
    if not isinstance(raw, list) or not raw:
        raise ReportError("'samples' must be a non-empty array")
    if len(raw) > 1000:
        raise ReportError("too many readings in one batch")
    return Report(tuple(_reading(item) for item in raw), factor, invert)


def _reading(item: Any) -> tuple[float | None, float, float, float]:
    if isinstance(item, dict):
        try:
            values = (item["x"], item["y"], item["z"])
        except KeyError as exc:
            raise ReportError(f"reading is missing {exc}") from exc
        stamp = item.get("t", item.get("timestamp"))
        return (_optional_number(stamp), *_triple(values))
    if isinstance(item, list):
        if len(item) == 3:
            return (None, *_triple(item))
        if len(item) == 4:
            return (_number(item[0]), *_triple(item[1:]))
        raise ReportError("array reading must be [x, y, z] or [t, x, y, z]")
    raise ReportError("reading must be an object or an array")


def _triple(values: Any) -> tuple[float, float, float]:
    x, y, z = (_number(value) for value in values)
    if max(abs(x), abs(y), abs(z)) > SANE_LIMIT:
        raise ReportError("acceleration out of range; check the sender's units")
    return x, y, z


def _number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ReportError(f"expected a number, got {value!r}")
    number = float(value)
    if not math.isfinite(number):
        raise ReportError("acceleration must be finite")
    return number


def _optional_number(value: Any) -> float | None:
    return None if value is None else _number(value)


def place_on_clock(report: Report, now: float) -> list[MotionSample]:
    """Convert a batch into samples timed against our own monotonic clock.

    The phone's clock is not ours and cannot be synchronised usefully over a
    link whose latency we do not measure. What *is* trustworthy is the spacing
    between readings within one batch, so the batch is anchored at its arrival
    time and earlier readings are backdated by their own spacing. Unusable or
    absent timestamps fall back to stamping everything on arrival, which costs
    at most one batch interval of temporal detail.
    """
    stamps = [stamp for stamp, _, _, _ in report.readings]
    usable = all(stamp is not None for stamp in stamps)
    if usable:
        ordered = [s for s in stamps if s is not None]
        span = (ordered[-1] - ordered[0]) / 1000.0
        usable = all(b >= a for a, b in itertools.pairwise(ordered)) and (
            0.0 <= span <= MAX_BATCH_SPAN_SECONDS
        )
    last = stamps[-1] if usable else None

    sign = -1.0 if report.invert else 1.0
    scale = report.factor * sign
    samples = []
    for stamp, x, y, z in report.readings:
        when = now if last is None or stamp is None else now - (last - stamp) / 1000.0
        samples.append(MotionSample(x * scale, y * scale, z * scale, when))
    return samples


# -------------------------------------------------------------------- server


class _Handler(BaseHTTPRequestHandler):
    """Serves the sender page and accepts posted readings."""

    server_version = f"motionless/{__version__}"
    protocol_version = "HTTP/1.1"

    @property
    def source(self) -> PhoneSource:
        server: _Server = self.server  # type: ignore[assignment]
        return server.source

    # BaseHTTPRequestHandler logs every request to stderr; route it to the
    # daemon's logger at debug level instead of the console.
    def log_message(self, format: str, *args: Any) -> None:
        log.debug("%s %s", self.address_string(), format % args)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            if not self._authorised():
                return
            self.source.stats.record_page_view()
            self._respond_bytes(
                HTTPStatus.OK, "text/html; charset=utf-8", self.source.page.encode("utf-8")
            )
            return
        if path == "/health":
            self._respond_json(
                HTTPStatus.OK,
                {"app": "motionless", "version": __version__, **self.source.stats.snapshot()},
            )
            return
        if path == "/favicon.ico":
            self._respond_bytes(HTTPStatus.NO_CONTENT, "image/x-icon", b"")
            return
        self._error(HTTPStatus.NOT_FOUND, "no such path")

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/motion":
            self._error(HTTPStatus.NOT_FOUND, "no such path")
            return
        if not self._authorised():
            return
        # Requiring JSON is a security measure, not a formality: a cross-origin
        # form or beacon post cannot set this header without a CORS preflight,
        # and we answer no preflight and send no CORS headers. So a random web
        # page cannot feed fake motion to a loopback-bound daemon.
        media_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if media_type != "application/json":
            self._error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "expected application/json")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._error(HTTPStatus.BAD_REQUEST, "invalid Content-Length")
            return
        if length <= 0 or length > MAX_BODY_BYTES:
            self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "body missing or too large")
            return

        body = self.rfile.read(length)
        try:
            count = self.source.ingest(body)
        except ReportError as exc:
            self.source.note_rejection(str(exc))
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        self._respond_json(HTTPStatus.OK, {"accepted": count})

    # ------------------------------------------------------------- plumbing

    def _authorised(self) -> bool:
        token = self.source.token
        if not token:
            return True
        query = parse_qs(urlparse(self.path).query)
        offered = (query.get("t") or [""])[0] or (self.headers.get("X-Motionless-Token") or "")
        if secrets.compare_digest(offered, token):
            return True
        self._error(HTTPStatus.FORBIDDEN, "bad or missing pairing token")
        return False

    def _respond_bytes(self, status: HTTPStatus, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # The page is only ever reached directly; refuse to be framed or sniffed.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _respond_json(self, status: HTTPStatus, data: dict[str, Any]) -> None:
        self._respond_bytes(status, "application/json", json.dumps(data).encode("utf-8"))

    def _error(self, status: HTTPStatus, detail: str) -> None:
        self._respond_json(status, {"error": detail})


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], source: PhoneSource) -> None:
        self.source = source
        super().__init__(address, _Handler)


# -------------------------------------------------------------------- source


class PhoneSource(MotionSource):
    """Serve the sender page and turn posted readings into motion samples."""

    name = "phone"
    description = "Your phone's accelerometer, over USB or Wi-Fi (no app to install)"

    def __init__(
        self,
        on_sample: SampleCallback,
        *,
        host: str = "127.0.0.1",
        port: int = 5577,
        units: str = "m/s^2",
        token: str = "",
        invert: str = "auto",
        tls_cert: str = "",
        tls_key: str = "",
    ) -> None:
        super().__init__(on_sample)
        self._host = host
        self._port = port
        self._units = units
        self._invert = invert
        self._tls = (tls_cert, tls_key)
        self.token = token
        self.stats = PhoneStats()
        self.page = render_page(token)
        self._advertised: str | None = None
        self._server: _Server | None = None
        self._ready = threading.Event()

    @classmethod
    def probe(cls) -> Availability:
        # Nothing to detect: the port either binds at start or the daemon says
        # why it could not. Not auto-selected, since it needs a phone paired.
        return Availability.ok("always available; run `motionless pair` to connect a phone")

    @property
    def address(self) -> tuple[str, int]:
        return self._host, self._port

    @property
    def secure(self) -> bool:
        """Whether this server speaks HTTPS."""
        return all(self._tls)

    @property
    def url(self) -> str:
        """The address to type into the phone.

        Bound to every interface, the literal ``0.0.0.0`` is useless to a
        person, so resolve it to the address a phone on the same network can
        actually reach. Cached: the answer only changes when the machine
        changes network, and by then the certificate needs regenerating anyway.
        """
        scheme = "https" if self.secure else "http"
        if self._host in _LOOPBACK:
            host = "localhost"
        elif self._host in ("0.0.0.0", "::"):
            if self._advertised is None:
                from motionless.pair import primary_address

                self._advertised = primary_address() or "localhost"
            host = self._advertised
        else:
            host = self._host
        suffix = f"/?t={self.token}" if self.token else "/"
        return f"{scheme}://{host}:{self._port}{suffix}"

    def details(self) -> dict[str, Any]:
        return {"url": self.url, "secure": self.secure, **self.stats.snapshot()}

    # ------------------------------------------------------------- ingestion

    def ingest(self, body: bytes) -> int:
        """Parse a posted batch and emit its samples. Returns how many."""
        report = parse_report(body, default_units=self._units)
        if self._invert == "always":
            report = Report(report.readings, report.factor, True)
        elif self._invert == "never":
            report = Report(report.readings, report.factor, False)
        samples = place_on_clock(report, time.monotonic())
        for sample in samples:
            self.emit(sample)
        self.stats.record_batch(len(samples))
        return len(samples)

    def note_rejection(self, detail: str) -> None:
        self.stats.record_rejection(detail)

    # ------------------------------------------------------------- lifecycle

    def wake(self) -> None:
        # serve_forever() blocks; shutdown() is the documented way out, and it
        # must be called from another thread, which is exactly where stop()
        # runs. Waiting for the server to exist first avoids a start/stop race
        # leaving a server running with nobody to shut it down.
        self._ready.wait(timeout=2.0)  # set even when the bind failed
        server = self._server
        if server is not None:
            with contextlib.suppress(OSError):
                server.shutdown()

    def _run(self) -> None:
        try:
            server = _Server((self._host, self._port), self)
            if self.secure:
                server.socket = self._wrap_tls(server.socket)
            self._server = server
        finally:
            # Set even when the bind or the certificate failed, so a concurrent
            # stop() is never left waiting on a server that will never exist.
            self._ready.set()
        try:
            if self.stopping:  # stop() beat us to it
                return
            log.info("phone sender page on %s", self.url)
            # A short poll interval because this is also the worst-case delay
            # on shutdown, and `motionless restart` waits on it.
            server.serve_forever(poll_interval=0.1)
        finally:
            self._server = None
            server.server_close()

    def _wrap_tls(self, sock: socket.socket) -> socket.socket:
        cert, key = self._tls
        for label, path in (("certificate", cert), ("key", key)):
            if not Path(path).exists():
                raise FileNotFoundError(f"TLS {label} not found: {path}; run `motionless pair`")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        return context.wrap_socket(sock, server_side=True)
