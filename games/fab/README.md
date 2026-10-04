# Flesh and Blood game pack

- `game.json`: recognition prompt, lookup settings, bin filters
- `default-bins.json`: starting bin setup (1 attack actions, 2 attack reactions, 3 defense reactions + blocks, 4 non-attack actions, 5 instants, 6 equipment, 7 weapons, 8 heroes / allies / demi-heroes / mentors, 9 resources + tokens, 10 catch-all)
- `cards.json`: the local card database used by the `local_json` lookup

## Data source and attribution

Card data comes from **The FaB Cube's `flesh-and-blood-cards`** dataset
(https://github.com/the-fab-cube/flesh-and-blood-cards, `json/english/card.json` and
`set.json`, `develop` branch). It is maintained by Tyler ([luceleaftea](https://github.com/luceleaftea))
and contributors. The repo has no formal license file. Its README says: "Please feel free to clone or fork
the repo and generally use it for whatever projects you like."

Flesh and Blood™, card names, card text and card images are the property of Legend Story Studios.
This project isn't affiliated with or endorsed by Legend Story Studios. Card images aren't stored
here. `image_url` points to the images Legend Story Studios hosts publicly, as listed in the dataset.

## cards.json format

`{"source", "generated", "cards": [...]}`. There is one flat record for each distinct printed card id.
Foil, edition and alternate-art copies that share a printed id are merged into the most standard copy.
Each record has these fields:
`id` (for example `WTR115`), `name`, `pitch` (`"1"`/`"2"`/`"3"`/`""`), `types` (every type word,
including class and talent), `classes`, `talents`, `cost`, `set`, `rarity`, `image_url`, `type_text`.

The records are sorted in two ways:
- For each name and pitch, the first record is the earliest standard (non-promo, non-foil) printing.
- Back faces of double-sided cards come after all front faces, so a printed id resolves to the front face.

## Refreshing

```
python3 scripts/update_fab_cards.py              # latest develop branch
python3 scripts/update_fab_cards.py --ref v8.2.0 # a pinned release tag
```

The script needs only `requests`. It takes a few seconds on a Pi 4 and writes the file atomically.
If the output is ever larger than 8 MB, it writes `cards.json.gz` instead. In that case, update
`lookup.file` in `game.json` to match. When a new set adds a class or talent, the script prints a
note. Add it to `CLASSES`/`TALENTS` in the script and to the matching options in `game.json`, then run
`python3 -m pytest -q tests/test_games_fab.py`.
