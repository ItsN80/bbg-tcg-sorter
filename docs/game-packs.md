# Game packs

A **game pack** teaches the sorter a new trading card game, or a variation of one
you already have, without changing any code. A pack is a `.zip` file holding a
few JSON files that describe:

- how to recognise a card from the camera image (the AI prompt and checks),
- where to look up full card details (Scryfall, or a card list inside the pack),
- which filters each bin offers on the Bin Settings page, and how they match.

**Packs never contain code.** The sorter only accepts `.json`, `.json.gz`,
`.csv` and `.md` files and rejects any other file type, so a pack can't run
anything on your Pi.

> **Tip:** the easiest way to start is to export a game you already have
> (Settings → Games → **Export**), unzip it, change `"id"` and `"name"`, edit
> it, zip it up again and upload it.

## Pack layout

```
my-game.zip
└── my-game/              (optional single top-level folder; its name is ignored)
    ├── game.json         required: the game definition
    ├── cards.json.gz     required for "local_json" lookup: card data (name it anything)
    ├── default-bins.json optional: starter bin filters
    └── README.md         optional: notes for whoever installs the pack
```

The files can sit at the root of the zip or inside **one** top-level folder.
Nested folders, extra files, hidden files (such as `.DS_Store` or a `__MACOSX`
folder) and duplicate names are rejected. The game is installed into
`storage/games/<id>/`, where `<id>` comes from `game.json`.

> **macOS users:** Finder's *Compress* adds a `__MACOSX` folder, which makes the
> pack fail to upload. Build the zip in Terminal instead:
> `cd my-game && zip -X ../my-game.zip *`

## game.json reference

Here's a complete example with comments. JSON doesn't allow comments, so leave
out everything after `//` in a real file.

```jsonc
{
  "schema_version": 1,                 // always 1
  "id": "mygame",                      // 1-32 chars: a-z 0-9 - _ ; must be unique
  "name": "My Card Game",              // shown on the game pill and in Settings

  "recognition": { ... },              // see "Recognition" below (prompt is required)

  "lookup": {                          // where full card details come from
    "type": "local_json",              // "scryfall" (Magic only) or "local_json"
    "file": "cards.json.gz"            // local_json only: data file in the pack
    // ...more local_json options below
  },

  "display": {                         // card keys shown in the Live Sorting view
    "name": "name",
    "set": "set_code",
    "number": "collector_number",
    "image": "image_url"
  },
  "placeholder_image": "https://...",  // optional image shown before the first card

  "filters": [ ... ],                  // the bin filters, see below (at least one)
  "summary": ["name", "types"]         // optional: filter keys shown in the bin list
                                       // summary, in order (default: all filters)
}
```

### Filters

Each filter is one input on the Bin Settings page. The value a user enters for a
bin is saved under the filter's `key`. When a card is scanned, it goes to the
first bin where **every** filter that has a value matches. A bin with no
filters set never matches, so unmatched cards end up in bin 10.

| Field | Required | Meaning |
|---|---|---|
| `key` | yes | Letters, digits or `_`. Unique within the game. Saved in `bin-info-<id>.json`. |
| `label` | no | Text shown on the form. Defaults to `key`. |
| `kind` | yes | `text`, `number`, `multi` (checkboxes) or `name_range`. |
| `field` | no | Card-data key the filter reads. Defaults to `key`. |
| `fallback_field` | no | Card key to use when `field` is missing on a card. |
| `match` | no | How it matches, see the next table. Defaults per kind: `text` → `contains_ci`, `number` → `number_equals`, `multi` → `any_of`, `name_range` → `name_range`. |
| `options` | multi only | Non-empty list of checkboxes; see below. |
| `columns` | no | Columns in the checkbox grid. |
| `maxlength` | no | Max length for a text input. |
| `empty_token` | no | `exact_set` only: the option value meaning "card has no values" (for example `"C"` for colourless). |
| `empty_is_zero` | no | `number_equals` only: whether a card with no value counts as 0 (default `true`). FaB sets `false` so heroes and equipment don't match "cost 0". |
| `legacy_key` | no | An older saved key to migrate values from. |
| `summary_prefix` | no | Text put in front of the value in the bin summary, for example `"CMC "`. |
| `summary_values` | no | `true`: summarise checkboxes by their value (`WU`) rather than their label. |
| `summary_joiner` | no | Separator between checkbox values in the summary. |
| `summary_upper` | no | `true`: upper-case the value in the summary. |

**Match modes**

| `match` | Matches when… | Example |
|---|---|---|
| `name_range` | The bin value is a letter range like `a-c` and the card's first letter is in that range, or else the value appears anywhere in the name | `a-m`, `goblin` |
| `contains_ci` | The bin text appears in the card value (ignoring case) | set `khm` |
| `equals_ci` | The bin text equals the card value (ignoring case) | rarity `rare` |
| `number_equals` | The numbers are equal; a non-numeric card value never matches | cost `3` |
| `all_substr` | Every ticked option appears in the card value | MTG type line: Legendary + Creature |
| `any_of` | At least one ticked option matches the card's value or list of values | class: Warrior or Ninja |
| `exact_set` | The ticked options are exactly the card's set of values | MTG colours: exactly W+U |

**Options** (for `multi` filters) can be plain strings, or objects:

```json
"options": [
  "Action",
  {"value": "R", "label": "Red", "swatch": "#F09595"},
  {"value": "W", "label": "White", "icon": "https://.../W.svg"}
]
```

`value` may only contain letters, digits, spaces and `_ . + -`. With `any_of`,
an option can also carry `requires` and `excludes`: lists of card values that
must all be present, or must all be absent, for the option to match (ignoring
case). Without `requires`, the option simply needs its own `value`.

```json
{"key": "card_type", "label": "Type", "kind": "multi", "field": "types", "match": "any_of",
 "options": [
   {"value": "attack_action", "label": "Attack action", "requires": ["Action", "Attack"]},
   {"value": "non_attack_action", "label": "Non-attack action",
    "requires": ["Action"], "excludes": ["Attack"]},
   "Equipment"
 ]}
```

### Recognition

The `recognition` block controls how a camera image becomes a set of card keys
(the "recognition keys") before the lookup.

```jsonc
"recognition": {
  "prompt": "You are identifying a ... card. Return ONLY JSON with keys card_name, card_id, pitch, confidence ...",
  "keys": ["card_name", "card_id", "pitch"],   // keys the prompt returns; the FIRST is the
                                               // card name (default: card_name, set_code,
                                               // collector_number)
  "normalize": {                               // optional clean-up per key (others are just trimmed)
    "card_id": "upper_alnum",                  // see the normalizer table below
    "pitch": "int"
  },
  "set_code": {"min_length": 3, "max_length": 5},   // settings for the set_code normalizer
  "reject_name_words": ["action", "equipment"],     // reject a "name" that is really a type line
  "require_when_filtered": {                   // if any bin sets one of these filters...
    "filters": ["set_symbol"],
    "keys": ["set_code", "collector_number"]   // ...these keys must be read, not "Unknown"
  },
  "aws": {                                     // only used by the AWS text-reading provider
    "patterns": {                              // regex per key, run on the text read from the
      "collector_number": "[A-Za-z]?\\s*(\\d{3,4})",  // bottom of the card; group 1 (else the
      "set_code": "\\b([A-Za-z]{3})\\b"               // whole match) is the value
    },
    "camera_crop": {}                          // optional default crop regions
  }
}
```

| Normalizer | Result | Examples |
|---|---|---|
| `set_code` | Upper-case first letters-and-digits token, cut to `max_length`. Shorter than `min_length` or all digits → Unknown. | `blc-en` → `BLC`, `304` → Unknown |
| `digits` | First run of digits, without leading zeros | `A123` → `123`, `0123/287` → `123` |
| `upper_alnum` | Upper-case, keeping only A-Z and 0-9 | `wtr-001` → `WTR001` |
| `int` | First whole number | `Pitch 3` → `3`, `01` → `1` |

`reject_name_words` rejects a name that equals one of the words, or whose part
before ` - ` or ` — ` is one of them. For example, `Creature - Elf` is rejected
when `creature` is in the list.

Only `prompt` is required. You can override the prompt and the AWS crop for each
game in Settings without editing the pack.

### Lookup

- **`scryfall`**: looks cards up on scryfall.com. Only useful for Magic.
- **`local_json`**: looks cards up in a data file shipped in the pack, with no
  internet access needed:

```jsonc
"lookup": {
  "type": "local_json",
  "file": "cards.json.gz",          // required: data file in the pack (.json or .json.gz)
  "id_field": "card_id",            // record field with the printed id; a string or a
                                    // list of ids (default "id")
  "name_field": "name",             // record field with the card name (default "name")
  "id_key": "card_id",              // recognition key holding the printed id (optional)
  "name_key": "card_name",          // recognition key holding the name (default "card_name")
  "match_keys": {"pitch": "pitch"}, // {recognition key: record field} to tell apart
                                    // cards that share a name (optional)
  "fuzzy_cutoff": 0.85,             // 0-1: how close a misread name may be (default 0.85)
  "id_pattern": "([A-Z0-9]{3}\\d{3})" // regex (group 1) to find the id inside a longer
                                    // read, tried if the full id misses (optional)
}
```

How a card is found:

1. If `id_key` was read, look for an exact id match. Both sides are compared
   upper-cased with A-Z and 0-9 only, so `wtr-001` matches `WTR001`. If that
   misses and `id_pattern` is set, the pattern picks the id out of the read
   (FaB: `WTR115-T` → `WTR115`). When several records share an id (double-sided
   cards), the one whose name was read wins; otherwise the first in the file.
2. Otherwise, look for an exact name match, ignoring case and extra spaces.
3. Otherwise, look for the closest name within `fuzzy_cutoff`.

At steps 2 and 3, the candidates are narrowed by every `match_keys` value that
was read. If the candidates still differ in a `match_keys` field (say, the same
name exists with pitch 1, 2 and 3 and the pitch wasn't read), the card counts as
unidentified rather than risking the wrong bin. If the candidates differ only in
other ways (reprints), the first one in the file wins.

**Data file format.** The data file is a JSON object with a `cards` list. Each
entry is one **printing**, so the same card in two sets is two records. Apart
from `id_field` and `name_field`, the fields are up to you: whatever your
filters' `field`s and `display` refer to. The matched record is the card the
filters see. Lists work well for multi-valued data:

```json
{
  "cards": [
    {"card_id": "WTR001", "name": "Rhinar, Reckless Rampage", "set_code": "WTR",
     "collector_number": "001", "types": ["Hero", "Brute"], "pitch": null,
     "image_url": "https://..."},
    {"card_id": "WTR002", "name": "Romping Club", "set_code": "WTR",
     "collector_number": "002", "types": ["Brute", "Weapon"], "pitch": null}
  ]
}
```

Save it with gzip (`gzip -k cards.json` creates `cards.json.gz`) to make the
pack much smaller.

## default-bins.json (optional)

Starter bin filters, used when the game has no saved bin settings yet. Keys are
bin numbers 1-10 and values map filter keys to values: strings for
`text`/`number`/`name_range` filters, and lists of option values for `multi`
filters. Anything that doesn't match a filter or option is ignored.

```json
{
  "1": {"card_type": ["attack_action"]},
  "2": {"name": "a-m"},
  "9": {"pitch": "3"}
}
```

## Uploading, exporting and deleting

Go to **Settings → Games**:

- **Upload** a `.zip`. The sorter checks the whole pack before installing it.
  If anything is wrong, it shows the problem and changes nothing. Uploading a
  pack whose `id` is already installed asks whether to replace that game. A
  built-in game's id (such as `mtg`) can never be reused, so change the id to
  make your own version.
- **Export** downloads any game, built-in or uploaded, as a pack. Its
  `game.json` is exactly the file on disk.
- **Delete** removes an uploaded game. Built-in games can't be deleted. The
  game's saved bin settings (`storage/bin-info-<id>.json`) are listed so you can
  remove them too.

## Limits

| Limit | Value |
|---|---|
| Zip file size | 50 MB |
| Total unpacked size (also applies to a gzipped data file once decompressed) | 200 MB |
| Entries in the zip | 20 |
| Allowed files | `game.json`, `default-bins.json`, `README.md`, and the `lookup.file` data file |
| Allowed extensions | `.json`, `.json.gz`, `.csv`, `.md` |
| Text encoding | UTF-8 without a byte-order mark |

Packs are also rejected for absolute paths, `..` in paths, backslashes,
symbolic links, password protection, and nested folders.
