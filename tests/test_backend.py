"""Choosing a display backend before GTK is imported."""

from __future__ import annotations

from pathlib import Path

import pytest

from motionless.overlay.backend import (
    LAYER_SHELL_TYPELIB,
    BackendPlan,
    apply_plan,
    desktop,
    layer_shell_available,
    plan_backend,
    session_type,
)

X11 = {"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"}
GNOME_WAYLAND = {
    "XDG_SESSION_TYPE": "wayland",
    "WAYLAND_DISPLAY": "wayland-0",
    "DISPLAY": ":0",
    "XDG_CURRENT_DESKTOP": "ubuntu:GNOME",
}
SWAY = {
    "XDG_SESSION_TYPE": "wayland",
    "WAYLAND_DISPLAY": "wayland-1",
    "XDG_CURRENT_DESKTOP": "sway",
}


class TestSessionType:
    @pytest.mark.parametrize(
        ("env", "expected"),
        [
            (X11, "x11"),
            (GNOME_WAYLAND, "wayland"),
            ({"WAYLAND_DISPLAY": "wayland-0"}, "wayland"),
            ({"DISPLAY": ":0"}, "x11"),
            ({}, "none"),
            ({"XDG_SESSION_TYPE": "tty"}, "none"),
            ({"XDG_SESSION_TYPE": "  X11  "}, "x11"),
        ],
    )
    def test_detection(self, env: dict[str, str], expected: str) -> None:
        assert session_type(env) == expected

    def test_desktop_name(self) -> None:
        assert desktop(GNOME_WAYLAND) == "ubuntu:GNOME"
        assert desktop({}) == ""


class TestLayerShellDetection:
    def test_found_on_the_introspection_path(self, tmp_path: Path) -> None:
        (tmp_path / LAYER_SHELL_TYPELIB).touch()
        assert layer_shell_available({"GI_TYPELIB_PATH": str(tmp_path)})

    def test_absent_when_not_installed(self, tmp_path: Path) -> None:
        assert not layer_shell_available({"GI_TYPELIB_PATH": str(tmp_path)})

    def test_handles_a_multi_entry_path(self, tmp_path: Path) -> None:
        (tmp_path / LAYER_SHELL_TYPELIB).touch()
        env = {"GI_TYPELIB_PATH": f"/nowhere:{tmp_path}"}
        assert layer_shell_available(env)


class TestPlanBackend:
    def test_xorg_uses_x11_with_no_caveats(self) -> None:
        plan = plan_backend(X11)
        assert (plan.gdk_backend, plan.mode, plan.caveats) == ("x11", "x11", ())
        assert plan.usable

    def test_wayland_with_layer_shell_stays_native(self, tmp_path: Path) -> None:
        (tmp_path / LAYER_SHELL_TYPELIB).touch()
        plan = plan_backend({**SWAY, "GI_TYPELIB_PATH": str(tmp_path)})
        assert plan.mode == "layer-shell"
        assert plan.gdk_backend == "wayland"
        assert plan.caveats == ()

    def test_gnome_wayland_falls_back_to_xwayland_with_a_caveat(self, tmp_path: Path) -> None:
        plan = plan_backend({**GNOME_WAYLAND, "GI_TYPELIB_PATH": str(tmp_path)})
        assert plan.mode == "xwayland"
        assert plan.gdk_backend == "x11"
        assert plan.caveats, "the fullscreen-window limitation must be surfaced"
        assert plan.usable

    def test_wayland_without_layer_shell_or_xwayland_is_unusable(self, tmp_path: Path) -> None:
        plan = plan_backend({**SWAY, "GI_TYPELIB_PATH": str(tmp_path)})
        assert plan.mode == "none"
        assert not plan.usable
        assert "gtk-layer-shell" in plan.reason

    def test_headless_is_unusable_and_says_why(self) -> None:
        plan = plan_backend({})
        assert not plan.usable
        assert "no graphical session" in plan.reason

    def test_an_explicit_gdk_backend_is_respected(self, tmp_path: Path) -> None:
        plan = plan_backend({**GNOME_WAYLAND, "GDK_BACKEND": "wayland"})
        assert plan.gdk_backend is None, "we must not override the user's choice"
        assert "respecting it" in plan.reason

    def test_an_explicit_wayland_backend_with_layer_shell_is_reported_as_such(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / LAYER_SHELL_TYPELIB).touch()
        plan = plan_backend({**SWAY, "GDK_BACKEND": "wayland", "GI_TYPELIB_PATH": str(tmp_path)})
        assert plan.mode == "layer-shell"


class TestApplyPlan:
    def test_exports_the_chosen_backend(self) -> None:
        env: dict[str, str] = {}
        apply_plan(BackendPlan("x11", "x11", "because"), env)
        assert env["GDK_BACKEND"] == "x11"

    def test_leaves_the_environment_alone_when_there_is_no_choice(self) -> None:
        env: dict[str, str] = {}
        apply_plan(BackendPlan(None, "layer-shell", "because"), env)
        assert env == {}
