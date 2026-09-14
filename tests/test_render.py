"""Dot layout, displacement and the fade animation."""

from __future__ import annotations

import pytest

from motionless.config import OverlayConfig
from motionless.motion import STILL, MotionState
from motionless.overlay.render import (
    CueAnimator,
    Style,
    anchors,
    displacement,
    dots,
    smoothstep,
)


def style(**overrides: object) -> Style:
    config = OverlayConfig(**overrides)  # type: ignore[arg-type]
    return Style.from_config(config, clamp=6.0)


class TestSmoothstep:
    def test_endpoints(self) -> None:
        assert smoothstep(0.0) == 0.0
        assert smoothstep(1.0) == 1.0

    def test_midpoint(self) -> None:
        assert smoothstep(0.5) == pytest.approx(0.5)

    def test_clamps_outside_the_unit_range(self) -> None:
        assert smoothstep(-5.0) == 0.0
        assert smoothstep(5.0) == 1.0

    def test_is_monotonic(self) -> None:
        values = [smoothstep(i / 20) for i in range(21)]
        assert values == sorted(values)


class TestAnchors:
    def test_edges_layout_keeps_the_middle_clear(self) -> None:
        placed = anchors(1920, 1080, style())
        band = 0.16 * 1080
        # Nothing should be painted over the content in the centre.
        assert all(min(a.x, a.y, 1920 - a.x, 1080 - a.y) <= band + 1e-6 for a in placed)

    def test_grid_layout_covers_the_whole_screen(self) -> None:
        assert len(anchors(1920, 1080, style(layout="grid"))) > len(anchors(1920, 1080, style()))

    def test_weight_falls_off_away_from_the_edge(self) -> None:
        placed = sorted(anchors(1920, 1080, style()), key=lambda a: a.y)
        assert placed[0].weight > placed[-1].weight or placed[0].weight == pytest.approx(
            placed[-1].weight
        )
        outermost = max(placed, key=lambda a: a.weight)
        assert min(outermost.x, outermost.y, 1920 - outermost.x, 1080 - outermost.y) < 0.16 * 1080

    def test_grid_is_centred(self) -> None:
        placed = anchors(1000, 1000, style(layout="grid", spacing=100))
        left = min(a.x for a in placed)
        right = 1000 - max(a.x for a in placed)
        assert left == pytest.approx(right)

    def test_layout_is_symmetric_left_to_right(self) -> None:
        placed = anchors(1000, 800, style())
        xs = sorted({round(a.x, 6) for a in placed})
        mirrored = sorted({round(1000 - x, 6) for x in xs})
        assert xs == mirrored

    @pytest.mark.parametrize(("width", "height"), [(0, 100), (100, 0), (-10, -10)])
    def test_degenerate_screens_produce_nothing(self, width: int, height: int) -> None:
        assert anchors(width, height, style()) == []

    def test_a_tiny_screen_still_gets_a_dot(self) -> None:
        assert len(anchors(20, 20, style())) >= 1

    def test_spacing_controls_density(self) -> None:
        assert len(anchors(1920, 1080, style(spacing=80))) < len(
            anchors(1920, 1080, style(spacing=30))
        )


class TestDisplacement:
    def test_rightward_acceleration_slides_dots_left(self) -> None:
        # Inertia: the dots behave like something loose on the dashboard.
        dx, _ = displacement(MotionState(lateral=1.0), style(travel=1000))
        assert dx < 0

    def test_braking_slides_dots_up_the_screen(self) -> None:
        _, dy = displacement(MotionState(longitudinal=-1.0), style(travel=1000))
        assert dy < 0

    def test_forward_acceleration_slides_dots_down(self) -> None:
        _, dy = displacement(MotionState(longitudinal=1.0), style(travel=1000))
        assert dy > 0

    def test_stillness_means_no_displacement(self) -> None:
        assert displacement(STILL, style()) == (0.0, 0.0)

    def test_sensitivity_scales_the_offset(self) -> None:
        motion = MotionState(lateral=1.0)
        gentle, _ = displacement(motion, style(sensitivity=2.0, travel=1000))
        strong, _ = displacement(motion, style(sensitivity=20.0, travel=1000))
        assert abs(strong) > abs(gentle)

    def test_travel_caps_the_magnitude_without_changing_direction(self) -> None:
        motion = MotionState(lateral=50.0, longitudinal=50.0)
        dx, dy = displacement(motion, style(travel=30.0))
        assert (dx**2 + dy**2) ** 0.5 == pytest.approx(30.0)
        assert dx < 0 and dy > 0

    def test_zero_travel_pins_the_dots(self) -> None:
        # travel = 0 disables the cap, not the movement.
        dx, _ = displacement(MotionState(lateral=1.0), style(travel=0.0))
        assert dx != 0.0


class TestDots:
    def test_nothing_is_drawn_when_invisible(self) -> None:
        placed = anchors(800, 600, style())
        assert dots(placed, MotionState(lateral=1.0), style(), 0.0) == []

    def test_nothing_is_drawn_at_zero_opacity(self) -> None:
        placed = anchors(800, 600, style())
        assert dots(placed, MotionState(lateral=1.0), style(opacity=0.0), 1.0) == []

    def test_visibility_scales_alpha(self) -> None:
        placed = anchors(800, 600, style())
        full = dots(placed, STILL, style(), 1.0)
        half = dots(placed, STILL, style(), 0.5)
        assert max(d.alpha for d in half) == pytest.approx(max(d.alpha for d in full) / 2)

    def test_alpha_never_exceeds_the_configured_opacity(self) -> None:
        placed = anchors(1920, 1080, style(opacity=0.4))
        assert all(d.alpha <= 0.4 + 1e-9 for d in dots(placed, STILL, style(opacity=0.4), 1.0))

    def test_visibility_is_clamped(self) -> None:
        placed = anchors(800, 600, style())
        assert dots(placed, STILL, style(), 5.0) == dots(placed, STILL, style(), 1.0)
        assert dots(placed, STILL, style(), -5.0) == []

    def test_an_upward_jolt_grows_the_dots(self) -> None:
        placed = anchors(800, 600, style())
        calm = dots(placed, STILL, style(), 1.0)[0]
        bump = dots(placed, MotionState(vertical=4.0), style(), 1.0)[0]
        assert bump.radius > calm.radius

    def test_a_drop_shrinks_the_dots_without_inverting_them(self) -> None:
        placed = anchors(800, 600, style())
        drop = dots(placed, MotionState(vertical=-6.0), style(), 1.0)[0]
        assert 0.0 < drop.radius < style().radius

    def test_every_dot_moves_together(self) -> None:
        placed = anchors(800, 600, style())
        motion = MotionState(lateral=1.0, longitudinal=1.0)
        moved = dots(placed, motion, style(), 1.0)
        resting = dots(placed, STILL, style(), 1.0)
        pairs = zip(moved, resting, strict=True)
        offsets = {(round(m.x - r.x, 6), round(m.y - r.y, 6)) for m, r in pairs}
        # A rigid translation reads as "the world is moving"; per-dot parallax
        # would read as noise.
        assert len(offsets) == 1


class TestCueAnimator:
    def test_starts_hidden(self) -> None:
        assert CueAnimator().visibility == 0.0

    def test_always_on_starts_visible_and_stays_visible(self) -> None:
        animator = CueAnimator(always_on=True)
        assert animator.visibility == 1.0
        assert animator.update(STILL, 10.0) == 1.0

    def test_fades_in_over_the_configured_duration(self) -> None:
        animator = CueAnimator(fade_in=1.0, activation=0.1)
        moving = MotionState(lateral=2.0)
        assert animator.update(moving, 0.5) == pytest.approx(0.5)
        assert animator.update(moving, 0.5) == pytest.approx(1.0)

    def test_fades_out_over_the_configured_duration(self) -> None:
        animator = CueAnimator(fade_in=0.0, fade_out=2.0, activation=0.1)
        animator.update(MotionState(lateral=2.0), 0.1)
        assert animator.update(STILL, 1.0) == pytest.approx(0.5)
        assert animator.update(STILL, 1.0) == pytest.approx(0.0)

    def test_visibility_stays_within_bounds(self) -> None:
        animator = CueAnimator(fade_in=0.1, fade_out=0.1, activation=0.1)
        assert animator.update(MotionState(lateral=2.0), 100.0) == 1.0
        assert animator.update(STILL, 100.0) == 0.0

    def test_zero_durations_snap(self) -> None:
        animator = CueAnimator(fade_in=0.0, fade_out=0.0, activation=0.1)
        assert animator.update(MotionState(lateral=2.0), 0.0) == 1.0
        assert animator.update(STILL, 0.0) == 0.0

    def test_below_the_activation_threshold_nothing_wakes_up(self) -> None:
        animator = CueAnimator(activation=1.0, fade_in=0.1)
        assert animator.update(MotionState(lateral=0.5), 1.0) == 0.0

    def test_a_vertical_jolt_alone_wakes_the_cue(self) -> None:
        animator = CueAnimator(activation=0.5, fade_in=0.0)
        assert animator.update(MotionState(vertical=2.0), 0.1) == 1.0

    def test_negative_time_steps_are_ignored(self) -> None:
        animator = CueAnimator(fade_in=1.0, activation=0.1)
        animator.update(MotionState(lateral=2.0), 0.5)
        assert animator.update(MotionState(lateral=2.0), -5.0) == pytest.approx(0.5)


def test_a_colour_alpha_channel_scales_opacity() -> None:
    solid = Style.from_config(OverlayConfig(color="#ffffffff", opacity=0.5), clamp=6.0)
    faded = Style.from_config(OverlayConfig(color="#ffffff80", opacity=0.5), clamp=6.0)
    assert faded.opacity == pytest.approx(solid.opacity * (128 / 255), rel=1e-3)
