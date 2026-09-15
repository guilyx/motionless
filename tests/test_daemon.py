"""Daemon wiring that does not need a display: sampling, staleness, status."""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

import pytest

from motionless.config import Config
from motionless.daemon import STALE_AFTER_SECONDS, Daemon
from motionless.motion import GRAVITY, MotionSample


def make_daemon(**overrides: object) -> Daemon:
    config = Config()
    config.motion.source = "demo"
    config.motion.smoothing_tau = 0.0
    config.motion.dead_zone = 0.0
    for key, value in overrides.items():
        setattr(config.overlay, key, value)
    return Daemon(config)


def settle(daemon: Daemon, seconds: float = 0.5, dt: float = 0.02) -> float:
    """Feed level samples so the gravity estimate converges."""
    count = int(seconds / dt)
    for index in range(count):
        daemon._on_sample(MotionSample(0.0, 0.0, GRAVITY, index * dt))
    daemon.current_motion()
    return count * dt


class TestSampling:
    def test_buffered_samples_are_drained_into_a_state(self) -> None:
        daemon = make_daemon()
        elapsed = settle(daemon)
        for index in range(10):
            daemon._on_sample(MotionSample(3.0, 0.0, GRAVITY, elapsed + index * 0.02))
        assert daemon.current_motion().lateral > 1.0

    def test_the_buffer_is_emptied_each_frame(self) -> None:
        daemon = make_daemon()
        daemon._on_sample(MotionSample(0.0, 0.0, GRAVITY, 0.0))
        daemon.current_motion()
        assert len(daemon._samples) == 0

    def test_the_buffer_is_bounded(self) -> None:
        daemon = make_daemon()
        for index in range(10_000):
            daemon._on_sample(MotionSample(0.0, 0.0, GRAVITY, index * 0.001))
        # A stalled main loop must not turn into unbounded memory growth.
        assert len(daemon._samples) <= daemon._samples.maxlen or 0

    def test_sample_count_is_tracked_for_status(self) -> None:
        daemon = make_daemon()
        for index in range(5):
            daemon._on_sample(MotionSample(0.0, 0.0, GRAVITY, index * 0.02))
        assert daemon.status()["samples"] == 5


class TestStaleness:
    def test_no_sensor_at_all_reads_as_still(self) -> None:
        assert make_daemon().current_motion().is_still

    def test_a_silent_sensor_settles_the_cue(self, monkeypatch: pytest.MonkeyPatch) -> None:
        daemon = make_daemon()
        elapsed = settle(daemon)
        for index in range(10):
            daemon._on_sample(MotionSample(3.0, 0.0, GRAVITY, elapsed + index * 0.02))
        assert daemon.current_motion().lateral > 1.0

        # Pretend the sensor went quiet: the dots must fade rather than freeze
        # mid-manoeuvre, which would be worse than showing nothing.
        real = time.monotonic
        monkeypatch.setattr(time, "monotonic", lambda: real() + STALE_AFTER_SECONDS + 1.0)
        assert daemon.current_motion().is_still

    def test_fresh_samples_are_not_stale(self) -> None:
        daemon = make_daemon()
        elapsed = settle(daemon)
        daemon._on_sample(MotionSample(5.0, 0.0, GRAVITY, elapsed))
        assert not daemon.current_motion().is_still


class TestStatus:
    def test_reports_the_essentials(self) -> None:
        status = make_daemon().status()
        for key in ("version", "pid", "uptime", "visible", "backend", "source", "motion"):
            assert key in status

    def test_motion_values_are_rounded_for_display(self) -> None:
        daemon = make_daemon()
        elapsed = settle(daemon)
        for index in range(10):
            daemon._on_sample(MotionSample(1.23456789, 0.0, GRAVITY, elapsed + index * 0.02))
        daemon.current_motion()
        assert len(str(daemon.status()["motion"]["lateral"]).split(".")[-1]) <= 3

    def test_no_overlay_yet_means_not_visible(self) -> None:
        assert make_daemon().status()["visible"] is False

    def test_source_errors_surface(self) -> None:
        daemon = make_daemon()
        assert daemon.status()["source_error"] is None


class TestQuit:
    def test_quit_is_scheduled_above_the_redraw_timer(self) -> None:
        # The overlay redraws on a PRIORITY_DEFAULT timeout. A quit scheduled
        # at the default idle priority sits below it, so on a machine slow
        # enough for drawing to saturate the main loop the daemon never stops.
        scheduled: list[tuple[object, object]] = []

        class FakeGLib:
            PRIORITY_HIGH = -100
            PRIORITY_DEFAULT = 0
            PRIORITY_DEFAULT_IDLE = 200

            @staticmethod
            def idle_add(callback: object, *, priority: object = 200) -> int:
                scheduled.append((callback, priority))
                return 1

        daemon = make_daemon()
        daemon._glib = FakeGLib()
        assert daemon._handle_command("quit", {}) == {"stopping": True}
        assert len(scheduled) == 1
        _callback, priority = scheduled[0]
        assert priority == FakeGLib.PRIORITY_HIGH
        assert priority < FakeGLib.PRIORITY_DEFAULT


class TestInitialVisibility:
    def test_defaults_to_visible(self) -> None:
        assert make_daemon()._initial_visible is True

    def test_start_hidden_configuration_is_honoured(self) -> None:
        config = Config()
        config.start_hidden = True
        assert Daemon(config)._initial_visible is False

    def test_an_explicit_override_wins(self) -> None:
        config = Config()
        config.start_hidden = True
        assert Daemon(config, visible=True)._initial_visible is True


class TestReloadOverrides:
    def test_reload_keeps_command_line_overrides(self, tmp_path: Path) -> None:
        # The config file says one source, the command line another. Reload
        # re-reads the file, and must not quietly undo the flag the daemon was
        # started with.
        path = tmp_path / "config.toml"
        config = Config()
        config.motion.source = "iio"
        config.save(path)

        started = Config.load(path)
        started.motion.source = "demo"
        daemon = Daemon(started, config_path=path, overrides={"motion.source": "demo"})
        daemon._overlay = _StubOverlay()
        assert daemon.reload()["source"] == "demo"
        assert daemon.config.motion.source == "demo"

    def test_reload_picks_up_settings_the_command_line_did_not_override(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "config.toml"
        Config().save(path)
        daemon = Daemon(Config.load(path), config_path=path, overrides={"motion.source": "demo"})
        daemon._overlay = _StubOverlay()

        changed = Config.load(path)
        changed.overlay.opacity = 0.2
        changed.save(path)
        daemon.reload()
        assert daemon.config.overlay.opacity == 0.2
        assert daemon.config.motion.source == "demo"

    def test_without_overrides_the_file_wins(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        config = Config()
        config.overlay.opacity = 0.25
        config.save(path)
        daemon = Daemon(Config(), config_path=path)
        daemon._overlay = _StubOverlay()
        daemon.reload()
        assert daemon.config.overlay.opacity == 0.25


class _StubOverlay:
    """Stands in for the GTK overlay, which needs a display."""

    visible = False
    monitor_count = 0

    def reload(self, _config: Config) -> None:
        return None


def test_a_headless_environment_refuses_to_run_rather_than_crashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    daemon = make_daemon()
    # The conftest fixture already clears DISPLAY/WAYLAND_DISPLAY, so the plan
    # is unusable and run() must say so instead of raising out of GTK.
    assert not daemon.plan.usable
    assert daemon.run() == 1


class TestLogging:
    def test_the_detached_daemon_does_not_write_every_line_twice(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`motionless start` redirects the child's stderr into the log file.

        A FileHandler on that same file as well would double every line, which
        makes a log unreadable at exactly the moment you need to read it.
        """
        from motionless.daemon import configure_logging
        from motionless.paths import ensure_dir, log_file, state_dir

        ensure_dir(state_dir())
        target = log_file()
        with target.open("a") as redirected:
            monkeypatch.setattr(sys, "stderr", redirected)
            configure_logging()
            handlers = logging.getLogger().handlers
            assert not any(isinstance(h, logging.FileHandler) for h in handlers)
        configure_logging(to_file=False)  # leave the root logger as we found it

    def test_a_normal_foreground_run_still_gets_a_log_file(self) -> None:
        from motionless.daemon import configure_logging

        configure_logging()
        try:
            handlers = logging.getLogger().handlers
            assert any(isinstance(h, logging.FileHandler) for h in handlers)
        finally:
            configure_logging(to_file=False)
