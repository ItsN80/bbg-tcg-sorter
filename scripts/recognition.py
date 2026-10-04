#!/usr/bin/env python3
"""
Pure, camera-free recognition helpers used by Read-Card.py.

Everything here is driven by a game definition (games/<id>/game.json,
"recognition" section) so new games need no code changes:
  keys               recognition keys, first one is the card name key
  normalize          {key: normalizer name}, see NORMALIZERS
  set_code           {"min_length": 3, "max_length": 5} for the set_code normalizer
  reject_name_words  names that are really a type line (e.g. "Creature - Elf")
  aws.patterns       {key: regex} applied to the AWS bottom-crop text

No hardware / network imports, so this module is unit-testable anywhere.
"""
import json
import os
import re
import sys

UNKNOWN = "Unknown"
NULL_STRINGS = {"unknown", "null", "none", "n/a", ""}

DEFAULT_KEYS = ["card_name", "set_code", "collector_number"]


def debug_log(enabled, message):
    if enabled:
        print(f"[READ-CARD DEBUG] {message}", file=sys.stderr)


def is_unknown(value):
    """True for None / empty / 'Unknown' / 'null' / 'none' / 'n/a' (any case)."""
    if value is None:
        return True
    return str(value).strip().lower() in NULL_STRINGS


def recognition_keys(game):
    keys = (game or {}).get("recognition", {}).get("keys") or DEFAULT_KEYS
    return [str(k) for k in keys]


def name_key(game):
    return recognition_keys(game)[0]


def unknown_result(game):
    return {k: UNKNOWN for k in recognition_keys(game)}


# ---------------------------------------------------------------------------
# Normalizers
# ---------------------------------------------------------------------------

def clean_collector_number(raw) -> str:
    """
    Extracts the first digit group from a collector number string.
    Examples:
      'A123' -> '123'
      '123/287' -> '123'
      '0123' -> '123'
      '123a' -> '123'
    """
    if not raw:
        return UNKNOWN

    s = str(raw).strip()

    m = re.search(r'(\d+)', s)
    if not m:
        return UNKNOWN

    digits = m.group(1)

    # Normalize leading zeros: '000' -> '0', '0123' -> '123'
    try:
        return str(int(digits))
    except ValueError:
        return digits


def clean_set_code(raw, min_length=3, max_length=5) -> str:
    """
    Normalize set code strings coming from OCR/LLM.
    Examples:
      'BLC-EN' -> 'BLC'
      ' blc '  -> 'BLC'
      'BLC/EN' -> 'BLC'
      None/'Unknown' -> 'Unknown'
    """
    if not raw:
        return UNKNOWN

    s = str(raw).strip().upper()
    if s in ("UNKNOWN", "N/A", "NONE", "NULL", ""):
        return UNKNOWN

    # Keep only the first alphanumeric token (split on - / space etc.)
    token = re.split(r'[^A-Z0-9]+', s)[0].strip()

    # Scryfall set codes are typically 3–5 chars; keep within that range
    if len(token) < min_length:
        return UNKNOWN
    if len(token) > max_length:
        token = token[:max_length]

    # Reject all-digit tokens (common OCR/LLM artifact like "304")
    if not re.search(r"[A-Z]", token):
        return UNKNOWN

    return token


def clean_upper_alnum(raw) -> str:
    """
    Uppercase and keep only A-Z / 0-9.
    Examples:
      'ros-001' -> 'ROS001'
      ' WTR 1 ' -> 'WTR1'
      None/'n/a' -> 'Unknown'
    """
    if is_unknown(raw):
        return UNKNOWN
    s = re.sub(r"[^A-Z0-9]", "", str(raw).upper())
    return s or UNKNOWN


def clean_int(raw) -> str:
    """
    First integer in the value, as a string.
    Examples:
      '2' -> '2', 'Pitch 3' -> '3', '01' -> '1', 'red' -> 'Unknown'
    """
    if raw is None or isinstance(raw, bool):
        return UNKNOWN
    m = re.search(r"\d+", str(raw))
    if not m:
        return UNKNOWN
    return str(int(m.group(0)))


def _set_code_normalizer(raw, recognition_cfg):
    sc = (recognition_cfg or {}).get("set_code") or {}
    try:
        min_len = int(sc.get("min_length", 3))
        max_len = int(sc.get("max_length", 5))
    except (TypeError, ValueError):
        min_len, max_len = 3, 5
    return clean_set_code(raw, min_len, max_len)


NORMALIZERS = {
    "set_code": _set_code_normalizer,
    "digits": lambda raw, cfg: clean_collector_number(raw),
    "upper_alnum": lambda raw, cfg: clean_upper_alnum(raw),
    "int": lambda raw, cfg: clean_int(raw),
}


def normalize_value(game, key, raw):
    """Apply the game's normalizer for `key` (if any). Always returns a string;
    unreadable values become 'Unknown'."""
    if isinstance(raw, bool) or raw is None:
        return UNKNOWN
    if isinstance(raw, (int, float)):
        raw = str(raw)
    if not isinstance(raw, str):
        return UNKNOWN
    raw = raw.strip()
    if is_unknown(raw):
        return UNKNOWN
    rcfg = (game or {}).get("recognition", {})
    norm_name = (rcfg.get("normalize") or {}).get(key)
    if not norm_name:
        return raw
    fn = NORMALIZERS.get(norm_name)
    if fn is None:
        print(f"[READ-CARD WARNING] unknown normalizer '{norm_name}' for key '{key}'", file=sys.stderr)
        return raw
    return fn(raw, rcfg)


# ---------------------------------------------------------------------------
# Name rejection / required keys
# ---------------------------------------------------------------------------

MTG_TYPE_WORDS = [
    "artifact", "battle", "conspiracy", "creature", "dungeon",
    "emblem", "enchantment", "instant", "kindred", "land",
    "phenomenon", "plane", "planeswalker", "scheme", "sorcery",
    "tribal", "vanguard"
]


def reject_name(name, words) -> bool:
    """
    Heuristic filter for cases where the model reads the type line
    (e.g. "Creature - Human Wizard") instead of card name.
    True when the name equals one of `words`, or the part before
    " - " / " — " is one of them.
    """
    if not name or not words:
        return False

    n = name.strip().lower()
    type_words = {str(w).strip().lower() for w in words}

    # Typical type-line separators
    if " - " in n or " — " in n:
        first = re.split(r"\s[-—]\s", n, maxsplit=1)[0].strip()
        if first in type_words:
            return True

    # Also reject direct single-type outputs like "Creature"
    return n in type_words


def looks_like_type_line(name, words=None) -> bool:
    """Backwards-compatible MTG wrapper around reject_name."""
    return reject_name(name, MTG_TYPE_WORDS if words is None else words)


def _truthy(value):
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def required_keys_from_env(game, environ=None, legacy_default=False):
    """
    Keys that must be recognized (not Unknown) or the whole result is rejected.
      REQUIRE_RECOGNITION_KEYS=set_code,collector_number   (comma-separated)
      REQUIRE_SET_AND_COLLECTOR=1  legacy alias for set_code,collector_number
    `legacy_default` is used when REQUIRE_SET_AND_COLLECTOR is not set
    (config ollama.require_set_and_collector).
    Keys the game does not recognize are ignored.
    """
    environ = os.environ if environ is None else environ
    wanted = []
    for k in (environ.get("REQUIRE_RECOGNITION_KEYS") or "").split(","):
        k = k.strip()
        if k and k not in wanted:
            wanted.append(k)
    legacy = environ.get("REQUIRE_SET_AND_COLLECTOR")
    legacy_on = legacy_default if legacy is None else _truthy(legacy)
    if legacy_on:
        for k in ("set_code", "collector_number"):
            if k not in wanted:
                wanted.append(k)
    game_keys = recognition_keys(game)
    return [k for k in wanted if k in game_keys]


# ---------------------------------------------------------------------------
# Ollama model output
# ---------------------------------------------------------------------------

def parse_first_json_object(text: str):
    """
    Parse the first JSON object from a string.
    Handles model outputs that include prose before/after JSON.
    """
    if not isinstance(text, str):
        raise ValueError("Response text is not a string")

    s = text.strip()
    if not s:
        raise ValueError("Empty response text")

    decoder = json.JSONDecoder()

    # Fast path: pure JSON payload.
    try:
        return decoder.decode(s)
    except json.JSONDecodeError:
        pass

    # Fallback: scan for the first '{' and attempt raw_decode from there.
    for i, ch in enumerate(s):
        if ch != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(s[i:])
            return obj
        except json.JSONDecodeError:
            continue

    raise ValueError("No valid JSON object found in response")


def parse_model_fields(parsed, game):
    """Extract + normalize the game's recognition keys from the model's JSON.
    The name key also accepts a bare "name" field (as before)."""
    if not isinstance(parsed, dict):
        parsed = {}
    keys = recognition_keys(game)
    result = {}
    for i, key in enumerate(keys):
        raw = parsed.get(key)
        if i == 0 and not raw:
            raw = parsed.get("name")
        if i == 0:
            # Name: only strings are accepted; no normalizer by default.
            raw = raw if isinstance(raw, str) else None
        result[key] = normalize_value(game, key, raw)
    return result


def parse_confidence(parsed):
    raw = parsed.get("confidence") if isinstance(parsed, dict) else None
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def evaluate_model_output(parsed, game, min_confidence, required_keys=(), dbg=False):
    """
    Conservative accept/reject policy for an Ollama result.
    Returns (result_dict, reject_reason). On rejection result_dict is all Unknown.
    """
    confidence = parse_confidence(parsed)
    result = parse_model_fields(parsed, game)
    nkey = name_key(game)
    card_name = result[nkey]

    # Conservative fail policy: if confidence is present and low, reject.
    # If confidence is omitted, allow name-only matching.
    if confidence is not None and confidence < min_confidence:
        return unknown_result(game), f"confidence {confidence} < min_confidence {min_confidence}"
    if confidence is None:
        debug_log(dbg, "Confidence missing/unparseable; continuing with name-based matching")

    if is_unknown(card_name):
        return unknown_result(game), f"{nkey} missing/unknown"

    words = game.get("recognition", {}).get("reject_name_words") or []
    if reject_name(card_name, words):
        return unknown_result(game), f"{nkey} looks like a type line ('{card_name}')"

    missing = [k for k in required_keys if result.get(k, UNKNOWN) == UNKNOWN]
    if missing:
        return unknown_result(game), f"required recognition keys are Unknown: {', '.join(missing)}"

    return result, None


# ---------------------------------------------------------------------------
# AWS Rekognition text
# ---------------------------------------------------------------------------

def extract_aws_fields(top_lines, bottom_lines, game):
    """
    Name = joined top-crop lines; every other key is pulled from the joined
    bottom-crop text with recognition.aws.patterns[key] (group 1, else group 0),
    then normalized.
    """
    keys = recognition_keys(game)
    patterns = game.get("recognition", {}).get("aws", {}).get("patterns") or {}
    result = {}
    card_name = " ".join(top_lines or []).strip()
    result[keys[0]] = card_name if card_name else UNKNOWN

    bottom_text = " ".join(bottom_lines or [])
    for key in keys[1:]:
        pattern = patterns.get(key)
        value = None
        if pattern:
            try:
                m = re.search(pattern, bottom_text)
            except re.error as e:
                print(f"[READ-CARD WARNING] bad aws pattern for '{key}': {e}", file=sys.stderr)
                m = None
            if m:
                value = m.group(1) if m.re.groups >= 1 and m.group(1) is not None else m.group(0)
        result[key] = normalize_value(game, key, value)
    return result
