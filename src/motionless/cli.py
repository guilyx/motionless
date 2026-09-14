"""Command line interface.

Design goals, in order: ``motionless toggle`` must be instant and safe to bind
to a hotkey; every command must work without a daemon already running; and
anything that can go wrong should say what to do about it.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from motionless import __version__
from motionless.config import Config, ConfigError
from motionless.doctor import Level, run_checks, worst
from motionless.ipc import DaemonUnavailableError, is_running, read_pid, request
from motionless.motion import MotionState
from motionless.paths import config_file, ensure_dir, log_file, state_dir
from motionless.service import ServiceError, render_unit
from motionless.service import install as service_install
from motionless.service import status as service_status
from motionless.service import uninstall as service_uninstall
from motionless.sources import UnknownSourceError, probe_all, source_names

#: How long ``start`` waits for the daemon to answer before giving up.
START_TIMEOUT_SECONDS = 10.0

_MARKERS = {Level.OK: "✔", Level.WARN: "!", Level.FAIL: "✘"}
_COLOURS = {Level.OK: "\033[32m", Level.WARN: "\033[33m", Level.FAIL: "\033[31m"}


def _use_colour(stream: Any = None) -> bool:
    target = stream or sys.stdout
    if os.environ.get("NO_COLOR") is not None:
        return False
    return bool(getattr(target, "isatty", lambda: False)())


def _paint(text: str, level: Level) -> str:
    if not _use_colour():
        return text
    return f"{_COLOURS[level]}{text}\033[0m"


def _fail(message: str, hint: str = "") -> int:
    print(f"motionless: {message}", file=sys.stderr)
    if hint:
        print(f"  hint: {hint}", file=sys.stderr)
    return 1


# --------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="motionless",
        description=(
            "Vehicle motion cues for Linux: a click-through overlay of drifting "
            "dots that react to your vehicle's movement, so screen use in a car, "
            "train or boat is easier on the stomach."
        ),
        epilog="Run `motionless doctor` if anything looks wrong.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-V", "--version", action="version", version=f"motionless {__version__}")
    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        metavar="PATH",
        help=f"configuration file to use (default: {config_file()})",
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    run = sub.add_parser("run", help="run the overlay in the foreground")
    run.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    run.add_argument(
        "-s", "--source", metavar="NAME", help=f"motion source ({', '.join(source_names())}, auto)"
    )
    run.add_argument("--hidden", action="store_true", help="start with cues hidden")
    run.add_argument(
        "--always-on", action="store_true", help="show cues even when stationary (for tuning)"
    )

    start = sub.add_parser("start", help="start the overlay in the background")
    start.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    start.add_argument("-s", "--source", metavar="NAME", help="motion source to use")
    start.add_argument("--hidden", action="store_true", help="start with cues hidden")
    start.add_argument(
        "--always-on", action="store_true", help="show cues even when stationary (for tuning)"
    )

    sub.add_parser("stop", help="stop the background overlay")
    restart = sub.add_parser("restart", help="stop then start the background overlay")
    restart.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    restart.add_argument("-s", "--source", metavar="NAME", help="motion source to use")
    restart.add_argument("--hidden", action="store_true", help="start with cues hidden")
    restart.add_argument("--always-on", action="store_true", help="show cues even when stationary")

    toggle = sub.add_parser("toggle", help="show or hide the cues (bind this to a hotkey)")
    toggle.add_argument(
        "--no-start",
        action="store_true",
        help="fail instead of starting the daemon when it is not running",
    )
    sub.add_parser("show", help="show the cues")
    sub.add_parser("hide", help="hide the cues")
    sub.add_parser("reload", help="re-read the configuration file")

    status = sub.add_parser("status", help="report what the daemon is doing")
    status.add_argument("--json", action="store_true", help="machine-readable output")

    preview = sub.add_parser(
        "preview", help="render the cue to a PNG for tuning (no display needed)"
    )
    preview.add_argument(
        "-o", "--output", type=Path, default=Path("motionless-preview.png"), metavar="PATH"
    )
    preview.add_argument("--width", type=int, default=1280)
    preview.add_argument("--height", type=int, default=800)
    preview.add_argument(
        "--lateral", type=float, default=2.2, metavar="M_S2", help="rightward acceleration"
    )
    preview.add_argument(
        "--longitudinal", type=float, default=-1.4, metavar="M_S2", help="forward acceleration"
    )
    preview.add_argument("--vertical", type=float, default=0.0, metavar="M_S2")
    preview.add_argument(
        "--background",
        default="#1b1d23",
        help="backdrop colour, or 'none' for a transparent PNG",
    )

    sub.add_parser("sources", help="list motion sources and whether they are usable")
    doctor = sub.add_parser("doctor", help="check this machine can run the overlay")
    doctor.add_argument("--json", action="store_true", help="machine-readable output")

    config_parser = sub.add_parser("config", help="inspect or change settings")
    config_sub = config_parser.add_subparsers(dest="config_command", metavar="<action>")
    config_sub.add_parser("path", help="print the configuration file path")
    config_sub.add_parser("show", help="print the effective configuration as TOML")
    config_sub.add_parser("keys", help="list every setting name")
    config_sub.add_parser("init", help="write the default configuration file")
    config_get = config_sub.add_parser("get", help="print one setting")
    config_get.add_argument("key", help="e.g. overlay.opacity")
    config_set = config_sub.add_parser("set", help="change one setting and save")
    config_set.add_argument("key", help="e.g. overlay.opacity")
    config_set.add_argument("value")
    config_sub.add_parser("edit", help="open the configuration in $EDITOR")

    service_parser = sub.add_parser("service", help="manage the systemd user service")
    service_sub = service_parser.add_subparsers(dest="service_command", metavar="<action>")
    install = service_sub.add_parser("install", help="install and enable the user service")
    install.add_argument("--now", action="store_true", help="also start it immediately")
    install.add_argument(
        "--no-enable", action="store_true", help="install the unit without enabling it"
    )
    service_sub.add_parser("uninstall", help="stop, disable and remove the user service")
    service_sub.add_parser("status", help="report the user service state")
    service_sub.add_parser("print", help="print the unit file without installing it")

    return parser


# ------------------------------------------------------------------- commands


def _load_config(args: argparse.Namespace) -> Config:
    config = Config.load(args.config)
    source = getattr(args, "source", None)
    if source:
        config.motion.source = source
        if source != "auto":
            from motionless.sources import get_source_class

            get_source_class(source)
    if getattr(args, "hidden", False):
        config.start_hidden = True
    if getattr(args, "always_on", False):
        config.overlay.always_on = True
    return config.validate()


def cmd_run(args: argparse.Namespace) -> int:
    from motionless.daemon import Daemon, configure_logging

    configure_logging(args.verbose)
    config = _load_config(args)
    return Daemon(config, config_path=args.config).run()


def _child_command(args: argparse.Namespace) -> list[str]:
    command = [sys.executable, "-m", "motionless"]
    if args.config:
        command += ["--config", str(args.config)]
    command.append("run")
    if getattr(args, "verbose", False):
        command.append("--verbose")
    if getattr(args, "source", None):
        command += ["--source", args.source]
    if getattr(args, "hidden", False):
        command.append("--hidden")
    if getattr(args, "always_on", False):
        command.append("--always-on")
    return command


def cmd_start(args: argparse.Namespace) -> int:
    if is_running():
        print("motionless is already running")
        return 0
    # Validate before detaching, so configuration mistakes surface here rather
    # than in a log file the user has not thought to look at yet.
    _load_config(args)

    ensure_dir(state_dir())
    command = _child_command(args)
    with log_file().open("ab") as sink:
        subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=sink,
            stderr=sink,
            start_new_session=True,
            close_fds=True,
        )

    deadline = time.monotonic() + START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if is_running():
            print(f"motionless started (pid {read_pid()})")
            return 0
        time.sleep(0.1)
    return _fail(
        "the daemon did not come up in time",
        f"check {log_file()}, or run `{shlex.join(command)}` to see the error",
    )


def cmd_stop(_args: argparse.Namespace) -> int:
    try:
        request("quit")
    except DaemonUnavailableError:
        print("motionless is not running")
        return 0
    print("motionless stopped")
    return 0


def cmd_restart(args: argparse.Namespace) -> int:
    cmd_stop(args)
    for _ in range(50):
        if not is_running():
            break
        time.sleep(0.1)
    return cmd_start(args)


def cmd_toggle(args: argparse.Namespace) -> int:
    try:
        data = request("toggle")
    except DaemonUnavailableError:
        if args.no_start:
            return _fail("motionless is not running", "start it with `motionless start`")
        # Toggling a stopped overlay most plausibly means "turn it on".
        result = cmd_start(args)
        return result
    print("cues shown" if data.get("visible") else "cues hidden")
    return 0


def _simple(command: str, message: str) -> int:
    try:
        request(command)
    except DaemonUnavailableError:
        return _fail("motionless is not running", "start it with `motionless start`")
    print(message)
    return 0


def cmd_show(_args: argparse.Namespace) -> int:
    return _simple("show", "cues shown")


def cmd_hide(_args: argparse.Namespace) -> int:
    return _simple("hide", "cues hidden")


def cmd_reload(_args: argparse.Namespace) -> int:
    return _simple("reload", "configuration reloaded")


def cmd_status(args: argparse.Namespace) -> int:
    try:
        data = request("status")
    except DaemonUnavailableError:
        if args.json:
            print(json.dumps({"running": False}, indent=2))
        else:
            print("motionless is not running")
        return 1
    if args.json:
        print(json.dumps({"running": True, **data}, indent=2))
        return 0

    motion = data.get("motion", {})
    rows = [
        ("state", "cues visible" if data.get("visible") else "running, cues hidden"),
        ("pid", data.get("pid")),
        ("uptime", f"{data.get('uptime', 0)}s"),
        ("backend", f"{data.get('backend')} ({data.get('backend_reason')})"),
        ("monitors", data.get("monitors")),
        ("source", data.get("source")),
        ("samples", data.get("samples")),
        (
            "motion",
            f"lat {motion.get('lateral', 0):+.2f}  "
            f"long {motion.get('longitudinal', 0):+.2f}  "
            f"vert {motion.get('vertical', 0):+.2f} m/s²",
        ),
    ]
    width = max(len(str(name)) for name, _ in rows)
    for name, value in rows:
        print(f"{name:<{width}}  {value}")
    if data.get("source_error"):
        print(_paint(f"{'source error':<{width}}  {data['source_error']}", Level.FAIL))
    return 0


def cmd_preview(args: argparse.Namespace) -> int:
    from motionless.preview import render_png

    config = _load_config(args)
    motion = MotionState(args.lateral, args.longitudinal, args.vertical)
    background = None if args.background.lower() in ("none", "transparent") else args.background
    written = render_png(
        args.output,
        config,
        motion,
        width=args.width,
        height=args.height,
        background=background,
    )
    print(f"wrote {written}")
    return 0


def cmd_sources(_args: argparse.Namespace) -> int:
    from motionless.sources import SOURCES

    for source in SOURCES:
        availability = probe_all()[source.name]
        level = Level.OK if availability.available else Level.WARN
        print(f"{_paint(_MARKERS[level], level)} {source.name:<6} {source.description}")
        if availability.detail:
            print(f"    {availability.detail}")
    print("\nSelect one with `motionless config set motion.source <name>`.")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    checks = run_checks(args.config)
    if args.json:
        print(
            json.dumps(
                [
                    {"name": c.name, "level": c.level.value, "detail": c.detail, "hint": c.hint}
                    for c in checks
                ],
                indent=2,
            )
        )
    else:
        width = max(len(check.name) for check in checks)
        for check in checks:
            marker = _paint(_MARKERS[check.level], check.level)
            print(f"{marker} {check.name:<{width}}  {check.detail}")
            if check.hint:
                print(f"  {'':<{width}}  → {check.hint}")
    overall = worst(checks)
    if overall is Level.FAIL:
        return 1
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    action = args.config_command or "show"
    target = args.config or config_file()

    if action == "path":
        print(target)
        return 0
    if action == "keys":
        for key in Config().setting_names():
            print(key)
        return 0

    config = Config.load(args.config)
    if action == "show":
        print(config.to_toml(), end="")
        return 0
    if action == "init":
        if target.exists():
            return _fail(f"{target} already exists", "edit it with `motionless config edit`")
        written = config.save(target)
        print(f"wrote {written}")
        return 0
    if action == "get":
        print(config.get(args.key))
        return 0
    if action == "set":
        value = config.set(args.key, args.value)
        config.save(target)
        print(f"{args.key} = {value!r}  ({target})")
        if is_running():
            try:
                request("reload")
                print("running daemon reloaded")
            except (DaemonUnavailableError, RuntimeError) as exc:
                print(f"could not reload the running daemon: {exc}", file=sys.stderr)
        return 0
    if action == "edit":
        if not target.exists():
            config.save(target)
        editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "nano"
        subprocess.run([*shlex.split(editor), str(target)], check=False)
        Config.load(target, missing_ok=False)
        if is_running():
            request("reload")
            print("running daemon reloaded")
        return 0
    return _fail(f"unknown config action {action!r}")


def cmd_service(args: argparse.Namespace) -> int:
    action = args.service_command or "status"
    if action == "print":
        print(render_unit(), end="")
        return 0
    if action == "install":
        path = service_install(enable=not args.no_enable, start=args.now)
        print(f"installed {path}")
        if not args.no_enable:
            print("enabled; cues will start with your graphical session")
        if args.now:
            print("started")
        return 0
    if action == "uninstall":
        print("removed the user service" if service_uninstall() else "no user service installed")
        return 0
    if action == "status":
        status = service_status()
        print(f"unit      {status.unit_path}")
        print(f"installed {'yes' if status.installed else 'no'}")
        print(f"enabled   {'yes' if status.enabled else 'no'}")
        print(f"active    {'yes' if status.active else 'no'}")
        return 0
    return _fail(f"unknown service action {action!r}")


_HANDLERS = {
    "run": cmd_run,
    "start": cmd_start,
    "stop": cmd_stop,
    "restart": cmd_restart,
    "toggle": cmd_toggle,
    "show": cmd_show,
    "hide": cmd_hide,
    "reload": cmd_reload,
    "status": cmd_status,
    "preview": cmd_preview,
    "sources": cmd_sources,
    "doctor": cmd_doctor,
    "config": cmd_config,
    "service": cmd_service,
}


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0

    handler = _HANDLERS[args.command]
    try:
        return handler(args)
    except KeyboardInterrupt:
        return 130
    except (ConfigError, UnknownSourceError) as exc:
        return _fail(str(exc), "see `motionless config keys` for valid settings")
    except ServiceError as exc:
        return _fail(str(exc))
    except DaemonUnavailableError as exc:
        return _fail(str(exc), "start it with `motionless start`")
    except (OSError, RuntimeError) as exc:
        return _fail(str(exc))
