"""Environment diagnostics."""

from __future__ import annotations

import builtins
from pathlib import Path

import pytest

from motionless import doctor
from motionless.config import Config
from motionless.doctor import (
    Check,
    Level,
    check_config,
    check_daemon,
    check_gtk,
    check_sources,
    run_checks,
    try_import,
    worst,
)


class TestTryImport:
    def test_a_working_module_reports_no_failure(self) -> None:
        assert try_import("json") is None

    def test_a_missing_module_reports_the_error(self) -> None:
        message = try_import("definitely_not_a_real_module_xyz")
        assert message is not None
        assert "ModuleNotFoundError" in message

    def test_a_module_that_raises_on_import_is_caught(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # PyGObject built for another Python minor version fails exactly like
        # this: found on the path, but an ImportError when it loads.
        def explode(name: str) -> None:
            raise ImportError("cannot import name '_gi'")

        monkeypatch.setattr(doctor.importlib, "import_module", explode)
        message = try_import("gi")
        assert message is not None
        assert "_gi" in message


class TestCheckGtk:
    def test_an_unloadable_but_present_module_gets_the_abi_hint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(doctor, "try_import", lambda _n: "ImportError: cannot import _gi")
        monkeypatch.setattr(doctor.importlib.util, "find_spec", lambda _n: object())
        checks = check_gtk()
        assert checks[0].level is Level.FAIL
        # The fix for an ABI mismatch is not "install the package".
        assert "apt install" not in checks[0].hint
        assert "system-site-packages" in checks[0].hint

    def test_a_genuinely_missing_module_gets_the_install_hint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(doctor, "try_import", lambda _n: "ModuleNotFoundError: no gi")
        monkeypatch.setattr(doctor.importlib.util, "find_spec", lambda _n: None)
        checks = check_gtk()
        assert checks[0].level is Level.FAIL
        assert "apt install" in checks[0].hint

    def test_reports_the_gtk_version_when_everything_works(self) -> None:
        if try_import("gi") is not None:
            pytest.skip("PyGObject is not usable on this interpreter")
        checks = {c.name: c for c in check_gtk()}
        assert checks["PyGObject"].level is Level.OK
        assert checks["GTK 3 typelib"].level is Level.OK
        assert checks["GTK 3 typelib"].detail.startswith("GTK ")

    def test_a_broken_install_short_circuits_the_remaining_gtk_checks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(doctor, "try_import", lambda _n: "ImportError: broken")
        monkeypatch.setattr(doctor.importlib.util, "find_spec", lambda _n: None)
        # Without PyGObject the later checks would all fail for the same
        # reason; one clear message beats three confusing ones.
        assert len(check_gtk()) == 1


class TestCheckConfig:
    def test_absent_file_is_fine_and_says_how_to_create_one(self, tmp_path: Path) -> None:
        check = check_config(tmp_path / "absent.toml")[0]
        assert check.level is Level.OK
        assert "config init" in check.hint

    def test_a_valid_file_passes(self, tmp_path: Path) -> None:
        path = Config().save(tmp_path / "config.toml")
        assert check_config(path)[0].level is Level.OK

    def test_a_broken_file_fails_with_the_reason(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        path.write_text("[overlay]\nopacity = 12\n")
        check = check_config(path)[0]
        assert check.level is Level.FAIL
        assert "opacity" in check.detail


class TestOther:
    def test_real_sources_are_reported_and_the_choice_is_named(self) -> None:
        names = {c.name for c in check_sources()}
        assert {"Source: iio", "Source: udp", "Source: demo"} <= names
        # "none" is an internal placeholder, not something to offer a user.
        assert "Source: none" not in names
        assert "Auto-detected source" in names

    def test_no_accelerometer_is_surfaced_as_a_warning_with_a_way_out(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from motionless.sources import IioSource
        from motionless.sources.base import Availability

        monkeypatch.setattr(
            IioSource, "probe", classmethod(lambda cls, *a: Availability.no("none here"))
        )
        chosen = next(c for c in check_sources() if c.name == "Auto-detected source")
        assert chosen.level is Level.WARN
        assert "no cues will appear" in chosen.detail
        assert "udp" in chosen.hint

    def test_a_stopped_daemon_is_a_warning_not_a_failure(self) -> None:
        check = check_daemon()[0]
        assert check.level is Level.WARN
        assert "motionless start" in check.hint

    def test_run_checks_covers_every_area(self) -> None:
        names = [c.name for c in run_checks()]
        for expected in ("motionless", "Python", "Display backend", "Configuration", "Daemon"):
            assert expected in names

    def test_every_failure_carries_a_hint_or_a_reason(self) -> None:
        for check in run_checks():
            if check.level is Level.FAIL:
                assert check.detail or check.hint


class TestWorst:
    def test_picks_the_most_severe(self) -> None:
        ok = Check("a", Level.OK, "")
        warn = Check("b", Level.WARN, "")
        fail = Check("c", Level.FAIL, "")
        assert worst([ok]) is Level.OK
        assert worst([ok, warn]) is Level.WARN
        assert worst([ok, warn, fail]) is Level.FAIL

    def test_no_checks_is_not_a_failure(self) -> None:
        assert worst([]) is Level.OK


def test_import_of_a_real_module_is_not_faked(monkeypatch: pytest.MonkeyPatch) -> None:
    # Guard against try_import silently swallowing a real success path.
    real = builtins.__import__
    assert real is builtins.__import__
    assert try_import("dataclasses") is None
