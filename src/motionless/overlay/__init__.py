"""Overlay rendering.

:mod:`motionless.overlay.render` and :mod:`motionless.overlay.backend` are
display-independent and safe to import anywhere.
:mod:`motionless.overlay.window` needs GTK and must only be imported after
:func:`motionless.overlay.backend.apply_plan`.
"""

from __future__ import annotations

from motionless.overlay import backend, render

__all__ = ["backend", "render"]
