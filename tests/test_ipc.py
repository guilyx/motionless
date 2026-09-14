"""The control socket protocol."""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from motionless.ipc import (
    COMMANDS,
    ControlServer,
    DaemonUnavailableError,
    PidFile,
    ProtocolError,
    decode,
    encode,
    is_running,
    read_pid,
    request,
)


class Harness:
    """Runs a ControlServer on a background thread for the duration of a test."""

    def __init__(self, path: Path, handler: Any) -> None:
        self.path = path
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._handler = handler
        self._server = ControlServer(self._record, path)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _record(self, command: str, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((command, args))
        return self._handler(command, args)

    def __enter__(self) -> Harness:
        self._server.bind()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return self

    def _serve(self) -> None:
        import selectors

        selector = selectors.DefaultSelector()
        selector.register(self._server.fileno(), selectors.EVENT_READ)
        while not self._stop.is_set():
            if selector.select(timeout=0.05):
                self._server.handle_ready()

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(2.0)
        self._server.close()


@pytest.fixture
def server(tmp_path: Path) -> Iterator[Harness]:
    with Harness(tmp_path / "control.sock", lambda c, a: {"echo": c, **a}) as harness:
        yield harness


class TestCodec:
    def test_round_trip(self) -> None:
        assert decode(encode({"a": 1})) == {"a": 1}

    def test_messages_are_newline_terminated(self) -> None:
        assert encode({"a": 1}).endswith(b"\n")

    @pytest.mark.parametrize("payload", [b"not json", b"[1,2]", b'"text"', b"\xff\xfe"])
    def test_malformed_payloads_are_rejected(self, payload: bytes) -> None:
        with pytest.raises(ProtocolError):
            decode(payload)


class TestRequest:
    def test_round_trip_with_arguments(self, server: Harness) -> None:
        assert request("toggle", path=server.path, mode="on") == {"echo": "toggle", "mode": "on"}
        assert server.calls == [("toggle", {"mode": "on"})]

    def test_ping_answers(self, server: Harness) -> None:
        assert is_running(server.path)

    def test_no_socket_means_no_daemon(self, tmp_path: Path) -> None:
        with pytest.raises(DaemonUnavailableError):
            request("ping", path=tmp_path / "absent.sock")
        assert not is_running(tmp_path / "absent.sock")

    def test_a_stale_socket_file_is_not_mistaken_for_a_daemon(self, tmp_path: Path) -> None:
        stale = tmp_path / "control.sock"
        stale.touch()
        assert not is_running(stale)

    def test_unknown_commands_are_refused_by_the_server(self, server: Harness) -> None:
        with pytest.raises(RuntimeError, match="unknown command"):
            request("self-destruct", path=server.path)
        assert server.calls == []

    def test_every_declared_command_is_accepted(self, server: Harness) -> None:
        for command in COMMANDS:
            request(command, path=server.path)
        assert [c for c, _ in server.calls] == list(COMMANDS)

    def test_a_handler_exception_becomes_an_error_response(self, tmp_path: Path) -> None:
        def explode(command: str, _args: dict[str, Any]) -> dict[str, Any]:
            if command == "status":
                raise ValueError("no can do")
            return {}

        with Harness(tmp_path / "s.sock", explode) as harness:
            with pytest.raises(RuntimeError, match="ValueError: no can do"):
                request("status", path=harness.path)
            # The server must survive a failing handler.
            assert is_running(harness.path)

    def test_non_object_args_are_refused(self, server: Harness) -> None:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(3.0)
        connection.connect(str(server.path))
        connection.sendall(encode({"command": "status", "args": ["not", "an", "object"]}))
        response = json.loads(connection.recv(4096).decode())
        connection.close()
        assert response["ok"] is False
        assert "args must be an object" in response["error"]

    def test_garbage_does_not_kill_the_server(self, server: Harness) -> None:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(3.0)
        connection.connect(str(server.path))
        connection.sendall(b"absolute nonsense\n")
        connection.recv(4096)
        connection.close()
        assert is_running(server.path)


class TestSlowCommands:
    def test_reload_is_allowed_longer_than_the_default(self) -> None:
        from motionless.ipc import DEFAULT_TIMEOUT, SLOW_COMMANDS

        # reload rebuilds overlay state. Giving up at the default made the CLI
        # report a failure for a change the daemon had actually applied, and
        # the raised error then skipped whatever the caller did next.
        assert SLOW_COMMANDS["reload"] > DEFAULT_TIMEOUT

    def test_a_slow_command_is_not_cut_short_by_the_default(self, tmp_path: Path) -> None:
        import time

        def slow(command: str, _args: dict[str, Any]) -> dict[str, Any]:
            if command == "reload":
                time.sleep(DEFAULT_TIMEOUT + 1.5)
            return {"reloaded": True}

        from motionless.ipc import DEFAULT_TIMEOUT

        with Harness(tmp_path / "s.sock", slow) as harness:
            started = time.monotonic()
            assert request("reload", path=harness.path) == {"reloaded": True}
            assert time.monotonic() - started > DEFAULT_TIMEOUT

    def test_other_commands_keep_the_short_timeout(self) -> None:
        from motionless.ipc import SLOW_COMMANDS

        assert "status" not in SLOW_COMMANDS
        assert "quit" not in SLOW_COMMANDS


class TestBinding:
    def test_bind_creates_the_socket_owner_only(self, tmp_path: Path) -> None:
        with Harness(tmp_path / "nested" / "control.sock", lambda c, a: {}) as harness:
            assert harness.path.exists()
            assert harness.path.stat().st_mode & 0o077 == 0

    def test_close_removes_the_socket(self, tmp_path: Path) -> None:
        path = tmp_path / "control.sock"
        with Harness(path, lambda c, a: {}):
            assert path.exists()
        assert not path.exists()

    def test_a_stale_socket_is_replaced(self, tmp_path: Path) -> None:
        path = tmp_path / "control.sock"
        path.parent.mkdir(exist_ok=True)
        path.touch()
        with Harness(path, lambda c, a: {"ok": True}) as harness:
            assert is_running(harness.path)

    def test_a_live_daemon_is_not_evicted(self, tmp_path: Path) -> None:
        path = tmp_path / "control.sock"
        with Harness(path, lambda c, a: {}):
            second = ControlServer(lambda c, a: {}, path)
            with pytest.raises(OSError, match="already listening"):
                second.bind()

    def test_fileno_before_bind_is_an_error(self, tmp_path: Path) -> None:
        with pytest.raises(RuntimeError, match="not bound"):
            ControlServer(lambda c, a: {}, tmp_path / "s.sock").fileno()


class TestPidFile:
    def test_writes_and_removes_the_pid(self, tmp_path: Path) -> None:
        path = tmp_path / "daemon.pid"
        with PidFile(path):
            assert path.read_text().strip() == str(os.getpid())
            assert read_pid(path) == os.getpid()
        assert not path.exists()

    def test_a_missing_file_reads_as_no_daemon(self, tmp_path: Path) -> None:
        assert read_pid(tmp_path / "absent.pid") is None

    def test_a_corrupt_file_reads_as_no_daemon(self, tmp_path: Path) -> None:
        path = tmp_path / "daemon.pid"
        path.write_text("not a pid")
        assert read_pid(path) is None

    def test_a_dead_pid_reads_as_no_daemon(self, tmp_path: Path) -> None:
        path = tmp_path / "daemon.pid"
        # A PID that cannot exist: the kernel's maximum plus one.
        path.write_text("999999999")
        assert read_pid(path) is None

    def test_another_process_pid_file_is_left_alone(self, tmp_path: Path) -> None:
        path = tmp_path / "daemon.pid"
        with PidFile(path):
            path.write_text("999999999")
        assert path.exists(), "a pid file we no longer own must not be deleted"


def test_requests_do_not_block_forever_on_a_silent_peer(tmp_path: Path) -> None:
    path = tmp_path / "control.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(1)
    started = time.monotonic()
    try:
        with pytest.raises((TimeoutError, ProtocolError, RuntimeError, OSError)):
            request("ping", path=path, timeout=0.5)
    finally:
        listener.close()
    assert time.monotonic() - started < 5.0
