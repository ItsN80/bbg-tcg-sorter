"""
Generic offline lookup against a data file shipped in the game's folder.

Data file (.json or .json.gz, detected by content):
    {"cards": [ {flat record}, ... ]}      one record per printing, any keys

game.json "lookup" keys:
    type          "local_json"
    file          data file name inside the game folder (required)
    id_field      record key holding the printed card id, string or list  (default "id")
    name_field    record key holding the card name                       (default "name")
    id_key        recognition key holding the printed id, e.g. "card_id" (optional)
    name_key      recognition key holding the card name                  (default "card_name")
    match_keys    {recognition key: record field} used to narrow same-name
                  candidates, e.g. {"pitch": "pitch"}                     (optional)
    fuzzy_cutoff  difflib cutoff for fuzzy name matching                 (default 0.85)
    id_pattern    regex (group 1) picking the id out of a longer read, tried when
                  the full id misses, e.g. FaB "WTR115T" (from "WTR115-T") -> WTR115

Algorithm:
  1) id_key recognized -> exact id match (both sides upper-cased, A-Z0-9 only),
     then via id_pattern. Records sharing an id (e.g. double-sided cards)
     prefer the one whose name matches the recognized name.
  2) exact (casefolded) name match, narrowed by known match_keys values
  3) fuzzy name match (difflib), narrowed the same way
  Candidates that still differ in a match_keys field -> ambiguous -> None.
  Candidates that only differ by printing -> first in file order.

Returned card = copy of the record + "matched_by" ("id" | "name" | "fuzzy").
Never raises; every step is reported in the attempts list.
"""
import difflib
import gzip
import json
import os
import re

import games
from recognition import clean_upper_alnum, debug_log, is_unknown

# Parsed data files + indexes, keyed by (path, mtime, size, id_field, name_field).
# Read-Card.py runs once per card so this mostly helps tests / long-lived callers.
_CACHE = {}
_NON_ALNUM = re.compile(r"[^A-Z0-9]")


def _norm_name(value):
    return " ".join(str(value).casefold().split())


def _norm_value(value):
    s = str(value).strip().casefold()
    try:
        f = float(s)
        if f == int(f):
            return str(int(f))
    except (ValueError, OverflowError):
        pass
    return s


def _read_data_file(path):
    with open(path, "rb") as f:
        raw = f.read()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return json.loads(raw)


class _Index:
    def __init__(self, cards, id_field, name_field):
        self.cards = cards
        self.by_id = {}
        self.by_name = {}
        for i, rec in enumerate(cards):
            ids = rec.get(id_field)
            if not isinstance(ids, list):
                ids = [ids]
            for card_id in ids:
                if card_id is None:
                    continue
                # Same result as clean_upper_alnum(), inlined: this runs per record.
                n = _NON_ALNUM.sub("", str(card_id).upper())
                if n:
                    ids_for = self.by_id.setdefault(n, [])
                    if i not in ids_for:
                        ids_for.append(i)
            name = rec.get(name_field)
            if name is not None and str(name).strip():
                self.by_name.setdefault(_norm_name(name), []).append(i)
        self.names = list(self.by_name)


def load_index(path, id_field="id", name_field="name"):
    st = os.stat(path)
    cache_key = (path, st.st_mtime_ns, st.st_size, id_field, name_field)
    idx = _CACHE.get(cache_key)
    if idx is None:
        data = _read_data_file(path)
        cards = data.get("cards") if isinstance(data, dict) else None
        if not isinstance(cards, list):
            raise ValueError('data file must be {"cards": [ ... ]}')
        cards = [c for c in cards if isinstance(c, dict)]
        idx = _Index(cards, id_field, name_field)
        _CACHE.clear()
        _CACHE[cache_key] = idx
    return idx


def _field_values(rec, field):
    value = rec.get(field)
    values = value if isinstance(value, list) else [value]
    return {_norm_value(v) for v in values if v is not None and str(v) != ""}


def _narrow(idx, indices, recognition, match_keys):
    """Keep candidates whose match fields agree with the recognized values.
    Returns (candidates, applied) where applied is a list of 'key=value'."""
    candidates = list(indices)
    applied = []
    for key, field in match_keys.items():
        value = recognition.get(key)
        if is_unknown(value):
            continue
        want = _norm_value(value)
        candidates = [i for i in candidates if want in _field_values(idx.cards[i], field)]
        applied.append(f"{key}={value}")
    return candidates, applied


def _ambiguous_fields(idx, candidates, match_keys):
    """match_keys fields whose values still differ between candidates."""
    differing = {}
    for key, field in match_keys.items():
        seen = {tuple(sorted(_field_values(idx.cards[i], field))) for i in candidates}
        if len(seen) > 1:
            differing[key] = sorted(",".join(v) or "(none)" for v in seen)
    return differing


def _resolve(idx, stage, name, indices, recognition, match_keys, attempts):
    candidates, applied = _narrow(idx, indices, recognition, match_keys)
    narrowed = f" narrowed by {', '.join(applied)}" if applied else ""
    if not candidates:
        attempts.append({"stage": stage, "details":
                         f"{len(indices)} record(s) named '{name}' but none{narrowed}", "error": None})
        return None
    differing = _ambiguous_fields(idx, candidates, match_keys)
    if differing:
        desc = "; ".join(f"{k} in {vals} (not recognized)" for k, vals in differing.items())
        attempts.append({"stage": stage, "details":
                         f"ambiguous: {len(candidates)} records named '{name}'{narrowed} differ by {desc}",
                         "error": None})
        return None
    card = dict(idx.cards[candidates[0]])
    card["matched_by"] = stage
    attempts.append({"stage": stage, "details":
                     f"ok: '{name}' ({len(candidates)} candidate printing(s){narrowed})", "error": None})
    return card


def lookup(game, recognition, dbg=False):
    attempts = []
    recognition = recognition or {}
    try:
        cfg = game.get("lookup") or {}
        id_field = cfg.get("id_field") or "id"
        name_field = cfg.get("name_field") or "name"
        id_key = cfg.get("id_key")
        name_key = cfg.get("name_key") or "card_name"
        match_keys = cfg.get("match_keys") or {}
        if not isinstance(match_keys, dict):
            match_keys = {}
        try:
            cutoff = float(cfg.get("fuzzy_cutoff", 0.85))
        except (TypeError, ValueError):
            cutoff = 0.85

        path = games.game_file_path(game, cfg.get("file"))
        idx = load_index(path, id_field, name_field)
        debug_log(dbg, f"local_json: {len(idx.cards)} records, {len(idx.names)} names from {path}")

        # 1) Exact printed id
        if id_key:
            raw_id = recognition.get(id_key)
            if not is_unknown(raw_id):
                card_id = clean_upper_alnum(raw_id)
                indices = idx.by_id.get(card_id)
                if indices is None and cfg.get("id_pattern"):
                    m = re.search(cfg["id_pattern"], card_id)
                    if m:
                        card_id = m.group(1) if m.groups() else m.group(0)
                        indices = idx.by_id.get(card_id)
                if indices:
                    i = indices[0]
                    raw_name = recognition.get(name_key)
                    if len(indices) > 1 and not is_unknown(raw_name):
                        # Shared id (double-sided card / token pair): prefer the
                        # face whose name was read; otherwise the front (first).
                        want = _norm_name(raw_name)
                        i = next((j for j in indices
                                  if _norm_name(idx.cards[j].get(name_field, "")) == want), i)
                    card = dict(idx.cards[i])
                    card["matched_by"] = "id"
                    attempts.append({"stage": "id", "details": f"ok: {card_id}", "error": None})
                    return card, attempts
                attempts.append({"stage": "id", "details": f"no record with id {card_id}", "error": None})
            else:
                attempts.append({"stage": "id", "details": f"{id_key} not recognized", "error": None})

        raw_name = recognition.get(name_key)
        if is_unknown(raw_name):
            attempts.append({"stage": "skipped", "details": f"{name_key} unknown — name lookup skipped",
                             "error": None})
            return None, attempts
        name = _norm_name(raw_name)

        # 2) Exact name
        indices = idx.by_name.get(name)
        if indices:
            return _resolve(idx, "name", raw_name, indices, recognition, match_keys, attempts), attempts
        attempts.append({"stage": "name", "details": f"no exact name match for '{raw_name}'", "error": None})

        # 3) Fuzzy name
        close = difflib.get_close_matches(name, idx.names, n=1, cutoff=cutoff)
        if not close:
            attempts.append({"stage": "fuzzy", "details":
                             f"no name within cutoff {cutoff} of '{raw_name}'", "error": None})
            return None, attempts
        debug_log(dbg, f"local_json: fuzzy '{raw_name}' -> '{close[0]}'")
        card = _resolve(idx, "fuzzy", close[0], idx.by_name[close[0]], recognition, match_keys, attempts)
        return card, attempts
    except Exception as e:
        attempts.append({"stage": "error", "details": "local_json lookup failed",
                         "error": f"{type(e).__name__}: {e}"})
        return None, attempts
