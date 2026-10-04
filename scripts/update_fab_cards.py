#!/usr/bin/env python3
"""
Download the community Flesh and Blood card dataset and write games/fab/cards.json
for the generic `local_json` card lookup.

Source: https://github.com/the-fab-cube/flesh-and-blood-cards (json/english/card.json
and set.json). One flat record is written per distinct printed card id of each card
(foil / edition / alternate-art copies that share a printed id are collapsed into the
most standard one), ordered so that for every name+pitch the most standard, earliest
non-promo printing comes first, and so that the front face of a double-sided card comes
before its back face (both faces share one printed id).

Usage:
  python3 scripts/update_fab_cards.py                 # latest data from the develop branch
  python3 scripts/update_fab_cards.py --ref v8.2.0    # a pinned release tag / branch
  python3 scripts/update_fab_cards.py --source-dir DIR  # use already-downloaded card.json + set.json
"""
import argparse
import datetime
import gzip
import json
import os
import sys
import time

import requests

REPO = "the-fab-cube/flesh-and-blood-cards"
DEFAULT_REF = "develop"  # the repo's default branch; main lags behind and mixes image hosts
RAW_URL = "https://raw.githubusercontent.com/{repo}/{ref}/json/english/{name}.json"

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GAME_DIR = os.path.join(BASE_DIR, "games", "fab")
GZIP_THRESHOLD = 8 * 1024 * 1024  # write cards.json.gz instead when the plain file is larger

# Keep these in step with the "classes" / "talents" options in games/fab/game.json
# (tests/test_games_fab.py checks every value found in the data has an option).
CLASSES = [
    "Generic", "Ninja", "Assassin", "Brute", "Ranger", "Bard", "Guardian", "Runeblade",
    "Merchant", "Illusionist", "Warrior", "Necromancer", "Mechanologist", "Wizard", "Pirate",
    "Adjudicator", "Shapeshifter", "Thief",
]
TALENTS = [
    "Draconic", "Elemental", "Mystic", "Light", "Ice", "Earth", "Shadow", "Lightning",
    "Royal", "Chaos", "Revered", "Reviled",
]
# Card types (the word(s) after class/talent and before the " - " subtypes).
CARD_TYPES = {
    "Action", "Attack Reaction", "Defense Reaction", "Instant", "Equipment", "Weapon",
    "Hero", "Demi-Hero", "Token", "Resource", "Block", "Mentor", "Macro", "Event",
    "Companion", "Placeholder Card", "Ally",
}
# One-off words printed in front of a card type that are not classes or talents
# (e.g. "Puffin Companion", "Rosetta Macro", "Assassin / Ranger").
KNOWN_OTHER_PREFIXES = {
    "Arakni", "High Seas", "Invocation", "Omens of the Third Age", "Puffin", "Rosetta", "Scurv",
}
# Option values used by the "rarity" filter in game.json.
RARITIES = {
    "C": "Common", "R": "Rare", "S": "Super Rare", "M": "Majestic", "L": "Legendary",
    "F": "Fabled", "T": "Token", "B": "Basic", "V": "Marvel", "P": "Promo",
}
EDITION_ORDER = {"U": 0, "N": 1, "F": 2, "A": 3}  # Unlimited prints are the common copy


def fetch_json(name, ref, source_dir=None):
    if source_dir:
        with open(os.path.join(source_dir, f"{name}.json"), "r", encoding="utf-8") as f:
            return json.load(f)
    url = RAW_URL.format(repo=REPO, ref=ref, name=name)
    print(f"Downloading {url}")
    resp = requests.get(url, timeout=120)
    resp.raise_for_status()
    return resp.json()


def set_release_dates(sets):
    dates = {}
    for s in sets:
        found = [p.get("initial_release_date") for p in s.get("printings", []) if p.get("initial_release_date")]
        if found:
            dates[s["id"]] = min(found)
    return dates


def is_back_face(printing):
    info = printing.get("double_sided_card_info") or []
    return bool(info) and not any(x.get("is_front") for x in info)


def printing_rank(printing, dates):
    """Lower sorts first: standard rarity, no alternate art, non-foil, earliest set."""
    rarity = printing.get("rarity")
    return (
        rarity == "P",
        rarity == "V",
        bool(printing.get("art_variations")),
        printing.get("foiling") != "S",
        dates.get(printing.get("set_id")) or "9999",
        EDITION_ORDER.get(printing.get("edition"), 9),
        printing.get("id", ""),
    )


def clean_id(raw):
    return "".join(ch for ch in str(raw or "").upper() if ch.isalnum())


def build_records(cards, dates):
    unknown_prefix = {}
    rows = []  # (sort key, record)
    for card_index, card in enumerate(cards):
        types = [str(t) for t in card.get("types", [])]
        classes = [t for t in types if t in CLASSES]
        talents = [t for t in types if t in TALENTS]
        # Report type words in front of the " - " subtypes that are not a known class,
        # talent or card type: usually a new class/talent from a new set.
        before_dash = f" {card.get('type_text', '').split(' - ')[0]} "
        for t in types:
            if (f" {t} " in before_dash and t not in CLASSES and t not in TALENTS
                    and t not in CARD_TYPES and t not in KNOWN_OTHER_PREFIXES):
                unknown_prefix.setdefault(t, card["name"])

        best = {}  # printed id -> best printing
        for p in card.get("printings", []):
            pid = clean_id(p.get("id"))
            if not pid:
                continue
            if pid not in best or printing_rank(p, dates) < printing_rank(best[pid], dates):
                best[pid] = p

        for pid, p in best.items():
            record = {
                "id": pid,
                "name": card["name"],
                "pitch": str(card.get("pitch") or ""),
                "types": types,
                "classes": classes,
                "talents": talents,
                "cost": str(card.get("cost") or ""),
                "set": str(p.get("set_id") or pid[:3]).upper(),
                "rarity": RARITIES.get(p.get("rarity"), p.get("rarity") or ""),
                "image_url": p.get("image_url") or "",
                "type_text": card.get("type_text", ""),
            }
            # Back faces go after every front face so a printed id resolves to the front;
            # within a card, the most standard printing comes first.
            rows.append(((is_back_face(p), card_index, printing_rank(p, dates)), record))

    if unknown_prefix:
        print("Note: type words before the card type that are not a known class/talent "
              "(add to CLASSES/TALENTS and game.json if they are new ones):")
        for word, example in sorted(unknown_prefix.items()):
            print(f"  {word!r} (e.g. {example})")

    rows.sort(key=lambda r: r[0])
    return [r[1] for r in rows]


def write_cards(payload, out_dir, force_gzip=False):
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    use_gzip = force_gzip or len(data) > GZIP_THRESHOLD
    name = "cards.json.gz" if use_gzip else "cards.json"
    path = os.path.join(out_dir, name)
    tmp = path + ".tmp"
    os.makedirs(out_dir, exist_ok=True)
    if use_gzip:
        with gzip.open(tmp, "wb", compresslevel=9) as f:
            f.write(data)
    else:
        with open(tmp, "wb") as f:
            f.write(data)
    os.replace(tmp, path)  # atomic: a running sorter never sees a half-written file
    stale = os.path.join(out_dir, "cards.json" if use_gzip else "cards.json.gz")
    if os.path.exists(stale):
        print(f"Note: {stale} also exists; game.json's lookup.file decides which one is used.")
    return path, len(data)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ref", default=DEFAULT_REF, help=f"git branch or tag of {REPO} (default: {DEFAULT_REF})")
    ap.add_argument("--source-dir", help="read card.json and set.json from this folder instead of downloading")
    ap.add_argument("--out-dir", default=GAME_DIR, help="folder to write cards.json into (default: games/fab)")
    ap.add_argument("--gzip", action="store_true", help="always write cards.json.gz")
    args = ap.parse_args(argv)

    started = time.time()
    cards = fetch_json("card", args.ref, args.source_dir)
    sets = fetch_json("set", args.ref, args.source_dir)
    records = build_records(cards, set_release_dates(sets))
    payload = {
        "source": f"https://github.com/{REPO} ({args.ref if not args.source_dir else 'local copy'}: json/english/card.json)",
        "generated": datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat(),
        "cards": records,
    }
    path, size = write_cards(payload, args.out_dir, args.gzip)
    print(f"Wrote {len(records)} printings of {len(cards)} cards to {path} "
          f"({size / 1024 / 1024:.1f} MB uncompressed) in {time.time() - started:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
