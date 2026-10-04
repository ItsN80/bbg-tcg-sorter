"""Unit tests for scripts/recognition.py (pure helpers, no camera / network)."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import games  # noqa: E402
import recognition as rec  # noqa: E402

MTG = games.get_game("mtg")

FAB = {
    "id": "fab",
    "recognition": {
        "keys": ["card_name", "card_id", "pitch"],
        "normalize": {"card_id": "upper_alnum", "pitch": "int"},
        "reject_name_words": ["action", "attack action", "instant"],
        "aws": {"patterns": {"card_id": r"\b([A-Za-z]{3}\s*-?\s*\d{3})\b"}},
    },
}


# ---- normalizers ----------------------------------------------------------

def test_clean_collector_number_docstring_examples():
    assert rec.clean_collector_number("A123") == "123"
    assert rec.clean_collector_number("123/287") == "123"
    assert rec.clean_collector_number("0123") == "123"
    assert rec.clean_collector_number("123a") == "123"
    assert rec.clean_collector_number("000") == "0"
    assert rec.clean_collector_number("") == "Unknown"
    assert rec.clean_collector_number(None) == "Unknown"
    assert rec.clean_collector_number("abc") == "Unknown"


def test_clean_set_code_docstring_examples():
    assert rec.clean_set_code("BLC-EN") == "BLC"
    assert rec.clean_set_code(" blc ") == "BLC"
    assert rec.clean_set_code("BLC/EN") == "BLC"
    assert rec.clean_set_code(None) == "Unknown"
    assert rec.clean_set_code("Unknown") == "Unknown"
    assert rec.clean_set_code("n/a") == "Unknown"
    assert rec.clean_set_code("304") == "Unknown"       # all digits
    assert rec.clean_set_code("ab") == "Unknown"        # too short
    assert rec.clean_set_code("abcdefg") == "ABCDE"     # truncated to 5
    assert rec.clean_set_code("ab", min_length=2) == "AB"
    assert rec.clean_set_code("abcdefg", max_length=3) == "ABC"


def test_upper_alnum_and_int():
    assert rec.clean_upper_alnum("ros-001") == "ROS001"
    assert rec.clean_upper_alnum(" wtr 1 ") == "WTR1"
    assert rec.clean_upper_alnum("--") == "Unknown"
    assert rec.clean_upper_alnum("null") == "Unknown"
    assert rec.clean_int("2") == "2"
    assert rec.clean_int("Pitch 3") == "3"
    assert rec.clean_int("01") == "1"
    assert rec.clean_int("red") == "Unknown"
    assert rec.clean_int(None) == "Unknown"


def test_normalize_value_uses_game_config():
    assert rec.normalize_value(MTG, "set_code", "blc-en") == "BLC"
    assert rec.normalize_value(MTG, "collector_number", "0042/287") == "42"
    assert rec.normalize_value(MTG, "card_name", "  Llanowar Elves ") == "Llanowar Elves"
    assert rec.normalize_value(MTG, "card_name", "null") == "Unknown"
    assert rec.normalize_value(MTG, "collector_number", None) == "Unknown"
    assert rec.normalize_value(MTG, "collector_number", 123) == "123"
    assert rec.normalize_value(FAB, "card_id", "ros-001") == "ROS001"
    assert rec.normalize_value(FAB, "pitch", 2) == "2"
    # set_code min/max from recognition.set_code
    short = {"recognition": {"normalize": {"set_code": "set_code"}, "set_code": {"min_length": 2, "max_length": 3}}}
    assert rec.normalize_value(short, "set_code", "m2") == "M2"
    assert rec.normalize_value(short, "set_code", "abcd") == "ABC"


# ---- reject rules -----------------------------------------------------------

def test_reject_name_mtg_type_lines():
    words = MTG["recognition"]["reject_name_words"]
    assert rec.reject_name("Creature - Human Wizard", words)
    assert rec.reject_name("Creature — Elf Druid", words)
    assert rec.reject_name("Creature", words)
    assert rec.reject_name("  instant ", words)
    assert not rec.reject_name("Llanowar Elves", words)
    assert not rec.reject_name("Legendary Creature - Elf", words)  # before-dash isn't one word
    assert not rec.reject_name("Lightning Bolt", words)
    assert not rec.reject_name("", words)
    assert not rec.reject_name("Creature", [])
    assert rec.looks_like_type_line("Sorcery") and not rec.looks_like_type_line("Shock")


def test_required_keys_from_env():
    assert rec.required_keys_from_env(MTG, {}) == []
    assert rec.required_keys_from_env(MTG, {"REQUIRE_SET_AND_COLLECTOR": "1"}) == ["set_code", "collector_number"]
    assert rec.required_keys_from_env(MTG, {"REQUIRE_SET_AND_COLLECTOR": "0"}, legacy_default=True) == []
    assert rec.required_keys_from_env(MTG, {}, legacy_default=True) == ["set_code", "collector_number"]
    assert rec.required_keys_from_env(MTG, {"REQUIRE_RECOGNITION_KEYS": " set_code , bogus"}) == ["set_code"]
    # legacy alias ignored for games without those keys
    assert rec.required_keys_from_env(FAB, {"REQUIRE_SET_AND_COLLECTOR": "1"}) == []
    assert rec.required_keys_from_env(FAB, {"REQUIRE_RECOGNITION_KEYS": "pitch"}) == ["pitch"]


# ---- Ollama model output ----------------------------------------------------

def test_parse_first_json_object():
    assert rec.parse_first_json_object('{"a": 1}') == {"a": 1}
    assert rec.parse_first_json_object('Sure! {"a": 1} hope that helps') == {"a": 1}


def test_evaluate_model_output_mtg_accept():
    parsed = {"card_name": "Llanowar Elves", "set_code": "dom-en", "collector_number": "168/269", "confidence": 0.95}
    result, reason = rec.evaluate_model_output(parsed, MTG, 0.8)
    assert reason is None
    assert result == {"card_name": "Llanowar Elves", "set_code": "DOM", "collector_number": "168"}


def test_evaluate_model_output_mtg_rejects():
    unknown = {"card_name": "Unknown", "set_code": "Unknown", "collector_number": "Unknown"}
    low = {"card_name": "Shock", "confidence": 0.5}
    r, reason = rec.evaluate_model_output(low, MTG, 0.8)
    assert r == unknown and reason.startswith("confidence")
    r, reason = rec.evaluate_model_output({"card_name": None, "confidence": 0.99}, MTG, 0.8)
    assert r == unknown and "missing" in reason
    r, reason = rec.evaluate_model_output({"card_name": "Creature - Elf"}, MTG, 0.8)
    assert r == unknown and "type line" in reason
    r, reason = rec.evaluate_model_output({"card_name": "Shock", "set_code": "M19"}, MTG, 0.8,
                                          ["set_code", "collector_number"])
    assert r == unknown and "collector_number" in reason
    # confidence missing -> name-only accepted; bare "name" accepted
    r, reason = rec.evaluate_model_output({"name": "Shock"}, MTG, 0.8)
    assert reason is None and r == {"card_name": "Shock", "set_code": "Unknown", "collector_number": "Unknown"}


def test_evaluate_model_output_fab():
    parsed = {"card_name": "Snatch", "card_id": "wtr-167", "pitch": "1", "confidence": 0.9}
    result, reason = rec.evaluate_model_output(parsed, FAB, 0.8)
    assert reason is None
    assert result == {"card_name": "Snatch", "card_id": "WTR167", "pitch": "1"}
    r, reason = rec.evaluate_model_output({"card_name": "Attack Action"}, FAB, 0.8)
    assert reason and r["card_name"] == "Unknown"


# ---- AWS extraction -----------------------------------------------------------

def old_aws_extract(top_lines, bottom_lines):
    """Read-Card.py before multi-game (detect_text_combined tail), verbatim."""
    import re
    card_name = " ".join(top_lines).strip() if top_lines else "Unknown"
    bottom_text = " ".join(bottom_lines)
    collector_match = re.search(r'[A-Za-z]?\s*(\d{3,4})', bottom_text)
    collector_number = collector_match.group(1) if collector_match else "Unknown"
    set_match = re.search(r'\b([A-Za-z]{3})\b', bottom_text)
    set_code = set_match.group(1).upper() if set_match else "Unknown"
    return card_name, collector_number, set_code


AWS_SAMPLES = [
    (["Llanowar Elves"], ["168/269 C", "DOM • EN Chris Rahn"]),
    (["Lightning", "Bolt"], ["R 0141", "m10 en"]),
    (["Shock"], ["U 0156", "M19 EN"]),          # set code w/ digit: regex needs 3 letters
    (["Sol Ring"], ["C 1234/2000 cmr"]),
    ([], ["no numbers here"]),
    (["Island"], []),
]


def test_aws_extract_matches_old_mtg_behaviour():
    for top, bottom in AWS_SAMPLES:
        new = rec.extract_aws_fields(top, bottom, MTG)
        name, collector, set_code = old_aws_extract(top, bottom)
        assert new["card_name"] == name
        assert new["set_code"] == set_code
        # old code kept raw digits ('0141'); the Scryfall lookup cleaned them to
        # '141' anyway, so the digits normalizer is lookup-equivalent.
        assert new["collector_number"] == rec.clean_collector_number(collector)
        assert rec.clean_set_code(new["set_code"]) == rec.clean_set_code(set_code)


def test_aws_extract_specific_values():
    r = rec.extract_aws_fields(["Lightning", "Bolt"], ["R 0141", "m10 en"], MTG)
    assert r == {"card_name": "Lightning Bolt", "set_code": "Unknown", "collector_number": "141"}
    r = rec.extract_aws_fields(["Llanowar Elves"], ["168/269 C", "DOM • EN"], MTG)
    assert r == {"card_name": "Llanowar Elves", "set_code": "DOM", "collector_number": "168"}


def test_aws_extract_group0_fallback_and_missing_pattern():
    game = {"recognition": {"keys": ["card_name", "card_id", "pitch"],
                            "normalize": {"card_id": "upper_alnum"},
                            "aws": {"patterns": {"card_id": r"[A-Z]{3}\d{3}"}}}}
    r = rec.extract_aws_fields(["Snatch"], ["WTR167 Rainbow Foil"], game)
    assert r == {"card_name": "Snatch", "card_id": "WTR167", "pitch": "Unknown"}
    r = rec.extract_aws_fields(["Snatch"], ["wtr - 167"], FAB)
    assert r["card_id"] == "WTR167"
