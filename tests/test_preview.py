"""Headless PNG preview rendering."""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from motionless.config import Config, ConfigError
from motionless.motion import STILL, MotionState
from motionless.preview import render_png

pytest.importorskip("cairo")


def png_size(path: Path) -> tuple[int, int]:
    header = path.read_bytes()[:24]
    assert header.startswith(b"\x89PNG\r\n\x1a\n")
    width, height = struct.unpack(">II", header[16:24])
    return width, height


def test_writes_a_png_of_the_requested_size(tmp_path: Path) -> None:
    target = render_png(tmp_path / "p.png", Config(), STILL, width=640, height=480)
    assert png_size(target) == (640, 480)


def test_creates_missing_directories(tmp_path: Path) -> None:
    target = render_png(tmp_path / "deep" / "nested" / "p.png", Config(), STILL)
    assert target.exists()


def test_motion_changes_the_output(tmp_path: Path) -> None:
    still = render_png(tmp_path / "a.png", Config(), STILL, width=400, height=300)
    moving = render_png(
        tmp_path / "b.png", Config(), MotionState(lateral=3.0), width=400, height=300
    )
    assert still.read_bytes() != moving.read_bytes()


def test_a_transparent_background_is_smaller_than_an_opaque_one(tmp_path: Path) -> None:
    opaque = render_png(tmp_path / "a.png", Config(), STILL, background="#000000")
    clear = render_png(tmp_path / "b.png", Config(), STILL, background=None)
    assert clear.stat().st_size <= opaque.stat().st_size


def test_invisible_cue_still_produces_a_file(tmp_path: Path) -> None:
    target = render_png(tmp_path / "p.png", Config(), STILL, visibility=0.0)
    assert target.exists()


@pytest.mark.parametrize(("width", "height"), [(0, 100), (100, 0), (-1, -1)])
def test_degenerate_sizes_are_refused(tmp_path: Path, width: int, height: int) -> None:
    with pytest.raises(ConfigError, match="must be positive"):
        render_png(tmp_path / "p.png", Config(), STILL, width=width, height=height)


def test_an_invalid_background_is_reported(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="invalid colour"):
        render_png(tmp_path / "p.png", Config(), STILL, background="chartreuse")
