"""XDG-compliant locations for configuration, runtime and state files."""

from __future__ import annotations

import os
from pathlib import Path

from motionless import APP_NAME


def _env_dir(variable: str, fallback: Path) -> Path:
    value = os.environ.get(variable)
    if value:
        candidate = Path(value)
        if candidate.is_absolute():
            return candidate
    return fallback


def config_dir() -> Path:
    """``$XDG_CONFIG_HOME/motionless``, by default ``~/.config/motionless``."""
    return _env_dir("XDG_CONFIG_HOME", Path.home() / ".config") / APP_NAME


def config_file() -> Path:
    return config_dir() / "config.toml"


def state_dir() -> Path:
    """``$XDG_STATE_HOME/motionless``, used for the log file."""
    return _env_dir("XDG_STATE_HOME", Path.home() / ".local" / "state") / APP_NAME


def log_file() -> Path:
    return state_dir() / "motionless.log"


def runtime_dir() -> Path:
    """Per-user runtime directory holding the control socket and PID file.

    ``$XDG_RUNTIME_DIR`` is the correct home for these; when it is missing
    (ssh sessions, minimal containers) we fall back to a uid-scoped directory
    under ``/tmp`` with owner-only permissions.
    """
    value = os.environ.get("XDG_RUNTIME_DIR")
    if value and Path(value).is_absolute():
        return Path(value) / APP_NAME
    return Path("/tmp") / f"{APP_NAME}-{os.getuid()}"


def socket_file() -> Path:
    return runtime_dir() / "control.sock"


def pid_file() -> Path:
    return runtime_dir() / "daemon.pid"


def ensure_dir(path: Path, mode: int = 0o700) -> Path:
    """Create ``path`` (and parents) if needed and return it."""
    path.mkdir(parents=True, exist_ok=True, mode=mode)
    return path


def user_unit_dir() -> Path:
    """Where the systemd *user* unit is installed."""
    return _env_dir("XDG_CONFIG_HOME", Path.home() / ".config") / "systemd" / "user"


def desktop_entry_dir() -> Path:
    return _env_dir("XDG_DATA_HOME", Path.home() / ".local" / "share") / "applications"
