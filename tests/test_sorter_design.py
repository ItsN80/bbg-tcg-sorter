"""Unit tests for sorter_design.py: ramp vs tower bins and servo routes."""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
import sorter_design as sd  # noqa: E402

RAMP = sd.normalize(None)
TOWER3 = sd.normalize({"design": "tower", "tower_levels": 3})


def test_ramp_is_the_default_and_bad_values_are_clamped():
    assert RAMP == {"design": "ramp", "tower_levels": sd.DEFAULT_TOWER_LEVELS}
    assert sd.normalize({"design": "spiral"})["design"] == "ramp"
    assert sd.normalize({"design": "tower", "tower_levels": 99})["tower_levels"] == 15
    assert sd.normalize({"design": "tower", "tower_levels": 0})["tower_levels"] == 1
    assert sd.normalize({"design": "tower", "tower_levels": "x"})["tower_levels"] == sd.DEFAULT_TOWER_LEVELS


def test_ramp_routes():
    assert sd.bin_count(RAMP) == 10 and sd.catch_all_bin(RAMP) == 10
    assert sd.route(RAMP, 1) == ("flapper_1", "open", "close", None)
    assert sd.route(RAMP, 9) == ("flapper_9", "open", "close", None)
    assert sd.route(RAMP, 10) is None
    assert sd.bin_label(RAMP, 3) == "" and sd.bin_label(RAMP, 10) == "Catch-all"


def test_tower_routes_two_bins_per_level_plus_catch_all():
    """Below level 1, the level above pushes the card through the same way."""
    assert sd.bin_count(TOWER3) == 7 and sd.catch_all_bin(TOWER3) == 7
    assert sd.route(TOWER3, 1) == ("flapper_1", "left", "center", None)
    assert sd.route(TOWER3, 2) == ("flapper_1", "right", "center", None)
    assert sd.route(TOWER3, 4) == ("flapper_2", "right", "center", "flapper_1")
    assert sd.route(TOWER3, 5) == ("flapper_3", "left", "center", "flapper_2")
    assert sd.route(TOWER3, 6) == ("flapper_3", "right", "center", "flapper_2")
    assert sd.route(TOWER3, 7) is None
    assert sd.bin_label(TOWER3, 4) == "Level 2 Right" and sd.bin_label(TOWER3, 7) == "Catch-all"
    assert sd.tower_flappers(TOWER3) == ["flapper_1", "flapper_2", "flapper_3"]


def test_largest_tower_fits_every_bin_and_channel():
    tower = sd.normalize({"design": "tower", "tower_levels": sd.MAX_TOWER_LEVELS})
    assert sd.bin_count(tower) == sd.MAX_BIN_COUNT == 31
    assert sd.route(tower, 30) == ("flapper_15", "right", "center", "flapper_14")
    channels = [sd.default_flapper_channel(n) for n in range(1, 16)]
    assert channels == [0, 1, 2, 3, 4, 5, 6, 7, 8, 10, 11, 12, 13, 14, 15]  # 9 is the card servo
