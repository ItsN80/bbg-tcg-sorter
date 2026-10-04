"""Regression tests: the generic games.py engine driven by games/mtg/game.json
must route Magic cards exactly like the original hard-coded matcher did."""
import itertools
import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import games  # noqa: E402

MTG = games.get_game("mtg")

# ---- Original implementation (Basic-Website.py before multi-game), verbatim ----
TYPE_MODIFIER_TAGS = ["Legendary", "Snow", "Token", "Basic"]
TYPE_BASE_TAGS = ["Artifact", "Enchantment", "Creature", "Instant", "Sorcery", "Land", "Planeswalker", "Battle"]
ALL_TYPE_TAGS = TYPE_MODIFIER_TAGS + TYPE_BASE_TAGS


def old_normalize_criteria_value(raw_value):
    if not isinstance(raw_value, dict):
        raw_value = {}
    colors_raw = raw_value.get("colors", [])
    colors = []
    if isinstance(colors_raw, list):
        colors = [c for c in colors_raw if c in ["W", "U", "B", "R", "G", "C"]]
    type_tags_raw = raw_value.get("type_tags")
    if type_tags_raw is None:
        legacy_type = str(raw_value.get("type", "")).strip()
        type_tags_raw = legacy_type.split()
    if not isinstance(type_tags_raw, list):
        type_tags_raw = []
    type_tags = [t for t in ALL_TYPE_TAGS if t in type_tags_raw]
    return {
        "name": str(raw_value.get("name", "")).strip(),
        "type_tags": type_tags,
        "colors": colors,
        "cmc": str(raw_value.get("cmc", "")).strip(),
        "set_symbol": str(raw_value.get("set_symbol", "")).strip(),
    }


def old_matches_criteria(card, criteria):
    if not (criteria.get("name") or criteria.get("type_tags") or criteria.get("cmc")
            or criteria.get("set_symbol") or criteria.get("colors")):
        return False
    if criteria.get("name"):
        name_crit = criteria["name"].strip()
        card_name = card.get("name", "").strip()
        if "-" in name_crit:
            parts = name_crit.split("-")
            if len(parts) == 2 and len(parts[0].strip()) == 1 and len(parts[1].strip()) == 1:
                start_letter = parts[0].strip().upper()
                end_letter = parts[1].strip().upper()
                if not card_name:
                    return False
                first_letter = card_name[0].upper()
                if first_letter < start_letter or first_letter > end_letter:
                    return False
            else:
                if name_crit.lower() not in card_name.lower():
                    return False
        else:
            if name_crit.lower() not in card_name.lower():
                return False
    if criteria.get("type_tags"):
        card_type_line = card.get("type", "").lower()
        for tag in criteria["type_tags"]:
            if tag.lower() not in card_type_line:
                return False
    if criteria.get("cmc"):
        try:
            if float(criteria["cmc"]) != float(card.get("cmc", 0)):
                return False
        except ValueError:
            return False
    if criteria.get("set_symbol"):
        if criteria["set_symbol"].lower() not in card.get("set_symbol", "").lower():
            return False
    if criteria.get("colors"):
        crit_colors = set(criteria["colors"])
        card_colors = set(card.get("color_identity", card.get("colors", [])))
        if crit_colors == {"C"}:
            if card_colors:
                return False
        else:
            if crit_colors != card_colors:
                return False
    return True
# ---- end original implementation ----


CARDS = [
    {"name": "Lightning Bolt", "type": "Instant", "colors": ["R"], "color_identity": ["R"], "cmc": 1.0, "set_symbol": "lea"},
    {"name": "Sol Ring", "type": "Artifact", "colors": [], "color_identity": [], "cmc": 1.0, "set_symbol": "c21"},
    {"name": "Forest", "type": "Basic Land — Forest", "colors": [], "color_identity": ["G"], "cmc": 0.0, "set_symbol": "blb"},
    {"name": "Snow-Covered Island", "type": "Basic Snow Land — Island", "colors": [], "color_identity": ["U"], "cmc": 0.0, "set_symbol": "khm"},
    {"name": "Atraxa, Praetors' Voice", "type": "Legendary Creature — Phyrexian Angel Horror", "colors": ["W", "U", "B", "G"], "color_identity": ["W", "U", "B", "G"], "cmc": 4.0, "set_symbol": "c16"},
    {"name": "Wrath of God", "type": "Sorcery", "colors": ["W"], "color_identity": ["W"], "cmc": 4.0, "set_symbol": "2xm"},
    {"name": "Teferi, Hero of Dominaria", "type": "Legendary Planeswalker — Teferi", "colors": ["W", "U"], "color_identity": ["W", "U"], "cmc": 5.0, "set_symbol": "dom"},
    {"name": "Invasion of Zendikar", "type": "Battle — Siege", "colors": ["G"], "color_identity": ["G"], "cmc": 4.0, "set_symbol": "mom"},
    {"name": "Goblin Token", "type": "Token Creature — Goblin", "colors": ["R"], "color_identity": ["R"], "cmc": 0.0, "set_symbol": "tm19"},
    {"name": "Rhystic Study", "type": "Enchantment", "colors": ["U"], "color_identity": ["U"], "cmc": 3.0, "set_symbol": "pcy"},
    {"name": "Weird Card", "type": "Artifact Creature", "colors": [], "color_identity": [], "cmc": "CMC not found", "set_symbol": "Set not found"},
    {"name": "", "type": "", "colors": [], "cmc": 0, "set_symbol": ""},
    {"name": "Old Payload", "type": "Creature", "colors": ["B"], "cmc": 2.0, "set_symbol": "m10"},  # no color_identity key
]

NAMES = ["", "a", "a-c", "s-z", "bolt", "LIGHT", "x - y", "a-", "t"]
TYPE_TAG_SETS = [[], ["Instant"], ["Legendary", "Creature"], ["Basic", "Land"], ["Snow"], ["Token"], ["Planeswalker"], ["Battle"], ["Artifact", "Creature"]]
COLOR_SETS = [[], ["C"], ["R"], ["W", "U"], ["W", "U", "B", "G"], ["G"], ["U", "C"]]
CMCS = ["", "0", "1", "4", "4.0", "abc"]
SETS = ["", "lea", "C1", "KHM", "m"]


def all_criteria():
    for name, tags, colors, cmc, set_sym in itertools.product(NAMES, TYPE_TAG_SETS, COLOR_SETS, CMCS, SETS):
        yield {"name": name, "type_tags": tags, "colors": colors, "cmc": cmc, "set_symbol": set_sym}


def test_mtg_definition_is_valid():
    with open(os.path.join(games.BUILTIN_GAMES_DIR, "mtg", "game.json")) as f:
        assert games.validate_game(json.load(f)) == []


def test_matching_identical_to_original():
    checked = 0
    for raw in all_criteria():
        old_crit = old_normalize_criteria_value(raw)
        new_crit = games.normalize_criteria_value(MTG, raw)
        assert new_crit == old_crit, raw
        for card in CARDS:
            assert games.matches_criteria(MTG, card, new_crit) == old_matches_criteria(card, old_crit), (raw, card)
            checked += 1
    assert checked > 100_000


def test_normalize_identical_to_original_including_legacy():
    rng = random.Random(1)
    samples = [
        {}, None, "junk", {"type": "Legendary Creature"}, {"type": "Basic Snow Land Forest"},
        {"type_tags": "Creature"}, {"colors": "W"}, {"colors": ["W", "X", "C"]},
        {"name": "  a-c ", "cmc": 3, "set_symbol": " khm "},
    ]
    for _ in range(500):
        samples.append({
            "name": rng.choice(NAMES),
            "type_tags": rng.sample(ALL_TYPE_TAGS + ["Bogus"], rng.randint(0, 4)),
            "colors": rng.sample(["W", "U", "B", "R", "G", "C", "Z"], rng.randint(0, 3)),
            "cmc": rng.choice(CMCS + [2, 2.5]),
            "set_symbol": rng.choice(SETS),
        })
    for raw in samples:
        new, old = games.normalize_criteria_value(MTG, raw), old_normalize_criteria_value(raw)
        # The old code kept colors in submitted order; the engine uses canonical
        # WUBRGC order. Colors match as a set, so only the order may differ.
        assert sorted(new.pop("colors")) == sorted(old.pop("colors")), raw
        assert new == old, raw


def test_existing_bin_info_round_trips():
    """Bins saved by the current program load unchanged."""
    path = os.path.join(os.path.dirname(games.BASE_DIR), "bbg-tcg-sorter", "storage", "bin-info.json")
    if not os.path.exists(path):
        path = os.path.join(games.BASE_DIR, "storage", "bin-info-default.json")
    with open(path) as f:
        raw = json.load(f)
    new = games.normalize_box_criteria(MTG, raw)
    for i in range(1, 11):
        assert new[i] == old_normalize_criteria_value(raw.get(str(i), {}))


def test_form_round_trip():
    crit = {"name": "a-c", "type_tags": ["Legendary", "Creature"], "colors": ["W", "U"], "cmc": "3", "set_symbol": "KHM"}
    form = {}
    for flt in MTG["filters"]:
        if flt["kind"] == "multi":
            for v in crit[flt["key"]]:
                form[games.form_field_name(flt, 4, v)] = "1"
        else:
            form[games.form_field_name(flt, 4)] = crit[flt["key"]]
    assert games.criteria_from_form(MTG, form, 4) == crit


def test_summary_matches_old_format():
    crit = {"name": "a-c", "type_tags": ["Legendary", "Creature"], "colors": ["W", "U"], "cmc": "3", "set_symbol": "khm"}
    assert games.summarize_criteria(MTG, crit) == "a-c · Legendary Creature · CMC 3 · KHM · WU"
    assert games.summarize_criteria(MTG, games.empty_criteria(MTG)) == ""


def test_validate_rejects_bad_packs():
    assert games.validate_game({"schema_version": 1, "id": "../x", "name": "x", "lookup": {"type": "python"},
                                "recognition": {"prompt": "p"}, "filters": []})
    bad_file = {"schema_version": 1, "id": "x", "name": "X", "lookup": {"type": "local_json", "file": "../etc/passwd"},
                "recognition": {"prompt": "p"}, "filters": [{"key": "name", "kind": "name_range"}]}
    assert any("lookup.file" in e for e in games.validate_game(bad_file))


def test_normalizer_names_match_recognition_module():
    sys.path.insert(0, os.path.join(games.BASE_DIR, "scripts"))
    import recognition
    assert games.NORMALIZER_NAMES == set(recognition.NORMALIZERS)


def test_get_game_strict():
    import pytest
    with pytest.raises(KeyError):
        games.get_game("not-installed", strict=True)
    assert games.get_game("not-installed")["id"] == "mtg"


def test_validate_rejects_type_mistakes():
    import copy
    with open(os.path.join(games.BUILTIN_GAMES_DIR, "mtg", "game.json")) as f:
        good = json.load(f)
    cases = [
        lambda d: d["filters"][1].update(match="exact_set", kind="text"),
        lambda d: d["filters"][2]["options"][0].update(requires="W"),
        lambda d: d["recognition"].update(normalize={"set_code": "magic"}),
        lambda d: d["recognition"]["aws"]["patterns"].update(set_code="("),
        lambda d: d.update(display={"name": 3}),
        lambda d: d.update(lookup={"type": "local_json", "file": "cards.txt"}),
        lambda d: d.update(lookup={"type": "local_json", "file": "c.json", "match_keys": ["pitch"]}),
        lambda d: d.update(lookup={"type": "local_json", "file": "c.json", "fuzzy_cutoff": 5}),
    ]
    for mutate in cases:
        d = copy.deepcopy(good)
        mutate(d)
        assert games.validate_game(d), d
