# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
- `motion.source = "auto"` no longer falls back to the synthetic `demo` source
  when no accelerometer is present. It reports no source and draws nothing
  instead. Cues that disagree with the vehicle you are in are a worse sensory
  mismatch than no cues, so inventing motion to fill a missing sensor works
  against the point of the program. `demo` remains available explicitly.
  `doctor` now names the auto-detected source and explains what to do when
  there is none.

### Fixed
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
