"""The motion pipeline: gravity removal, smoothing, axis mapping, dead zone."""

from __future__ import annotations

import math

import pytest

from motionless.motion import (
    GRAVITY,
    STILL,
    AxisMap,
    MotionProcessor,
    MotionSample,
    MotionState,
    _clamp,
    _dead_zone,
    _ema_alpha,
)


def feed(
    processor: MotionProcessor,
    samples: list[tuple[float, float, float]],
    dt: float = 0.02,
    start: float = 0.0,
) -> MotionState:
    state = STILL
    for index, (x, y, z) in enumerate(samples):
        state = processor.update(MotionSample(x, y, z, start + index * dt))
    return state


def settle(processor: MotionProcessor, seconds: float = 0.5, dt: float = 0.02) -> float:
    """Let the processor learn gravity while the device rests flat."""
    count = int(seconds / dt)
    feed(processor, [(0.0, 0.0, GRAVITY)] * count, dt)
    return count * dt


class TestEmaAlpha:
    def test_zero_tau_follows_input_exactly(self) -> None:
        assert _ema_alpha(0.1, 0.0) == 1.0

    def test_one_time_constant_covers_63_percent(self) -> None:
        assert _ema_alpha(2.0, 2.0) == pytest.approx(1 - math.exp(-1), abs=1e-9)

    def test_no_elapsed_time_means_no_movement(self) -> None:
        assert _ema_alpha(0.0, 1.0) == 0.0

    def test_alpha_grows_with_elapsed_time(self) -> None:
        assert _ema_alpha(0.5, 1.0) < _ema_alpha(1.5, 1.0) < 1.0


class TestDeadZone:
    def test_suppresses_small_values(self) -> None:
        assert _dead_zone(0.1, 0.2) == 0.0
        assert _dead_zone(-0.1, 0.2) == 0.0

    def test_is_continuous_at_the_boundary(self) -> None:
        # Just outside the dead zone must be near zero, not a sudden step.
        assert _dead_zone(0.2001, 0.2) == pytest.approx(0.0001, abs=1e-9)

    def test_preserves_sign(self) -> None:
        assert _dead_zone(-1.0, 0.2) == pytest.approx(-0.8)

    def test_disabled_by_zero_threshold(self) -> None:
        assert _dead_zone(0.05, 0.0) == 0.05


def test_clamp_bounds_both_directions() -> None:
    assert _clamp(10.0, 6.0) == 6.0
    assert _clamp(-10.0, 6.0) == -6.0
    assert _clamp(1.0, 6.0) == 1.0


class TestAxisMap:
    def test_identity_by_default(self) -> None:
        assert AxisMap().apply(1.0, 2.0, 3.0) == (1.0, 2.0, 3.0)

    def test_reorders_axes(self) -> None:
        mapping = AxisMap(lateral="y", longitudinal="z", vertical="x")
        assert mapping.apply(1.0, 2.0, 3.0) == (2.0, 3.0, 1.0)

    def test_inverts_with_a_minus_prefix(self) -> None:
        assert AxisMap(lateral="-x").apply(1.0, 2.0, 3.0) == (-1.0, 2.0, 3.0)

    def test_accepts_an_explicit_plus(self) -> None:
        assert AxisMap(lateral="+x").apply(1.0, 2.0, 3.0) == (1.0, 2.0, 3.0)

    def test_is_case_and_space_insensitive(self) -> None:
        assert AxisMap(lateral=" -Y ").apply(1.0, 2.0, 3.0) == (-2.0, 2.0, 3.0)

    @pytest.mark.parametrize("spec", ["w", "", "xy", "--x", "1"])
    def test_rejects_nonsense_at_construction(self, spec: str) -> None:
        with pytest.raises(ValueError, match="invalid axis"):
            AxisMap(lateral=spec)


class TestMotionState:
    def test_magnitude_ignores_vertical(self) -> None:
        assert MotionState(3.0, 4.0, 99.0).magnitude == pytest.approx(5.0)

    def test_still_state_reports_itself_as_still(self) -> None:
        assert STILL.is_still
        assert not MotionState(vertical=0.1).is_still


class TestMotionProcessor:
    def test_gravity_is_removed_from_the_first_sample(self) -> None:
        processor = MotionProcessor()
        state = processor.update(MotionSample(0.0, 0.0, GRAVITY, 0.0))
        # Seeding on the first sample avoids reporting a 1g "acceleration" at
        # startup, which would flash the cue every time the daemon restarts.
        assert state.is_still
        assert processor.has_gravity_estimate

    def test_an_offset_present_from_the_first_sample_is_taken_as_gravity(self) -> None:
        # Seeding on sample one means a permanently tilted mount produces no
        # cue at all, rather than a constant fake lean.
        processor = MotionProcessor(smoothing_tau=0.0, dead_zone=0.0)
        assert feed(processor, [(2.0, 0.0, GRAVITY)] * 30).is_still

    def test_a_push_after_settling_registers(self) -> None:
        processor = MotionProcessor(smoothing_tau=0.05, dead_zone=0.0)
        elapsed = settle(processor)
        state = feed(processor, [(2.0, 0.0, GRAVITY)] * 30, start=elapsed)
        assert state.lateral > 1.5
        assert state.longitudinal == pytest.approx(0.0, abs=1e-6)

    def test_a_force_held_far_longer_than_gravity_tau_fades(self) -> None:
        # Documented trade-off: without a gyroscope, a very long constant force
        # is indistinguishable from a tilted device.
        processor = MotionProcessor(gravity_tau=0.5, smoothing_tau=0.0, dead_zone=0.0)
        elapsed = settle(processor)
        held = feed(processor, [(2.0, 0.0, GRAVITY)] * 500, start=elapsed)
        assert abs(held.lateral) < 0.1

    def test_a_larger_gravity_tau_sustains_the_cue_for_longer(self) -> None:
        short = MotionProcessor(gravity_tau=0.5, smoothing_tau=0.0, dead_zone=0.0)
        long = MotionProcessor(gravity_tau=10.0, smoothing_tau=0.0, dead_zone=0.0)
        results = []
        for processor in (short, long):
            elapsed = settle(processor)
            results.append(feed(processor, [(2.0, 0.0, GRAVITY)] * 100, start=elapsed).lateral)
        assert results[1] > results[0]

    def test_gravity_estimate_absorbs_a_new_resting_orientation(self) -> None:
        processor = MotionProcessor(gravity_tau=0.2, smoothing_tau=0.05, dead_zone=0.0)
        feed(processor, [(0.0, 0.0, GRAVITY)] * 10)
        # Device laid on its side: a big step that should decay away, not
        # register as a permanent 1g cornering force.
        state = feed(processor, [(GRAVITY, 0.0, 0.0)] * 200, dt=0.02)
        assert abs(state.lateral) < 0.3

    def test_dead_zone_suppresses_sensor_noise(self) -> None:
        processor = MotionProcessor(dead_zone=0.5, smoothing_tau=0.0)
        elapsed = settle(processor)
        state = feed(processor, [(0.2, -0.2, GRAVITY)] * 20, start=elapsed)
        assert state.is_still

    def test_output_is_clamped(self) -> None:
        processor = MotionProcessor(clamp=2.0, smoothing_tau=0.0, dead_zone=0.0)
        elapsed = settle(processor)
        state = feed(processor, [(50.0, -50.0, GRAVITY)] * 20, start=elapsed)
        assert state.lateral == pytest.approx(2.0)
        assert state.longitudinal == pytest.approx(-2.0)

    def test_smoothing_delays_the_response(self) -> None:
        quick = MotionProcessor(smoothing_tau=0.01, dead_zone=0.0)
        slow = MotionProcessor(smoothing_tau=1.0, dead_zone=0.0)
        results = []
        for processor in (quick, slow):
            elapsed = settle(processor)
            results.append(feed(processor, [(3.0, 0.0, GRAVITY)] * 5, start=elapsed).lateral)
        assert results[0] > results[1]

    def test_axis_map_is_applied_to_the_output(self) -> None:
        processor = MotionProcessor(
            axis_map=AxisMap(lateral="-y", longitudinal="x"),
            smoothing_tau=0.0,
            dead_zone=0.0,
        )
        elapsed = settle(processor)
        state = feed(processor, [(1.0, 2.0, GRAVITY)] * 20, start=elapsed)
        assert state.lateral == pytest.approx(-2.0, abs=0.2)
        assert state.longitudinal == pytest.approx(1.0, abs=0.2)

    def test_reset_forgets_everything(self) -> None:
        processor = MotionProcessor()
        settle(processor)
        processor.reset()
        assert not processor.has_gravity_estimate
        assert processor.state is STILL

    def test_rejects_impossible_settings(self) -> None:
        with pytest.raises(ValueError, match="clamp"):
            MotionProcessor(clamp=0.0)
        with pytest.raises(ValueError, match="dead_zone"):
            MotionProcessor(dead_zone=-1.0)

    def test_out_of_order_timestamps_do_not_crash(self) -> None:
        processor = MotionProcessor()
        processor.update(MotionSample(0.0, 0.0, GRAVITY, 10.0))
        state = processor.update(MotionSample(1.0, 0.0, GRAVITY, 5.0))
        assert math.isfinite(state.lateral)


def test_sample_scaling() -> None:
    scaled = MotionSample(1.0, 2.0, 3.0, 7.0).scaled(2.0)
    assert (scaled.x, scaled.y, scaled.z) == (2.0, 4.0, 6.0)
    assert scaled.timestamp == 7.0
