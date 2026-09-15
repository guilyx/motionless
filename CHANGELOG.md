# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
- The installer no longer prints the package manager's output. `apt-get update`
  alone is around sixty lines of repository chatter that says nothing about
  whether the install is going well. Long steps now show a single line with a
  spinner and an elapsed counter, replaced by a tick when they finish.
  Output is captured rather than discarded: a step that fails prints
  everything it produced, so nothing is hidden at the moment it matters.
  Without a terminal — piped, or in CI — it degrades to plain lines.

### Added
- `motionless sources` now names the source auto-detection would choose, and
  `--auto` prints just that name for scripts. The installer uses it to say,
  in the case that cannot be guessed from the overlay, that this machine has
  no sensor and what to do about it.

### Changed
- The installer's closing output explains what it found rather than listing
  commands: whether a motion sensor exists, the phone route with the exact
  commands when it does not, and the axis check to run parked when it does.
- `motion.source = "auto"` no longer falls back to the synthetic `demo` source
  when no accelerometer is present. It reports no source and draws nothing
  instead. Cues that disagree with the vehicle you are in are a worse sensory
  mismatch than no cues, so inventing motion to fill a missing sensor works
  against the point of the program. `demo` remains available explicitly.
  `doctor` now names the auto-detected source and explains what to do when
  there is none.

### Fixed
- `motionless config set` and `motionless reload` reported a failure for a
  change the daemon had in fact applied. Reload tore down and rebuilt every
  overlay window even when nothing about the geometry had changed; on a loaded
  machine that took longer than the control socket's three-second timeout, so
  the client gave up while the daemon carried on and succeeded. Reload now
  restyles the existing windows and only rebuilds when the set of monitors to
  cover changes, and commands that do real work are allowed longer.
- The overlay connected a pair of monitor-added/removed handlers on every
  start and never disconnected them, so each rebuild left another copy behind.
- `motionless stop` could hang and leave the daemon running. The quit was
  scheduled with `GLib.idle_add`, which runs at `PRIORITY_DEFAULT_IDLE` —
  below the overlay's `PRIORITY_DEFAULT` redraw timer. On a machine slow
  enough for drawing to saturate the main loop, the quit was starved
  indefinitely. It is now scheduled at `PRIORITY_HIGH`. Found when a loaded
  CI runner, which took eleven seconds just to open the overlay, hit exactly
  this.
- The installer asked pip to install `motionless-overlay` before checking
  whether it exists, so every run printed pipx's "Fatal error from pip
  prevented installation" before falling back to the repository and
  succeeding. It now asks PyPI first and goes straight to the repository,
  silently, while the package is unpublished.
- The installer looked frozen at `[sudo] password for ...` when run through
  `curl | bash`. It now explains that sudo will ask, and that nothing is
  echoed while you type, before the prompt appears rather than after.
- `apt-get` ran with `-qq` and inherited the curl pipe as its standard input,
  so a long silent stretch followed the password and anything that tried to
  prompt — `needrestart` on Ubuntu 22.04 and later opens a dialog mid-install
  — could block forever. Package managers now report progress, read from
  `/dev/null`, and run with `DEBIAN_FRONTEND=noninteractive` and
  `NEEDRESTART_MODE=a`.

### Documentation
- Install instructions no longer tell you to `pipx install motionless-overlay`.
  Nothing has been published to PyPI, so that command fails; installing from
  the repository is documented as the path that works today. The PyPI badge,
  which rendered an error for a package that does not exist, is removed until
  the first release, and CONTRIBUTING now documents the release steps.
- Added a Prior art section. An earlier draft claimed there was no Linux
  equivalent; [Mewtion](https://github.com/aayuxh-vim/Mewtion) exists and
  predates this, and the section now says where each is stronger.

## [0.1.0] - 2026-09-14

First release.

### Added
- Click-through, always-on-top overlay of drifting dots that react to vehicle
  acceleration, in the spirit of Apple's Vehicle Motion Cues.
- Display backends: `wlr-layer-shell` (Sway, Hyprland, KWin) and X11, with an
  automatic XWayland fallback for GNOME's Wayland session.
- Pluggable motion sources: `iio` (built-in accelerometer via sysfs), `udp`
  (stream readings from a phone) and `demo` (synthetic drive loop).
- Gravity-compensating motion pipeline with configurable smoothing, dead zone
  and axis mapping.
- `motionless` CLI: `run`, `start`, `stop`, `restart`, `toggle`, `show`,
  `hide`, `reload`, `status`, `preview`, `sources`, `doctor`, `config` and
  `service`.
- Unix-socket control protocol, so `motionless toggle` is instant and safe to
  bind to a hotkey.
- systemd user service integration and a one-line installer.

[Unreleased]: https://github.com/guilyx/motionless/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/guilyx/motionless/releases/tag/v0.1.0
