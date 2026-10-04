"""
Scryfall lookup (Magic: The Gathering).

Reads recognition keys card_name, set_code, collector_number and tries, in order:
  1) exact      /cards/<set>/<collector>          (needs set + collector)
  2) fuzzy_set  /cards/named?fuzzy=<name>&set=<set>   or  fuzzy (no set known)
  3) fuzzy_fallback /cards/named?fuzzy=<name>     (only after fuzzy_set failed)

Card dict keys are fixed (CSV export and bin matching depend on them):
  name, type, colors, color_identity, cmc, set_symbol, collector_number,
  card_identified_url
"""
import sys
import time

import requests

from recognition import UNKNOWN, clean_collector_number, clean_set_code, debug_log, is_unknown

SCRYFALL_TIMEOUT_SECONDS = 30
SCRYFALL_MAX_ATTEMPTS = 3
USER_AGENT = "bbg-tcg-sorter/1.0"
CARDS_URL = "https://api.scryfall.com/cards"
NAMED_URL = "https://api.scryfall.com/cards/named"


def _to_card(data, fallback_collector=UNKNOWN):
    return {
        "name": data.get("name", "Unknown"),
        "type": data.get("type_line", "Type not found"),
        "colors": data.get("colors", []),
        "color_identity": data.get("color_identity", data.get("colors", [])),
        "cmc": data.get("cmc", "CMC not found"),
        "set_symbol": data.get("set", "Set not found"),
        "collector_number": data.get("collector_number", fallback_collector),
        "card_identified_url": data.get("image_uris", {}).get("normal", "")
    }


def lookup(game, recognition, dbg=False):
    recognition = recognition or {}
    return fetch_card_info(
        recognition.get("card_name"),
        recognition.get("set_code"),
        recognition.get("collector_number"),
        dbg=dbg,
    )


def fetch_card_info(card_name, set_code, collector_number, dbg=False):
    if not card_name or is_unknown(card_name):
        return None, [{"stage": "skipped", "url": None, "status_code": None,
                       "details": "card_name unknown — Scryfall lookup skipped", "error": None}]

    scryfall_attempts = []

    def record_attempt(stage, url, status_code=None, details=None, error=None):
        scryfall_attempts.append({
            "stage": stage,
            "url": url,
            "status_code": status_code,
            "details": details,
            "error": error
        })

    def scryfall_get_with_retries(stage, url, params=None):
        last_error = None
        for attempt in range(1, SCRYFALL_MAX_ATTEMPTS + 1):
            try:
                debug_log(
                    dbg,
                    (
                        f"Scryfall request [{stage}] attempt {attempt}/{SCRYFALL_MAX_ATTEMPTS}: "
                        f"url={url} params={params}"
                    )
                )
                response = requests.get(url, params=params, timeout=SCRYFALL_TIMEOUT_SECONDS,
                                        headers={"User-Agent": USER_AGENT})
                return response, None
            except requests.RequestException as e:
                last_error = str(e)
                debug_log(
                    dbg,
                    (
                        f"Scryfall request [{stage}] attempt {attempt}/{SCRYFALL_MAX_ATTEMPTS} failed: "
                        f"{last_error}"
                    )
                )
                if attempt < SCRYFALL_MAX_ATTEMPTS:
                    sleep_seconds = attempt
                    debug_log(dbg, f"Scryfall retry sleep: {sleep_seconds}s")
                    time.sleep(sleep_seconds)
        return None, last_error

    def try_stage(stage, url, params=None, fallback_collector=UNKNOWN):
        """One lookup stage. Returns the card dict on HTTP 200, else None."""
        response, req_error = scryfall_get_with_retries(stage, url, params=params)
        if response is None:
            record_attempt(stage, url, error=req_error or "Unknown request error")
            return None
        # Fuzzy stages report the final URL (with query string), like before.
        attempt_url = getattr(response, "url", url) if params else url
        try:
            if response.status_code == 200:
                data = response.json()
                record_attempt(stage, attempt_url, status_code=response.status_code, details="ok")
                return _to_card(data, fallback_collector)
        except Exception as e:
            record_attempt(stage, attempt_url, status_code=response.status_code,
                           details="invalid JSON in 200 response", error=str(e))
            return None
        try:
            err = response.json()
            record_attempt(stage, attempt_url, status_code=response.status_code, details=err.get("details", ""))
            if stage == "exact":
                print(f"[SCRYFALL EXACT FAILED] status={response.status_code} details={err.get('details', '')}",
                      file=sys.stderr)
        except Exception:
            record_attempt(stage, attempt_url, status_code=response.status_code, details="non-json error body")
            if stage == "exact":
                print(f"[SCRYFALL EXACT FAILED] status={response.status_code}", file=sys.stderr)
        return None

    # Normalize inputs (idempotent if recognition already normalized them)
    set_code_clean = clean_set_code(set_code)
    collector_number_clean = clean_collector_number(collector_number)
    debug_log(
        dbg,
        (
            "Scryfall lookup inputs: "
            f"card_name='{card_name}', set_code_raw='{set_code}', collector_raw='{collector_number}', "
            f"set_code_clean='{set_code_clean}', collector_clean='{collector_number_clean}'"
        )
    )

    # 1) Best match: exact by set + collector number
    if set_code_clean != UNKNOWN and collector_number_clean != UNKNOWN:
        exact_url = f"{CARDS_URL}/{set_code_clean}/{collector_number_clean}"
        card = try_stage("exact", exact_url, fallback_collector=collector_number_clean)
        if card:
            return card, scryfall_attempts
        # If exact lookup fails (bad set/collector), fall through to fuzzy

    # 2) Fallback: fuzzy by name (optionally constrain by set)
    if set_code_clean != UNKNOWN:
        fuzzy_params = {"fuzzy": card_name, "set": set_code_clean}
        stage = "fuzzy_set"
    else:
        fuzzy_params = {"fuzzy": card_name}
        stage = "fuzzy"
    card = try_stage(stage, NAMED_URL, params=fuzzy_params)
    if card:
        return card, scryfall_attempts

    # 3) Last fallback: fuzzy without set constraint (if set-constrained fuzzy failed)
    if stage == "fuzzy_set":
        card = try_stage("fuzzy_fallback", NAMED_URL, params={"fuzzy": card_name})
        if card:
            return card, scryfall_attempts

    return None, scryfall_attempts
