"""Motion sources: packet parsing, sysfs reading, registry and lifecycle."""

from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

import pytest

from motionless.config import MotionConfig
from motionless.motion import GRAVITY, MotionSample
from motionless.sources import (
    AUTO_ORDER,
    SOURCES,
    DemoSource,
    IioSource,
    UdpSource,
    UnknownSourceError,
    build,
    detect,
    get_source_class,
    probe_all,
    source_names,
)
from motionless.sources.base import Availability, MotionSource
from motionless.sources.demo import CYCLE_SECONDS, demo_acceleration
from motionless.sources.iio import find_accelerometers, read_sample, read_scale
from motionless.sources.udp import PacketError, parse_packet


class TestAvailability:
    def test_is_truthy_when_available(self) -> None:
        assert Availability.ok("fine")
        assert not Availability.no("nope")

    def test_carries_a_detail(self) -> None:
        assert Availability.no("no sensor").detail == "no sensor"


class TestRegistry:
    def test_every_source_has_a_unique_name_and_description(self) -> None:
        names = source_names()
        assert len(names) == len(set(names))
        assert all(source.description for source in SOURCES)

    def test_lookup_by_name(self) -> None:
        assert get_source_class("demo") is DemoSource

    def test_unknown_name_lists_the_alternatives(self) -> None:
        with pytest.raises(UnknownSourceError, match="available: "):
            get_source_class("telepathy")

    def test_probe_covers_every_source(self) -> None:
        assert set(probe_all()) == set(source_names())

    def test_udp_is_not_auto_selected(self) -> None:
        # It reports itself available but does nothing without a sender, so
        # auto-selecting it would look like a silent failure.
        assert UdpSource not in AUTO_ORDER

    def test_detect_always_returns_something_usable(self) -> None:
        assert detect() in AUTO_ORDER

    def test_build_honours_an_explicit_choice(self) -> None:
        source = build(
            MotionConfig(
                source="udp",
            ),
            lambda _s: None,
        )
        assert isinstance(source, UdpSource)

    def test_build_passes_udp_settings_through(self) -> None:
        config = MotionConfig(source="udp")
        config.udp.port = 6123
        source = build(config, lambda _s: None)
        assert isinstance(source, UdpSource)
        assert source.address == ("127.0.0.1", 6123)

    def test_build_auto_resolves(self) -> None:
        assert isinstance(build(MotionConfig(source="auto"), lambda _s: None), MotionSource)


class TestUdpParsing:
    @pytest.mark.parametrize(
        "payload",
        [
            b'{"x": 1, "y": 2, "z": 3}',
            b'{"ax": 1, "ay": 2, "az": 3}',
            b'{"accelX": 1, "accelY": 2, "accelZ": 3}',
            b'{"accelerometer": {"x": 1, "y": 2, "z": 3}}',
            b'{"accel": [1, 2, 3]}',
            b'{"motion": {"ax": 1, "ay": 2, "az": 3}}',
            b"[1, 2, 3]",
            b"[1, 2, 3, 4]",
            b"1,2,3",
            b"1;2;3",
            b"  1, 2, 3  \n",
            b'{"x": "1", "y": "2", "z": "3"}',
        ],
    )
    def test_accepted_shapes(self, payload: bytes) -> None:
        assert parse_packet(payload) == (1.0, 2.0, 3.0)

    def test_extra_keys_are_ignored(self) -> None:
        assert parse_packet(b'{"x":1,"y":2,"z":3,"t":99,"name":"phone"}') == (1.0, 2.0, 3.0)

    @pytest.mark.parametrize(
        "payload",
        [
            b"",
            b"   ",
            b"{not json",
            b"[1, 2]",
            b"1,2",
            b'{"x": 1, "y": 2}',
            b'{"x": "left", "y": 2, "z": 3}',
            b'"just a string"',
            b"null",
            b"hello world",
        ],
    )
    def test_rejected_shapes(self, payload: bytes) -> None:
        with pytest.raises(PacketError):
            parse_packet(payload)

    def test_invalid_utf8_is_rejected_not_raised_as_unicode_error(self) -> None:
        with pytest.raises(PacketError):
            parse_packet(b"\xff\xfe\xfd")


class TestUdpSource:
    def test_receives_and_converts_a_datagram(self) -> None:
        received: list[MotionSample] = []
        source = UdpSource(received.append, host="127.0.0.1", port=0)
        # Port 0 would be assigned by the kernel but then unknowable, so bind a
        # real ephemeral port first and reuse it.
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()

        source = UdpSource(received.append, host="127.0.0.1", port=port)
        source.start()
        try:
            deadline = time.monotonic() + 5.0
            sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            while not received and time.monotonic() < deadline:
                sender.sendto(b'{"x":1,"y":2,"z":3}', ("127.0.0.1", port))
                time.sleep(0.05)
            sender.close()
        finally:
            source.stop()
        assert received, f"no sample received (source error: {source.error})"
        assert (received[0].x, received[0].y, received[0].z) == (1.0, 2.0, 3.0)

    def test_g_units_are_converted_to_si(self) -> None:
        source = UdpSource(lambda _s: None, units="g")
        assert source._factor == pytest.approx(GRAVITY)

    def test_si_is_the_default(self) -> None:
        assert UdpSource(lambda _s: None)._factor == 1.0

    def test_unparsable_datagrams_are_counted_not_fatal(self) -> None:
        source = UdpSource(lambda _s: None)
        assert source.rejected == 0

    def test_probe_is_always_available(self) -> None:
        assert UdpSource.probe()


class TestIioSource:
    @pytest.fixture
    def fake_device(self, tmp_path: Path) -> Path:
        root = tmp_path / "iio"
        device = root / "iio:device0"
        device.mkdir(parents=True)
        for axis, raw in (("x", "100"), ("y", "-200"), ("z", "1000")):
            (device / f"in_accel_{axis}_raw").write_text(f"{raw}\n")
        (device / "in_accel_scale").write_text("0.009806\n")
        return root

    def test_finds_a_device(self, fake_device: Path) -> None:
        assert [d.name for d in find_accelerometers(fake_device)] == ["iio:device0"]

    def test_ignores_devices_without_all_three_axes(self, tmp_path: Path) -> None:
        device = tmp_path / "iio:device0"
        device.mkdir(parents=True)
        (device / "in_accel_x_raw").write_text("1")
        assert find_accelerometers(tmp_path) == []

    def test_missing_root_is_not_an_error(self, tmp_path: Path) -> None:
        assert find_accelerometers(tmp_path / "absent") == []

    def test_probe_reports_the_device_it_would_use(self, fake_device: Path) -> None:
        availability = IioSource.probe(fake_device)
        assert availability.available
        assert "iio:device0" in availability.detail

    def test_probe_explains_an_empty_system(self, tmp_path: Path) -> None:
        assert "no accelerometer" in IioSource.probe(tmp_path).detail

    def test_shared_scale_is_applied(self, fake_device: Path) -> None:
        device = fake_device / "iio:device0"
        scales = {axis: read_scale(device, axis) for axis in "xyz"}
        sample = read_sample(device, scales)
        assert sample.x == pytest.approx(100 * 0.009806)
        assert sample.y == pytest.approx(-200 * 0.009806)

    def test_per_axis_scale_wins_over_the_shared_one(self, fake_device: Path) -> None:
        device = fake_device / "iio:device0"
        (device / "in_accel_x_scale").write_text("2.0\n")
        assert read_scale(device, "x") == 2.0
        assert read_scale(device, "y") == pytest.approx(0.009806)

    def test_a_device_without_a_scale_reports_si_directly(self, tmp_path: Path) -> None:
        device = tmp_path / "iio:device0"
        device.mkdir(parents=True)
        assert read_scale(device, "x") == 1.0

    def test_an_unreadable_scale_falls_back(self, fake_device: Path) -> None:
        (fake_device / "iio:device0" / "in_accel_scale").write_text("not a number")
        assert read_scale(fake_device / "iio:device0", "x") == 1.0

    def test_resolve_rejects_a_path_that_is_not_an_accelerometer(self, tmp_path: Path) -> None:
        source = IioSource(lambda _s: None, device=str(tmp_path))
        with pytest.raises(FileNotFoundError, match="does not look like"):
            source.resolve_device()

    def test_resolve_reports_an_empty_system_clearly(self, tmp_path: Path) -> None:
        source = IioSource(lambda _s: None, root=tmp_path)
        with pytest.raises(FileNotFoundError, match="no IIO accelerometer"):
            source.resolve_device()

    def test_emits_samples_from_a_fake_device(self, fake_device: Path) -> None:
        received: list[MotionSample] = []
        source = IioSource(received.append, poll_hz=200.0, root=fake_device)
        source.start()
        deadline = time.monotonic() + 5.0
        while not received and time.monotonic() < deadline:
            time.sleep(0.02)
        source.stop()
        assert received, f"no samples (source error: {source.error})"
        assert received[0].z == pytest.approx(1000 * 0.009806)


class TestDemoSource:
    def test_is_deterministic(self) -> None:
        assert demo_acceleration(3.0) == demo_acceleration(3.0)

    def test_loops(self) -> None:
        assert demo_acceleration(1.0) == pytest.approx(demo_acceleration(1.0 + CYCLE_SECONDS))

    def test_includes_gravity_on_the_vertical_axis(self) -> None:
        samples = [demo_acceleration(t / 10) for t in range(int(CYCLE_SECONDS * 10))]
        assert all(abs(z - GRAVITY) < 1.0 for _x, _y, z in samples)

    def test_exercises_both_planar_axes(self) -> None:
        samples = [demo_acceleration(t / 10) for t in range(int(CYCLE_SECONDS * 10))]
        assert max(abs(x) for x, _y, _z in samples) > 1.0
        assert max(abs(y) for _x, y, _z in samples) > 1.0

    def test_intensity_scales_the_planar_axes_only(self) -> None:
        gentle = demo_acceleration(3.0, intensity=0.5)
        strong = demo_acceleration(3.0, intensity=1.0)
        assert abs(strong[0]) > abs(gentle[0])
        assert gentle[2] == pytest.approx(GRAVITY + (strong[2] - GRAVITY) * 0.5)

    def test_emits_samples(self) -> None:
        received: list[MotionSample] = []
        source = DemoSource(received.append, rate_hz=200.0)
        source.start()
        deadline = time.monotonic() + 5.0
        while len(received) < 3 and time.monotonic() < deadline:
            time.sleep(0.02)
        source.stop()
        assert len(received) >= 3, f"only {len(received)} samples (error: {source.error})"


class TestLifecycle:
    def test_starting_twice_is_refused(self) -> None:
        source = DemoSource(lambda _s: None, rate_hz=10.0)
        source.start()
        try:
            with pytest.raises(RuntimeError, match="already started"):
                source.start()
        finally:
            source.stop()

    def test_stopping_an_unstarted_source_is_harmless(self) -> None:
        DemoSource(lambda _s: None).stop()

    def test_can_be_restarted(self) -> None:
        source = DemoSource(lambda _s: None, rate_hz=50.0)
        source.start()
        source.stop()
        source.start()
        source.stop()

    def test_an_exception_in_the_loop_is_captured_not_lost(self) -> None:
        class Exploding(MotionSource):
            name = "exploding"

            def _run(self) -> None:
                raise ValueError("sensor on fire")

        source = Exploding(lambda _s: None)
        source.start()
        deadline = time.monotonic() + 5.0
        while source.error is None and time.monotonic() < deadline:
            time.sleep(0.02)
        source.stop()
        assert isinstance(source.error, ValueError)

    def test_stop_is_observed_promptly(self) -> None:
        source = DemoSource(lambda _s: None, rate_hz=60.0)
        source.start()
        started = time.monotonic()
        source.stop()
        assert time.monotonic() - started < 2.0

    def test_emit_reaches_the_callback_on_the_source_thread(self) -> None:
        threads: list[str] = []
        source = DemoSource(lambda _s: threads.append(threading.current_thread().name))
        source.start()
        deadline = time.monotonic() + 5.0
        while not threads and time.monotonic() < deadline:
            time.sleep(0.02)
        source.stop()
        assert threads and threads[0].startswith("motion-")
