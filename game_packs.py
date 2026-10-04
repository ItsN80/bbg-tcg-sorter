#!/usr/bin/env python3
"""
Game packs: install / export / delete user-uploadable game definitions.

A pack is a .zip containing only data (never code):
  game.json           required, see games.py for the schema
  default-bins.json   optional, starter bin criteria {"1": {...}, ...}
  <lookup.file>       required when lookup.type == "local_json": {"cards": [...]}
  README.md           optional, notes for the user

Files sit either at the zip root or inside exactly one top-level folder
(whose name is ignored; the folder installed is storage/games/<id>).

Stdlib only, no Flask: the web app calls these and turns PackError into a
flash message.
"""
import gzip
import io
import json
import os
import shutil
import stat
import tempfile
import zipfile
import zlib

import games

MAX_ZIP_BYTES = 50 * 1024 * 1024
MAX_UNPACKED_BYTES = 200 * 1024 * 1024
MAX_MEMBERS = 20
CHUNK = 64 * 1024

GAME_FILE = "game.json"
DEFAULT_BINS_FILE = "default-bins.json"
README_FILE = "README.md"
FIXED_FILES = (GAME_FILE, DEFAULT_BINS_FILE, README_FILE)
# Longest first so "x.json.gz" is classified as .json.gz, not .gz.
ALLOWED_EXTENSIONS = (".json.gz", ".json", ".csv", ".md")
LOCAL_JSON_EXTENSIONS = (".json.gz", ".json")
# Staging dirs start with "." and hold the pack one level down, so
# games.load_games() never mistakes a half-installed pack for a real one.
STAGING_PREFIX = ".pack-"


class PackError(Exception):
    """A user-facing problem with a game pack. str(e) is safe to show in the UI."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mb(n):
    return f"{n / (1024 * 1024):g} MB"


def _extension(name):
    lower = name.lower()
    for ext in ALLOWED_EXTENSIONS:
        if lower.endswith(ext):
            return ext
    return None


def _open_zip(src, max_zip_bytes):
    """Opens a path or a binary file object (e.g. werkzeug FileStorage) as a ZipFile,
    enforcing the compressed size limit before parsing anything."""
    too_big = PackError(f"The pack is larger than the {_mb(max_zip_bytes)} limit.")
    if isinstance(src, (str, bytes, os.PathLike)):
        try:
            size = os.path.getsize(src)
        except OSError as e:
            raise PackError(f"Could not read the uploaded file: {e}") from e
        if size > max_zip_bytes:
            raise too_big
        fileobj = src
    else:
        stream = getattr(src, "stream", src)
        try:
            pos = stream.tell()
            stream.seek(0, os.SEEK_END)
            size = stream.tell() - pos
            stream.seek(pos)
            fileobj = stream
        except (AttributeError, OSError, ValueError):
            # Not seekable: read at most limit+1 bytes into memory.
            data = stream.read(max_zip_bytes + 1)
            size = len(data)
            fileobj = io.BytesIO(data)
        if size > max_zip_bytes:
            raise too_big
    if size == 0:
        raise PackError("The uploaded file is empty.")
    try:
        return zipfile.ZipFile(fileobj)
    except (zipfile.BadZipFile, zlib.error, EOFError, ValueError) as e:
        raise PackError("The uploaded file is not a valid .zip archive.") from e


def _check_member_name(name):
    """Raises PackError for unsafe names; returns the path segments."""
    if not name:
        raise PackError("The zip contains an entry with an empty name.")
    if "\\" in name:
        raise PackError(f"'{name}' uses backslashes in its path; please re-create the zip "
                        "with a standard zip tool.")
    if name.startswith("/") or (len(name) > 1 and name[1] == ":"):
        raise PackError(f"'{name}' is an absolute path, which is not allowed in a pack.")
    if "\x00" in name:
        raise PackError("The zip contains an entry with an invalid name.")
    parts = name.rstrip("/").split("/")
    for part in parts:
        if part in ("", ".", ".."):
            raise PackError(f"'{name}' contains an invalid path segment ('..', '.' or '//').")
    return parts


def _check_member_type(info):
    if info.flag_bits & 0x1:
        raise PackError(f"'{info.filename}' is encrypted; password-protected packs are not supported.")
    mode = (info.external_attr >> 16) & 0xFFFF
    fmt = stat.S_IFMT(mode)
    if fmt == stat.S_IFLNK:
        raise PackError(f"'{info.filename}' is a symbolic link; packs may only contain regular files.")
    if fmt not in (0, stat.S_IFREG, stat.S_IFDIR):
        raise PackError(f"'{info.filename}' is not a regular file.")


def _scan_zip(zf, max_unpacked_bytes):
    """Validates the archive layout and returns {pack-relative name: ZipInfo}."""
    infos = zf.infolist()
    if len(infos) > MAX_MEMBERS:
        raise PackError(f"The zip has {len(infos)} entries; a pack may contain at most {MAX_MEMBERS}.")

    top_folders = set()
    files = []          # (relative name, info)
    for info in infos:
        _check_member_type(info)
        parts = _check_member_name(info.filename)
        if info.is_dir():
            if len(parts) > 1:
                raise PackError(f"'{info.filename}' is a nested folder; files must be at the zip root "
                                "or inside one top-level folder.")
            top_folders.add(parts[0])
            continue
        if len(parts) > 2:
            raise PackError(f"'{info.filename}' is in a nested folder; files must be at the zip root "
                            "or inside one top-level folder.")
        if len(parts) == 2:
            top_folders.add(parts[0])
        files.append((parts, info))

    if len(top_folders) > 1:
        raise PackError("The zip contains more than one top-level folder: "
                        + ", ".join(sorted(top_folders)) + ".")
    if top_folders and any(len(parts) == 1 for parts, _ in files):
        raise PackError("The zip mixes files at the root with files inside a folder; "
                        "put all pack files in one place.")

    members = {}
    seen_lower = set()
    for parts, info in files:
        rel = parts[-1]
        if rel in members or rel.lower() in seen_lower:
            raise PackError(f"The zip contains '{rel}' more than once.")
        seen_lower.add(rel.lower())
        if rel.startswith("."):
            raise PackError(f"'{info.filename}' is a hidden file; remove it from the zip "
                            "(macOS: use 'zip -X' or remove .DS_Store files).")
        if _extension(rel) is None:
            raise PackError(f"'{info.filename}' is not an allowed file type. Packs may only contain "
                            + ", ".join(ALLOWED_EXTENSIONS) + " files (packs never contain code).")
        members[rel] = info

    if sum(info.file_size for info in members.values()) > max_unpacked_bytes:
        raise PackError(f"The pack unpacks to more than {_mb(max_unpacked_bytes)}.")
    if GAME_FILE not in members:
        raise PackError("The zip has no game.json (it must be at the root or in one top-level folder).")
    return members


def _read_member(zf, info, limit, label):
    """Reads one member fully, enforcing `limit` while streaming (headers can lie)."""
    out = io.BytesIO()
    _stream_member(zf, info, out, limit, label)
    return out.getvalue()


def _stream_member(zf, info, out, limit, label):
    written = 0
    try:
        with zf.open(info) as src:
            while True:
                chunk = src.read(CHUNK)
                if not chunk:
                    break
                written += len(chunk)
                if written > limit or written > info.file_size:
                    raise PackError(f"{label} is larger than its declared size or the "
                                    f"{_mb(limit)} limit; the zip looks corrupt or malicious.")
                out.write(chunk)
    except PackError:
        raise
    except (zipfile.BadZipFile, zlib.error, EOFError, OSError, RuntimeError, NotImplementedError) as e:
        raise PackError(f"Could not extract {label}: {e}") from e
    return written


def _decode_json(data, label):
    if data.startswith(b"\xef\xbb\xbf"):
        raise PackError(f"{label} starts with a byte-order mark; save it as UTF-8 without BOM.")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise PackError(f"{label} is not valid UTF-8 text.") from e
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise PackError(f"{label} is not valid JSON (line {e.lineno}, column {e.colno}: {e.msg}).") from e


def _load_data_file(path, name, limit):
    """Parses a local_json data file and checks {"cards": [objects]}. Gzip is detected
    by content, like scripts/lookups/local_json.py does."""
    label = f"Data file '{name}'"
    try:
        with open(path, "rb") as f:
            is_gzip = f.read(2) == b"\x1f\x8b"
        with (gzip.open(path, "rb") if is_gzip else open(path, "rb")) as f:
            data = f.read(limit + 1)
    except (OSError, EOFError, zlib.error) as e:
        raise PackError(f"{label} could not be decompressed: {e}") from e
    if len(data) > limit:
        raise PackError(f"{label} unpacks to more than {_mb(limit)}.")
    doc = _decode_json(data, label)
    if not isinstance(doc, dict) or not isinstance(doc.get("cards"), list):
        raise PackError(f'{label} must be a JSON object with a "cards" list: {{"cards": [...]}}.')
    if not doc["cards"]:
        raise PackError(f"{label} contains no cards.")
    for i, card in enumerate(doc["cards"]):
        if not isinstance(card, dict):
            raise PackError(f"{label}: cards[{i}] must be an object.")
    return doc


def _is_direct_child(path, parent):
    return os.path.dirname(os.path.realpath(path)) == os.path.realpath(parent)


# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------

def install_pack(src, games_by_id, user_games_dir=None, max_zip_bytes=MAX_ZIP_BYTES,
                 max_unpacked_bytes=MAX_UNPACKED_BYTES, replace=False):
    """Validates and installs a pack zip (path or binary file object) into
    user_games_dir/<id>. Returns the game id. Raises PackError on any problem,
    leaving user_games_dir untouched. The caller should reload games afterwards."""
    user_games_dir = user_games_dir or games.USER_GAMES_DIR

    with _open_zip(src, max_zip_bytes) as zf:
        members = _scan_zip(zf, max_unpacked_bytes)

        game_bytes = _read_member(zf, members[GAME_FILE], max_unpacked_bytes, "game.json")
        defn = _decode_json(game_bytes, "game.json")
        errors = games.validate_game(defn)
        if errors:
            raise PackError("game.json has problems: " + "; ".join(errors) + ".")
        gid = defn["id"]
        lookup = defn["lookup"]

        expected = set(FIXED_FILES)
        data_file = None
        if lookup["type"] == "local_json":
            data_file = lookup["file"]
            if not data_file.lower().endswith(LOCAL_JSON_EXTENSIONS):
                raise PackError(f"lookup.file '{data_file}' must end in .json or .json.gz.")
            if data_file not in members:
                raise PackError(f"game.json says the card data is in '{data_file}', "
                                "but that file is not in the zip.")
            expected.add(data_file)
        unexpected = sorted(set(members) - expected)
        if unexpected:
            raise PackError(f"Unexpected file(s) in the pack: {', '.join(unexpected)}. Allowed: "
                            + ", ".join(sorted(expected)) + ".")

        # Id collisions (checked before touching the disk).
        existing = games_by_id.get(gid)
        if (existing and existing.get("_builtin")) or os.path.isdir(os.path.join(games.BUILTIN_GAMES_DIR, gid)):
            raise PackError(f"'{gid}' is the id of a built-in game. Change \"id\" in game.json "
                            "to install your own version alongside it.")
        final = os.path.join(user_games_dir, gid)
        old_dirs = []
        if existing:
            old_dirs.append(existing["_dir"])
        if os.path.lexists(final):
            old_dirs.append(final)
        if old_dirs and not replace:
            raise PackError(f"A game with id '{gid}' is already installed. Delete it first or "
                            "choose to replace it.")
        for d in old_dirs:
            if os.path.islink(d) or not _is_direct_child(d, user_games_dir):
                raise PackError(f"The installed game '{gid}' is not in the uploaded games folder "
                                "and cannot be replaced.")
        old_dirs = list(dict.fromkeys(os.path.realpath(d) for d in old_dirs))

        os.makedirs(user_games_dir, exist_ok=True)
        stage_root = tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=user_games_dir)
        try:
            staged = os.path.join(stage_root, gid)
            os.mkdir(staged)
            budget = max_unpacked_bytes
            for name, info in members.items():
                with open(os.path.join(staged, name), "wb") as out:
                    budget -= _stream_member(zf, info, out, budget, f"'{name}'")

            if data_file:
                _load_data_file(os.path.join(staged, data_file), data_file, max_unpacked_bytes)
            if README_FILE in members:
                with open(os.path.join(staged, README_FILE), "rb") as f:
                    try:
                        f.read().decode("utf-8")
                    except UnicodeDecodeError as e:
                        raise PackError("README.md is not valid UTF-8 text.") from e

            try:
                loaded = games.load_game_dir(staged, builtin=False)
            except Exception as e:  # validate_game passed, so this is unexpected
                raise PackError(f"game.json could not be loaded: {e}") from e

            if DEFAULT_BINS_FILE in members:
                with open(os.path.join(staged, DEFAULT_BINS_FILE), "rb") as f:
                    bins = _decode_json(f.read(), "default-bins.json")
                if not isinstance(bins, dict):
                    raise PackError('default-bins.json must be a JSON object like {"1": {...}, "2": {...}}.')
                try:
                    games.normalize_box_criteria(loaded, bins)
                except Exception as e:
                    raise PackError(f"default-bins.json could not be applied to this game: {e}") from e

            # Swap into place. Old versions are moved inside stage_root so the
            # finally block deletes them; on failure they are moved back.
            moved = []
            try:
                for i, d in enumerate(old_dirs):
                    aside = os.path.join(stage_root, f"old-{i}")
                    os.rename(d, aside)
                    moved.append((aside, d))
                os.rename(staged, final)
            except OSError as e:
                for aside, d in reversed(moved):
                    try:
                        os.rename(aside, d)
                    except OSError:
                        pass
                raise PackError(f"Could not install the pack: {e}") from e
        finally:
            shutil.rmtree(stage_root, ignore_errors=True)
    return gid


# ---------------------------------------------------------------------------
# Export / delete / summary
# ---------------------------------------------------------------------------

def _pack_files(game):
    """Names of the files in a game's folder that belong in its pack."""
    names = [GAME_FILE, DEFAULT_BINS_FILE, README_FILE]
    lookup = game.get("lookup") or {}
    if lookup.get("type") == "local_json" and lookup.get("file"):
        names.append(lookup["file"])
    out = []
    for name in names:
        path = games.game_file_path(game, name)
        if os.path.isfile(path) and not os.path.islink(path):
            out.append(name)
    return out


def export_pack(game):
    """Zip bytes for a loaded game (built-in or uploaded), files inside an <id>/ folder.
    game.json is the file as stored on disk, not the normalized definition."""
    files = _pack_files(game)
    if GAME_FILE not in files:
        raise PackError(f"Game '{game.get('id')}' has no game.json on disk.")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in files:
            compress = zipfile.ZIP_STORED if name.lower().endswith(".gz") else zipfile.ZIP_DEFLATED
            zf.write(games.game_file_path(game, name), f"{game['id']}/{name}", compress_type=compress)
    return buf.getvalue()


def delete_pack(game_id, games_by_id, user_games_dir=None):
    """Removes an uploaded game's folder. Refuses built-ins. Returns paths of related
    storage files that exist (e.g. storage/bin-info-<id>.json); the caller decides
    whether to delete them (and any config["games"][<id>] entry)."""
    user_games_dir = user_games_dir or games.USER_GAMES_DIR
    if not isinstance(game_id, str) or not games.GAME_ID_RE.match(game_id):
        raise PackError("Invalid game id.")
    game = games_by_id.get(game_id)
    if (game and game.get("_builtin")) or os.path.isdir(os.path.join(games.BUILTIN_GAMES_DIR, game_id)):
        raise PackError(f"'{game_id}' is a built-in game and cannot be deleted.")
    target = game["_dir"] if game else os.path.join(user_games_dir, game_id)
    if not os.path.isdir(target):
        raise PackError(f"No uploaded game with id '{game_id}'.")
    if os.path.islink(target) or not _is_direct_child(target, user_games_dir):
        raise PackError(f"'{game_id}' is not in the uploaded games folder and cannot be deleted.")

    trash_root = tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=user_games_dir)
    try:
        os.rename(target, os.path.join(trash_root, game_id))
    except OSError as e:
        shutil.rmtree(trash_root, ignore_errors=True)
        raise PackError(f"Could not delete '{game_id}': {e}") from e
    shutil.rmtree(trash_root, ignore_errors=True)

    storage_dir = os.path.dirname(os.path.abspath(user_games_dir))
    related = [os.path.join(storage_dir, f"bin-info-{game_id}.json")]
    return [p for p in related if os.path.exists(p)]


def pack_summary(game):
    """Small dict describing a game for the Settings -> Games list."""
    lookup = game.get("lookup") or {}
    data_file = lookup.get("file") if lookup.get("type") == "local_json" else None
    data_size = None
    if data_file:
        try:
            data_size = os.path.getsize(games.game_file_path(game, data_file))
        except (OSError, ValueError):
            data_size = None
    files = _pack_files(game) if game.get("_dir") else []
    return {
        "id": game.get("id"),
        "name": game.get("name"),
        "builtin": bool(game.get("_builtin")),
        "lookup_type": lookup.get("type"),
        "filter_count": len(game.get("filters") or []),
        "data_file": data_file,
        "data_file_bytes": data_size,
        "has_default_bins": DEFAULT_BINS_FILE in files,
        "has_readme": README_FILE in files,
    }
