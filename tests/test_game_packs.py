"""Tests for game_packs.py: installing, exporting and deleting uploaded game packs."""
import gzip
import io
import json
import os
import stat
import struct
import sys
import warnings
import zipfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import games  # noqa: E402
import game_packs  # noqa: E402
from game_packs import PackError  # noqa: E402

MTG_DIR = os.path.join(games.BUILTIN_GAMES_DIR, "mtg")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def minimal_game(gid="testgame", name="Test Game", data_file="cards.json"):
    return {
        "schema_version": 1,
        "id": gid,
        "name": name,
        "recognition": {"prompt": "Identify the card. Return JSON {card_name}."},
        "lookup": {"type": "local_json", "file": data_file},
        "display": {"name": "name"},
        "filters": [
            {"key": "name", "label": "Name", "kind": "name_range"},
            {"key": "kind", "label": "Kind", "kind": "multi", "field": "types",
             "options": ["Action", {"value": "attack", "label": "Attack action",
                                    "requires": ["Action", "Attack"]}]},
        ],
    }


CARDS = {"cards": [{"id": "T001", "name": "Alpha", "types": ["Action"]},
                   {"id": "T002", "name": "Beta", "types": ["Action", "Attack"]}]}


def as_bytes(value):
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8")
    return json.dumps(value).encode("utf-8")


def make_zip(files, prefix=""):
    """files: {name: bytes | str | json-able}. Returns zip bytes."""
    buf = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # duplicate-name test
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, value in (files.items() if isinstance(files, dict) else files):
                zf.writestr(prefix + name, as_bytes(value))
    return buf.getvalue()


def minimal_zip(prefix="", game=None, **extra):
    files = {"game.json": game or minimal_game(), "cards.json": CARDS}
    files.update(extra)
    return make_zip(files, prefix)


@pytest.fixture
def user_dir(tmp_path):
    d = tmp_path / "storage" / "games"
    d.mkdir(parents=True)
    return str(d)


@pytest.fixture
def builtins():
    return {"mtg": games._load_game_dir(MTG_DIR, builtin=True)}


def loaded(user_dir, builtins):
    """games_by_id as the app would see it: built-ins plus whatever is installed."""
    out = dict(builtins)
    for name in os.listdir(user_dir):
        path = os.path.join(user_dir, name)
        if os.path.isfile(os.path.join(path, "game.json")):
            g = games._load_game_dir(path, builtin=False)
            out.setdefault(g["id"], g)
    return out


def install(data, user_dir, games_by_id, **kw):
    return game_packs.install_pack(io.BytesIO(data), games_by_id, user_games_dir=user_dir, **kw)


# ---------------------------------------------------------------------------
# Successful installs
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("prefix", ["", "my-pack/"])
def test_install_minimal_pack(prefix, user_dir, builtins):
    gid = install(minimal_zip(prefix), user_dir, builtins)
    assert gid == "testgame"
    assert os.listdir(user_dir) == ["testgame"]
    assert sorted(os.listdir(os.path.join(user_dir, gid))) == ["cards.json", "game.json"]
    game = games._load_game_dir(os.path.join(user_dir, gid), builtin=False)
    assert game["name"] == "Test Game" and game["_builtin"] is False


def test_install_with_top_folder_dir_entry(user_dir, builtins):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("pack/", b"")
        zf.writestr("pack/game.json", as_bytes(minimal_game()))
        zf.writestr("pack/cards.json", as_bytes(CARDS))
    assert install(buf.getvalue(), user_dir, builtins) == "testgame"


def test_install_from_path(tmp_path, user_dir, builtins):
    p = tmp_path / "pack.zip"
    p.write_bytes(minimal_zip())
    assert game_packs.install_pack(str(p), builtins, user_games_dir=user_dir) == "testgame"


def test_install_full_pack_gz_bins_readme(user_dir, builtins):
    data = make_zip({
        "game.json": minimal_game(data_file="cards.json.gz"),
        "cards.json.gz": gzip.compress(as_bytes(CARDS)),
        "default-bins.json": {"1": {"name": "a-m"}, "2": {"kind": ["attack"]}},
        "README.md": "# Test game\n",
    })
    gid = install(data, user_dir, builtins)
    game = loaded(user_dir, builtins)[gid]
    summary = game_packs.pack_summary(game)
    assert summary == {
        "id": "testgame", "name": "Test Game", "builtin": False, "lookup_type": "local_json",
        "filter_count": 2, "data_file": "cards.json.gz",
        "data_file_bytes": os.path.getsize(os.path.join(user_dir, gid, "cards.json.gz")),
        "has_default_bins": True, "has_readme": True,
    }


def test_gzip_detected_by_content(user_dir, builtins):
    data = make_zip({"game.json": minimal_game(), "cards.json": gzip.compress(as_bytes(CARDS))})
    assert install(data, user_dir, builtins) == "testgame"


def test_game_json_stored_verbatim(user_dir, builtins):
    raw = b'{\n  "schema_version": 1, "id": "testgame", "name": "Test Game",\n' \
          b'  "recognition": {"prompt": "p"}, "lookup": {"type": "local_json", "file": "cards.json"},\n' \
          b'  "filters": [{"key": "name", "kind": "name_range"}]\n}\n'
    gid = install(make_zip({"game.json": raw, "cards.json": CARDS}), user_dir, builtins)
    with open(os.path.join(user_dir, gid, "game.json"), "rb") as f:
        assert f.read() == raw
    # Export re-reads the file, so it round-trips byte for byte.
    game = loaded(user_dir, builtins)[gid]
    with zipfile.ZipFile(io.BytesIO(game_packs.export_pack(game))) as zf:
        assert zf.read("testgame/game.json") == raw
        assert sorted(zf.namelist()) == ["testgame/cards.json", "testgame/game.json"]


def test_export_mtg_change_id_reinstall(user_dir, builtins):
    exported = game_packs.export_pack(builtins["mtg"])
    on_disk = sorted(n for n in ("game.json", "default-bins.json", "README.md")
                     if os.path.isfile(os.path.join(MTG_DIR, n)))
    with zipfile.ZipFile(io.BytesIO(exported)) as zf:
        assert sorted(zf.namelist()) == ["mtg/" + n for n in on_disk]
        contents = {n: zf.read("mtg/" + n) for n in on_disk}
    for n in on_disk:
        with open(os.path.join(MTG_DIR, n), "rb") as f:
            assert contents[n] == f.read()

    # Unchanged re-upload collides with the built-in.
    with pytest.raises(PackError, match="built-in"):
        install(exported, user_dir, builtins)
    assert os.listdir(user_dir) == []

    defn = json.loads(contents["game.json"])
    defn["id"] = "mtg-custom"
    defn["name"] = "My Magic"
    contents["game.json"] = defn
    gid = install(make_zip(contents, prefix="mtg/"), user_dir, builtins)
    assert gid == "mtg-custom"
    assert sorted(os.listdir(os.path.join(user_dir, gid))) == on_disk
    game = loaded(user_dir, builtins)[gid]
    assert game["name"] == "My Magic"
    assert game["filters"] == builtins["mtg"]["filters"]
    assert game_packs.pack_summary(game)["lookup_type"] == "scryfall"


def test_pack_summary_builtin(builtins):
    s = game_packs.pack_summary(builtins["mtg"])
    assert s["builtin"] is True and s["lookup_type"] == "scryfall"
    assert s["filter_count"] == 5 and s["data_file"] is None and s["data_file_bytes"] is None


# ---------------------------------------------------------------------------
# Replace / collisions
# ---------------------------------------------------------------------------

def test_uploaded_collision_requires_replace(user_dir, builtins):
    install(minimal_zip(**{"README.md": "old"}), user_dir, builtins)
    by_id = loaded(user_dir, builtins)
    with pytest.raises(PackError, match="already installed"):
        install(minimal_zip(game=minimal_game(name="New")), user_dir, by_id)
    assert os.listdir(user_dir) == ["testgame"]
    assert loaded(user_dir, builtins)["testgame"]["name"] == "Test Game"

    install(minimal_zip(game=minimal_game(name="New")), user_dir, by_id, replace=True)
    assert os.listdir(user_dir) == ["testgame"]
    assert loaded(user_dir, builtins)["testgame"]["name"] == "New"
    assert not os.path.exists(os.path.join(user_dir, "testgame", "README.md"))


def test_collision_with_folder_not_in_registry(user_dir, builtins):
    # e.g. a broken pack that load_games skipped still occupies the folder.
    os.mkdir(os.path.join(user_dir, "testgame"))
    with pytest.raises(PackError, match="already installed"):
        install(minimal_zip(), user_dir, builtins)
    install(minimal_zip(), user_dir, builtins, replace=True)
    assert os.path.isfile(os.path.join(user_dir, "testgame", "game.json"))


def test_failed_replace_keeps_old_version(user_dir, builtins):
    install(minimal_zip(), user_dir, builtins)
    by_id = loaded(user_dir, builtins)
    with pytest.raises(PackError):
        install(make_zip({"game.json": minimal_game(name="New")}), user_dir, by_id, replace=True)
    assert os.listdir(user_dir) == ["testgame"]
    assert loaded(user_dir, builtins)["testgame"]["name"] == "Test Game"


# ---------------------------------------------------------------------------
# Rejections: nothing may be left behind in user_games_dir
# ---------------------------------------------------------------------------

def raw_zip(entries):
    """entries: list of (ZipInfo-or-name, bytes)."""
    buf = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with zipfile.ZipFile(buf, "w") as zf:
            for info, data in entries:
                zf.writestr(info, data)
    return buf.getvalue()


def symlink_zip():
    info = zipfile.ZipInfo("cards.json")
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    return raw_zip([("game.json", as_bytes(minimal_game())), (info, b"/etc/passwd")])


def encrypted_zip():
    data = bytearray(minimal_zip())
    # Set the "encrypted" general-purpose flag bit in every local and central header.
    for sig, off in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        i = data.find(sig)
        while i != -1:
            (flags,) = struct.unpack_from("<H", data, i + off)
            struct.pack_into("<H", data, i + off, flags | 0x1)
            i = data.find(sig, i + 4)
    return bytes(data)


def lying_size_zip():
    """A member whose headers declare 10 bytes but which inflates to 5 MB."""
    big = b"0" * (5 * 1024 * 1024)
    data = bytearray(minimal_zip(**{"README.md": big}))
    with zipfile.ZipFile(io.BytesIO(bytes(data))) as zf:
        info = zf.getinfo("README.md")
    struct.pack_into("<I", data, info.header_offset + 22, 10)       # local header
    i = data.find(b"PK\x01\x02")
    while i != -1:
        (name_len,) = struct.unpack_from("<H", data, i + 28)
        if data[i + 46:i + 46 + name_len] == b"README.md":
            struct.pack_into("<I", data, i + 24, 10)                # central directory
        i = data.find(b"PK\x01\x02", i + 4)
    return bytes(data)


def bad_game(**changes):
    g = minimal_game()
    g.update(changes)
    return g


REJECTS = {
    "not a zip": (b"this is not a zip file", "not a valid .zip"),
    "dotdot": (make_zip({"game.json": minimal_game(), "cards.json": CARDS, "../evil.json": "{}"}), "invalid path"),
    "dotdot in folder": (make_zip({"game.json": minimal_game(), "x/../../evil.json": "{}"}), "invalid path"),
    "absolute": (make_zip({"/etc/game.json": minimal_game()}), "absolute"),
    "backslash": (raw_zip([("pack\\game.json", as_bytes(minimal_game()))]), "backslash"),
    "symlink": (symlink_zip(), "symbolic link"),
    "encrypted": (encrypted_zip(), "encrypted"),
    "unknown ext": (minimal_zip(**{"install.sh": "rm -rf /"}), "not an allowed file type"),
    "python file": (minimal_zip(**{"plugin.py": "import os"}), "never contain code"),
    "unexpected json": (minimal_zip(**{"extra.json": "{}"}), "Unexpected file"),
    "hidden file": (minimal_zip(**{".DS_Store.json": "{}"}), "hidden"),
    "missing game.json": (make_zip({"cards.json": CARDS}), "no game.json"),
    "missing data file": (make_zip({"game.json": minimal_game()}), "not in the zip"),
    "csv data file": (make_zip({"game.json": minimal_game(data_file="cards.csv"), "cards.csv": "a,b"}),
                      ".json or .json.gz"),
    "bad game json": (make_zip({"game.json": "{not json", "cards.json": CARDS}), "not valid JSON"),
    "bom game json": (make_zip({"game.json": b"\xef\xbb\xbf" + as_bytes(minimal_game()), "cards.json": CARDS}),
                      "byte-order mark"),
    "bad data json": (make_zip({"game.json": minimal_game(), "cards.json": "[1,"}), "not valid JSON"),
    "data not cards obj": (make_zip({"game.json": minimal_game(), "cards.json": [{"name": "x"}]}), '"cards" list'),
    "data card not obj": (make_zip({"game.json": minimal_game(), "cards.json": {"cards": ["x"]}}), "must be an object"),
    "data empty": (make_zip({"game.json": minimal_game(), "cards.json": {"cards": []}}), "no cards"),
    "data truncated gzip": (make_zip({"game.json": minimal_game(data_file="c.json.gz"),
                                      "c.json.gz": gzip.compress(as_bytes(CARDS))[:20]}), "decompressed"),
    "invalid schema": (minimal_zip(game=bad_game(filters=[])), "filters must be a non-empty list"),
    "bad lookup type": (minimal_zip(game=bad_game(lookup={"type": "python"})), "lookup.type"),
    "bad id": (minimal_zip(game=bad_game(id="Bad Id!")), "id must be"),
    "default bins list": (minimal_zip(**{"default-bins.json": [1, 2]}), "default-bins.json must be"),
    "default bins bad json": (minimal_zip(**{"default-bins.json": "{"}), "not valid JSON"),
    "nested folders": (make_zip({"game.json": minimal_game(), "cards.json": CARDS}, prefix="a/b/"), "nested"),
    "nested dir entry": (raw_zip([("a/b/", b""), ("game.json", as_bytes(minimal_game()))]), "nested"),
    "two top folders": (raw_zip([("a/game.json", as_bytes(minimal_game())), ("b/cards.json", as_bytes(CARDS))]),
                        "more than one top-level folder"),
    "root and folder": (raw_zip([("game.json", as_bytes(minimal_game())), ("a/cards.json", as_bytes(CARDS))]),
                        "mixes files"),
    "duplicate": (make_zip([("game.json", minimal_game()), ("cards.json", CARDS), ("cards.json", CARDS)]),
                  "more than once"),
    "too many members": (make_zip({f"n{i}.md": "x" for i in range(21)}), "at most 20"),
    "lying sizes": (lying_size_zip(), "declared size|corrupt|Could not extract"),
}


@pytest.mark.parametrize("case", sorted(REJECTS))
def test_rejects(case, user_dir, builtins):
    data, message = REJECTS[case]
    with pytest.raises(PackError, match=message):
        install(data, user_dir, builtins)
    assert os.listdir(user_dir) == []


def test_reject_builtin_id(user_dir, builtins):
    with pytest.raises(PackError, match="built-in"):
        install(minimal_zip(game=bad_game(id="mtg")), user_dir, builtins)
    with pytest.raises(PackError, match="built-in"):
        install(minimal_zip(game=bad_game(id="mtg")), user_dir, builtins, replace=True)
    assert os.listdir(user_dir) == []


def test_reject_zip_bomb_declared_size(user_dir, builtins):
    bomb = make_zip({"game.json": minimal_game(), "cards.json": CARDS, "README.md": b" " * (3 * 1024 * 1024)})
    assert len(bomb) < 64 * 1024  # highly compressible
    with pytest.raises(PackError, match="unpacks to more than"):
        install(bomb, user_dir, builtins, max_unpacked_bytes=1024 * 1024)
    assert os.listdir(user_dir) == []


def test_reject_gzip_bomb_in_data_file(user_dir, builtins):
    cards = {"cards": [{"name": "x" * 1000}] * 3000}  # ~3 MB of JSON, gzips to a few KB
    data = make_zip({"game.json": minimal_game(data_file="c.json.gz"), "c.json.gz": gzip.compress(as_bytes(cards))})
    with pytest.raises(PackError, match="unpacks to more than"):
        install(data, user_dir, builtins, max_unpacked_bytes=1024 * 1024)
    assert os.listdir(user_dir) == []


def test_reject_zip_too_large(user_dir, builtins):
    with pytest.raises(PackError, match="larger than"):
        install(minimal_zip(), user_dir, builtins, max_zip_bytes=100)
    assert os.listdir(user_dir) == []


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------

def test_delete_uploaded(user_dir, builtins):
    install(minimal_zip(), user_dir, builtins)
    storage = os.path.dirname(user_dir)
    bin_info = os.path.join(storage, "bin-info-testgame.json")
    with open(bin_info, "w") as f:
        f.write("{}")
    related = game_packs.delete_pack("testgame", loaded(user_dir, builtins), user_games_dir=user_dir)
    assert related == [bin_info]
    assert os.path.exists(bin_info)  # caller decides
    assert os.listdir(user_dir) == []


def test_delete_without_related_files(user_dir, builtins):
    install(minimal_zip(), user_dir, builtins)
    assert game_packs.delete_pack("testgame", loaded(user_dir, builtins), user_games_dir=user_dir) == []


def test_delete_refuses_builtin_and_unknown(user_dir, builtins):
    with pytest.raises(PackError, match="built-in"):
        game_packs.delete_pack("mtg", builtins, user_games_dir=user_dir)
    with pytest.raises(PackError, match="No uploaded game"):
        game_packs.delete_pack("nope", builtins, user_games_dir=user_dir)
    with pytest.raises(PackError, match="Invalid"):
        game_packs.delete_pack("../storage", builtins, user_games_dir=user_dir)
    assert os.path.isfile(os.path.join(MTG_DIR, "game.json"))
