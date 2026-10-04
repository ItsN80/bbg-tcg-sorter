"""Flesh and Blood game pack: games/fab/game.json + default-bins.json + cards.json,
exercised through the generic games.py engine with real card records."""
import collections
import gzip
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import games  # noqa: E402

FAB_DIR = os.path.join(games.BUILTIN_GAMES_DIR, "fab")

# Non-deck oddities that are allowed to fall through to the catch-all bin 10.
BIN10_OK_TYPES = {"Event", "Macro", "Placeholder Card"}
BIN10_OK_NAMES = {"Marked"}  # typeless "Marked" reminder card


def _load_json(path):
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def fab():
    return games.load_games()["fab"]


@pytest.fixture(scope="module")
def cards(fab):
    data = _load_json(games.game_file_path(fab, fab["lookup"]["file"]))
    assert data["source"] and data["generated"]
    return data["cards"]


@pytest.fixture(scope="module")
def bins(fab):
    return games.normalize_box_criteria(fab, _load_json(os.path.join(FAB_DIR, "default-bins.json")))


def route(fab, bins, card):
    for i in range(1, games.BIN_COUNT + 1):
        if games.matches_criteria(fab, card, bins[i]):
            return i
    return games.BIN_COUNT


def first(cards, name, pitch=None):
    for c in cards:
        if c["name"] == name and (pitch is None or c["pitch"] == pitch):
            return c
    raise AssertionError(f"{name} (pitch {pitch}) not in cards file")


def by_id(cards, card_id):
    return next(c for c in cards if c["id"] == card_id)


def test_definition_is_valid():
    with open(os.path.join(FAB_DIR, "game.json")) as f:
        assert games.validate_game(json.load(f)) == []
    loaded = games.load_games()
    assert "fab" in loaded and "mtg" in loaded
    assert loaded["fab"]["name"] == "Flesh and Blood"


def test_lookup_file_matches_definition(fab):
    lookup = fab["lookup"]
    assert lookup["type"] == "local_json"
    assert os.path.isfile(games.game_file_path(fab, lookup["file"]))


def test_default_bins_round_trip(fab):
    raw = _load_json(os.path.join(FAB_DIR, "default-bins.json"))
    assert set(raw) == {str(i) for i in range(1, games.BIN_COUNT + 1)}
    keys = {flt["key"] for flt in fab["filters"]}
    for value in raw.values():
        assert set(value) == keys
    assert games.normalize_box_criteria(fab, raw) == {int(k): v for k, v in raw.items()}
    assert not games.is_criteria_set(raw["10"])


def test_record_shape(cards):
    assert len(cards) > 5000
    want = {"id", "name", "pitch", "types", "classes", "talents", "cost", "set", "rarity", "image_url", "type_text"}
    for c in cards:
        assert want <= set(c), c
        assert c["id"] == c["id"].upper() and c["id"].isalnum(), c
        assert c["pitch"] in ("", "1", "2", "3"), c
        assert len(c["set"]) == 3, c
        assert isinstance(c["types"], list) and isinstance(c["classes"], list) and isinstance(c["talents"], list)


@pytest.mark.parametrize("name,pitch,expected", [
    ("Head Jab", "1", 1),                    # Ninja Action - Attack
    ("Alpha Rampage", "1", 1),               # Brute Action - Attack
    ("Razor Reflex", "1", 2),                # Generic Attack Reaction
    ("Sink Below", "1", 3),                  # Generic Defense Reaction
    ("Tome of Fyendal", "2", 4),             # Generic Action (non-attack)
    ("Energy Potion", "3", 4),               # Generic Action - Item
    ("Sigil of Solace", "1", 5),             # Generic Instant
    ("Fyendal's Spring Tunic", "", 6),       # Generic Equipment - Chest
    ("Dawnblade", "", 7),                    # Warrior Weapon - Sword (2H)
    ("Dorinthea Ironsong", "", 8),           # Warrior Hero
    ("Ira, Crimson Haze", "", 8),            # Ninja Hero - Young
    ("Copper", "", 9),                       # Generic Token - Item
    ("Quicken", "", 9),                      # Generic Token - Aura
    ("Cracked Bauble", "2", 9),              # Generic Resource
])
def test_known_cards_route_to_expected_bin(fab, bins, cards, name, pitch, expected):
    card = first(cards, name, pitch)
    assert route(fab, bins, card) == expected, card


def test_edge_types_route_sensibly(fab, bins, cards):
    def first_with(pred):
        return next(c for c in cards if pred(c))
    evo = first_with(lambda c: {"Action", "Equipment", "Evo"} <= set(c["types"]))
    assert route(fab, bins, evo) == 6, evo                     # Evo "Action Equipment" is equipment
    evo_instant = first_with(lambda c: {"Instant", "Equipment"} <= set(c["types"]))
    assert route(fab, bins, evo_instant) == 6, evo_instant
    block = first_with(lambda c: "Block" in c["types"])
    assert route(fab, bins, block) == 3, block                 # blocks ride with defense reactions
    demi = first_with(lambda c: c["types"] and "Demi-Hero" in c["types"] and "Equipment" not in c["types"])
    assert route(fab, bins, demi) == 8, demi
    mentor = first_with(lambda c: "Mentor" in c["types"])
    assert route(fab, bins, mentor) == 8, mentor
    ally_token = first_with(lambda c: {"Token", "Ally"} <= set(c["types"]))
    assert route(fab, bins, ally_token) == 8, ally_token


def test_printed_id_resolves_to_front_face_and_original_printing(cards):
    assert by_id(cards, "WTR115")["name"] == "Dawnblade"
    assert by_id(cards, "UPR009")["name"] == "Invoke Azvolai"   # front; back face "Azvolai" shares the id
    # First record for a name+pitch is its earliest standard printing.
    assert first(cards, "Sink Below", "2")["id"] == "WTR216"
    assert first(cards, "Dawnblade", "")["set"] == "WTR"


def test_every_card_routes_and_histogram(fab, bins, cards):
    histogram = collections.Counter()
    unexpected = collections.Counter()
    for card in cards:
        b = route(fab, bins, card)
        histogram[b] += 1
        if b == games.BIN_COUNT and card["name"] not in BIN10_OK_NAMES \
                and not BIN10_OK_TYPES & set(card["types"]):
            unexpected[card["type_text"] or card["name"]] += 1
    print("\nFaB default-bin routing:", {k: histogram[k] for k in sorted(histogram)})
    if unexpected:
        print("Unexpectedly in bin 10:", unexpected.most_common(20))
    assert sum(histogram.values()) == len(cards)
    assert not unexpected


def test_every_multi_option_matches_a_real_card(fab, cards):
    for flt in fab["filters"]:
        if flt["kind"] != "multi":
            continue
        for opt in flt["options"]:
            crit = games.empty_criteria(fab)
            crit[flt["key"]] = [opt["value"]]
            assert any(games.matches_criteria(fab, c, crit) for c in cards), (flt["key"], opt["value"])


def test_action_option_is_non_attack(fab, cards):
    crit = games.empty_criteria(fab)
    crit["types"] = ["Action"]
    assert not games.matches_criteria(fab, first(cards, "Head Jab", "1"), crit)
    assert games.matches_criteria(fab, first(cards, "Tome of Fyendal", "2"), crit)


def test_every_class_and_talent_in_data_has_an_option(fab, cards):
    opts = {flt["key"]: set(games.option_values(flt)) for flt in fab["filters"] if flt["kind"] == "multi"}
    for c in cards:
        assert set(c["classes"]) <= opts["classes"], c
        assert set(c["talents"]) <= opts["talents"], c
        assert c["rarity"] in opts["rarity"], c


@pytest.mark.parametrize("key,value", [("cost", "3"), ("cost", "0"), ("set", "wtr"), ("set", "ROS"), ("name", "a-c")])
def test_scalar_filters_match_real_cards(fab, cards, key, value):
    crit = games.empty_criteria(fab)
    crit[key] = value
    assert any(games.matches_criteria(fab, c, crit) for c in cards)


def test_combined_filters(fab, cards):
    crit = games.empty_criteria(fab)
    crit.update({"types": ["Attack action"], "classes": ["Ninja"], "pitch": ["1"], "set": "WTR"})
    hits = [c for c in cards if games.matches_criteria(fab, c, crit)]
    assert hits and all(c["set"] == "WTR" and c["pitch"] == "1" and "Ninja" in c["classes"] for c in hits)


def test_summary(fab):
    crit = games.empty_criteria(fab)
    crit.update({"name": "a-c", "types": ["Attack action", "Instant"], "pitch": ["1", "3"], "cost": "2", "set": "wtr",
                 "rarity": ["Super Rare"]})
    assert games.summarize_criteria(fab, crit) == "a-c · Attack action, Instant · Red, Blue · Cost 2 · WTR · Super rare"


# --- lookup edge cases found while integrating (real cards.json) ---
def _lookup(rec):
    sys.path.insert(0, os.path.join(games.BASE_DIR, "scripts"))
    from lookups import lookup_card
    full = {"card_name": "Unknown", "card_id": "Unknown", "pitch": "Unknown"}
    full.update(rec)
    return lookup_card(games.get_game("fab", strict=True), full)[0]


def test_old_style_id_with_rarity_suffix_resolves():
    assert _lookup({"card_id": "WTR115T"})["id"] == "WTR115"   # printed "WTR115-T"
    assert _lookup({"card_id": "ENROS001"})["id"] == "ROS001"  # footer "EN | ROS001"


def test_shared_id_prefers_the_face_that_was_read():
    assert _lookup({"card_id": "ENG025", "card_name": "Inner Chi"})["name"] == "Inner Chi"
    assert _lookup({"card_id": "ENG025"})["name"] == "A Drop in the Ocean"


def test_same_name_needs_pitch():
    assert _lookup({"card_name": "Sink Below"}) is None
    assert _lookup({"card_name": "Sink Below", "pitch": "2"})["pitch"] == "2"


def test_cost_zero_does_not_match_cardless_cost():
    g = games.get_game("fab")
    crit = games.normalize_criteria_value(g, {"cost": "0"})
    assert not games.matches_criteria(g, {"cost": "", "types": ["Hero"]}, crit)
    assert games.matches_criteria(g, {"cost": "0", "types": ["Action"]}, crit)
