"""The phone source: parsing, timing, the HTTP surface, and pairing."""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

from motionless import pair as pair_module
from motionless.config import Config, ConfigError
from motionless.motion import GRAVITY, MotionSample
from motionless.pair import (
    AdbDevice,
    PairError,
    adb_devices,
    adb_reverse,
    certificate_is_usable,
    ensure_certificate,
    find_adb,
    local_addresses,
    parse_devices,
    qr_code,
)
from motionless.sources import PhoneSource, build
from motionless.sources.phone import (
    MAX_BATCH_SPAN_SECONDS,
    Report,
    ReportError,
    parse_report,
    place_on_clock,
)
from motionless.sources.sender import render_page

requires_openssl = pytest.mark.skipif(
    shutil.which("openssl") is None, reason="openssl is needed to generate a certificate"
)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class TestParseReport:
    def test_batch_of_stamped_arrays(self) -> None:
        report = parse_report(b'{"samples": [[10, 0.1, -0.2, 9.8], [26, 0.2, -0.3, 9.7]]}')
        assert report.readings[0] == (10.0, 0.1, -0.2, 9.8)
        assert len(report.readings) == 2

    def test_batch_without_timestamps(self) -> None:
        report = parse_report(b'{"samples": [[0.1, -0.2, 9.8]]}')
        assert report.readings == ((None, 0.1, -0.2, 9.8),)

    def test_objects_are_accepted_so_hand_rolled_senders_work(self) -> None:
        report = parse_report(b'{"samples": [{"t": 4, "x": 1, "y": 2, "z": 3}]}')
        assert report.readings == ((4.0, 1.0, 2.0, 3.0),)

    def test_a_bare_reading_is_a_batch_of_one(self) -> None:
        # So `curl -d '{"x":0,"y":0,"z":9.8}'` is a usable smoke test.
        assert parse_report(b'{"x": 0, "y": 0, "z": 9.8}').readings == ((None, 0.0, 0.0, 9.8),)

    def test_g_units_become_metres_per_second_squared(self) -> None:
        assert parse_report(b'{"units": "g", "samples": [[0, 1, 0, 0]]}').factor == GRAVITY

    def test_the_sender_declares_an_inverted_platform(self) -> None:
        assert parse_report(b'{"invert": true, "samples": [[0, 1, 2, 3]]}').invert

    @pytest.mark.parametrize(
        ("payload", "why"),
        [
            (b"", "empty body"),
            (b"not json", "invalid JSON"),
            (b"[1, 2, 3]", "expected a JSON object"),
            (b'{"samples": []}', "non-empty array"),
            (b'{"samples": [[1, 2]]}', "must be"),
            (b'{"samples": [{"x": 1, "y": 2}]}', "missing"),
            (b'{"samples": [[0, "a", 2, 3]]}', "expected a number"),
            (b'{"units": "furlongs", "samples": [[0, 1, 2, 3]]}', "unknown units"),
            (b'{"samples": [[0, 1e9, 0, 0]]}', "out of range"),
            (b'{"samples": [[0, true, 0, 0]]}', "expected a number"),
        ],
    )
    def test_unusable_bodies_are_refused_with_a_reason(self, payload: bytes, why: str) -> None:
        with pytest.raises(ReportError, match=why):
            parse_report(payload)

    def test_an_absurd_batch_is_refused_rather_than_buffered(self) -> None:
        with pytest.raises(ReportError, match="too many"):
            parse_report(json.dumps({"samples": [[0, 0, 0, 0]] * 1001}).encode())


class TestPlaceOnClock:
    def test_spacing_within_a_batch_survives_the_trip(self) -> None:
        report = Report(((100.0, 0, 0, 1), (120.0, 0, 0, 2), (180.0, 0, 0, 3)))
        samples = place_on_clock(report, now=1000.0)
        assert [s.timestamp for s in samples] == [999.92, 999.94, 1000.0]

    def test_the_batch_is_anchored_at_its_arrival(self) -> None:
        samples = place_on_clock(Report(((5.0, 0, 0, 1),)), now=42.0)
        assert samples[0].timestamp == 42.0

    def test_unstamped_readings_all_land_on_arrival(self) -> None:
        samples = place_on_clock(Report(((None, 0, 0, 1), (None, 0, 0, 2))), now=7.0)
        assert [s.timestamp for s in samples] == [7.0, 7.0]

    def test_an_implausible_span_is_ignored_rather_than_backdating_wildly(self) -> None:
        # A sender stamping in epoch milliseconds, or in seconds, would
        # otherwise push readings minutes into the past and they would be
        # thrown away as stale.
        span = (MAX_BATCH_SPAN_SECONDS + 1) * 1000.0
        samples = place_on_clock(Report(((0.0, 0, 0, 1), (span, 0, 0, 2))), now=50.0)
        assert [s.timestamp for s in samples] == [50.0, 50.0]

    def test_out_of_order_timestamps_fall_back_to_arrival(self) -> None:
        samples = place_on_clock(Report(((30.0, 0, 0, 1), (10.0, 0, 0, 2))), now=3.0)
        assert [s.timestamp for s in samples] == [3.0, 3.0]

    def test_inversion_flips_every_axis(self) -> None:
        samples = place_on_clock(Report(((None, 1.0, -2.0, 9.8),), invert=True), now=0.0)
        assert (samples[0].x, samples[0].y, samples[0].z) == (-1.0, 2.0, -9.8)

    def test_units_and_inversion_compose(self) -> None:
        report = Report(((None, 1.0, 0.0, 0.0),), factor=GRAVITY, invert=True)
        assert place_on_clock(report, now=0.0)[0].x == pytest.approx(-GRAVITY)


@pytest.fixture
def running_source() -> Iterator[tuple[PhoneSource, list[MotionSample], str]]:
    samples: list[MotionSample] = []
    port = free_port()
    source = PhoneSource(samples.append, port=port, token="tok3n")
    source.start()
    deadline = time.monotonic() + 5.0
    while source._server is None and source.error is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert source.error is None, source.error
    try:
        yield source, samples, f"http://127.0.0.1:{port}"
    finally:
        source.stop()


def fetch(
    url: str, body: bytes | None = None, *, content_type: str = "application/json", **headers: str
) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body)
    if body is not None:
        request.add_header("Content-Type", content_type)
    for name, value in headers.items():
        request.add_header(name.replace("_", "-"), value)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


class TestHttpSurface:
    def test_the_page_is_served_and_carries_the_token(
        self, running_source: tuple[PhoneSource, list[MotionSample], str]
    ) -> None:
        _source, _samples, base = running_source
        status, body = fetch(f"{base}/?t=tok3n")
        assert status == 200
        assert b"Start sending motion" in body
        assert b"tok3n" in body

    def test_posted_readings_become_samples(
        self, running_source: tuple[PhoneSource, list[MotionSample], str]
    ) -> None:
        _source, samples, base = running_source
        payload = json.dumps({"samples": [[0, 0.5, -0.5, 9.8]]}).encode()
        assert fetch(f"{base}/motion?t=tok3n", payload) == (200, b'{"accepted": 1}')
        assert [(s.x, s.y, s.z) for s in samples] == [(0.5, -0.5, 9.8)]

    def test_the_token_may_travel_in_a_header(
        self, running_source: tuple[PhoneSource, list[MotionSample], str]
    ) -> None:
        _source, _samples, base = running_source
        payload = json.dumps({"x": 0, "y": 0, "z": 9.8}).encode()
        status, _ = fetch(f"{base}/motion", payload, X_Motionless_Token="tok3n")
        assert status == 200

    def test_a_wrong_token_is_refused(
        self, running_source: tuple[PhoneSource, list[MotionSample], str]
    ) -> None:
        _source, samples, base = running_source
        assert fetch(f"{base}/?t=nope")[0] == 403
        assert fetch(f"{base}/motion?t=nope", b'{"x":0,"y":0,"z":0}')[0] == 403
        assert samples == []

    def test_a_cross_origin_post_cannot_smuggle_motion_past_the_content_type(
        self, running_source: tuple[PhoneSource, list[MotionSample], str]
    ) -> None:
        # text/plain is the interesting one: a hostile web page can send it to
        # a loopback port with no CORS preflight. Requiring application/json
        # forces a preflight, which we never answer.
        _source, samples, base = running_source
        payload = json.dumps({"x": 9, "y": 9, "z": 9}).encode()
        status, _ = fetch(f"{base}/motion?t=tok3n", payload, content_type="text/plain")
        assert status == 415
        assert samples == []

    def test_no_cors_headers_are_ever_sent(
        self, running_source: tuple[PhoneSource, list[MotionSample], str]
    ) -> None:
        _source, _samples, base = running_source
        with urllib.request.urlopen(f"{base}/health", timeout=5) as response:
            assert "Access-Control-Allow-Origin" not in response.headers

    def test_an_oversized_body_is_refused_before_it_is_read(
        self, running_source: tuple[PhoneSource, list[MotionSample], str]
    ) -> None:
        _source, _samples, base = running_source
        payload = json.dumps({"samples": [[0, 1, 1, 1]] * 20000}).encode()
        assert fetch(f"{base}/motion?t=tok3n", payload)[0] == 413

    def test_garbage_is_rejected_with_a_reason_and_counted(
        self, running_source: tuple[PhoneSource, list[MotionSample], str]
    ) -> None:
        source, samples, base = running_source
        status, body = fetch(f"{base}/motion?t=tok3n", b"{oops}")
        assert status == 400
        assert b"invalid JSON" in body
        assert samples == []
        assert source.stats.snapshot()["rejected"] == 1

    def test_health_needs_no_token_so_pairing_can_be_checked(
        self, running_source: tuple[PhoneSource, list[MotionSample], str]
    ) -> None:
        _source, _samples, base = running_source
        status, body = fetch(f"{base}/health")
        assert status == 200
        assert json.loads(body)["app"] == "motionless"

    def test_unknown_paths_are_not_found(
        self, running_source: tuple[PhoneSource, list[MotionSample], str]
    ) -> None:
        _source, _samples, base = running_source
        assert fetch(f"{base}/admin?t=tok3n")[0] == 404
        assert fetch(f"{base}/elsewhere?t=tok3n", b"{}")[0] == 404


class TestLifecycle:
    def test_a_loopback_source_needs_no_token(self) -> None:
        samples: list[MotionSample] = []
        source = PhoneSource(samples.append, port=free_port())
        source.start()
        try:
            deadline = time.monotonic() + 5.0
            while source._server is None and time.monotonic() < deadline:
                time.sleep(0.01)
            assert fetch(source.url.replace("localhost", "127.0.0.1"))[0] == 200
        finally:
            source.stop()

    def test_stop_is_prompt(self) -> None:
        source = PhoneSource(lambda _s: None, port=free_port())
        source.start()
        time.sleep(0.2)
        started = time.monotonic()
        source.stop()
        assert time.monotonic() - started < 2.0

    def test_stopping_before_the_server_exists_does_not_leave_it_running(self) -> None:
        port = free_port()
        source = PhoneSource(lambda _s: None, port=port)
        source.start()
        source.stop()
        # The port is free again, so nothing was left listening.
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", port))

    def test_a_missing_certificate_surfaces_as_an_error_not_a_silent_no_op(self) -> None:
        source = PhoneSource(
            lambda _s: None, port=free_port(), tls_cert="/nope/cert.pem", tls_key="/nope/key.pem"
        )
        source.start()
        deadline = time.monotonic() + 5.0
        while source.error is None and time.monotonic() < deadline:
            time.sleep(0.01)
        source.stop()
        assert isinstance(source.error, FileNotFoundError)

    def test_an_occupied_port_surfaces_as_an_error(self) -> None:
        with socket.socket() as held:
            held.bind(("127.0.0.1", 0))
            held.listen(1)
            port = held.getsockname()[1]
            source = PhoneSource(lambda _s: None, port=port)
            source.start()
            deadline = time.monotonic() + 5.0
            while source.error is None and time.monotonic() < deadline:
                time.sleep(0.01)
            source.stop()
            assert isinstance(source.error, OSError)

    def test_the_invert_setting_overrides_what_the_sender_claims(self) -> None:
        samples: list[MotionSample] = []
        source = PhoneSource(samples.append, invert="never")
        source.ingest(b'{"invert": true, "samples": [[0, 1, 2, 3]]}')
        assert (samples[0].x, samples[0].y, samples[0].z) == (1.0, 2.0, 3.0)

        forced: list[MotionSample] = []
        PhoneSource(forced.append, invert="always").ingest(b'{"samples": [[0, 1, 2, 3]]}')
        assert (forced[0].x, forced[0].y, forced[0].z) == (-1.0, -2.0, -3.0)

    def test_details_tell_the_user_where_to_point_the_phone(self) -> None:
        source = PhoneSource(lambda _s: None, port=1234, token="abc")
        assert source.details()["url"] == "http://localhost:1234/?t=abc"
        assert source.details()["secure"] is False

    def test_probe_is_available_but_says_it_needs_pairing(self) -> None:
        assert PhoneSource.probe()
        assert "pair" in PhoneSource.probe().detail


class TestSenderPage:
    def test_the_page_is_self_contained(self) -> None:
        page = render_page()
        # The phone is in a car with no network beyond the cable; a page that
        # fetches a stylesheet or a script would render blank.
        assert 'src="http' not in page
        assert 'href="http' not in page

    def test_it_asks_ios_for_permission_from_a_tap(self) -> None:
        page = render_page()
        assert "requestPermission" in page
        assert 'addEventListener("click", start)' in page

    def test_it_warns_when_the_context_is_insecure(self) -> None:
        assert "isSecureContext" in render_page()

    def test_a_token_that_would_break_out_of_the_page_is_refused(self) -> None:
        with pytest.raises(ValueError, match="URL-safe"):
            render_page('"; alert(1); //')


class TestPhoneConfig:
    def test_it_is_reachable_through_the_registry(self) -> None:
        config = Config()
        config.motion.source = "phone"
        config.motion.phone.port = 4321
        source = build(config.motion, lambda _s: None)
        assert isinstance(source, PhoneSource)
        assert source.address == ("127.0.0.1", 4321)

    def test_a_network_bind_without_a_token_is_refused(self) -> None:
        config = Config()
        config.motion.phone.host = "0.0.0.0"
        with pytest.raises(ConfigError, match="token is required"):
            config.validate()

    def test_a_half_configured_certificate_is_refused(self) -> None:
        config = Config()
        config.motion.phone.tls_cert = "/tmp/cert.pem"
        with pytest.raises(ConfigError, match="both tls_cert and tls_key"):
            config.validate()

    def test_an_unknown_inversion_mode_is_refused(self) -> None:
        config = Config()
        config.motion.phone.invert = "sideways"
        with pytest.raises(ConfigError, match=r"motion\.phone\.invert"):
            config.validate()

    def test_interdependent_settings_can_be_applied_together(self) -> None:
        # Applied one at a time, the host would be rejected before the token
        # arrived — which is exactly what `motionless pair` needs to do.
        config = Config()
        config.update({"motion.phone.host": "0.0.0.0", "motion.phone.token": "abc"})
        assert config.motion.phone.host == "0.0.0.0"

    def test_a_failed_batch_leaves_nothing_behind(self) -> None:
        config = Config()
        with pytest.raises(ConfigError):
            config.update({"motion.phone.port": "9999", "motion.phone.units": "furlongs"})
        assert config.motion.phone.port == 5577


class TestPairing:
    def test_adb_device_lines_are_parsed(self) -> None:
        devices = parse_devices(
            "List of devices attached\nR58M1234\tdevice\nemulator-5554\toffline\n\n"
        )
        assert devices == [AdbDevice("R58M1234", "device"), AdbDevice("emulator-5554", "offline")]
        assert [d.ready for d in devices] == [True, False]

    def test_daemon_chatter_is_ignored(self) -> None:
        assert parse_devices("* daemon starting on tcp:5037 *\nList of devices attached\n") == []

    @requires_openssl
    def test_a_certificate_is_reused_while_it_still_covers_the_address(
        self, tmp_path: Path
    ) -> None:
        cert, key = ensure_certificate(["192.168.1.5"], directory=tmp_path)
        assert cert.exists() and key.exists()
        assert certificate_is_usable(tmp_path, ["192.168.1.5"])
        first = cert.read_bytes()
        ensure_certificate(["192.168.1.5"], directory=tmp_path)
        assert cert.read_bytes() == first

    @requires_openssl
    def test_a_certificate_is_regenerated_for_a_new_network(self, tmp_path: Path) -> None:
        ensure_certificate(["192.168.1.5"], directory=tmp_path)
        first = (tmp_path / "cert.pem").read_bytes()
        assert not certificate_is_usable(tmp_path, ["10.0.0.9"])
        ensure_certificate(["10.0.0.9"], directory=tmp_path)
        assert (tmp_path / "cert.pem").read_bytes() != first

    @requires_openssl
    def test_the_private_key_is_not_world_readable(self, tmp_path: Path) -> None:
        _cert, key = ensure_certificate(["192.168.1.5"], directory=tmp_path)
        assert key.stat().st_mode & 0o077 == 0

    def test_a_missing_certificate_is_not_usable(self, tmp_path: Path) -> None:
        assert not certificate_is_usable(tmp_path, ["192.168.1.5"])

    def test_a_certificate_without_its_sidecar_is_regenerated(self, tmp_path: Path) -> None:
        (tmp_path / "cert.pem").write_text("not really a certificate")
        (tmp_path / "key.pem").write_text("nor a key")
        assert not certificate_is_usable(tmp_path, ["192.168.1.5"])

    def test_a_corrupt_sidecar_is_regenerated_rather_than_trusted(self, tmp_path: Path) -> None:
        for name in ("cert.pem", "key.pem"):
            (tmp_path / name).write_text("x")
        (tmp_path / "cert.json").write_text("{ this is not json")
        assert not certificate_is_usable(tmp_path, ["192.168.1.5"])

    def test_an_expiring_certificate_is_replaced_before_the_roadside(self, tmp_path: Path) -> None:
        for name in ("cert.pem", "key.pem"):
            (tmp_path / name).write_text("x")
        (tmp_path / "cert.json").write_text(
            json.dumps({"addresses": ["192.168.1.5"], "expires": time.time() + 3600})
        )
        assert not certificate_is_usable(tmp_path, ["192.168.1.5"])

    def test_pairing_without_openssl_says_what_to_install(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(pair_module.shutil, "which", lambda _name: None)
        with pytest.raises(PairError, match="openssl is needed"):
            ensure_certificate(["192.168.1.5"], directory=tmp_path)


class TestAdb:
    def test_adb_is_found_in_the_sdk_when_it_is_not_on_the_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sdk = tmp_path / "adb"
        sdk.write_text("#!/bin/sh\n")
        monkeypatch.setattr(pair_module.shutil, "which", lambda _name: None)
        monkeypatch.setattr(pair_module, "_ADB_FALLBACKS", (sdk,))
        assert find_adb() == str(sdk)

    def test_no_adb_anywhere_is_reported_as_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(pair_module.shutil, "which", lambda _name: None)
        monkeypatch.setattr(pair_module, "_ADB_FALLBACKS", ())
        assert find_adb() is None

    def test_a_failing_adb_reports_what_it_said(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def failed(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess([], 1, "", "error: no devices/emulators found")

        monkeypatch.setattr(pair_module.subprocess, "run", failed)
        with pytest.raises(PairError, match="no devices/emulators found"):
            adb_devices("adb")

    def test_an_adb_that_hangs_does_not_hang_pairing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def timeout(*_args: object, **_kwargs: object) -> None:
            raise subprocess.TimeoutExpired("adb", 15.0)

        monkeypatch.setattr(pair_module.subprocess, "run", timeout)
        with pytest.raises(PairError, match="timed out"):
            adb_devices("adb")

    def test_reverse_forwards_our_port_to_the_named_device(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[list[str]] = []

        def record(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            seen.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        monkeypatch.setattr(pair_module.subprocess, "run", record)
        adb_reverse("/usr/bin/adb", 5577, serial="R58M1234")
        assert seen == [["/usr/bin/adb", "-s", "R58M1234", "reverse", "tcp:5577", "tcp:5577"]]


class TestAddresses:
    def test_loopback_and_link_local_addresses_are_not_offered_to_a_phone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        listing = (
            "1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever\n"
            "2: wlan0 inet 192.168.1.37/24 scope global wlan0\\ valid_lft 3000sec\n"
            "3: eth0  inet 169.254.4.4/16 scope global eth0\\   valid_lft forever\n"
            "4: eth1  inet 10.0.0.9/24 scope global eth1\\      valid_lft forever\n"
        )
        monkeypatch.setattr(pair_module, "primary_address", lambda: "192.168.1.37")
        monkeypatch.setattr(pair_module.shutil, "which", lambda name: "/usr/bin/" + name)
        monkeypatch.setattr(
            pair_module.subprocess,
            "run",
            lambda *_a, **_k: subprocess.CompletedProcess([], 0, listing, ""),
        )
        assert local_addresses() == ["192.168.1.37", "10.0.0.9"]

    def test_without_the_ip_command_the_routing_guess_still_answers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(pair_module, "primary_address", lambda: "192.168.1.37")
        monkeypatch.setattr(pair_module.shutil, "which", lambda _name: None)
        assert local_addresses() == ["192.168.1.37"]

    def test_a_machine_with_no_route_offers_nothing_rather_than_loopback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(pair_module, "primary_address", lambda: None)
        monkeypatch.setattr(pair_module.shutil, "which", lambda _name: None)
        assert local_addresses() == []

    def test_a_qr_code_is_optional_not_required(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(pair_module.shutil, "which", lambda _name: None)
        assert qr_code("https://example.invalid") is None

    def test_a_qr_code_is_rendered_when_qrencode_is_there(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(pair_module.shutil, "which", lambda name: "/usr/bin/" + name)
        monkeypatch.setattr(
            pair_module.subprocess,
            "run",
            lambda *_a, **_k: subprocess.CompletedProcess([], 0, "\u2588\u2588\n", ""),
        )
        assert qr_code("https://example.invalid") == "\u2588\u2588\n"
