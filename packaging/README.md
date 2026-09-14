# Packaging

## `motionless.desktop`

An autostart entry, as an alternative to the systemd user service for desktops
that do not reach `graphical-session.target`:

```bash
mkdir -p ~/.config/autostart
cp packaging/motionless.desktop ~/.config/autostart/
```

Prefer `motionless service install` where systemd is available: it restarts the
overlay if it crashes and stops it cleanly when the session ends.

## Distribution packages

motionless is published to PyPI as `motionless-overlay` and installs into an
isolated environment via `pipx`. If you package it for a distribution, note
that PyGObject and pycairo must come from the distribution rather than pip —
they are deliberately not declared as runtime dependencies for that reason.

Runtime requirements: Python 3.10+, PyGObject with the GTK 3 typelib, pycairo,
and optionally `gtk-layer-shell` for native Wayland overlays.
