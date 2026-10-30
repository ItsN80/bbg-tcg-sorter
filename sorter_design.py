"""The physical sorter designs and how their bins map to servo moves.

ramp  : always 10 bins. Bins 1-9 each have a flapper that opens to catch the
        card; with every flapper closed the card runs off the end into bin 10.
tower : 1-15 levels, flapper 1 at the top. A level's flapper tips left or
        right to drop the card into that level's two bins, and is centered
        otherwise. Below level 1 the card doesn't fall all the way on its own,
        so once it's released the level above tips the same way to push it
        through. With every flapper centered the card falls out the bottom
        into the last bin (2 x levels + 1).

Either way the last bin is the catch-all: failed reads and cards no bin
matches go there.
"""

from collections import namedtuple

RAMP = "ramp"
TOWER = "tower"
DESIGNS = (RAMP, TOWER)

RAMP_BIN_COUNT = 10
MAX_TOWER_LEVELS = 15  # flappers 10-15 use the PCA9685's spare channels 10-15
DEFAULT_TOWER_LEVELS = 3
MAX_BIN_COUNT = 2 * MAX_TOWER_LEVELS + 1  # bins kept per game, for the largest tower
RAMP_FLAPPERS = [f"flapper_{i}" for i in range(1, RAMP_BIN_COUNT)]

# How a released card reaches a bin: tip `flapper` to `action` before the
# release, tip `push_flapper` (if any) the same way after it, then put every
# flapper that moved back to `return_action`.
Route = namedtuple("Route", "flapper action return_action push_flapper")


def normalize(raw):
    """config["sorter"] -> {"design": ..., "tower_levels": ...}, ramp by default."""
    if not isinstance(raw, dict):
        raw = {}
    design = raw.get("design") if raw.get("design") in DESIGNS else RAMP
    try:
        levels = int(raw.get("tower_levels", DEFAULT_TOWER_LEVELS))
    except (TypeError, ValueError):
        levels = DEFAULT_TOWER_LEVELS
    return {"design": design, "tower_levels": max(1, min(MAX_TOWER_LEVELS, levels))}


def bin_count(design):
    if design["design"] == TOWER:
        return 2 * design["tower_levels"] + 1
    return RAMP_BIN_COUNT


def catch_all_bin(design):
    return bin_count(design)


def tower_flappers(design):
    """Flapper keys of the tower's levels, top first."""
    return [f"flapper_{level}" for level in range(1, design["tower_levels"] + 1)]


def default_flapper_channel(flapper_num):
    """PCA9685 channel servo_controller.py drives flapper_num on when config
    sets none: flappers 1-9 on 0-8, flappers 10-15 on 10-15 (9 is the card servo)."""
    return flapper_num - 1 if flapper_num <= 9 else flapper_num


def route(design, bin_num):
    """The Route that sends the released card into bin_num, or None for the
    catch-all (no flapper moves)."""
    if bin_num == catch_all_bin(design):
        return None
    if design["design"] == TOWER:
        level = (bin_num + 1) // 2
        push = f"flapper_{level - 1}" if level > 1 else None
        return Route(f"flapper_{level}", "left" if bin_num % 2 else "right", "center", push)
    return Route(f"flapper_{bin_num}", "open", "close", None)


def bin_label(design, bin_num):
    """Where bin_num is on the machine, e.g. "Level 2 Left"; "" for ramp bins."""
    if bin_num == catch_all_bin(design):
        return "Catch-all"
    if design["design"] == TOWER:
        return f"Level {(bin_num + 1) // 2} {'Left' if bin_num % 2 else 'Right'}"
    return ""


def describe(design):
    if design["design"] == TOWER:
        levels = design["tower_levels"]
        return f"Tower, {levels} level{'s' if levels != 1 else ''} ({bin_count(design)} bins)"
    return f"Ramp ({RAMP_BIN_COUNT} bins)"
