"""systemd user unit rendering and lifecycle."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from motionless import service
from motionless.service import UNIT_NAME, ServiceError, executable, render_unit, unit_path


class FakeSystemctl:
    """Records `systemctl --user ...` invocations instead of running them."""

    def __init__(self, *, returncode: int = 0, stdout: str = "") -> None:
        self.calls: list[list[str]] = []
        self.returncode = returncode
        self.stdout = stdout

    def __call__(self, argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, "")

    @property
    def commands(self) -> list[str]:
        return [" ".join(call[2:]) for call in self.calls]


@pytest.fixture
def systemctl(monkeypatch: pytest.MonkeyPatch) -> FakeSystemctl:
    fake = FakeSystemctl()
    monkeypatch.setattr(service.subprocess, "run", fake)
    monkeypatch.setattr(service.shutil, "which", lambda name: f"/usr/bin/{name}")
    return fake


class TestRenderUnit:
    def test_contains_the_required_sections(self) -> None:
        unit = render_unit("/usr/bin/motionless")
        for section in ("[Unit]", "[Service]", "[Install]"):
            assert section in unit

    def test_execstart_uses_the_given_executable(self) -> None:
        unit = render_unit("/opt/bin/motionless")
        assert "ExecStart=/opt/bin/motionless run" in unit
        assert "ExecReload=/opt/bin/motionless reload" in unit

    def test_binds_to_the_graphical_session(self) -> None:
        unit = render_unit("/usr/bin/motionless")
        # A cosmetic overlay must not linger after the session ends.
        assert "PartOf=graphical-session.target" in unit
        assert "WantedBy=graphical-session.target" in unit

    def test_restarts_on_failure(self) -> None:
        assert "Restart=on-failure" in render_unit("/usr/bin/motionless")

    def test_falls_back_to_python_m_when_the_script_is_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(service.shutil, "which", lambda _name: None)
        assert "-m motionless" in executable()

    def test_prefers_the_console_script(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(service.shutil, "which", lambda _name: "/usr/local/bin/motionless")
        assert executable() == "/usr/local/bin/motionless"


class TestInstall:
    def test_writes_enables_and_reloads(self, systemctl: FakeSystemctl) -> None:
        path = service.install()
        assert path == unit_path()
        assert path.read_text().startswith("#")
        assert systemctl.commands == ["daemon-reload", f"enable {UNIT_NAME}"]

    def test_now_also_starts_it(self, systemctl: FakeSystemctl) -> None:
        service.install(start=True)
        assert f"restart {UNIT_NAME}" in systemctl.commands

    def test_no_enable_only_installs(self, systemctl: FakeSystemctl) -> None:
        service.install(enable=False)
        assert systemctl.commands == ["daemon-reload"]

    def test_creates_the_unit_directory(self, systemctl: FakeSystemctl) -> None:
        assert not unit_path().parent.exists()
        service.install()
        assert unit_path().parent.is_dir()

    def test_reinstalling_overwrites(self, systemctl: FakeSystemctl) -> None:
        path = service.install()
        path.write_text("stale")
        assert "ExecStart" in service.install().read_text()


class TestUninstall:
    def test_removes_an_installed_unit(self, systemctl: FakeSystemctl) -> None:
        service.install()
        assert service.uninstall() is True
        assert not unit_path().exists()
        assert f"disable --now {UNIT_NAME}" in systemctl.commands

    def test_is_harmless_when_nothing_is_installed(self, systemctl: FakeSystemctl) -> None:
        assert service.uninstall() is False


class TestStatus:
    def test_reports_enabled_and_active(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(service.shutil, "which", lambda name: f"/usr/bin/{name}")
        outputs = iter(["enabled", "active"])
        monkeypatch.setattr(
            service.subprocess,
            "run",
            lambda argv, **_k: subprocess.CompletedProcess(argv, 0, next(outputs), ""),
        )
        unit_path().parent.mkdir(parents=True, exist_ok=True)
        unit_path().write_text("unit")
        status = service.status()
        assert status.installed and status.enabled and status.active

    def test_without_systemd_it_only_reports_the_file(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(service.shutil, "which", lambda _name: None)
        status = service.status()
        assert not status.enabled and not status.active


class TestErrors:
    def test_a_failing_systemctl_raises_with_its_output(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(service.shutil, "which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr(
            service.subprocess,
            "run",
            lambda argv, **_k: subprocess.CompletedProcess(argv, 1, "", "Failed to connect to bus"),
        )
        with pytest.raises(ServiceError, match="Failed to connect to bus"):
            service.systemctl("daemon-reload")

    def test_check_false_swallows_the_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(service.shutil, "which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr(
            service.subprocess,
            "run",
            lambda argv, **_k: subprocess.CompletedProcess(argv, 1, "", "nope"),
        )
        assert service.systemctl("is-enabled", check=False).returncode == 1

    def test_a_system_without_systemd_says_so(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(service.shutil, "which", lambda _name: None)
        with pytest.raises(ServiceError, match="does not use systemd"):
            service.systemctl("daemon-reload")


def test_unit_path_is_inside_the_xdg_config_home(isolated_xdg: Path) -> None:
    assert unit_path() == isolated_xdg / "config" / "systemd" / "user" / UNIT_NAME
