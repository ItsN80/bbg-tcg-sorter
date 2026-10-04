"""End-to-end tests of the Flask app's multi-game behaviour.

The real Basic-Website.py is loaded from a temporary copy of the repo with the
hardware modules (pigpio, led_controller) stubbed out, so these tests never
touch GPIO/LEDs, the live install, or this checkout's storage/ folder."""
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import types
import zipfile
from unittest import mock

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIVE_BIN_INFO = "/home/admin/bbg-tcg-sorter/storage/bin-info.json"


def _fake_hardware_modules():
    pigpio = types.ModuleType("pigpio")
    pigpio.INPUT, pigpio.PUD_UP = 0, 2
    pigpio.pi = lambda: mock.MagicMock(connected=True, read=mock.MagicMock(return_value=1))
    led = types.ModuleType("led_controller")
    state = {"enabled": False, "gpio": 18, "count": 0, "color": {"r": 0, "g": 140, "b": 255}}
    led.LEDController = lambda *a, **k: mock.MagicMock(get_state=mock.MagicMock(return_value=state))
    led.normalize_led_config = lambda raw: raw if isinstance(raw, dict) else {}
    return {"pigpio": pigpio, "led_controller": led}


@pytest.fixture
def web(tmp_path, monkeypatch):
    root = tmp_path / "sorter"
    root.mkdir()
    for name in ("Basic-Website.py", "games.py", "game_packs.py"):
        shutil.copy(os.path.join(REPO, name), root / name)
    shutil.copytree(os.path.join(REPO, "games"), root / "games")
    shutil.copytree(os.path.join(REPO, "templates"), root / "templates")
    (root / "static" / "images").mkdir(parents=True)
    (root / "counters").mkdir()
    (root / "storage").mkdir()
    shutil.copy(os.path.join(REPO, "storage", "config-default.json"), root / "storage" / "config-default.json")
    # A pre-multi-game MTG bin file, to exercise the migration.
    legacy = {"1": {"name": "", "type": "", "type_tags": ["Instant"], "colors": [], "cmc": "", "set_symbol": ""},
              "2": {"name": "", "type": "Legendary Creature", "colors": ["W", "U"], "cmc": "", "set_symbol": ""}}
    (root / "storage" / "bin-info.json").write_text(json.dumps(legacy))

    saved_modules = {k: sys.modules.get(k) for k in ("games", "game_packs", "pigpio", "led_controller")}
    for k in ("games", "game_packs"):
        sys.modules.pop(k, None)
    sys.modules.update(_fake_hardware_modules())
    monkeypatch.syspath_prepend(str(root))
    monkeypatch.chdir(root)

    spec = importlib.util.spec_from_file_location("sorter_web", root / "Basic-Website.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.app.config["TESTING"] = True
    client = module.app.test_client()
    with client.session_transaction() as sess:
        sess["logged_in"] = True
    try:
        yield module, client, root
    finally:
        for k, v in saved_modules.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


def test_index_renders_mtg_and_migrates_legacy_bins(web):
    module, client, root = web
    html = client.get("/").get_data(as_text=True)
    assert "Magic: The Gathering" in html
    assert 'name="f_type_tags__Instant_1"' in html
    assert 'name="f_colors__W_1"' in html and "svgs.scryfall.io/card-symbols/W.svg" in html
    migrated = json.loads((root / "storage" / "bin-info-mtg.json").read_text())
    assert migrated["1"]["type_tags"] == ["Instant"]
    assert migrated["2"]["type_tags"] == ["Legendary", "Creature"]
    assert migrated["2"]["colors"] == ["W", "U"]
    assert ">Instant<" in html                      # bin 1 summary
    assert "Legendary Creature · WU" in html        # bin 2 summary, old format
    assert (root / "storage" / "bin-info.json").exists()  # legacy file kept for rollback


def test_every_rendered_field_round_trips(web):
    module, client, root = web
    html = client.get("/").get_data(as_text=True)
    names = set(re.findall(r'<input[^>]*name="(f_[^"]+_3)"', html))
    form = {"bin_index": "3", "game_id": "mtg"}
    for n in names:
        form[n] = "2" if "cmc" in n else ("KHM" if "set_symbol" in n else ("a-c" if n.startswith("f_name_") else "on"))
    r = client.post("/update_bin_criteria", data=form)
    body = r.get_json()
    assert r.status_code == 200 and body["success"]
    game = module.games.get_game("mtg")
    for flt in game["filters"]:
        if flt["kind"] == "multi":
            assert body["criteria"][flt["key"]] == [o["value"] for o in flt["options"]]
        else:
            assert body["criteria"][flt["key"]]
    assert body["summary"].startswith("a-c · Legendary Snow Token Basic")
    saved = json.loads((root / "storage" / "bin-info-mtg.json").read_text())
    assert saved["3"]["cmc"] == "2" and saved["3"]["set_symbol"] == "KHM"


def test_bin_save_for_wrong_game_is_refused(web):
    module, client, root = web
    r = client.post("/update_bin_criteria", data={"bin_index": "1", "game_id": "fab", "f_name_1": "x"})
    assert r.status_code == 409


def test_switch_game_and_back(web):
    module, client, root = web
    if "fab" not in module.game_registry:
        pytest.skip("FaB pack not present")
    assert client.post("/set_game", data={"game_id": "fab"}).get_json()["success"]
    html = client.get("/").get_data(as_text=True)
    assert "Flesh and Blood" in html
    assert 'name="f_pitch__1_1"' in html and "filter-swatch" in html
    assert (root / "storage" / "bin-info-fab.json").exists()
    assert json.loads((root / "storage" / "config.json").read_text())["active_game"] == "fab"
    # Settings page edits the FaB prompt
    s = client.get("/settings").get_data(as_text=True)
    assert "Recognition Prompt (Flesh and Blood)" in s
    client.post("/settings", data={"save": "1", "ollama_prompt": "FAB PROMPT"})
    cfg = json.loads((root / "storage" / "config.json").read_text())
    assert cfg["games"]["fab"]["prompt"] == "FAB PROMPT"
    assert client.post("/set_game", data={"game_id": "mtg"}).get_json()["success"]
    assert "Magic: The Gathering" in client.get("/").get_data(as_text=True)


def test_switch_refused_while_sorting_and_unknown_game(web):
    module, client, root = web
    assert client.post("/set_game", data={"game_id": "nope"}).status_code == 404
    module.sorting_active = True
    try:
        assert client.post("/set_game", data={"game_id": "mtg"}).status_code == 409
    finally:
        module.sorting_active = False


def test_legacy_mtg_prompt_migrates_on_save(web):
    module, client, root = web
    cfg = json.loads((root / "storage" / "config.json").read_text())
    cfg.setdefault("ollama", {})["prompt"] = "OLD MTG PROMPT"
    (root / "storage" / "config.json").write_text(json.dumps(cfg))
    assert "OLD MTG PROMPT" in client.get("/settings").get_data(as_text=True)
    client.post("/settings", data={"save": "1", "ollama_prompt": "OLD MTG PROMPT"})
    cfg = json.loads((root / "storage" / "config.json").read_text())
    assert cfg["games"]["mtg"]["prompt"] == "OLD MTG PROMPT"
    assert "prompt" not in cfg["ollama"]


def test_export_upload_delete_pack(web):
    module, client, root = web
    r = client.get("/games/mtg/export")
    assert r.status_code == 200
    src = zipfile.ZipFile(io.BytesIO(r.data))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for name in src.namelist():
            data = src.read(name)
            if name.endswith("game.json"):
                d = json.loads(data)
                d["id"], d["name"] = "mtg-copy", "MTG Copy"
                data = json.dumps(d).encode()
            z.writestr(name, data)
    out.seek(0)
    r = client.post("/games/upload", data={"pack": (out, "copy.zip")}, content_type="multipart/form-data")
    assert "Installed game pack: MTG Copy" in r.get_data(as_text=True)
    assert "mtg-copy" in module.game_registry
    assert "MTG Copy" in client.get("/").get_data(as_text=True)

    bad = io.BytesIO()
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("../evil/game.json", "{}")
    bad.seek(0)
    r = client.post("/games/upload", data={"pack": (bad, "bad.zip")}, content_type="multipart/form-data")
    assert "Upload rejected" in r.get_data(as_text=True)

    client.post("/set_game", data={"game_id": "mtg-copy"})
    client.get("/")  # creates bin-info-mtg-copy.json
    r = client.post("/games/mtg-copy/delete")
    assert "Switch to another game" in r.get_data(as_text=True)
    client.post("/set_game", data={"game_id": "mtg"})
    r = client.post("/games/mtg-copy/delete")
    assert "Deleted game pack" in r.get_data(as_text=True)
    assert "mtg-copy" not in module.game_registry
    assert not (root / "storage" / "bin-info-mtg-copy.json").exists()
    assert "Switch to another game" in client.post("/games/mtg/delete").get_data(as_text=True)
    if "fab" in module.game_registry:
        client.post("/set_game", data={"game_id": "fab"})
        assert "Delete failed" in client.post("/games/mtg/delete").get_data(as_text=True)  # built-in
        assert "mtg" in module.game_registry


def test_sorting_loop_routes_with_active_game(web):
    """One sorting iteration with subprocesses faked: the read is told which game
    to use, the card is matched with that game's rules, and the right flapper fires."""
    module, client, root = web
    calls = []
    card = {"name": "Lightning Bolt", "type": "Instant", "colors": ["R"], "color_identity": ["R"], "cmc": 1.0,
            "set_symbol": "lea", "collector_number": "161", "card_identified_url": "http://img/bolt.jpg", "game": "mtg"}

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs.get("env")))
        script = os.path.basename(cmd[1])
        if script == "Read-Card.py":
            module.sorting_active = False  # stop after this card
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(card) + "\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    module.box_criteria[1] = module.games.normalize_criteria_value(module.active_game, {"type_tags": ["Instant"], "set_symbol": "lea"})
    module.sorting_active = True
    module.csv_enabled = True
    with mock.patch.object(module.subprocess, "run", side_effect=fake_run), \
         mock.patch.object(module.time, "sleep"):
        module.sorting_loop()

    read_env = next(env for cmd, env in calls if cmd[1].endswith("Read-Card.py"))
    assert read_env["SORTER_GAME"] == "mtg"
    assert read_env["REQUIRE_RECOGNITION_KEYS"] == "set_code,collector_number"
    assert any(cmd[2:] == ["flapper_1", "open"] for cmd, _ in calls)
    assert module.bin_counts[1] == 1
    assert module.card_identified_name == "Lightning Bolt"
    assert module.card_identified_set == "lea" and module.card_identified_collector_number == "161"
    assert module.card_identified_url == "http://img/bolt.jpg"
    header = (root / "storage" / "card_info.csv").read_text().splitlines()[0]
    assert "game" not in header.split(",") and "name" in header
