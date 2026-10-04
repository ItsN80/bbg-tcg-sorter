"""Unit tests for scripts/lookups (no real network: requests.get is monkeypatched)."""
import gzip
import json
import os
import sys

import pytest
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import games  # noqa: E402
import lookups  # noqa: E402
from lookups import local_json, scryfall  # noqa: E402

MTG = games.get_game("mtg")
CARD_KEYS = ["name", "type", "colors", "color_identity", "cmc", "set_symbol",
             "collector_number", "card_identified_url"]


# ---- Scryfall -------------------------------------------------------------------

class FakeResponse:
    def __init__(self, status_code, body, url):
        self.status_code = status_code
        self._body = body
        self.url = url

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


SCRYFALL_SHOCK = {
    "name": "Shock", "type_line": "Instant", "colors": ["R"], "color_identity": ["R"],
    "cmc": 1.0, "set": "m19", "collector_number": "156",
    "image_uris": {"normal": "https://img/shock.jpg"},
}


@pytest.fixture
def fake_get(monkeypatch):
    """Install a scripted requests.get; returns the list of recorded calls."""
    calls = []
    script = []

    def _get(url, params=None, timeout=None, headers=None):
        calls.append({"url": url, "params": params, "timeout": timeout, "headers": headers})
        item = script.pop(0)
        if isinstance(item, Exception):
            raise item
        status, body = item
        q = "&".join(f"{k}={v}" for k, v in (params or {}).items())
        return FakeResponse(status, body, url + ("?" + q if q else ""))

    monkeypatch.setattr(scryfall.requests, "get", _get)
    monkeypatch.setattr(scryfall.time, "sleep", lambda s: None)
    return calls, script


def test_to_card_keys_and_defaults():
    card = scryfall._to_card(SCRYFALL_SHOCK, "999")
    assert list(card) == CARD_KEYS
    assert card == {"name": "Shock", "type": "Instant", "colors": ["R"], "color_identity": ["R"],
                    "cmc": 1.0, "set_symbol": "m19", "collector_number": "156",
                    "card_identified_url": "https://img/shock.jpg"}
    empty = scryfall._to_card({}, "42")
    assert empty == {"name": "Unknown", "type": "Type not found", "colors": [], "color_identity": [],
                     "cmc": "CMC not found", "set_symbol": "Set not found", "collector_number": "42",
                     "card_identified_url": ""}
    assert scryfall._to_card({"colors": ["G"]})["color_identity"] == ["G"]
    assert scryfall._to_card({})["collector_number"] == "Unknown"


def test_scryfall_exact_hit(fake_get):
    calls, script = fake_get
    script.append((200, SCRYFALL_SHOCK))
    card, attempts = lookups.lookup_card(MTG, {"card_name": "Shock", "set_code": "M19", "collector_number": "0156"})
    assert card["name"] == "Shock" and list(card) == CARD_KEYS
    assert calls[0]["url"] == "https://api.scryfall.com/cards/M19/156"
    assert calls[0]["headers"] == {"User-Agent": "bbg-tcg-sorter/1.0"} and calls[0]["timeout"] == 30
    assert [a["stage"] for a in attempts] == ["exact"]
    assert attempts[0]["status_code"] == 200


def test_scryfall_stage_order_exact_fuzzy_set_fallback(fake_get):
    calls, script = fake_get
    script.extend([
        (404, {"details": "No card at M19/999"}),
        (404, {"details": "No card named Shok in M19"}),
        (200, SCRYFALL_SHOCK),
    ])
    card, attempts = lookups.lookup_card(MTG, {"card_name": "Shok", "set_code": "M19", "collector_number": "999"})
    assert card["name"] == "Shock"
    assert [a["stage"] for a in attempts] == ["exact", "fuzzy_set", "fuzzy_fallback"]
    assert attempts[0]["details"] == "No card at M19/999"
    assert calls[1]["params"] == {"fuzzy": "Shok", "set": "M19"}
    assert calls[2]["params"] == {"fuzzy": "Shok"}
    assert attempts[1]["url"].startswith("https://api.scryfall.com/cards/named?")


def test_scryfall_no_set_goes_straight_to_fuzzy(fake_get):
    calls, script = fake_get
    script.append((404, ValueError("not json")))
    card, attempts = lookups.lookup_card(MTG, {"card_name": "Nope", "set_code": "Unknown", "collector_number": "12"})
    assert card is None
    assert [a["stage"] for a in attempts] == ["fuzzy"]
    assert attempts[0]["details"] == "non-json error body"
    assert len(calls) == 1


def test_scryfall_unknown_name_skips_network(fake_get):
    calls, _ = fake_get
    card, attempts = lookups.lookup_card(MTG, {"card_name": "Unknown", "set_code": "M19", "collector_number": "1"})
    assert card is None and calls == []
    assert attempts[0]["stage"] == "skipped"


def test_scryfall_retries_then_records_error(fake_get):
    calls, script = fake_get
    script.extend([requests.ConnectionError("boom")] * 3)
    card, attempts = lookups.lookup_card(MTG, {"card_name": "Shock", "set_code": "Unknown", "collector_number": "Unknown"})
    assert card is None
    assert len(calls) == 3
    assert attempts == [{"stage": "fuzzy", "url": "https://api.scryfall.com/cards/named",
                         "status_code": None, "details": None, "error": "boom"}]


def test_unsupported_lookup_type():
    card, attempts = lookups.lookup_card({"lookup": {"type": "nope"}}, {})
    assert card is None and attempts[0]["error"]


# ---- local_json -----------------------------------------------------------------

FAB_CARDS = [
    {"id": "WTR167", "name": "Snatch", "pitch": 1, "set": "WTR"},
    {"id": "WTR168", "name": "Snatch", "pitch": 2, "set": "WTR"},
    {"id": "WTR169", "name": "Snatch", "pitch": 3, "set": "WTR"},
    {"id": ["ROS001", "ROS001-CF"], "name": "Aurora, Shooting Star", "pitch": None, "set": "ROS"},
    {"id": "ARC159", "name": "Command and Conquer", "pitch": 1, "set": "ARC"},
    {"id": "DYN000", "name": "Command and Conquer", "pitch": 1, "set": "DYN"},  # reprint, same pitch
]


def make_game(tmp_path, file_name="cards.json", gz=False, **lookup_extra):
    data = json.dumps({"cards": FAB_CARDS}).encode()
    path = tmp_path / file_name
    path.write_bytes(gzip.compress(data) if gz else data)
    lookup = {"type": "local_json", "file": file_name, "id_key": "card_id",
              "match_keys": {"pitch": "pitch"}}
    lookup.update(lookup_extra)
    return {"id": "fab", "_dir": str(tmp_path), "lookup": lookup}


def rec(name="Unknown", card_id="Unknown", pitch="Unknown"):
    return {"card_name": name, "card_id": card_id, "pitch": pitch}


def test_local_exact_id(tmp_path):
    game = make_game(tmp_path)
    card, attempts = lookups.lookup_card(game, rec("Totally Wrong", "WTR168"))
    assert card["id"] == "WTR168" and card["pitch"] == 2 and card["matched_by"] == "id"
    assert "matched_by" not in FAB_CARDS[1]


def test_local_id_dashes_lowercase_and_list_ids(tmp_path):
    game = make_game(tmp_path)
    card, _ = lookups.lookup_card(game, rec(card_id="wtr-169"))
    assert card["id"] == "WTR169"
    card, _ = lookups.lookup_card(game, rec(card_id="ros 001 cf"))
    assert card["name"] == "Aurora, Shooting Star"


def test_local_unknown_id_falls_back_to_name(tmp_path):
    game = make_game(tmp_path)
    card, attempts = lookups.lookup_card(game, rec("snatch", "XXX999", "3"))
    assert card["id"] == "WTR169" and card["matched_by"] == "name"
    assert [a["stage"] for a in attempts] == ["id", "name"]


def test_local_exact_name_with_pitch(tmp_path):
    game = make_game(tmp_path)
    card, _ = lookups.lookup_card(game, rec("  SNATCH ", pitch="2"))
    assert card["id"] == "WTR168"


def test_local_ambiguous_pitch_unknown(tmp_path):
    game = make_game(tmp_path)
    card, attempts = lookups.lookup_card(game, rec("Snatch"))
    assert card is None
    assert "ambiguous" in attempts[-1]["details"] and "pitch" in attempts[-1]["details"]


def test_local_pitch_not_found(tmp_path):
    game = make_game(tmp_path)
    card, attempts = lookups.lookup_card(game, rec("Command and Conquer", pitch="3"))
    assert card is None and "none" in attempts[-1]["details"]


def test_local_reprints_return_first_in_file_order(tmp_path):
    game = make_game(tmp_path)
    card, _ = lookups.lookup_card(game, rec("Command and Conquer"))
    assert card["id"] == "ARC159"


def test_local_fuzzy_name(tmp_path):
    game = make_game(tmp_path)
    card, attempts = lookups.lookup_card(game, rec("Comand and Conquor"))
    assert card["id"] == "ARC159" and card["matched_by"] == "fuzzy"
    card, attempts = lookups.lookup_card(game, rec("Snatc", pitch="1"))
    assert card["id"] == "WTR167"
    card, attempts = lookups.lookup_card(game, rec("Something Else Entirely"))
    assert card is None and attempts[-1]["stage"] == "fuzzy"


def test_local_json_gz(tmp_path):
    game = make_game(tmp_path, file_name="cards.json.gz", gz=True)
    card, _ = lookups.lookup_card(game, rec(card_id="ARC-159"))
    assert card["name"] == "Command and Conquer"


def test_local_custom_fields(tmp_path):
    cards = [{"code": "X1", "title": "Alpha"}, {"code": "X2", "title": "Beta"}]
    (tmp_path / "d.json").write_text(json.dumps({"cards": cards}))
    game = {"id": "x", "_dir": str(tmp_path),
            "lookup": {"type": "local_json", "file": "d.json", "id_field": "code",
                       "name_field": "title", "name_key": "nm", "id_key": "cid", "fuzzy_cutoff": 0.5}}
    assert lookups.lookup_card(game, {"cid": "x-2"})[0]["title"] == "Beta"
    assert lookups.lookup_card(game, {"nm": "alpha"})[0]["code"] == "X1"
    assert lookups.lookup_card(game, {"nm": "Bet"})[0]["code"] == "X2"


def test_local_missing_file_and_bad_format_never_raise(tmp_path):
    game = make_game(tmp_path)
    game["lookup"]["file"] = "missing.json"
    card, attempts = lookups.lookup_card(game, rec("Snatch"))
    assert card is None and attempts[-1]["stage"] == "error"
    (tmp_path / "bad.json").write_text("[1, 2, 3]")
    game["lookup"]["file"] = "bad.json"
    card, attempts = lookups.lookup_card(game, rec("Snatch"))
    assert card is None and attempts[-1]["stage"] == "error"


def test_local_cache_reloads_when_file_changes(tmp_path):
    game = make_game(tmp_path)
    assert lookups.lookup_card(game, rec(card_id="WTR167"))[0]["name"] == "Snatch"
    path = tmp_path / "cards.json"
    path.write_text(json.dumps({"cards": [{"id": "WTR167", "name": "Renamed"}]}))
    os.utime(path, ns=(1, 1))
    assert lookups.lookup_card(game, rec(card_id="WTR167"))[0]["name"] == "Renamed"
