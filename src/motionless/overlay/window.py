"""The GTK overlay: transparent, always on top, and transparent to input.

This is the only module that touches GTK, and it is kept as thin as possible —
layout and animation live in :mod:`motionless.overlay.render`, which has no
display dependency and carries the tests.

Import this module only after :func:`motionless.overlay.backend.apply_plan`
has run: ``GDK_BACKEND`` is read when GDK is imported.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from typing import Any

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")

import cairo  # noqa: E402
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from motionless.config import Config  # noqa: E402
from motionless.motion import STILL, MotionState  # noqa: E402
from motionless.overlay import render  # noqa: E402

log = logging.getLogger(__name__)

#: Provides the motion state for the next frame. Called on the GTK main thread.
MotionProvider = Callable[[], MotionState]


def _load_layer_shell() -> Any | None:
    try:
        gi.require_version("GtkLayerShell", "0.1")
        from gi.repository import GtkLayerShell
    except (ValueError, ImportError):  # pragma: no cover - depends on the host
        return None
    return GtkLayerShell


class OverlayWindow(Gtk.Window):
    """A full-screen, click-through, transparent window for one monitor."""

    def __init__(self, monitor: Gdk.Monitor, layer_shell: Any | None) -> None:
        # An override-redirect (POPUP) window is the X11 way to stay above the
        # stack without the window manager reparenting or focusing us. Under
        # layer-shell we need a real toplevel for the protocol to attach to.
        window_type = Gtk.WindowType.TOPLEVEL if layer_shell else Gtk.WindowType.POPUP
        super().__init__(type=window_type)
        self._monitor = monitor
        self._layer_shell = layer_shell
        self._dots: list[render.Dot] = []
        self._rgb = (1.0, 1.0, 1.0)

        self.set_app_paintable(True)
        self.set_decorated(False)
        self.set_resizable(False)
        self.set_accept_focus(False)
        self.set_focus_on_map(False)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_type_hint(Gdk.WindowTypeHint.NOTIFICATION)

        screen = self.get_screen()
        visual = screen.get_rgba_visual()
        if visual is None:
            log.warning(
                "no RGBA visual available; the overlay needs a compositor to be transparent"
            )
        else:
            self.set_visual(visual)

        if layer_shell:
            self._setup_layer_shell(layer_shell, monitor)
        else:
            self._setup_x11(monitor)

        self.connect("realize", self._on_realize)
        self.connect("draw", self._on_draw)
        self.connect("screen-changed", self._on_screen_changed)

    # ------------------------------------------------------------- placement

    def _setup_layer_shell(self, layer_shell: Any, monitor: Gdk.Monitor) -> None:
        layer_shell.init_for_window(self)
        layer_shell.set_namespace(self, "motionless")
        layer_shell.set_layer(self, layer_shell.Layer.OVERLAY)
        layer_shell.set_monitor(self, monitor)
        for edge in ("LEFT", "RIGHT", "TOP", "BOTTOM"):
            layer_shell.set_anchor(self, getattr(layer_shell.Edge, edge), True)
        # -1 means "ignore exclusive zones": cover panels and docks too.
        layer_shell.set_exclusive_zone(self, -1)
        layer_shell.set_keyboard_mode(self, layer_shell.KeyboardMode.NONE)

    def _setup_x11(self, monitor: Gdk.Monitor) -> None:
        geometry = monitor.get_geometry()
        self.set_keep_above(True)
        self.set_default_size(geometry.width, geometry.height)
        self.resize(geometry.width, geometry.height)
        self.move(geometry.x, geometry.y)

    def _on_screen_changed(self, _widget: Gtk.Widget, _previous: Gdk.Screen | None) -> None:
        visual = self.get_screen().get_rgba_visual()
        if visual is not None:
            self.set_visual(visual)

    # ---------------------------------------------------------- click-through

    def _on_realize(self, _widget: Gtk.Widget) -> None:
        surface = self.get_window()
        if surface is None:  # pragma: no cover - realize always provides one
            return
        # An empty input region is what makes clicks land on whatever is
        # underneath. set_pass_through covers the same ground on newer GDK and
        # on Wayland, so we do both and let the backend use what it supports.
        surface.input_shape_combine_region(cairo.Region(), 0, 0)
        if hasattr(surface, "set_pass_through"):
            surface.set_pass_through(True)
        if not self._layer_shell:
            self.set_keep_above(True)
            surface.set_keep_above(True)

    # -------------------------------------------------------------- painting

    def set_dots(self, dots: list[render.Dot], rgb: tuple[float, float, float]) -> None:
        """Hand this window the circles for the next frame."""
        self._dots = dots
        self._rgb = rgb

    def _on_draw(self, _widget: Gtk.Widget, context: cairo.Context) -> bool:
        # SOURCE rather than OVER so each frame replaces the previous one
        # instead of accumulating alpha into an opaque smear.
        context.set_operator(cairo.OPERATOR_SOURCE)
        context.set_source_rgba(0.0, 0.0, 0.0, 0.0)
        context.paint()
        if not self._dots:
            return False

        context.set_operator(cairo.OPERATOR_OVER)
        red, green, blue = self._rgb
        for dot in self._dots:
            context.set_source_rgba(red, green, blue, dot.alpha)
            context.arc(dot.x, dot.y, dot.radius, 0.0, 2.0 * 3.141592653589793)
            context.fill()
        return False

    @property
    def logical_size(self) -> tuple[int, int]:
        geometry = self._monitor.get_geometry()
        return geometry.width, geometry.height


class Overlay:
    """Owns one window per monitor and drives the animation frame clock."""

    def __init__(self, config: Config, motion_provider: MotionProvider) -> None:
        self._config = config
        self._provider = motion_provider
        self._layer_shell = _load_layer_shell()
        self._windows: list[tuple[OverlayWindow, list[render.Anchor]]] = []
        self._timer: int | None = None
        self._last_tick: float = 0.0
        self._current_fps = 0
        self._visible = False
        self._style = render.Style.from_config(config.overlay, clamp=config.motion.clamp)
        self._animator = render.CueAnimator(
            activation=config.overlay.activation,
            fade_in=config.overlay.fade_in,
            fade_out=config.overlay.fade_out,
            always_on=config.overlay.always_on,
        )
        self._monitor_handlers: list[int] = []

    # ------------------------------------------------------------- lifecycle

    @property
    def visible(self) -> bool:
        return self._visible

    @property
    def monitor_count(self) -> int:
        return len(self._windows)

    def start(self, *, visible: bool = True) -> None:
        self._build_windows()
        display = Gdk.Display.get_default()
        if display is not None:
            self._monitor_handlers = [
                display.connect("monitor-added", self._on_monitors_changed),
                display.connect("monitor-removed", self._on_monitors_changed),
            ]
        if visible:
            self.show()

    def stop(self) -> None:
        self._stop_timer()
        display = Gdk.Display.get_default()
        if display is not None:
            for handler in self._monitor_handlers:
                display.disconnect(handler)
        self._monitor_handlers = []
        for window, _ in self._windows:
            window.destroy()
        self._windows = []
        self._visible = False

    def show(self) -> None:
        if self._visible:
            return
        self._visible = True
        for window, _ in self._windows:
            window.show_all()
            # Re-assert stacking after map; some window managers lower
            # override-redirect windows when another client goes fullscreen.
            if not self._layer_shell:
                window.set_keep_above(True)
        self._last_tick = GLib.get_monotonic_time() / 1e6
        self._start_timer(self._config.overlay.fps)

    def hide(self) -> None:
        if not self._visible:
            return
        self._visible = False
        self._stop_timer()
        for window, _ in self._windows:
            window.hide()

    def toggle(self) -> bool:
        if self._visible:
            self.hide()
        else:
            self.show()
        return self._visible

    def reload(self, config: Config) -> None:
        """Apply a new configuration.

        Only a change of *which monitors* to cover needs the windows torn down
        and rebuilt. Everything else — colour, spacing, sensitivity, frame rate
        — is a matter of restyling the windows that already exist. Rebuilding
        regardless made reload slow enough on a loaded machine to exceed the
        control socket's timeout, so `motionless config set` reported a failure
        for a change the daemon had in fact applied.
        """
        previous = self._config
        self._config = config
        self._style = render.Style.from_config(config.overlay, clamp=config.motion.clamp)
        self._animator = render.CueAnimator(
            activation=config.overlay.activation,
            fade_in=config.overlay.fade_in,
            fade_out=config.overlay.fade_out,
            always_on=config.overlay.always_on,
        )

        if config.overlay.monitors != previous.overlay.monitors:
            was_visible = self._visible
            self.stop()
            self.start(visible=was_visible)
            return

        # Same windows, new resting positions.
        self._windows = [
            (window, render.anchors(*window.logical_size, self._style))
            for window, _ in self._windows
        ]
        if self._visible:
            self._start_timer(config.overlay.fps)

    # --------------------------------------------------------------- windows

    def _monitors(self) -> Iterable[Gdk.Monitor]:
        display = Gdk.Display.get_default()
        if display is None:
            raise RuntimeError("no display available; cannot open the overlay")
        if self._config.overlay.monitors == "primary":
            primary = display.get_primary_monitor() or display.get_monitor(0)
            return [primary] if primary is not None else []
        return [
            monitor
            for index in range(display.get_n_monitors())
            if (monitor := display.get_monitor(index)) is not None
        ]

    def _build_windows(self) -> None:
        self._windows = []
        for monitor in self._monitors():
            window = OverlayWindow(monitor, self._layer_shell)
            geometry = monitor.get_geometry()
            anchors = render.anchors(geometry.width, geometry.height, self._style)
            self._windows.append((window, anchors))
        log.info("overlay opened on %d monitor(s)", len(self._windows))

    def _on_monitors_changed(self, *_args: object) -> None:
        log.info("monitor layout changed; rebuilding overlay")
        was_visible = self._visible
        self.stop()
        self._build_windows()
        if was_visible:
            self.show()

    # ----------------------------------------------------------- frame clock

    def _start_timer(self, fps: int) -> None:
        if self._timer is not None and self._current_fps == fps:
            return
        self._stop_timer()
        self._current_fps = fps
        self._timer = GLib.timeout_add(max(1, 1000 // fps), self._on_frame)

    def _stop_timer(self) -> None:
        if self._timer is not None:
            GLib.source_remove(self._timer)
            self._timer = None
            self._current_fps = 0

    def _on_frame(self) -> bool:
        if not self._visible:
            self._timer = None
            self._current_fps = 0
            return False

        now = GLib.get_monotonic_time() / 1e6
        dt = max(0.0, now - self._last_tick)
        self._last_tick = now

        motion = self._provider() or STILL
        visibility = self._animator.update(motion, dt)

        for window, anchors in self._windows:
            window.set_dots(render.dots(anchors, motion, self._style, visibility), self._style.rgb)
            window.queue_draw()

        # Drop to the idle rate once the cue has fully faded: an overlay that
        # redraws 60 times a second to paint nothing is a battery bug.
        target = (
            self._config.overlay.idle_fps
            if visibility <= 0.0 and not self._animator.is_active(motion)
            else self._config.overlay.fps
        )
        if target != self._current_fps:
            GLib.idle_add(self._start_timer, target)
            self._timer = None
            self._current_fps = 0
            return False
        return True
