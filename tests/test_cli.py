"""The command line interface, exercised end to end without a display."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from motionless import cli
from motionless.config import Config
from motionless.ipc import DaemonUnavailableError
from motionless.paths import config_file
from motionless.sources import UnknownSourceError


@pytest.fixture
def no_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(*_args: object, **_kwargs: object) -> dict[str, Any]:
        raise DaemonUnavailableError("no daemon listening")

    monkeypatch.setattr(cli, "request", unavailable)
    monkeypatch.setattr(cli, "is_running", lambda *_a, **_k: False)


@pytest.fixture
def fake_daemon(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []
    responses = {
        "toggle": {"visible": True},
        "status": {
            "version": "0.1.0",
            "pid": 1234,
            "uptime": 12.5,
            "visible": True,
            "backend": "x11",
            "backend_reason": "Xorg session",
            "monitors": 2,
            "source": "demo",
            "samples": 900,
            "motion": {"lateral": 1.0, "longitudinal": -0.5, "vertical": 0.1},
        },
    }

    def fake(command: str, **_kwargs: object) -> dict[str, Any]:
        seen.append(command)
        return responses.get(command, {})

    monkeypatch.setattr(cli, "request", fake)
    monkeypatch.setattr(cli, "is_running", lambda *_a, **_k: True)
    return seen


class TestParser:
    def test_no_command_prints_help_and_succeeds(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert cli.main([]) == 0
        assert "toggle" in capsys.readouterr().out

    def test_version(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exc:
            cli.main(["--version"])
        assert exc.value.code == 0
        assert "motionless" in capsys.readouterr().out

    def test_every_subcommand_has_a_handler(self) -> None:
        parser = cli.build_parser()
        actions = [a for a in parser._actions if a.dest == "command"]
        assert set(actions[0].choices) == set(cli._HANDLERS)


class TestStatus:
    def test_reports_a_missing_daemon_as_a_failure(
        self, no_daemon: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert cli.main(["status"]) == 1
        assert "not running" in capsys.readouterr().out

    def test_json_output_is_parsable_when_stopped(
        self, no_daemon: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cli.main(["status", "--json"])
        assert json.loads(capsys.readouterr().out) == {"running": False}

    def test_human_output_covers_the_essentials(
        self, fake_daemon: list[str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert cli.main(["status"]) == 0
        out = capsys.readouterr().out
        assert "cues visible" in out
        assert "demo" in out
        assert "x11" in out

    def test_json_output_when_running(
        self, fake_daemon: list[str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert cli.main(["status", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["running"] is True
        assert payload["source"] == "demo"


class TestControlCommands:
    def test_toggle_talks_to_the_daemon(
        self, fake_daemon: list[str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert cli.main(["toggle"]) == 0
        assert fake_daemon == ["toggle"]
        assert "shown" in capsys.readouterr().out

    def test_toggle_starts_a_stopped_daemon(
        self, no_daemon: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        started: list[bool] = []
        monkeypatch.setattr(cli, "cmd_start", lambda _a: started.append(True) or 0)
        # A hotkey press should turn the overlay on, not report an error.
        assert cli.main(["toggle"]) == 0
        assert started == [True]

    def test_toggle_no_start_refuses_instead(
        self, no_daemon: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert cli.main(["toggle", "--no-start"]) == 1
        assert "not running" in capsys.readouterr().err

    @pytest.mark.parametrize(("command", "text"), [("show", "shown"), ("hide", "hidden")])
    def test_show_and_hide(
        self,
        fake_daemon: list[str],
        capsys: pytest.CaptureFixture[str],
        command: str,
        text: str,
    ) -> None:
        assert cli.main([command]) == 0
        assert text in capsys.readouterr().out

    def test_commands_that_need_a_daemon_fail_helpfully(
        self, no_daemon: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        for command in ("show", "hide", "reload"):
            assert cli.main([command]) == 1
        assert "motionless start" in capsys.readouterr().err

    def test_stop_is_idempotent(self, no_daemon: None, capsys: pytest.CaptureFixture[str]) -> None:
        # Stopping something already stopped is a success, not an error.
        assert cli.main(["stop"]) == 0
        assert "not running" in capsys.readouterr().out


class TestConfigCommand:
    def test_path(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert cli.main(["config", "path"]) == 0
        assert capsys.readouterr().out.strip() == str(config_file())

    def test_show_emits_valid_toml(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert cli.main(["config", "show"]) == 0
        out = capsys.readouterr().out
        assert "[overlay]" in out

    def test_keys_lists_settings(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert cli.main(["config", "keys"]) == 0
        assert "overlay.opacity" in capsys.readouterr().out

    def test_init_writes_the_file_then_refuses_to_overwrite(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        target = tmp_path / "config.toml"
        assert cli.main(["--config", str(target), "config", "init"]) == 0
        assert target.exists()
        assert cli.main(["--config", str(target), "config", "init"]) == 1
        assert "already exists" in capsys.readouterr().err

    def test_set_then_get_round_trips_through_the_file(
        self, tmp_path: Path, no_daemon: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        target = tmp_path / "config.toml"
        assert cli.main(["--config", str(target), "config", "set", "overlay.opacity", "0.3"]) == 0
        capsys.readouterr()
        assert cli.main(["--config", str(target), "config", "get", "overlay.opacity"]) == 0
        assert capsys.readouterr().out.strip() == "0.3"
        assert Config.load(target).overlay.opacity == 0.3

    def test_set_rejects_a_bad_value_without_writing(
        self, tmp_path: Path, no_daemon: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        target = tmp_path / "config.toml"
        Config().save(target)
        assert cli.main(["--config", str(target), "config", "set", "overlay.opacity", "7"]) == 1
        assert "opacity" in capsys.readouterr().err
        assert Config.load(target).overlay.opacity == Config().overlay.opacity

    def test_set_reloads_a_running_daemon(
        self, tmp_path: Path, fake_daemon: list[str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        target = tmp_path / "config.toml"
        assert cli.main(["--config", str(target), "config", "set", "overlay.fps", "30"]) == 0
        assert "reload" in fake_daemon

    def test_unknown_key_is_reported(
        self, no_daemon: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert cli.main(["config", "get", "overlay.sparkles"]) == 1
        assert "unknown setting" in capsys.readouterr().err


class TestDoctorAndSources:
    def test_sources_lists_every_source(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert cli.main(["sources"]) == 0
        out = capsys.readouterr().out
        for name in ("iio", "udp", "demo"):
            assert name in out

    def test_doctor_json_is_parsable(self, capsys: pytest.CaptureFixture[str]) -> None:
        cli.main(["doctor", "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert {"name", "level", "detail", "hint"} <= set(payload[0])

    def test_doctor_reports_problems_with_a_nonzero_exit(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Headless test environment: doctor must notice and say so.
        code = cli.main(["doctor"])
        out = capsys.readouterr().out
        assert "Display backend" in out
        assert code in (0, 1)


class TestPreview:
    def test_writes_a_png(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        pytest.importorskip("cairo")
        target = tmp_path / "out.png"
        assert cli.main(["preview", "-o", str(target), "--width", "400", "--height", "300"]) == 0
        assert target.read_bytes().startswith(b"\x89PNG")

    def test_transparent_background(self, tmp_path: Path) -> None:
        pytest.importorskip("cairo")
        target = tmp_path / "out.png"
        assert (
            cli.main(
                [
                    "preview",
                    "-o",
                    str(target),
                    "--background",
                    "none",
                    "--width",
                    "200",
                    "--height",
                    "200",
                ]
            )
            == 0
        )
        assert target.exists()


class TestStartCommand:
    def test_start_validates_configuration_before_detaching(
        self,
        tmp_path: Path,
        no_daemon: None,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        target = tmp_path / "config.toml"
        target.write_text("[overlay]\nopacity = 9.0\n")
        spawned: list[object] = []
        monkeypatch.setattr(cli.subprocess, "Popen", lambda *a, **k: spawned.append(a))
        # A bad config must fail here, not in a log file the user never opens.
        assert cli.main(["--config", str(target), "start"]) == 1
        assert spawned == []
        assert "opacity" in capsys.readouterr().err

    def test_start_reports_an_already_running_daemon(
        self, fake_daemon: list[str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert cli.main(["start"]) == 0
        assert "already running" in capsys.readouterr().out

    def test_child_command_passes_flags_through(self) -> None:
        parser = cli.build_parser()
        args = parser.parse_args(
            ["--config", "/tmp/c.toml", "start", "-v", "-s", "demo", "--hidden", "--always-on"]
        )
        command = cli._child_command(args)
        assert command[1:3] == ["-m", "motionless"]
        for flag in (
            "--config",
            "/tmp/c.toml",
            "run",
            "--verbose",
            "--source",
            "demo",
            "--hidden",
            "--always-on",
        ):
            assert flag in command

    def test_overrides_are_collected_for_the_daemon_to_re_apply(self) -> None:
        parser = cli.build_parser()
        args = parser.parse_args(["run", "-s", "demo", "--hidden", "--always-on"])
        assert cli._overrides(args) == {
            "motion.source": "demo",
            "start_hidden": "true",
            "overlay.always_on": "true",
        }

    def test_no_flags_means_no_overrides(self) -> None:
        parser = cli.build_parser()
        assert cli._overrides(parser.parse_args(["run"])) == {}

    def test_overrides_are_validated_when_collected(self) -> None:
        parser = cli.build_parser()
        args = parser.parse_args(["run", "-s", "telepathy"])
        with pytest.raises(UnknownSourceError):
            cli._overrides(args)

    def test_an_unknown_source_is_rejected_before_starting(
        self, no_daemon: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert cli.main(["start", "--source", "telepathy"]) == 1
        assert "unknown motion source" in capsys.readouterr().err


class TestServiceCommand:
    def test_print_emits_a_unit_file(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert cli.main(["service", "print"]) == 0
        out = capsys.readouterr().out
        assert "[Service]" in out
        assert "ExecStart=" in out

    def test_status_reports_an_uninstalled_unit(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from motionless.service import ServiceStatus

        monkeypatch.setattr(
            cli,
            "service_status",
            lambda: ServiceStatus(False, False, False, Path("/nowhere/motionless.service")),
        )
        assert cli.main(["service", "status"]) == 0
        out = capsys.readouterr().out
        assert "installed no" in out
        assert "enabled   no" in out

    def test_install_failure_is_reported_not_raised(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def refuse(**_kwargs: object) -> Path:
            raise cli.ServiceError("systemctl not found; this system does not use systemd")

        monkeypatch.setattr(cli, "service_install", refuse)
        assert cli.main(["service", "install"]) == 1
        assert "systemctl not found" in capsys.readouterr().err


class TestPair:
    """`motionless pair` — the command that makes a phone the sensor."""

    @pytest.fixture
    def android(self, monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, str]]:
        """A single Android phone, plugged in, with USB debugging accepted."""
        from motionless import pair as pair_module

        reversed_ports: list[tuple[int, str]] = []
        monkeypatch.setattr(pair_module, "find_adb", lambda: "/usr/bin/adb")
        monkeypatch.setattr(cli, "find_adb", lambda: "/usr/bin/adb", raising=False)
        monkeypatch.setattr(
            pair_module, "adb_devices", lambda _adb: [pair_module.AdbDevice("R58M1234", "device")]
        )
        monkeypatch.setattr(
            pair_module,
            "adb_reverse",
            lambda _adb, port, serial="": reversed_ports.append((port, serial)),
        )
        return reversed_ports

    def test_usb_forwards_the_port_and_points_the_phone_at_localhost(
        self,
        android: list[tuple[int, str]],
        no_daemon: None,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        assert cli.main(["pair"]) == 0
        out = capsys.readouterr().out
        assert android == [(5577, "R58M1234")]
        assert "http://localhost:5577/" in out
        # No certificate and no token: localhost is already a secure context.
        config = Config.load()
        assert config.motion.source == "phone"
        assert config.motion.phone.host == "127.0.0.1"
        assert config.motion.phone.token == ""
        assert config.motion.phone.tls_cert == ""

    def test_usb_without_adb_says_what_to_install(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from motionless import pair as pair_module

        monkeypatch.setattr(pair_module, "find_adb", lambda: None)
        assert cli.main(["pair", "--usb"]) == 1
        assert "adb is not installed" in capsys.readouterr().err

    def test_usb_with_an_unauthorised_phone_names_the_actual_problem(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The generic "turn on USB debugging" advice sends people to a setting
        # they have already changed; this state needs its own answer.
        from motionless import pair as pair_module

        monkeypatch.setattr(pair_module, "find_adb", lambda: "/usr/bin/adb")
        monkeypatch.setattr(
            pair_module,
            "adb_devices",
            lambda _adb: [pair_module.AdbDevice("R58M1234", "unauthorized")],
        )
        assert cli.main(["pair", "--usb"]) == 1
        err = capsys.readouterr().err
        assert "has not authorised this computer" in err
        assert "Allow" in err

    def test_wifi_writes_a_token_and_a_certificate(
        self, monkeypatch: pytest.MonkeyPatch, no_daemon: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from motionless import pair as pair_module

        monkeypatch.setattr(pair_module, "local_addresses", lambda: ["192.168.1.37"])
        monkeypatch.setattr(
            pair_module, "ensure_certificate", lambda _a: (Path("/c/cert.pem"), Path("/c/key.pem"))
        )
        monkeypatch.setattr(pair_module, "qr_code", lambda _url: None)
        assert cli.main(["pair", "--wifi"]) == 0
        out = capsys.readouterr().out

        config = Config.load()
        assert config.motion.phone.host == "0.0.0.0"
        assert config.motion.phone.token, "a network bind without a token is not acceptable"
        assert config.motion.phone.tls_cert == "/c/cert.pem"
        assert f"https://192.168.1.37:5577/?t={config.motion.phone.token}" in out
        assert "certificate warning" in out

    def test_wifi_without_a_network_says_so_rather_than_printing_a_dead_url(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from motionless import pair as pair_module

        monkeypatch.setattr(pair_module, "local_addresses", lambda: [])
        assert cli.main(["pair", "--wifi"]) == 1
        assert "no local network address" in capsys.readouterr().err

    def test_no_phone_plugged_in_falls_back_to_wifi_and_explains_why(
        self, monkeypatch: pytest.MonkeyPatch, no_daemon: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from motionless import pair as pair_module

        monkeypatch.setattr(pair_module, "find_adb", lambda: "/usr/bin/adb")
        monkeypatch.setattr(pair_module, "adb_devices", lambda _adb: [])
        monkeypatch.setattr(pair_module, "local_addresses", lambda: ["192.168.1.37"])
        monkeypatch.setattr(
            pair_module, "ensure_certificate", lambda _a: (Path("/c/cert.pem"), Path("/c/key.pem"))
        )
        monkeypatch.setattr(pair_module, "qr_code", lambda _url: None)
        assert cli.main(["pair"]) == 0
        assert "so: Wi-Fi" in capsys.readouterr().out

    def test_show_reprints_the_url_without_changing_anything(
        self,
        android: list[tuple[int, str]],
        no_daemon: None,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        cli.main(["pair"])
        capsys.readouterr()
        before = config_file().read_text()
        assert cli.main(["pair", "--show"]) == 0
        assert capsys.readouterr().out.strip() == "http://localhost:5577/"
        assert config_file().read_text() == before

    def test_show_before_any_pairing_says_so(
        self, no_daemon: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert cli.main(["pair", "--show"]) == 1
        assert "not 'phone'" in capsys.readouterr().err

    def test_a_running_daemon_is_told_to_reload(
        self, android: list[tuple[int, str]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Otherwise pairing appears to do nothing until the user restarts.
        seen: list[str] = []
        monkeypatch.setattr(cli, "is_running", lambda *_a, **_k: True)
        monkeypatch.setattr(cli, "request", lambda command, **_k: seen.append(command) or {})
        assert cli.main(["pair"]) == 0
        assert "reload" in seen

    def test_the_port_can_be_overridden(
        self, android: list[tuple[int, str]], no_daemon: None
    ) -> None:
        assert cli.main(["pair", "--port", "6000"]) == 0
        assert android == [(6000, "R58M1234")]
        assert Config.load().motion.phone.port == 6000
