"""Configuration loading, validation, dotted access and TOML round-tripping."""

from __future__ import annotations

from pathlib import Path

import pytest

from motionless.config import Config, ConfigError, MotionConfig, OverlayConfig, parse_color


class TestRoundTrip:
    def test_defaults_survive_a_write_and_read(self, tmp_path: Path) -> None:
        original = Config()
        path = original.save(tmp_path / "config.toml")
        assert Config.load(path) == original

    def test_changed_values_survive(self, tmp_path: Path) -> None:
        config = Config()
        config.overlay.color = "#ff8800"
        config.overlay.fps = 30
        config.overlay.idle_fps = 5
        config.motion.source = "udp"
        config.motion.udp.port = 9999
        config.start_hidden = True
        path = config.save(tmp_path / "config.toml")
        assert Config.load(path) == config

    def test_save_is_atomic_and_leaves_no_temporary_file(self, tmp_path: Path) -> None:
        path = Config().save(tmp_path / "config.toml")
        assert path.exists()
        assert list(tmp_path.glob("*.tmp")) == []

    def test_quotes_in_strings_are_escaped(self, tmp_path: Path) -> None:
        config = Config()
        config.motion.iio.device = 'odd"path\\here'
        path = config.save(tmp_path / "config.toml")
        assert Config.load(path).motion.iio.device == 'odd"path\\here'


class TestLoading:
    def test_missing_file_yields_defaults(self, tmp_path: Path) -> None:
        assert Config.load(tmp_path / "absent.toml") == Config()

    def test_missing_file_can_be_an_error(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="no configuration file"):
            Config.load(tmp_path / "absent.toml", missing_ok=False)

    def test_partial_file_keeps_other_defaults(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        path.write_text("[overlay]\nopacity = 0.2\n")
        config = Config.load(path)
        assert config.overlay.opacity == 0.2
        assert config.overlay.spacing == OverlayConfig().spacing

    def test_invalid_toml_is_reported_with_the_path(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        path.write_text("this is not = = toml")
        with pytest.raises(ConfigError, match="invalid TOML"):
            Config.load(path)

    def test_unknown_key_is_rejected_rather_than_ignored(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        path.write_text("[overlay]\ncolour = '#fff'\n")
        # A silently ignored typo is the worst outcome: the user changes a
        # setting, sees no effect, and blames the program.
        with pytest.raises(ConfigError, match=r"overlay\.colour"):
            Config.load(path)

    def test_wrong_type_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        path.write_text("[overlay]\nfps = 'sixty'\n")
        with pytest.raises(ConfigError, match=r"overlay\.fps must be an integer"):
            Config.load(path)

    def test_booleans_are_not_accepted_as_numbers(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        path.write_text("[overlay]\nopacity = true\n")
        with pytest.raises(ConfigError, match="must be a number"):
            Config.load(path)

    def test_integers_are_widened_to_floats(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        path.write_text("[overlay]\nspacing = 60\n")
        assert Config.load(path).overlay.spacing == 60.0


class TestValidation:
    @pytest.mark.parametrize(
        ("key", "value", "message"),
        [
            ("overlay.layout", "circles", "layout"),
            ("overlay.monitors", "second", "monitors"),
            ("overlay.opacity", "1.5", "opacity"),
            ("overlay.margin", "0.9", "margin"),
            ("overlay.margin", "0", "margin"),
            ("overlay.spacing", "1", "spacing"),
            ("overlay.radius", "0", "radius"),
            ("overlay.fps", "0", "fps"),
            ("overlay.color", "not-a-colour", "colour"),
            ("overlay.sensitivity", "-1", "sensitivity"),
            ("motion.clamp", "0", "clamp"),
            ("motion.dead_zone", "-1", "dead_zone"),
            ("motion.axis_lateral", "q", "invalid axis"),
            ("motion.udp.port", "0", "port"),
            ("motion.udp.units", "furlongs", "units"),
            ("motion.iio.poll_hz", "500", "poll_hz"),
        ],
    )
    def test_bad_values_are_refused(self, key: str, value: str, message: str) -> None:
        with pytest.raises(ConfigError, match=message):
            Config().set(key, value)

    def test_idle_fps_cannot_exceed_fps(self) -> None:
        config = Config()
        config.overlay.fps = 30
        with pytest.raises(ConfigError, match="idle_fps"):
            config.set("overlay.idle_fps", "60")

    def test_a_rejected_value_is_rolled_back(self) -> None:
        config = Config()
        with pytest.raises(ConfigError):
            config.set("overlay.opacity", "9")
        # `config set` catches the error and carries on, so leaving the invalid
        # value in place would write a broken file to disk.
        assert config.overlay.opacity == OverlayConfig().opacity
        config.validate()

    def test_rollback_applies_to_cross_field_rules_too(self) -> None:
        config = Config()
        config.overlay.fps = 30
        config.overlay.idle_fps = 5
        with pytest.raises(ConfigError, match="idle_fps"):
            config.set("overlay.idle_fps", "60")
        assert config.overlay.idle_fps == 5


class TestDottedAccess:
    def test_get_and_set_a_nested_value(self) -> None:
        config = Config()
        assert config.set("motion.udp.port", "6000") == 6000
        assert config.get("motion.udp.port") == 6000

    def test_booleans_accept_friendly_spellings(self) -> None:
        config = Config()
        for text in ("true", "yes", "on", "1"):
            assert config.set("start_hidden", text) is True
        for text in ("false", "no", "off", "0"):
            assert config.set("start_hidden", text) is False

    def test_a_non_boolean_is_refused(self) -> None:
        with pytest.raises(ConfigError, match="boolean"):
            Config().set("start_hidden", "maybe")

    def test_a_non_number_is_refused(self) -> None:
        with pytest.raises(ConfigError, match="must be a number"):
            Config().set("overlay.opacity", "quite a lot")

    @pytest.mark.parametrize("key", ["nope", "overlay.nope", "overlay.color.deep", "motion.udp.x"])
    def test_unknown_keys_are_refused(self, key: str) -> None:
        with pytest.raises(ConfigError, match="unknown setting"):
            Config().get(key)

    def test_a_section_is_not_a_setting(self) -> None:
        with pytest.raises(ConfigError, match="is a section"):
            Config().get("overlay")

    def test_keys_lists_every_leaf_and_no_sections(self) -> None:
        keys = Config().setting_names()
        assert "overlay.opacity" in keys
        assert "motion.udp.port" in keys
        assert "start_hidden" in keys
        assert "overlay" not in keys
        assert len(keys) == len(set(keys))


class TestColour:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("#fff", (1.0, 1.0, 1.0, 1.0)),
            ("#ffffff", (1.0, 1.0, 1.0, 1.0)),
            ("ffffff", (1.0, 1.0, 1.0, 1.0)),
            ("#000000ff", (0.0, 0.0, 0.0, 1.0)),
            ("#00000000", (0.0, 0.0, 0.0, 0.0)),
        ],
    )
    def test_accepted_forms(self, text: str, expected: tuple[float, ...]) -> None:
        assert parse_color(text) == pytest.approx(expected)

    @pytest.mark.parametrize("text", ["#ff", "#fffff", "red", "", "#gggggg"])
    def test_rejected_forms(self, text: str) -> None:
        with pytest.raises(ConfigError, match="invalid colour"):
            parse_color(text)


def test_axis_map_is_built_from_configuration() -> None:
    motion = MotionConfig(axis_lateral="-y", axis_longitudinal="x", axis_vertical="z")
    assert motion.axis_map().apply(1.0, 2.0, 3.0) == (-2.0, 1.0, 3.0)
