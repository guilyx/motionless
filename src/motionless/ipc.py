"""Control protocol between the CLI and the running daemon.

One newline-delimited JSON object each way over a Unix domain socket. That is
enough for ``toggle`` to be instant and scriptable, needs no session bus, and
keeps the socket owner-only by virtue of living in ``$XDG_RUNTIME_DIR``.

The server is intentionally main-loop agnostic: it exposes a file descriptor
for the caller to watch (GLib, selectors, a test) and handles one connection
per :meth:`ControlServer.handle_ready` call.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
from collections.abc import Callable
from pathlib import Path
from typing import Any

from motionless.paths import ensure_dir, pid_file, socket_file

#: Commands the daemon understands.
COMMANDS = ("ping", "status", "show", "hide", "toggle", "reload", "quit")

#: Requests and responses are small; this guards against a runaway peer.
MAX_MESSAGE_BYTES = 64 * 1024

#: Seconds a CLI call waits for the daemon to answer.
DEFAULT_TIMEOUT = 3.0

#: Dispatches a command name plus arguments and returns the response payload.
CommandHandler = Callable[[str, dict[str, Any]], dict[str, Any]]


class DaemonUnavailableError(RuntimeError):
    """Raised when no daemon is listening on the control socket."""


class ProtocolError(RuntimeError):
    """Raised for a malformed request or response."""


def encode(message: dict[str, Any]) -> bytes:
    return (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")


def decode(payload: bytes) -> dict[str, Any]:
    try:
        message = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"malformed message: {exc}") from exc
    if not isinstance(message, dict):
        raise ProtocolError("message must be a JSON object")
    return message


def _read_line(connection: socket.socket) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = connection.recv(4096)
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
        if b"\n" in chunk:
            break
        if size > MAX_MESSAGE_BYTES:
            raise ProtocolError("message too large")
    return b"".join(chunks).split(b"\n", 1)[0]


# ----------------------------------------------------------------------- client


def request(
    command: str,
    *,
    path: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    **args: Any,
) -> dict[str, Any]:
    """Send ``command`` to the daemon and return its response payload.

    Raises :class:`DaemonUnavailableError` if nothing is listening, and
    :class:`RuntimeError` if the daemon reports a failure.
    """
    target = path or socket_file()
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(timeout)
    try:
        connection.connect(str(target))
    except (FileNotFoundError, ConnectionRefusedError, OSError) as exc:
        connection.close()
        raise DaemonUnavailableError(f"no daemon listening at {target}") from exc
    try:
        connection.sendall(encode({"command": command, "args": args}))
        response = decode(_read_line(connection))
    finally:
        connection.close()

    if not response.get("ok", False):
        raise RuntimeError(str(response.get("error", "daemon reported an unknown failure")))
    data = response.get("data", {})
    return data if isinstance(data, dict) else {"result": data}


def is_running(path: Path | None = None) -> bool:
    """Whether a daemon answers on the control socket."""
    try:
        request("ping", path=path, timeout=1.0)
    except (DaemonUnavailableError, RuntimeError, ProtocolError, TimeoutError):
        return False
    return True


def read_pid(path: Path | None = None) -> int | None:
    """The daemon's PID from the pid file, if it names a live process."""
    target = path or pid_file()
    try:
        pid = int(target.read_text().strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        return pid
    return pid


# ----------------------------------------------------------------------- server


class ControlServer:
    """Listening half of the control protocol."""

    def __init__(self, handler: CommandHandler, path: Path | None = None) -> None:
        self._handler = handler
        self.path = path or socket_file()
        self._socket: socket.socket | None = None

    def bind(self) -> int:
        """Create the listening socket and return its file descriptor."""
        ensure_dir(self.path.parent)
        self._clear_stale_socket()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(self.path))
        # Belt and braces: $XDG_RUNTIME_DIR is already 0700, but a /tmp
        # fallback deserves an explicit mode on the socket too.
        with contextlib.suppress(OSError):
            self.path.chmod(0o600)
        server.listen(8)
        server.setblocking(False)
        self._socket = server
        return server.fileno()

    def _clear_stale_socket(self) -> None:
        if not self.path.exists():
            return
        if is_running(self.path):
            raise OSError(f"another motionless daemon is already listening at {self.path}")
        self.path.unlink(missing_ok=True)

    def fileno(self) -> int:
        if self._socket is None:
            raise RuntimeError("control server is not bound")
        return self._socket.fileno()

    def handle_ready(self) -> None:
        """Accept and serve one pending connection. Never raises on peer error."""
        if self._socket is None:
            return
        try:
            connection, _ = self._socket.accept()
        except (BlockingIOError, OSError):
            return
        connection.settimeout(DEFAULT_TIMEOUT)
        try:
            response = self._dispatch(_read_line(connection))
        except (ProtocolError, OSError) as exc:
            response = {"ok": False, "error": str(exc)}
        with contextlib.suppress(OSError):
            connection.sendall(encode(response))
        connection.close()

    def _dispatch(self, raw: bytes) -> dict[str, Any]:
        message = decode(raw)
        command = message.get("command")
        if not isinstance(command, str) or command not in COMMANDS:
            return {
                "ok": False,
                "error": f"unknown command {command!r}; expected one of {', '.join(COMMANDS)}",
            }
        args = message.get("args") or {}
        if not isinstance(args, dict):
            return {"ok": False, "error": "args must be an object"}
        try:
            data = self._handler(command, args)
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return {"ok": True, "data": data}

    def close(self) -> None:
        if self._socket is not None:
            with contextlib.suppress(OSError):
                self._socket.close()
            self._socket = None
        self.path.unlink(missing_ok=True)


class PidFile:
    """Writes the daemon's PID on enter and removes it on exit."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or pid_file()

    def __enter__(self) -> PidFile:
        ensure_dir(self.path.parent)
        self.path.write_text(f"{os.getpid()}\n", encoding="utf-8")
        return self

    def __exit__(self, *_exc: object) -> None:
        with contextlib.suppress(OSError):
            if self.path.exists() and self.path.read_text().strip() == str(os.getpid()):
                self.path.unlink()
