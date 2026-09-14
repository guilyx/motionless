"""Environment diagnostics — ``motionless doctor``.

The failure modes of a screen overlay are all environmental (no compositor, no
GTK bindings, a Wayland session without layer-shell), so the most useful thing
the program can do when something is off is explain exactly which piece is
missing and how to install it.
"""

from __future__ import annotations

import importlib
import importlib.util
import platform
import shutil
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from motionless import __version__
from motionless import service as service_module
from motionless.config import Config, ConfigError
from motionless.ipc import is_running, read_pid
from motionless.overlay import backend as backend_module
from motionless.paths import config_file
from motionless.sources import probe_all


class Level(Enum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True)
class Check:
    name: str
    level: Level
    detail: str
    hint: str = ""


APT_HINT = "sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0"


def try_import(name: str) -> str | None:
    """Import ``name``; return the failure message, or ``None`` on success.

    Actually importing matters. PyGObject's compiled extension is built for
    one specific Python minor version, so a module that is *found* on the path
    can still fail to load. Reporting "importable" from ``find_spec`` alone
    would hide the most common broken install there is: a virtualenv on a
    different Python than the distribution's ``python3-gi``.
    """
    try:
        importlib.import_module(name)
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


def _mismatch_hint() -> str:
    running = f"{sys.version_info.major}.{sys.version_info.minor}"
    return (
        f"the module is installed but will not load on Python {running} — it is built "
        f"for your distribution's python3. Install with "
        f"`pipx install --system-site-packages motionless-overlay`, or build the "
        f"virtualenv with that interpreter"
    )


def check_gtk() -> list[Check]:
    checks: list[Check] = []

    failure = try_import("gi")
    if failure is not None:
        # Present but unloadable is an ABI mismatch, not a missing package,
        # and needs a completely different fix.
        hint = _mismatch_hint() if importlib.util.find_spec("gi") else APT_HINT
        return [Check("PyGObject", Level.FAIL, failure, hint)]
    checks.append(Check("PyGObject", Level.OK, "imports cleanly"))

    failure = try_import("cairo")
    if failure is not None:
        hint = _mismatch_hint() if importlib.util.find_spec("cairo") else APT_HINT
        checks.append(Check("pycairo", Level.FAIL, failure, hint))
    else:
        checks.append(Check("pycairo", Level.OK, "imports cleanly"))

    try:
        import gi

        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk

        checks.append(
            Check(
                "GTK 3 typelib",
                Level.OK,
                f"GTK {Gtk.get_major_version()}.{Gtk.get_minor_version()}",
            )
        )
    except (ImportError, ValueError) as exc:
        checks.append(Check("GTK 3 typelib", Level.FAIL, str(exc), APT_HINT))
    return checks


def check_display() -> list[Check]:
    plan = backend_module.plan_backend()
    level = Level.OK if plan.usable else Level.FAIL
    if plan.caveats:
        level = Level.WARN
    checks = [
        Check(
            "Display backend",
            level,
            f"{plan.mode} — {plan.reason}",
            "; ".join(plan.caveats),
        )
    ]

    if backend_module.layer_shell_available():
        checks.append(Check("gtk-layer-shell", Level.OK, "installed"))
    else:
        checks.append(
            Check(
                "gtk-layer-shell",
                Level.WARN if plan.mode != "none" else Level.FAIL,
                "not installed",
                "sudo apt install gir1.2-gtklayershell-0.1  "
                "(needed for native Wayland overlays on Sway/Hyprland/KDE)",
            )
        )

    session = backend_module.session_type()
    desktop = backend_module.desktop()
    checks.append(Check("Session", Level.OK, f"{session or 'unknown'} / {desktop or 'unknown'}"))
    if (
        session == "wayland"
        and "GNOME" in desktop.upper()
        and not backend_module.layer_shell_available()
    ):
        checks.append(
            Check(
                "GNOME Wayland",
                Level.WARN,
                "Mutter does not implement wlr-layer-shell, so cues run through XWayland",
                "works for normal windows; log into 'Ubuntu on Xorg' if you want no caveats",
            )
        )
    return checks


def check_sources() -> list[Check]:
    checks = []
    for name, availability in probe_all().items():
        level = Level.OK if availability.available else Level.WARN
        checks.append(Check(f"Source: {name}", level, availability.detail or "-"))
    return checks


def check_config(path: Path | None = None) -> list[Check]:
    target = path or config_file()
    if not target.exists():
        return [
            Check(
                "Configuration",
                Level.OK,
                f"using built-in defaults (no {target})",
                "run `motionless config init` to write a commented file you can edit",
            )
        ]
    try:
        Config.load(target, missing_ok=False)
    except ConfigError as exc:
        return [Check("Configuration", Level.FAIL, f"{target}: {exc}")]
    return [Check("Configuration", Level.OK, str(target))]


def check_daemon() -> list[Check]:
    if is_running():
        pid = read_pid()
        return [Check("Daemon", Level.OK, f"running (pid {pid})" if pid else "running")]
    return [Check("Daemon", Level.WARN, "not running", "start it with `motionless start`")]


def check_service() -> list[Check]:
    if shutil.which("systemctl") is None:
        return [Check("systemd user unit", Level.WARN, "systemctl not found")]
    status = service_module.status()
    if not status.installed:
        return [
            Check(
                "systemd user unit",
                Level.WARN,
                "not installed",
                "run `motionless service install` to start cues with your session",
            )
        ]
    state = "enabled" if status.enabled else "installed but not enabled"
    return [
        Check(
            "systemd user unit", Level.OK, f"{state}, {'active' if status.active else 'inactive'}"
        )
    ]


def run_checks(config_path: Path | None = None) -> list[Check]:
    """Every diagnostic, in the order they are worth reading."""
    return [
        Check("motionless", Level.OK, f"version {__version__}"),
        Check("Python", Level.OK, platform.python_version()),
        *check_gtk(),
        *check_display(),
        *check_sources(),
        *check_config(config_path),
        *check_daemon(),
        *check_service(),
    ]


def worst(checks: list[Check]) -> Level:
    if any(check.level is Level.FAIL for check in checks):
        return Level.FAIL
    if any(check.level is Level.WARN for check in checks):
        return Level.WARN
    return Level.OK
