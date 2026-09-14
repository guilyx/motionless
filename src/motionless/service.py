"""systemd *user* service integration.

A user unit is the right shape for this: it starts with the graphical session,
restarts if it crashes, and never needs root.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from motionless.paths import user_unit_dir

UNIT_NAME = "motionless.service"

UNIT_TEMPLATE = """\
# Installed by `motionless service install`. Edit freely; reinstalling overwrites.
[Unit]
Description=motionless — vehicle motion cues overlay
Documentation=https://github.com/guilyx/motionless
PartOf=graphical-session.target
After=graphical-session.target

[Service]
Type=simple
ExecStart={exec_start} run
ExecReload={exec_start} reload
Restart=on-failure
RestartSec=2
# The overlay is cosmetic; never let it outrank real work.
Nice=5

[Install]
WantedBy=graphical-session.target
"""


class ServiceError(RuntimeError):
    """Raised when systemd is unavailable or a unit operation fails."""


@dataclass(frozen=True)
class ServiceStatus:
    installed: bool
    enabled: bool
    active: bool
    unit_path: Path


def executable() -> str:
    """The command systemd should run.

    Prefer the installed console script; fall back to ``python -m motionless``
    so a unit written from a virtualenv or a checkout still works.
    """
    found = shutil.which("motionless")
    if found:
        return found
    return f"{sys.executable} -m motionless"


def unit_path() -> Path:
    return user_unit_dir() / UNIT_NAME


def render_unit(exec_start: str | None = None) -> str:
    return UNIT_TEMPLATE.format(exec_start=exec_start or executable())


def systemctl(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    if shutil.which("systemctl") is None:
        raise ServiceError("systemctl not found; this system does not use systemd")
    result = subprocess.run(
        ["systemctl", "--user", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ServiceError(f"systemctl --user {' '.join(args)} failed: {detail}")
    return result


def install(*, enable: bool = True, start: bool = False) -> Path:
    """Write the unit, reload systemd, and optionally enable and start it."""
    path = unit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_unit(), encoding="utf-8")
    systemctl("daemon-reload")
    if enable:
        systemctl("enable", UNIT_NAME)
    if start:
        systemctl("restart", UNIT_NAME)
    return path


def uninstall() -> bool:
    """Stop, disable and remove the unit. Returns whether it existed."""
    path = unit_path()
    existed = path.exists()
    if existed:
        systemctl("disable", "--now", UNIT_NAME, check=False)
    path.unlink(missing_ok=True)
    if existed:
        systemctl("daemon-reload", check=False)
    return existed


def status() -> ServiceStatus:
    path = unit_path()
    if shutil.which("systemctl") is None:
        return ServiceStatus(path.exists(), False, False, path)
    enabled = systemctl("is-enabled", UNIT_NAME, check=False).stdout.strip() == "enabled"
    active = systemctl("is-active", UNIT_NAME, check=False).stdout.strip() == "active"
    return ServiceStatus(path.exists(), enabled, active, path)
