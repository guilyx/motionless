"""Decide which display backend the overlay should use — before GTK loads.

A motion-cue overlay needs three things from the compositor: stay above other
windows, cover the whole screen, and never take input. Which protocol can
deliver that depends on the session:

``wlr-layer-shell``
    The right answer. Supported by wlroots compositors (Sway, Hyprland,
    river), KDE Plasma's KWin, and anything else implementing the protocol.

``X11``
    Also the right answer, via an override-redirect window with an empty
    input shape. Works on an Xorg session and, through XWayland, on Wayland
    sessions that lack layer-shell.

GNOME's Mutter is the awkward case: it is Wayland-only in its default session
and has declined to implement wlr-layer-shell, so there is no native way for
an ordinary client to place an always-on-top overlay. We fall back to
XWayland, which does work, with the caveats listed in
:data:`XWAYLAND_CAVEATS`.

Nothing here imports ``gi``: the choice has to be made *before* GTK is
imported, because ``GDK_BACKEND`` is read at import time.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from pathlib import Path

#: Typelib that indicates gtk-layer-shell is installed.
LAYER_SHELL_TYPELIB = "GtkLayerShell-0.1.typelib"

#: Standard search locations for GObject-Introspection typelibs.
TYPELIB_DIRS = (
    "/usr/lib/x86_64-linux-gnu/girepository-1.0",
    "/usr/lib/aarch64-linux-gnu/girepository-1.0",
    "/usr/lib64/girepository-1.0",
    "/usr/lib/girepository-1.0",
    "/usr/local/lib/girepository-1.0",
)

XWAYLAND_CAVEATS = (
    "running through XWayland: cues stay above normal windows, but a "
    "fullscreen Wayland window (video, games) may cover them"
)


def layer_shell_available(env: Mapping[str, str] | None = None) -> bool:
    """Whether the gtk-layer-shell typelib is installed."""
    environ = os.environ if env is None else env
    search = [
        *(p for p in environ.get("GI_TYPELIB_PATH", "").split(os.pathsep) if p),
        *TYPELIB_DIRS,
    ]
    return any((Path(directory) / LAYER_SHELL_TYPELIB).exists() for directory in search)


def session_type(env: Mapping[str, str] | None = None) -> str:
    """``"wayland"``, ``"x11"`` or ``"none"`` for the current session."""
    environ = os.environ if env is None else env
    declared = environ.get("XDG_SESSION_TYPE", "").strip().lower()
    if declared in ("wayland", "x11"):
        return declared
    if environ.get("WAYLAND_DISPLAY"):
        return "wayland"
    if environ.get("DISPLAY"):
        return "x11"
    return "none"


def desktop(env: Mapping[str, str] | None = None) -> str:
    environ = os.environ if env is None else env
    return environ.get("XDG_CURRENT_DESKTOP", "").strip()


@dataclass(frozen=True)
class BackendPlan:
    """The chosen backend, plus what to tell the user about it."""

    #: Value to force into ``GDK_BACKEND``, or ``None`` to let GTK decide.
    gdk_backend: str | None
    #: ``layer-shell``, ``x11``, ``xwayland`` or ``none``.
    mode: str
    #: Human-readable explanation, shown by ``motionless doctor``.
    reason: str
    #: Non-fatal limitations of this mode.
    caveats: tuple[str, ...] = ()

    @property
    def usable(self) -> bool:
        return self.mode != "none"


def plan_backend(env: Mapping[str, str] | None = None) -> BackendPlan:
    """Choose the display backend for this session."""
    environ = os.environ if env is None else env
    forced = environ.get("GDK_BACKEND", "").strip().lower()
    session = session_type(environ)
    has_x11 = bool(environ.get("DISPLAY"))

    if forced:
        mode = "layer-shell" if forced == "wayland" and layer_shell_available(environ) else forced
        return BackendPlan(None, mode, f"GDK_BACKEND is set to {forced!r}; respecting it")

    if session == "wayland":
        if layer_shell_available(environ):
            return BackendPlan(
                "wayland",
                "layer-shell",
                f"Wayland session with gtk-layer-shell available ({desktop(environ) or 'unknown'})",
            )
        if has_x11:
            return BackendPlan(
                "x11",
                "xwayland",
                (
                    f"Wayland session without gtk-layer-shell "
                    f"({desktop(environ) or 'unknown'}); using XWayland"
                ),
                (XWAYLAND_CAVEATS,),
            )
        return BackendPlan(
            None,
            "none",
            "Wayland session with neither gtk-layer-shell nor XWayland; "
            "install gtk-layer-shell or enable XWayland",
        )

    if session == "x11" or has_x11:
        return BackendPlan("x11", "x11", "Xorg session")

    return BackendPlan(
        None, "none", "no graphical session detected (no DISPLAY or WAYLAND_DISPLAY)"
    )


def apply_plan(plan: BackendPlan, env: MutableMapping[str, str] | None = None) -> None:
    """Export ``GDK_BACKEND``. Must run before ``gi.repository.Gtk`` is imported."""
    environ = os.environ if env is None else env
    if plan.gdk_backend:
        environ["GDK_BACKEND"] = plan.gdk_backend
