"""
Card lookups: turn a recognition dict into a full card dict.

    lookup_card(game, recognition, dbg=False) -> (card_dict_or_None, attempts)

Dispatches on game["lookup"]["type"] (see games.LOOKUP_TYPES):
  scryfall    lookups/scryfall.py   (MTG, online)
  local_json  lookups/local_json.py (offline data file in the game folder)

`attempts` is a list of dicts with at least "stage", "details", "error",
reported back to the server as "lookup_responses" when no card is found.
Lookups never raise.
"""
import os
import sys

_SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO_ROOT = os.path.dirname(_SCRIPTS_DIR)
for _p in (_SCRIPTS_DIR, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def lookup_card(game, recognition, dbg=False):
    lookup_type = ((game or {}).get("lookup") or {}).get("type")
    try:
        if lookup_type == "scryfall":
            from . import scryfall
            return scryfall.lookup(game, recognition, dbg=dbg)
        if lookup_type == "local_json":
            from . import local_json
            return local_json.lookup(game, recognition, dbg=dbg)
    except Exception as e:  # defensive: a lookup must never crash Read-Card.py
        return None, [{"stage": "error", "details": f"{lookup_type} lookup crashed",
                       "error": f"{type(e).__name__}: {e}"}]
    return None, [{"stage": "dispatch", "details": None,
                   "error": f"unsupported lookup type: {lookup_type!r}"}]
