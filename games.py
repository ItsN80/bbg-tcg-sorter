#!/usr/bin/env python3
"""
Game registry and generic bin-matching engine.

Each supported trading card game is described by a declarative game.json
(no code), found in:
  games/<id>/game.json          built-in games shipped with the program
  storage/games/<id>/game.json  game packs uploaded from the Settings page

This module is imported by both Basic-Website.py and scripts/Read-Card.py, so
it must stay free of Flask / hardware imports.

game.json shape (schema_version 1):
{
  "schema_version": 1,
  "id": "mtg",                      # [a-z0-9_-], unique
  "name": "Magic: The Gathering",   # shown on the game pill
  "recognition": {...},             # prompt + validation, used by Read-Card.py
  "lookup": {"type": "scryfall"},   # one of LOOKUP_TYPES, plus type-specific keys
  "display": {                      # which card keys the Live Sorting view shows
      "name": "name", "set": "set_symbol",
      "number": "collector_number", "image": "card_identified_url"
  },
  "placeholder_image": "...",       # optional image URL shown before the first card
  "filters": [ ...see FILTER_KINDS / MATCH_MODES below... ],
  "summary": ["name", ...]          # optional: filter keys shown in the bin list summary
}

A filter:
  {"key": "type_tags",          # key stored in bin-info-<game>.json (and form field name)
   "label": "Type",
   "kind": "multi",             # text | number | multi | name_range
   "field": "type",             # card key the filter reads (defaults to key)
   "match": "all_substr",       # see MATCH_MODES
   "options": ["Legendary", {"value": "1", "label": "Red", "swatch": "#F09595"}, ...],
   "columns": 2,                # optional: checkbox grid columns
   "maxlength": 3,              # optional: text input max length
   "empty_token": "C",          # exact_set only: option meaning "card has no values"
   "empty_is_zero": false,      # number_equals only: card with no value != 0 (default true, as MTG)
   "legacy_key": "type",        # optional: older bin-info key to migrate from
   "summary_prefix": "CMC ",    # optional: prefix in the bin list summary
   "summary_values": true,      # optional: summarize multi options by value, not label
   "summary_joiner": "",        # optional: separator between multi options in the summary
   "summary_upper": true}       # optional: upper-case value in the bin list summary

Multi options may carry "requires": [...] and/or "excludes": [...]: lists of
card values that must all be present / must all be absent (case-insensitive)
for the option to match, e.g. FaB "Attack action" = requires Action + Attack.
"""
import json
import os
import re
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BUILTIN_GAMES_DIR = os.path.join(BASE_DIR, "games")
USER_GAMES_DIR = os.path.join(BASE_DIR, "storage", "games")
DEFAULT_GAME_ID = "mtg"
BIN_COUNT = 10
SCHEMA_VERSION = 1

GAME_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
LOOKUP_TYPES = {"scryfall", "local_json"}
FILTER_KINDS = {"text", "number", "multi", "name_range"}
MATCH_MODES = {
    "name_range",   # "a" / "a-c" first-letter range, otherwise substring of the name
    "contains_ci",  # criteria text is a case-insensitive substring of the card value
    "equals_ci",    # case-insensitive equality
    "number_equals",# numeric equality (non-numeric card value never matches)
    "all_substr",   # every selected option appears in the card value (e.g. MTG type line)
    "any_of",       # at least one selected option matches the card value/list
    "exact_set",    # selected options equal the card's value set (e.g. MTG colors)
}
# Which match modes make sense for each filter kind.
KIND_MATCHES = {
    "text": {"contains_ci", "equals_ci", "name_range"},
    "number": {"number_equals", "equals_ci"},
    "multi": {"all_substr", "any_of", "exact_set"},
    "name_range": {"name_range", "contains_ci", "equals_ci"},
}
# Kept in sync with scripts/recognition.py NORMALIZERS (tested).
NORMALIZER_NAMES = {"set_code", "digits", "upper_alnum", "int"}
DEFAULT_MATCH = {
    "text": "contains_ci",
    "number": "number_equals",
    "multi": "any_of",
    "name_range": "name_range",
}


# ---------------------------------------------------------------------------
# Loading / validation
# ---------------------------------------------------------------------------

def _option_value(opt):
    return str(opt.get("value", opt.get("label", ""))) if isinstance(opt, dict) else str(opt)


def option_values(flt):
    return [_option_value(o) for o in flt.get("options", [])]


def _normalize_filter(flt):
    """Fill in defaults so the rest of the code can rely on every key existing."""
    flt = dict(flt)
    flt.setdefault("label", flt["key"])
    flt.setdefault("field", flt["key"])
    flt.setdefault("match", DEFAULT_MATCH.get(flt.get("kind"), "contains_ci"))
    if flt.get("kind") == "multi":
        options = []
        for opt in flt.get("options", []):
            if isinstance(opt, dict):
                opt = dict(opt)
                opt["value"] = _option_value(opt)
                opt.setdefault("label", opt["value"])
            else:
                opt = {"value": str(opt), "label": str(opt)}
            options.append(opt)
        flt["options"] = options
    return flt


def validate_game(defn):
    """Returns a list of human-readable problems; empty list means valid."""
    errors = []
    if not isinstance(defn, dict):
        return ["game.json must be a JSON object"]

    if defn.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    gid = defn.get("id")
    if not isinstance(gid, str) or not GAME_ID_RE.match(gid):
        errors.append("id must be 1-32 characters of lowercase letters, digits, '-' or '_'")
    if not isinstance(defn.get("name"), str) or not defn.get("name", "").strip():
        errors.append("name is required")

    lookup = defn.get("lookup")
    if not isinstance(lookup, dict) or lookup.get("type") not in LOOKUP_TYPES:
        errors.append(f"lookup.type must be one of: {', '.join(sorted(LOOKUP_TYPES))}")
    elif lookup["type"] == "local_json":
        f = lookup.get("file")
        if (not isinstance(f, str) or not f or "/" in f or "\\" in f or f.startswith(".")
                or not (f.endswith(".json") or f.endswith(".json.gz"))):
            errors.append("lookup.file must be a plain .json or .json.gz file name inside the game folder")
        if "match_keys" in lookup and not (isinstance(lookup["match_keys"], dict) and
                                           all(isinstance(k, str) and isinstance(v, str)
                                               for k, v in lookup["match_keys"].items())):
            errors.append("lookup.match_keys must map recognition keys to card fields (strings)")
        cutoff = lookup.get("fuzzy_cutoff", 0.85)
        if isinstance(cutoff, bool) or not isinstance(cutoff, (int, float)) or not 0 < cutoff <= 1:
            errors.append("lookup.fuzzy_cutoff must be a number between 0 and 1")
        for k in ("id_field", "name_field", "id_key", "name_key"):
            if k in lookup and not isinstance(lookup[k], str):
                errors.append(f"lookup.{k} must be a string")
        if "id_pattern" in lookup:
            try:
                re.compile(lookup["id_pattern"])
            except (TypeError, re.error):
                errors.append("lookup.id_pattern is not a valid regular expression")

    recognition = defn.get("recognition")
    if not isinstance(recognition, dict) or not isinstance(recognition.get("prompt"), str):
        errors.append("recognition.prompt is required")
        recognition = {}
    keys = recognition.get("keys", [])
    if not isinstance(keys, list) or not all(isinstance(k, str) and k for k in keys):
        errors.append("recognition.keys must be a list of strings")
    normalize = recognition.get("normalize", {})
    if not isinstance(normalize, dict) or any(v not in NORMALIZER_NAMES for v in normalize.values()):
        errors.append(f"recognition.normalize values must be one of: {', '.join(sorted(NORMALIZER_NAMES))}")
    words = recognition.get("reject_name_words", [])
    if not isinstance(words, list) or not all(isinstance(w, str) for w in words):
        errors.append("recognition.reject_name_words must be a list of strings")
    patterns = recognition.get("aws", {}).get("patterns", {}) if isinstance(recognition.get("aws", {}), dict) else None
    if not isinstance(patterns, dict):
        errors.append("recognition.aws.patterns must be an object")
    else:
        for k, pat in patterns.items():
            try:
                re.compile(pat)
            except (TypeError, re.error):
                errors.append(f"recognition.aws.patterns.{k} is not a valid regular expression")

    display = defn.get("display", {})
    if not isinstance(display, dict) or not all(isinstance(v, str) for v in display.values()):
        errors.append("display must map name/set/number/image to card keys (strings)")

    filters = defn.get("filters")
    if not isinstance(filters, list) or not filters:
        errors.append("filters must be a non-empty list")
        filters = []
    seen = set()
    for i, flt in enumerate(filters):
        where = f"filters[{i}]"
        if not isinstance(flt, dict):
            errors.append(f"{where} must be an object")
            continue
        key = flt.get("key")
        if not isinstance(key, str) or not re.match(r"^[A-Za-z0-9_]+$", key):
            errors.append(f"{where}.key must be letters, digits or '_'")
        elif key in seen:
            errors.append(f"{where}.key '{key}' is duplicated")
        seen.add(key)
        kind = flt.get("kind")
        if kind not in FILTER_KINDS:
            errors.append(f"{where}.kind must be one of: {', '.join(sorted(FILTER_KINDS))}")
        match = flt.get("match", DEFAULT_MATCH.get(kind))
        if match not in MATCH_MODES:
            errors.append(f"{where}.match must be one of: {', '.join(sorted(MATCH_MODES))}")
        elif kind in KIND_MATCHES and match not in KIND_MATCHES[kind]:
            errors.append(f"{where}.match '{match}' can't be used with kind '{kind}' "
                          f"(use one of: {', '.join(sorted(KIND_MATCHES[kind]))})")
        for k in ("field", "fallback_field", "label", "legacy_key"):
            if k in flt and not isinstance(flt[k], str):
                errors.append(f"{where}.{k} must be a string")
        if kind == "multi":
            opts = flt.get("options")
            if not isinstance(opts, list) or not opts:
                errors.append(f"{where}.options must be a non-empty list")
            else:
                for o in opts:
                    if isinstance(o, dict):
                        for k in ("requires", "excludes"):
                            if k in o and not (isinstance(o[k], list) and all(isinstance(x, str) for x in o[k])):
                                errors.append(f"{where}.options '{_option_value(o)}': {k} must be a list of strings")
                    elif not isinstance(o, str):
                        errors.append(f"{where}.options must be strings or objects")
                values = [_option_value(o) for o in opts]
                if len(set(values)) != len(values):
                    errors.append(f"{where}.options contain duplicate values")
                bad = [v for v in values if not re.match(r"^[A-Za-z0-9_ .+-]+$", v)]
                if bad:
                    errors.append(f"{where}.options have unsupported characters: {bad[:3]}")

    summary = defn.get("summary")
    if summary is not None and (not isinstance(summary, list) or any(k not in seen for k in summary)):
        errors.append("summary must list existing filter keys")
    return errors


def _load_game_dir(path, builtin):
    game_file = os.path.join(path, "game.json")
    with open(game_file, "r", encoding="utf-8") as f:
        defn = json.load(f)
    errors = validate_game(defn)
    if errors:
        raise ValueError(f"{game_file}: " + "; ".join(errors))
    defn["filters"] = [_normalize_filter(flt) for flt in defn["filters"]]
    defn.setdefault("summary", [flt["key"] for flt in defn["filters"]])
    defn.setdefault("display", {})
    defn["_dir"] = path
    defn["_builtin"] = builtin
    return defn


def load_game_dir(path, builtin):
    """Loads and validates one game folder (raises ValueError if invalid)."""
    return _load_game_dir(path, builtin)


def load_games():
    """Returns {game_id: definition}. Built-in games load first and win on id clashes.
    A broken pack is skipped (and logged) rather than taking the whole UI down."""
    games = {}
    for root, builtin in ((BUILTIN_GAMES_DIR, True), (USER_GAMES_DIR, False)):
        if not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root)):
            if name.startswith("."):  # e.g. game_packs.py staging folders
                continue
            path = os.path.join(root, name)
            if not os.path.isfile(os.path.join(path, "game.json")):
                continue
            try:
                defn = _load_game_dir(path, builtin)
            except Exception as e:
                print(f"Skipping game pack {path}: {e}", file=sys.stderr)
                continue
            if defn["id"] in games:
                print(f"Skipping game pack {path}: id '{defn['id']}' already loaded", file=sys.stderr)
                continue
            games[defn["id"]] = defn
    return games


def get_game(game_id=None, games=None, strict=False):
    """Returns the requested game. Non-strict (the web UI) falls back to the
    default game so a removed pack can't break the page; strict (Read-Card.py)
    raises KeyError so a card is never looked up as the wrong game."""
    games = games if games is not None else load_games()
    game = games.get(game_id or DEFAULT_GAME_ID)
    if game is None and strict:
        raise KeyError(f"game '{game_id}' is not installed or failed to load")
    return game or games.get(DEFAULT_GAME_ID) or next(iter(games.values()))


def game_file_path(game, name):
    """Absolute path of a data file inside a game's folder (no path traversal)."""
    if not name or os.path.basename(name) != name:
        raise ValueError(f"invalid game file name: {name!r}")
    return os.path.join(game["_dir"], name)


def game_settings(config, game_id):
    """Per-game overrides saved from the Settings page: config["games"][<id>]."""
    games_cfg = (config or {}).get("games")
    entry = games_cfg.get(game_id) if isinstance(games_cfg, dict) else None
    return entry if isinstance(entry, dict) else {}


def resolve_prompt(game, config):
    """Recognition prompt: per-game override > legacy ollama.prompt (MTG only,
    from before multi-game) > the game's built-in prompt."""
    override = (game_settings(config, game["id"]).get("prompt") or "").strip()
    if override:
        return override
    if game["id"] == DEFAULT_GAME_ID:
        legacy = ((config or {}).get("ollama", {}).get("prompt") or "").strip()
        if legacy:
            return legacy
    return game["recognition"]["prompt"]


def resolve_camera_crop(game, config):
    """AWS crop regions: per-game override > legacy camera_crop (MTG only) >
    the game's recognition.aws.camera_crop > {} (Read-Card.py defaults)."""
    crop = game_settings(config, game["id"]).get("camera_crop")
    if isinstance(crop, dict) and crop:
        return crop
    if game["id"] == DEFAULT_GAME_ID and isinstance((config or {}).get("camera_crop"), dict):
        return config["camera_crop"]
    return game.get("recognition", {}).get("aws", {}).get("camera_crop", {}) or {}


def required_recognition_keys(game, criteria_by_box):
    """Recognition keys that must be read (not Unknown) because a bin filters on
    something only they can pin down, e.g. MTG set filter -> set_code + collector."""
    rule = game.get("recognition", {}).get("require_when_filtered") or {}
    if any_box_requires(criteria_by_box, rule.get("filters", [])):
        return list(rule.get("keys", []))
    return []


# Games the hosted DO Serverless recognition service has a server-side prompt for.
# Mirrors the server's allowlist: it answers 400 "unsupported game" for anything
# else and never accepts client prompts, so uploaded packs need direct Ollama/AWS.
DO_SERVERLESS_GAMES = {"mtg", "fab"}


def do_serverless_supports(game):
    return bool(game.get("_builtin")) and game["id"] in DO_SERVERLESS_GAMES


def unsupported_provider_message(game, config):
    """Why the active game can't be recognized with the configured provider, or None."""
    provider = ((config or {}).get("recognition_provider") or "aws").lower().strip()
    if provider == "do_serverless" and not do_serverless_supports(game):
        return (f"The hosted DO Serverless service doesn't support {game['name']} yet. "
                f"Switch Recognition Provider to Ollama (direct) or Amazon Rekognition in Settings.")
    return None


def public_game(game):
    """The definition without private keys, safe to hand to templates / JSON."""
    return {k: v for k, v in game.items() if not k.startswith("_")}


# ---------------------------------------------------------------------------
# Bin criteria
# ---------------------------------------------------------------------------

def empty_criteria(game):
    return {flt["key"]: ([] if flt["kind"] == "multi" else "") for flt in game["filters"]}


def default_box_criteria(game):
    return {i: empty_criteria(game) for i in range(1, BIN_COUNT + 1)}


def normalize_criteria_value(game, raw_value):
    if not isinstance(raw_value, dict):
        raw_value = {}
    out = {}
    for flt in game["filters"]:
        key = flt["key"]
        if flt["kind"] == "multi":
            raw = raw_value.get(key)
            if raw is None and flt.get("legacy_key"):
                # Legacy migration: older bins stored a single combined string
                # (e.g. MTG "Legendary Creature") — split it into recognized options.
                raw = str(raw_value.get(flt["legacy_key"], "")).split()
            if not isinstance(raw, list):
                raw = []
            raw = {str(v) for v in raw}
            out[key] = [v for v in option_values(flt) if v in raw]
        else:
            raw = raw_value.get(key)
            if raw is None and flt.get("legacy_key"):
                raw = raw_value.get(flt["legacy_key"])
            out[key] = str(raw if raw is not None else "").strip()
    return out


def normalize_box_criteria(game, raw):
    normalized = default_box_criteria(game)
    if not isinstance(raw, dict):
        return normalized
    for raw_key, raw_value in raw.items():
        try:
            box_num = int(raw_key)
        except (TypeError, ValueError):
            continue
        if box_num < 1 or box_num > BIN_COUNT or not isinstance(raw_value, dict):
            continue
        normalized[box_num] = normalize_criteria_value(game, raw_value)
    return normalized


def form_field_name(flt, bin_index, option_value=None):
    """HTML form field name for a filter input. Shared by card_form.html and
    criteria_from_form so the two can never drift apart."""
    if option_value is None:
        return f"f_{flt['key']}_{bin_index}"
    return f"f_{flt['key']}__{option_value}_{bin_index}"


def criteria_from_form(game, form, bin_index):
    raw = {}
    for flt in game["filters"]:
        if flt["kind"] == "multi":
            raw[flt["key"]] = [v for v in option_values(flt) if form.get(form_field_name(flt, bin_index, v))]
        else:
            raw[flt["key"]] = form.get(form_field_name(flt, bin_index), "")
    return normalize_criteria_value(game, raw)


def is_criteria_set(crit):
    return any(v for v in (crit or {}).values())


def summarize_criteria(game, crit):
    by_key = {flt["key"]: flt for flt in game["filters"]}
    parts = []
    for key in game.get("summary", []):
        flt = by_key.get(key)
        value = (crit or {}).get(key)
        if not flt or not value:
            continue
        if flt["kind"] == "multi":
            labels = {} if flt.get("summary_values") else {o["value"]: o["label"] for o in flt["options"]}
            joiner = flt.get("summary_joiner", " " if flt["match"] == "all_substr" else ", ")
            text = joiner.join(labels.get(v, v) for v in value)
        else:
            text = value.upper() if flt.get("summary_upper") else value
        parts.append(flt.get("summary_prefix", "") + text)
    return " · ".join(parts)


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def _as_list(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(v) for v in value if v is not None and str(v) != ""]
    s = str(value)
    return [s] if s != "" else []


def _match_name_range(crit_value, card_value):
    name_crit = crit_value.strip()
    card_name = str(card_value or "").strip()
    if "-" in name_crit:
        parts = name_crit.split("-")
        if len(parts) == 2 and len(parts[0].strip()) == 1 and len(parts[1].strip()) == 1:
            start_letter = parts[0].strip().upper()
            end_letter = parts[1].strip().upper()
            if not card_name:
                return False
            first_letter = card_name[0].upper()
            return start_letter <= first_letter <= end_letter
    return name_crit.lower() in card_name.lower()


def _option_matches(opt, card_values_lower):
    requires = opt.get("requires")
    excludes = opt.get("excludes")
    if requires is None:
        requires = [opt["value"]]
    if not all(str(r).lower() in card_values_lower for r in requires):
        return False
    if excludes and any(str(x).lower() in card_values_lower for x in excludes):
        return False
    return True


def _match_filter(flt, crit_value, card):
    field = flt["field"]
    card_value = card.get(field)
    if card_value is None and flt.get("fallback_field"):
        card_value = card.get(flt["fallback_field"])
    match = flt["match"]

    if match == "name_range":
        return _match_name_range(crit_value, card_value)
    if match == "contains_ci":
        return crit_value.lower() in str(card_value or "").lower()
    if match == "equals_ci":
        return crit_value.strip().lower() == str(card_value or "").strip().lower()
    if match == "number_equals":
        if card_value in (None, "") and not flt.get("empty_is_zero", True):
            return False  # e.g. FaB: a hero has no cost, which is not "cost 0"
        try:
            return float(crit_value) == float(card_value if card_value not in (None, "") else 0)
        except (TypeError, ValueError):
            return False
    if match == "all_substr":
        haystack = " ".join(_as_list(card_value)).lower()
        return all(v.lower() in haystack for v in crit_value)
    if match == "any_of":
        card_values = {v.lower() for v in _as_list(card_value)}
        opts = {o["value"]: o for o in flt["options"]}
        return any(_option_matches(opts.get(v, {"value": v}), card_values) for v in crit_value)
    if match == "exact_set":
        crit_set = set(crit_value)
        card_set = set(_as_list(card_value))
        empty_token = flt.get("empty_token")
        if empty_token and crit_set == {empty_token}:
            return not card_set
        return crit_set == card_set
    return False


def matches_criteria(game, card, criteria):
    """True if the card satisfies every filter set on this bin. A bin with no
    filters set never matches (unmatched cards fall through to bin 10)."""
    if not is_criteria_set(criteria):
        return False
    for flt in game["filters"]:
        value = criteria.get(flt["key"])
        if not value:
            continue
        if not _match_filter(flt, value, card):
            return False
    return True


def any_box_requires(criteria_by_box, keys):
    """True if any bin sets a filter on one of `keys` (e.g. set code), so
    recognition should insist on reading that part of the card."""
    return any((crit or {}).get(k) for crit in criteria_by_box for k in keys)
