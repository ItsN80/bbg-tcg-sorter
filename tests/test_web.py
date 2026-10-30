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
    for name in ("Basic-Website.py", "games.py", "game_packs.py", "camera_client.py", "sorter_design.py"):
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

    saved_modules = {k: sys.modules.get(k) for k in ("games", "game_packs", "camera_client", "sorter_design", "pigpio", "led_controller")}
    for k in ("games", "game_packs", "camera_client", "sorter_design"):
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


# --- hosted DO Serverless: per-game prompts on the server ---
def _install_custom_pack(module, client):
    src = zipfile.ZipFile(io.BytesIO(client.get("/games/mtg/export").data))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for name in src.namelist():
            data = src.read(name)
            if name.endswith("game.json"):
                d = json.loads(data)
                d["id"], d["name"] = "custom", "Custom Game"
                data = json.dumps(d).encode()
            z.writestr(name, data)
    out.seek(0)
    client.post("/games/upload", data={"pack": (out, "c.zip")}, content_type="multipart/form-data")
    assert "custom" in module.game_registry


def _set_provider(root, provider):
    cfg = json.loads((root / "storage" / "config.json").read_text())
    cfg["recognition_provider"] = provider
    cfg.setdefault("ollama", {})["base_url"] = "https://do.example"
    (root / "storage" / "config.json").write_text(json.dumps(cfg))


def test_do_serverless_refuses_custom_pack(web):
    module, client, root = web
    _install_custom_pack(module, client)
    _set_provider(root, "do_serverless")
    client.post("/set_game", data={"game_id": "custom"})
    html = client.get("/").get_data(as_text=True)
    assert "doesn&#39;t support Custom Game yet" in html or "doesn't support Custom Game yet" in html
    with mock.patch.object(module.threading, "Thread") as thread:
        client.post("/", data={"start_sorting": "1"})
    assert not thread.called and not module.sorting_active
    r = client.post("/api/test_recognition")
    assert r.status_code == 400 and "Custom Game" in r.get_json()["error"]
    # Built-in hosted games are fine; a custom pack with direct Ollama is fine.
    client.post("/set_game", data={"game_id": "fab"})
    assert 'class="notice-banner"' not in client.get("/").get_data(as_text=True)
    _set_provider(root, "ollama")
    client.post("/set_game", data={"game_id": "custom"})
    assert 'class="notice-banner"' not in client.get("/").get_data(as_text=True)


def test_test_recognition_sends_game_to_do(web):
    module, client, root = web
    _set_provider(root, "do_serverless")
    client.post("/set_game", data={"game_id": "fab"})
    (root / "static" / "images" / "card_scanned.png").write_bytes(b"img")
    sent = {}

    def fake_post(url, json=None, **kw):
        sent.update(json)
        return mock.MagicMock(status_code=200, json=lambda: {"response": "{}"})

    with mock.patch.object(module.requests, "post", side_effect=fake_post):
        assert client.post("/api/test_recognition").get_json()["success"]
    assert sent["game"] == "fab"


def test_sorting_stops_when_server_rejects_game(web):
    module, client, root = web
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[1].endswith("Read-Card.py"):
            out = {"error": "not supported", "unsupported_game": True, "game": "mtg"}
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(out), stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    module.sorting_active = True
    with mock.patch.object(module.subprocess, "run", side_effect=fake_run), \
         mock.patch.object(module.time, "sleep"):
        module.sorting_loop()  # must return on its own, after one card
    assert not module.sorting_active
    assert sum(1 for c in calls if c[1].endswith("Read-Card.py")) == 1
    assert module.bin_counts[10] == 1


def test_sorting_and_stop_never_change_led_colour(web):
    """The LEDs light the card for the camera: no status colours, and Stop
    doesn't switch them off mid-card."""
    module, client, root = web
    module.led_controller.reset_mock()

    def fake_run(cmd, **kwargs):
        if cmd[1].endswith("Read-Card.py"):
            module.sorting_active = False
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"error": "unreadable"}), stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    module.sorting_active = True
    with mock.patch.object(module.subprocess, "run", side_effect=fake_run), \
         mock.patch.object(module.time, "sleep"):
        module.sorting_loop()  # a failed read used to flash red
    client.post("/", data={"stop_sorting": "1"})
    led = module.led_controller
    assert not led.set_color.called and not led.off.called


def test_camera_test_saves_card_crop_and_reports_capture_errors(web):
    module, client, root = web
    html = client.get("/camera_test").get_data(as_text=True)
    assert 'name="card_x2" value="756"' in html  # unset = full processed image
    for title in ("Original Image", "Whole Card Cropped Image", "Cropped Combined Image"):
        assert title in html

    form = {"card_x1": "170", "card_y1": "110", "card_x2": "735", "card_y2": "890",
            "top_x1": "160", "top_y1": "155", "top_x2": "577", "top_y2": "235",
            "bottom_x1": "160", "bottom_y1": "828", "bottom_x2": "577", "bottom_y2": "885"}
    failed = subprocess.CompletedProcess([], 1, stdout=json.dumps({"error": "top_crop is empty"}), stderr="")
    with mock.patch.object(module.subprocess, "run", return_value=failed) as run:
        html = client.post("/camera_test", data=form).get_data(as_text=True)
    assert run.call_args.args[0][1].endswith("Test-Camera.py")
    assert "test capture failed: top_crop is empty" in html
    saved = json.loads((root / "storage" / "config.json").read_text())
    assert saved["card_crop"] == {"x1": 170, "y1": 110, "x2": 735, "y2": 890}
    assert saved["camera_crop"]["top_crop"]["y2"] == 235


# --- camera: live view relay + scanned-image refresh ---
def test_camera_stream_relays_and_frees_its_slot(web):
    module, client, root = web
    upstream = mock.MagicMock(headers={"Content-Type": "multipart/x-mixed-replace; boundary=FRAME"})
    upstream.iter_content.return_value = iter([b"--FRAME\r\n", b"jpeg"])
    with mock.patch.object(module.requests, "get", return_value=upstream) as get:
        r = client.get("/camera/stream")
        assert get.call_args.args[0].endswith("/stream.mjpg")
        assert r.headers["Content-Type"].startswith("multipart/x-mixed-replace")
        assert r.get_data() == b"--FRAME\r\njpeg"
        r.close()
    assert upstream.close.called
    # Every slot is free again: the limit's worth of views can open at once.
    opened = []
    with mock.patch.object(module.requests, "get", return_value=upstream):
        for _ in range(module.STREAM_VIEWER_LIMIT):
            opened.append(client.get("/camera/stream"))
            assert opened[-1].status_code == 200
        assert client.get("/camera/stream").status_code == 503  # one too many
    for resp in opened:
        resp.close()


def test_camera_stream_unavailable_without_service(web):
    module, client, root = web
    with mock.patch.object(module.requests, "get", side_effect=module.requests.ConnectionError("refused")):
        r = client.get("/camera/stream")
    assert r.status_code == 503 and "Camera service unavailable" in r.get_data(as_text=True)
    assert module.stream_slots.acquire(blocking=False)  # the failed attempt gave its slot back
    module.stream_slots.release()


def test_scanned_image_version_changes_only_with_the_image(web):
    module, client, root = web
    assert client.get("/get_move_count").get_json()["card_scanned_version"] == 0  # no scan yet
    scanned = root / "static" / "images" / "card_scanned.png"
    scanned.write_bytes(b"one")
    os.utime(scanned, ns=(1_000_000_000, 1_000_000_000))
    first = client.get("/get_move_count").get_json()["card_scanned_version"]
    assert first == client.get("/get_move_count").get_json()["card_scanned_version"]
    os.utime(scanned, ns=(2_000_000_000, 2_000_000_000))
    assert client.get("/get_move_count").get_json()["card_scanned_version"] != first


# --- Flapper calibration pages (ramp open/close, tower left/center/right) ---
def test_tower_calibration_page_shows_levels_with_defaults(web):
    module, client, root = web
    html = client.get("/servo_test/tower").get_data(as_text=True)
    for key in ("flapper_1", "flapper_2", "flapper_3"):
        assert f'data-servo="{key}"' in html
    assert 'data-servo="flapper_4"' not in html
    assert 'data-saved="60"' in html and 'data-saved="90"' in html and 'data-saved="120"' in html
    assert "The sorter is set to <strong>Ramp</strong>" in html
    assert client.get("/servo_test/spiral").status_code == 404


def test_ramp_calibration_replaces_the_settings_flapper_fields(web):
    module, client, root = web
    html = client.get("/settings").get_data(as_text=True)
    assert "/servo_test/ramp" in html and "/servo_test/tower" in html
    assert "flapper_1_open_degrees" not in html and 'id="card_servo_open_degrees"' in html
    assert client.get("/servo_test").headers["Location"].endswith("/servo_test/ramp")  # the current design

    config = json.loads((root / "storage" / "config.json").read_text())
    html = client.get("/servo_test/ramp").get_data(as_text=True)
    assert 'data-servo="flapper_9"' in html and 'data-servo="flapper_10"' not in html
    assert f'data-saved="{config["flappers"]["flapper_1"]["open_degrees"]}"' in html
    assert "Bin 9" in html and "Close All" in html

    ok = subprocess.CompletedProcess([], 0, stdout="", stderr="")
    with mock.patch.object(module.subprocess, "run", return_value=ok) as run:
        r = client.post("/api/servo_test/move", json={"design": "ramp", "servo": "flapper_9", "position": "open", "degrees": 55})
        assert r.get_json()["success"]
        assert run.call_args.args[0][2:] == ["flapper_9", "open"]
        assert client.post("/api/servo_test/move", json={"design": "ramp", "servo": "flapper_1", "position": "left", "degrees": 55}).status_code == 400
        assert client.post("/api/servo_test/ramp/rest").get_json()["success"]
        assert run.call_args.args[0][2:] == [",".join(f"flapper_{i}" for i in range(1, 10)), "close"]
    saved = json.loads((root / "storage" / "config.json").read_text())["flappers"]["flapper_9"]
    assert saved["open_degrees"] == 55

    # Saving Settings no longer touches flapper degrees.
    client.post("/settings", data=_settings_form())
    assert json.loads((root / "storage" / "config.json").read_text())["flappers"]["flapper_9"]["open_degrees"] == 55


def test_servo_test_move_saves_degrees_and_moves(web):
    module, client, root = web
    ok = subprocess.CompletedProcess([], 0, stdout="", stderr="")
    with mock.patch.object(module.subprocess, "run", return_value=ok) as run:
        r = client.post("/api/servo_test/move", json={"design": "tower", "servo": "flapper_2", "position": "right", "degrees": 135})
    assert r.get_json()["success"]
    assert run.call_args.args[0][1].endswith("servo_controller.py")
    assert run.call_args.args[0][2:] == ["flapper_2", "right"]
    saved = json.loads((root / "storage" / "config.json").read_text())["flappers"]["flapper_2"]
    assert saved["right_degrees"] == 135
    assert "open_degrees" in saved  # the sorter's open/close settings are untouched
    assert 'value="135"' in client.get("/servo_test/tower").get_data(as_text=True)


def test_servo_test_move_rejects_bad_input_and_sorting(web):
    module, client, root = web
    with mock.patch.object(module.subprocess, "run") as run:
        for body in ({"design": "tower", "servo": "flapper_4", "position": "left", "degrees": 90},
                     {"design": "tower", "servo": "card_servo", "position": "left", "degrees": 90},
                     {"design": "tower", "servo": "flapper_1", "position": "open", "degrees": 90},
                     {"design": "tower", "servo": "flapper_1", "position": "left", "degrees": 181},
                     {"design": "tower", "servo": "flapper_1", "position": "left", "degrees": "abc"},
                     {"servo": "flapper_1", "position": "left", "degrees": 90}):
            assert client.post("/api/servo_test/move", json=body).status_code == 400
        module.sorting_active = True
        try:
            assert client.post("/api/servo_test/move", json={"design": "tower", "servo": "flapper_1", "position": "left", "degrees": 90}).status_code == 409
            assert client.post("/api/servo_test/tower/rest").status_code == 409
            assert client.post("/api/servo_test/feed").status_code == 409
            assert client.post("/api/servo_test/release").status_code == 409
        finally:
            module.sorting_active = False
    assert not run.called


def test_servo_test_release_and_feed(web):
    module, client, root = web
    ok = subprocess.CompletedProcess([], 0, stdout="", stderr="")
    with mock.patch.object(module.subprocess, "run", return_value=ok) as run, \
         mock.patch.object(module.time, "sleep"):
        assert client.post("/api/servo_test/release").get_json()["success"]
    assert [c.args[0][2:] for c in run.call_args_list] == [["card_servo", "open"], ["card_servo", "close"]]

    failed = subprocess.CompletedProcess([], 1, stdout="Timeout: Sensor 1 did not go BLOCKED in time\n1\n", stderr="")
    with mock.patch.object(module.subprocess, "run", return_value=failed) as run:
        r = client.post("/api/servo_test/feed")
    assert run.call_args.args[0][1].endswith("Feed-Card.py")
    assert r.status_code == 500 and "Sensor 1 did not go BLOCKED" in r.get_json()["output"]
    assert module.feed_test_lock.acquire(blocking=False)  # freed after the feed
    module.feed_test_lock.release()


# --- Sorter design: ramp (default) or tower ---
def _set_design(root, design, levels=3):
    path = root / "storage" / "config.json"
    config = json.loads(path.read_text())
    config["sorter"] = {"design": design, "tower_levels": levels}
    path.write_text(json.dumps(config))


def _settings_form(**extra):
    form = {"save": "1", "smtp_port": "587"}
    form.update(extra)
    return form


def test_tower_sorting_routes_by_level_and_side(web):
    """Tower: every level is centered before the first card; a matched card tips
    its level's flapper, is released, the level above tips the same way to push
    it through, both re-center; failures go to the last bin."""
    module, client, root = web
    _set_design(root, "tower", 2)
    calls = []
    cards = iter([
        {"name": "Lightning Bolt", "type": "Instant", "colors": ["R"], "set_symbol": "lea", "collector_number": "161"},
        {"error": "unreadable"},
    ])

    def fake_run(cmd, **kwargs):
        calls.append(cmd[2:])
        if os.path.basename(cmd[1]) == "Read-Card.py":
            card = next(cards)
            if "error" in card:
                module.sorting_active = False
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(card), stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    module.box_criteria[1] = module.games.empty_criteria(module.active_game)  # the fixture's bin 1 takes Instants
    module.box_criteria[3] = module.games.normalize_criteria_value(module.active_game, {"type_tags": ["Instant"]})
    module.sorting_active = True
    with mock.patch.object(module.subprocess, "run", side_effect=fake_run), \
         mock.patch.object(module.time, "sleep"):
        module.sorting_loop()

    servo_calls = [c for c in calls if c]
    assert servo_calls[0] == ["flapper_1,flapper_2", "center"]
    first_card = servo_calls[1:6]
    assert first_card == [["flapper_2", "left"], ["card_servo", "open"], ["flapper_1", "left"],
                          ["flapper_2,flapper_1", "center"], ["card_servo", "close"]]
    assert servo_calls[6:] == [["card_servo", "open"], ["card_servo", "close"]]  # catch-all: no flapper moves
    assert module.bin_counts[3] == 1 and module.bin_counts[5] == 1 and module.bin_counts[10] == 0


def test_bin_pages_follow_the_design(web):
    module, client, root = web
    html = client.get("/").get_data(as_text=True)
    assert 'data-bin="10"' in html and 'data-bin="11"' not in html
    assert "Ramp (10 bins)" in html
    assert client.post("/update_bin_criteria", data={"bin_index": "11"}).status_code == 400

    _set_design(root, "tower", 6)
    html = client.get("/").get_data(as_text=True)
    assert 'data-bin="13"' in html and 'data-bin="14"' not in html
    assert "Level 6 Right" in html and "Catch-all" in html
    assert 'id="binCount13"' in html and "i <= 13;" in html
    r = client.post("/update_bin_criteria", data={"bin_index": "12", "game_id": "mtg", "f_name_12": "a-c"})
    assert r.get_json()["success"]
    saved = json.loads((root / "storage" / "bin-info-mtg.json").read_text())
    assert saved["12"]["name"] == "a-c"

    # Back to the ramp: bin 12 is hidden but its criteria are kept.
    _set_design(root, "ramp")
    assert 'data-bin="12"' not in client.get("/").get_data(as_text=True)
    with module.lock:
        module.load_game_state("mtg")
    assert module.box_criteria[12]["name"] == "a-c"


def test_settings_saves_design_and_refuses_change_while_sorting(web):
    module, client, root = web
    html = client.get("/settings").get_data(as_text=True)
    assert 'value="ramp" checked' in html

    client.post("/settings", data=_settings_form(sorter_design="tower", tower_levels="5"))
    assert json.loads((root / "storage" / "config.json").read_text())["sorter"] == {"design": "tower", "tower_levels": 5}

    html = client.post("/settings", data=_settings_form(sorter_design="tower", tower_levels="16")).get_data(as_text=True)
    assert "Tower Levels must be a whole number from 1 to 15" in html

    module.sorting_active = True
    try:
        html = client.post("/settings", data=_settings_form(sorter_design="ramp")).get_data(as_text=True)
    finally:
        module.sorting_active = False
    assert "Stop sorting before changing the sorter design" in html
    assert json.loads((root / "storage" / "config.json").read_text())["sorter"]["design"] == "tower"


def test_servo_test_covers_every_tower_level(web):
    module, client, root = web
    _set_design(root, "tower", 12)
    html = client.get("/servo_test/tower").get_data(as_text=True)
    assert 'data-servo="flapper_12"' in html and 'data-servo="flapper_13"' not in html
    assert "channel 11" in html and "Bin 24" in html  # flapper 12 sits on channel 11
    ok = subprocess.CompletedProcess([], 0, stdout="", stderr="")
    with mock.patch.object(module.subprocess, "run", return_value=ok) as run:
        r = client.post("/api/servo_test/move", json={"design": "tower", "servo": "flapper_12", "position": "left", "degrees": 70})
        assert r.get_json()["success"]
        assert client.post("/api/servo_test/move", json={"design": "tower", "servo": "flapper_13", "position": "left", "degrees": 70}).status_code == 400
        assert client.post("/api/servo_test/tower/rest").get_json()["success"]
    assert run.call_args.args[0][2:] == [",".join(f"flapper_{i}" for i in range(1, 13)), "center"]


def test_test_deposit_runs_the_sorting_cycle(web):
    """Test Deposit: flappers to rest, optional feed, then the sorting loop's
    own delivery (with the level-above push on a tower)."""
    module, client, root = web
    _set_design(root, "tower", 3)
    html = client.get("/servo_test/tower").get_data(as_text=True)
    assert 'data-bin="7"' in html and 'data-bin="8"' not in html and "Catch-all" in html

    calls = []
    def fake_run(cmd, **kwargs):
        calls.append(cmd[1].rsplit("/", 1)[-1:] + cmd[2:])
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with mock.patch.object(module.subprocess, "run", side_effect=fake_run), \
         mock.patch.object(module.time, "sleep"):
        assert client.post("/api/servo_test/tower/deposit", json={"bin": 6, "feed": True}).get_json()["success"]
    assert calls == [
        ["servo_controller.py", "flapper_1,flapper_2,flapper_3", "center"],
        ["Feed-Card.py"],
        ["servo_controller.py", "flapper_3", "right"],
        ["servo_controller.py", "card_servo", "open"],
        ["servo_controller.py", "flapper_2", "right"],
        ["servo_controller.py", "flapper_3,flapper_2", "center"],
        ["servo_controller.py", "card_servo", "close"],
    ]

    # Ramp page, no feed, catch-all: flappers close, the card servo cycles.
    calls.clear()
    with mock.patch.object(module.subprocess, "run", side_effect=fake_run), \
         mock.patch.object(module.time, "sleep"):
        assert client.post("/api/servo_test/ramp/deposit", json={"bin": 10, "feed": False}).get_json()["success"]
        assert client.post("/api/servo_test/ramp/deposit", json={"bin": 11}).status_code == 400
    assert calls == [["servo_controller.py", ",".join(f"flapper_{i}" for i in range(1, 10)), "close"],
                     ["servo_controller.py", "card_servo", "open"], ["servo_controller.py", "card_servo", "close"]]

    # A failed feed stops before the card is released.
    failed = subprocess.CompletedProcess([], 1, stdout="Timeout: Sensor 2 did not go BLOCKED in time\n1\n", stderr="")
    with mock.patch.object(module.subprocess, "run", return_value=failed) as run:
        r = client.post("/api/servo_test/tower/deposit", json={"bin": 1, "feed": True})
    assert r.status_code == 500 and "Sensor 2" in r.get_json()["output"]
    assert not any(c.args[0][2:] == ["card_servo", "open"] for c in run.call_args_list)
    assert module.servo_test_lock.acquire(blocking=False)  # freed after the failure
    module.servo_test_lock.release()
