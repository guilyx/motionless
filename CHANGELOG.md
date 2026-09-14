# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
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
