"""The long-lived process: reads motion, paints cues, answers the CLI."""

from __future__ import annotations

import collections
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any

from motionless import __version__
from motionless.config import Config
from motionless.ipc import ControlServer, PidFile
from motionless.motion import STILL, MotionProcessor, MotionSample, MotionState
from motionless.overlay import backend as backend_module
from motionless.paths import ensure_dir, log_file, state_dir
from motionless.sources import build as build_source

log = logging.getLogger("motionless")

#: Samples buffered between frames. A frame at 60 Hz consumes at most a
#: handful; the cap only matters if the main loop stalls.
SAMPLE_BUFFER = 256

#: Treat motion as stale — and settle the dots — after this long without a
#: sample, so a dead sensor fades the cue out instead of freezing it.
STALE_AFTER_SECONDS = 1.0


def configure_logging(verbose: bool = False, *, to_file: bool = True) -> None:
    """Log to stderr, and to ``$XDG_STATE_HOME/motionless/motionless.log``."""
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if to_file:
        try:
            ensure_dir(state_dir())
            handlers.append(logging.FileHandler(log_file(), encoding="utf-8"))
        except OSError:  # pragma: no cover - read-only home
            pass
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )


class Daemon:
    """Wires a motion source to the overlay and serves control commands."""

    def __init__(
        self,
        config: Config,
        *,
        visible: bool | None = None,
        config_path: Path | None = None,
    ) -> None:
        self.config = config
        self.config_path = config_path
        self.plan = backend_module.plan_backend()
        self._samples: collections.deque[MotionSample] = collections.deque(maxlen=SAMPLE_BUFFER)
        self._lock = threading.Lock()
        self._processor = self._build_processor(config)
        self._source = build_source(config.motion, self._on_sample)
        self._server = ControlServer(self._handle_command)
        self._overlay: Any = None
        self._loop: Any = None
        self._glib: Any = None
        self._sample_count = 0
        self._last_sample_at = 0.0
        self._started_at = time.time()
        self._initial_visible = (not config.start_hidden) if visible is None else visible

    @staticmethod
    def _build_processor(config: Config) -> MotionProcessor:
        return MotionProcessor(
            axis_map=config.motion.axis_map(),
            gravity_tau=config.motion.gravity_tau,
            smoothing_tau=config.motion.smoothing_tau,
            dead_zone=config.motion.dead_zone,
            clamp=config.motion.clamp,
        )

    # ------------------------------------------------------------- motion in

    def _on_sample(self, sample: MotionSample) -> None:
        """Called from the source thread; must not touch GTK."""
        with self._lock:
            self._samples.append(sample)
            self._sample_count += 1
            self._last_sample_at = time.monotonic()

    def current_motion(self) -> MotionState:
        """Drain buffered samples and return the latest state (main thread)."""
        with self._lock:
            pending = list(self._samples)
            self._samples.clear()
            last_at = self._last_sample_at
        for sample in pending:
            self._processor.update(sample)
        if not pending and (last_at == 0.0 or time.monotonic() - last_at > STALE_AFTER_SECONDS):
            return STILL
        return self._processor.state

    # ---------------------------------------------------------------- run

    def run(self) -> int:
        if not self.plan.usable:
            log.error("cannot open an overlay: %s", self.plan.reason)
            return 1
        backend_module.apply_plan(self.plan)
        log.info("motionless %s starting (%s: %s)", __version__, self.plan.mode, self.plan.reason)
        for caveat in self.plan.caveats:
            log.warning("%s", caveat)

        # Imported here, never at module scope: GDK reads GDK_BACKEND when it
        # is imported, so the plan has to be applied first.
        from gi.repository import GLib, Gtk

        from motionless.overlay.window import Overlay

        self._glib = GLib
        self._loop = Gtk.main
        self._overlay = Overlay(self.config, self.current_motion)

        with PidFile():
            fd = self._server.bind()
            GLib.io_add_watch(fd, GLib.PRIORITY_DEFAULT, GLib.IO_IN, self._on_socket_ready)
            for sig in (signal.SIGINT, signal.SIGTERM):
                GLib.unix_signal_add(GLib.PRIORITY_HIGH, sig, self._on_signal)

            self._source.start()
            log.info("motion source: %s", self._source.name)
            self._overlay.start(visible=self._initial_visible)
            log.info(
                "overlay ready on %d monitor(s), cues %s",
                self._overlay.monitor_count,
                "visible" if self._overlay.visible else "hidden",
            )
            try:
                Gtk.main()
            finally:
                self._shutdown()
        return 0

    def _on_socket_ready(self, *_args: object) -> bool:
        self._server.handle_ready()
        return True

    def _on_signal(self, *_args: object) -> bool:
        log.info("signal received, shutting down")
        self.quit()
        return False

    def quit(self) -> None:
        from gi.repository import Gtk

        Gtk.main_quit()

    def _shutdown(self) -> None:
        if self._overlay is not None:
            self._overlay.stop()
        self._source.stop()
        self._server.close()
        if self._source.error is not None:
            log.error("motion source %s failed: %s", self._source.name, self._source.error)
        log.info("motionless stopped")

    # ------------------------------------------------------------- commands

    def _handle_command(self, command: str, _args: dict[str, Any]) -> dict[str, Any]:
        if command == "ping":
            return {"version": __version__, "pid": os.getpid()}
        if command == "status":
            return self.status()
        if command == "show":
            self._overlay.show()
            return {"visible": True}
        if command == "hide":
            self._overlay.hide()
            return {"visible": False}
        if command == "toggle":
            return {"visible": self._overlay.toggle()}
        if command == "reload":
            return self.reload()
        if command == "quit":
            # Defer so the reply reaches the client before the loop ends.
            self._glib.idle_add(self.quit)
            return {"stopping": True}
        raise ValueError(f"unhandled command {command!r}")  # pragma: no cover

    def reload(self) -> dict[str, Any]:
        """Re-read the configuration file and restart the source and overlay."""
        config = Config.load(self.config_path)
        restart_source = config.motion != self.config.motion
        self.config = config
        self._processor = self._build_processor(config)
        if restart_source:
            self._source.stop()
            self._source = build_source(config.motion, self._on_sample)
            self._source.start()
            log.info("motion source restarted: %s", self._source.name)
        self._overlay.reload(config)
        log.info("configuration reloaded")
        return {"reloaded": True, "source": self._source.name}

    def status(self) -> dict[str, Any]:
        motion = self._processor.state
        error = self._source.error
        return {
            "version": __version__,
            "pid": os.getpid(),
            "uptime": round(time.time() - self._started_at, 1),
            "visible": bool(self._overlay and self._overlay.visible),
            "backend": self.plan.mode,
            "backend_reason": self.plan.reason,
            "monitors": self._overlay.monitor_count if self._overlay else 0,
            "source": self._source.name,
            "source_error": f"{type(error).__name__}: {error}" if error else None,
            "samples": self._sample_count,
            "calibrated": self._processor.has_gravity_estimate,
            "motion": {
                "lateral": round(motion.lateral, 3),
                "longitudinal": round(motion.longitudinal, 3),
                "vertical": round(motion.vertical, 3),
                "magnitude": round(motion.magnitude, 3),
            },
            "config": str(self.config_path) if self.config_path else None,
        }
