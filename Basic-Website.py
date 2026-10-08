#!/usr/bin/env python3

from flask import Flask, Response, render_template, request, jsonify, redirect, url_for, send_file, session
import subprocess
import os
import secrets
import threading
import time
import atexit
import json  # for parsing card info output
import csv   # for writing CSV files
import shutil  # for copying files
import requests  # for downloading images
import base64  # for encoding test-recognition images
import io  # for in-memory game pack exports
import pigpio
import urllib.parse
import smtplib
from email.mime.text import MIMEText
from werkzeug.security import generate_password_hash, check_password_hash
from led_controller import LEDController, normalize_led_config
import camera_client
import games
import game_packs

app = Flask(__name__)

# Global variables
sorting_active = False      # Whether the sorting loop is active
sorting_thread = None       # Thread running the sorting loop
box_criteria = {}           # Dictionary mapping box numbers (1-10) to criteria for the active game
game_registry = {}          # {game_id: definition} from games.load_games()
active_game = None          # Definition of the game currently being sorted
lock = threading.Lock()       # Protects sorting_active, box_criteria, game_registry, active_game
stats_lock = threading.Lock() # Protects move_count, monthly_move_count, failed_read_count, card_identified_url

# Global File Path
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Global variables for CSV saving
csv_enabled = False  # Whether to append card output to CSV (set via a checkbox on the main page)
csv_lock = threading.Lock()  # Protects CSV file access

# File Paths
COUNTER_FILE = os.path.join(BASE_DIR, "counters", "move_count.txt")
MONTHLY_COUNTER_FILE = os.path.join(BASE_DIR, "counters", "monthly_move_count.txt")
FAILED_READS_FILE = os.path.join(BASE_DIR, "counters", "failed_reads.txt")
CONFIG_FILE = os.path.join(BASE_DIR, "storage", "config.json")
FEED_LOG_FILE = os.path.join(BASE_DIR, "storage", "feed_debug.log")
LEGACY_BIN_INFO_FILE = os.path.join(BASE_DIR, "storage", "bin-info.json")  # MTG bins before multi-game
SCANNED_IMAGE_SRC = os.path.join(BASE_DIR, "storage", "scanned_card.png")
SCANNED_IMAGE_DEST = os.path.join(BASE_DIR, "static", "images", "card_scanned.png")
IDENTIFIED_IMAGE_DEST = os.path.join(BASE_DIR, "static", "images", "card_identified.png")
FAILED_IMAGE_DEST = os.path.join(BASE_DIR, "static", "images", "failed")
SUCCESS_SCAN_DEST = os.path.join(BASE_DIR, "static", "images", "scans")
COMBINED_CROP_FILE = os.path.join(BASE_DIR, "storage", "combined_crop.jpg")

#Create Paths if they do not exist
os.makedirs(FAILED_IMAGE_DEST, exist_ok=True)
os.makedirs(SUCCESS_SCAN_DEST, exist_ok=True)
os.makedirs(games.USER_GAMES_DIR, exist_ok=True)

# Check if config.json exists; if not, copy config-default.json as config.json
if not os.path.isfile(CONFIG_FILE):
    default_config_file = os.path.join(BASE_DIR, "storage", "config-default.json")
    if os.path.exists(default_config_file):
        shutil.copy(default_config_file, CONFIG_FILE)
        print("Default configuration file created from config-default.json.")
    else:
        print("Default configuration file config-default.json not found. Please create one.")

# Global variable for the identified card URL (from API)
card_identified_url = ""
card_identified_name = ""
card_identified_set = ""
card_identified_collector_number = ""
do_credits = None  # Cached remaining_credits from DO Serverless backend
bin_counts = {i: 0 for i in range(1, 11)}  # In-memory only; resets on restart.

# Initialize pigpio globally
pi = pigpio.pi()
if not pi.connected:
    print("Failed to connect to pigpio daemon.")
    exit()

# Card Sensors
sensor1_pin = 8
sensor2_pin = 14

# Card Sensor State - raw = 0 when triggered, raw = 1 when clear
sensor_active_low = False

# Configure GPIO pins for input
pi.set_mode(sensor1_pin, pigpio.INPUT)
pi.set_mode(sensor2_pin, pigpio.INPUT)

# Use pull-ups so the input doesn't float (common for active-low sensors)
pi.set_pull_up_down(sensor1_pin, pigpio.PUD_UP)
pi.set_pull_up_down(sensor2_pin, pigpio.PUD_UP)


def read_sensor_status():
    s1_raw = pi.read(sensor1_pin)
    s2_raw = pi.read(sensor2_pin)

    if sensor_active_low:
        s1_triggered = 1 if s1_raw == 0 else 0
        s2_triggered = 1 if s2_raw == 0 else 0
    else:
        s1_triggered = s1_raw
        s2_triggered = s2_raw

    return {
        "sensor1": {"pin": sensor1_pin, "raw": s1_raw, "triggered": s1_triggered},
        "sensor2": {"pin": sensor2_pin, "raw": s2_raw, "triggered": s2_triggered},
    }

def get_move_count():
    try:
        with open(COUNTER_FILE, "r") as f:
            return int(f.read())
    except FileNotFoundError:
        return 0
    
def get_failed_read_count():
    try:
        with open(FAILED_READS_FILE, "r") as f:
            return int(f.read())
    except FileNotFoundError:
        return 0

def save_failed_read_count(count):
    with open(FAILED_READS_FILE, "w") as f:
        f.write(str(count))

def save_move_count(count):
    with open(COUNTER_FILE, "w") as f:
        f.write(str(count))

def get_monthly_move_count():
    try:
        with open(MONTHLY_COUNTER_FILE, "r") as f:
            return int(f.read())
    except FileNotFoundError:
        return 0

def save_monthly_move_count(count):
    with open(MONTHLY_COUNTER_FILE, "w") as f:
        f.write(str(count))

move_count = get_move_count()
monthly_move_count = get_monthly_move_count()
failed_read_count = get_failed_read_count()

def read_config():
    try:
        with open(CONFIG_FILE, "r") as f:
            config = json.load(f)
            return ensure_auth_config(ensure_led_config(config))
    except Exception as e:
        print("Error reading config file:", e)
        return ensure_auth_config(ensure_led_config({}))

def write_config(config):
    try:
        ensure_auth_config(ensure_led_config(config))
        with open(CONFIG_FILE, "w") as f:
            json.dump(config, f, indent=4)
        return True
    except Exception as e:
        print("Error writing config file:", e)
        return False


def fetch_do_balance():
    config = read_config()
    if config.get("recognition_provider") != "do_serverless":
        return None
    ollama_cfg = config.get("ollama", {})
    base_url = (ollama_cfg.get("base_url") or "").rstrip("/")
    api_key = ollama_cfg.get("api_key", "")
    if not base_url or not api_key:
        return None
    try:
        r = requests.get(
            f"{base_url}/balance",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10
        )
        r.raise_for_status()
        return r.json().get("remaining_credits")
    except Exception as e:
        print(f"fetch_do_balance error: {e}")
        return None


def ensure_led_config(config):
    if not isinstance(config, dict):
        config = {}
    config["led"] = normalize_led_config(config.get("led"))
    return config


def ensure_auth_config(config):
    auth_cfg = config.get("auth")
    if not isinstance(auth_cfg, dict):
        auth_cfg = {}
    auth_cfg.setdefault("password_hash", "")
    auth_cfg.setdefault("secret_key", "")
    config["auth"] = auth_cfg
    return config


def parse_hex_color(value):
    if not isinstance(value, str):
        raise ValueError("color must be a hex string like #RRGGBB")
    raw = value.strip()
    if len(raw) != 7 or not raw.startswith("#"):
        raise ValueError("color must be in #RRGGBB format")
    try:
        r = int(raw[1:3], 16)
        g = int(raw[3:5], 16)
        b = int(raw[5:7], 16)
    except ValueError:
        raise ValueError("color must be valid hex in #RRGGBB format")
    return {"r": r, "g": g, "b": b}


def normalize_api_brightness(value):
    if isinstance(value, bool):
        raise ValueError("brightness must be numeric")
    if isinstance(value, int) and 0 <= value <= 100:
        return value / 100.0
    try:
        as_float = float(value)
    except (TypeError, ValueError):
        raise ValueError("brightness must be a number in 0..1 or 0..100")
    if 0.0 <= as_float <= 1.0:
        return as_float
    if 1.0 < as_float <= 100.0 and float(as_float).is_integer():
        return as_float / 100.0
    raise ValueError("brightness must be in 0..1 or 0..100")


runtime_config = read_config()
if not runtime_config["auth"]["secret_key"]:
    runtime_config["auth"]["secret_key"] = secrets.token_hex(32)
write_config(runtime_config)
app.secret_key = runtime_config["auth"]["secret_key"]
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
led_controller = LEDController(runtime_config.get("led"))
led_state = led_controller.get_state()
print(f"LED enabled: {led_state['enabled']}, gpio: {led_state['gpio']}, count: {led_state['count']}")

_startup_credits = fetch_do_balance()
if _startup_credits is not None:
    do_credits = _startup_credits
    print(f"DO Serverless credits on startup: {do_credits}")


@atexit.register
def shutdown_led_controller():
    try:
        led_controller.close()
    except Exception:
        pass

def bin_info_path(game_id):
    return os.path.join(BASE_DIR, "storage", f"bin-info-{game_id}.json")

def default_bins_path(game):
    return os.path.join(game["_dir"], "default-bins.json")

def csv_path(game_id):
    # MTG keeps the original file name so existing exports stay where they were.
    if game_id == games.DEFAULT_GAME_ID:
        return os.path.join(BASE_DIR, "storage", "card_info.csv")
    return os.path.join(BASE_DIR, "storage", f"card_info-{game_id}.csv")

def read_default_bins(game):
    try:
        with open(default_bins_path(game), "r", encoding="utf-8") as f:
            return games.normalize_box_criteria(game, json.load(f))
    except FileNotFoundError:
        return games.default_box_criteria(game)
    except Exception as e:
        print(f"Error reading default bins for {game['id']}: {e}")
        return games.default_box_criteria(game)

def read_bin_info(game):
    path = bin_info_path(game["id"])
    if not os.path.isfile(path):
        # First run for this game: MTG migrates the pre-multi-game bin-info.json
        # (copied, not moved, so rolling back to an older version still works);
        # every other game starts from its pack's default-bins.json.
        if game["id"] == games.DEFAULT_GAME_ID and os.path.isfile(LEGACY_BIN_INFO_FILE):
            try:
                with open(LEGACY_BIN_INFO_FILE, "r", encoding="utf-8") as f:
                    criteria = games.normalize_box_criteria(game, json.load(f))
                print("Migrated storage/bin-info.json to bin-info-mtg.json.")
            except Exception as e:
                print("Error reading legacy bin info file:", e)
                criteria = read_default_bins(game)
        else:
            criteria = read_default_bins(game)
        write_bin_info(game, criteria)
        return criteria
    try:
        with open(path, "r", encoding="utf-8") as f:
            return games.normalize_box_criteria(game, json.load(f))
    except Exception as e:
        print("Error reading bin info file:", e)
        return games.default_box_criteria(game)

def write_bin_info(game, criteria):
    try:
        normalized = games.normalize_box_criteria(game, criteria)
        serialized = {str(i): normalized.get(i, {}) for i in range(1, games.BIN_COUNT + 1)}
        with open(bin_info_path(game["id"]), "w", encoding="utf-8") as f:
            json.dump(serialized, f, indent=4)
        return True
    except Exception as e:
        print("Error writing bin info file:", e)
        return False

def load_game_state(requested_id=None):
    """(Re)loads the game registry and the active game's bins. Caller holds `lock`."""
    global game_registry, active_game, box_criteria
    game_registry = games.load_games()
    game_id = requested_id or read_config().get("active_game") or games.DEFAULT_GAME_ID
    active_game = games.get_game(game_id, game_registry)
    box_criteria = read_bin_info(active_game)

with lock:
    load_game_state()

def card_display(game, card):
    """The name / set / number / image shown on the Live Sorting tab, using the
    card keys named in the game's "display" block."""
    display = game.get("display", {})
    return {k: str(card.get(display.get(k, ""), "") or "") for k in ("name", "set", "number", "image")}

def append_card_to_csv(game_id, card):
    # Use card keys as field names (one CSV per game, since each game's cards
    # carry different keys).
    row = {k: v for k, v in card.items() if k not in ("game", "matched_by")}
    fieldnames = list(row.keys())
    path = csv_path(game_id)
    file_exists = os.path.isfile(path) and os.path.getsize(path) > 0
    with csv_lock:
        with open(path, 'a', newline='') as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            if not file_exists:
                writer.writeheader()
            writer.writerow(row)

def update_images(card):
    """Update the scanned card image by copying the captured file.
       (We now use the API-provided URL to update the 'Card Identified' image.)
    """
    try:
        if os.path.exists(SCANNED_IMAGE_SRC):
            shutil.copy(SCANNED_IMAGE_SRC, SCANNED_IMAGE_DEST)
            print("Updated scanned card image.")
        else:
            print("Scanned image source not found:", SCANNED_IMAGE_SRC)
    except Exception as e:
        print("Error updating scanned card image:", e)
    
    # Optionally, you could download the image from card_identified_url as well,
    # but in this revision we assume the front-end uses card_identified_url.

def save_failed_read_details(timestamp, card, read_stdout="", read_stderr=""):
    details = {
        "timestamp": timestamp,
        "error": card.get("error", "Unknown read error"),
        "provider": card.get("provider", ""),
        "game": card.get("game", ""),
        "recognition_response": card.get("recognition", {}),
        "lookup_responses": card.get("lookup_responses", card.get("scryfall_responses", [])),
        "raw_read_stdout": read_stdout,
        "raw_read_stderr": read_stderr
    }
    details_path = os.path.join(FAILED_IMAGE_DEST, f"failed_{timestamp}.json")
    try:
        with open(details_path, "w", encoding="utf-8") as f:
            json.dump(details, f, indent=2)
    except Exception as e:
        print(f"Failed to save failed read details: {e}")

def save_successful_scan(timestamp, game, card):
    """Optionally save a successful scan's images + parsed data, for
    prompt-tuning reference. Gated by config["save_scans"]["enabled"]."""
    try:
        # card_scanned.png is the reliable fixed path — Read-Card.py's main() copies the
        # exact image it sent for recognition here regardless of provider. combined_crop.jpg
        # is only ever produced by the AWS Rekognition path, so it's best-effort/optional.
        if os.path.exists(SCANNED_IMAGE_DEST):
            shutil.copy(SCANNED_IMAGE_DEST, os.path.join(SUCCESS_SCAN_DEST, f"scan_{timestamp}.png"))
        if os.path.exists(COMBINED_CROP_FILE):
            shutil.copy(COMBINED_CROP_FILE, os.path.join(SUCCESS_SCAN_DEST, f"{timestamp}_combined_crop.jpg"))

        shown = card_display(game, card)
        details = {
            "timestamp": timestamp,
            "game": game["id"],
            "name": shown["name"],
            "set": shown["set"],
            "number": shown["number"],
        }
        details_path = os.path.join(SUCCESS_SCAN_DEST, f"scan_{timestamp}.json")
        with open(details_path, "w", encoding="utf-8") as f:
            json.dump(details, f, indent=2)
    except Exception as e:
        print(f"Failed to save successful scan: {e}")

def enforce_scan_retention(max_saved):
    """Keeps only the newest `max_saved` saved-scan entries, deleting the
    oldest ones (and their paired image files) once the cap is exceeded."""
    try:
        json_files = sorted(
            f for f in os.listdir(SUCCESS_SCAN_DEST) if f.startswith("scan_") and f.endswith(".json")
        )
        excess = len(json_files) - max_saved
        if excess <= 0:
            return
        for filename in json_files[:excess]:
            ts = filename[len("scan_"):-len(".json")]
            for related in (f"scan_{ts}.png", f"{ts}_combined_crop.jpg", f"scan_{ts}.json"):
                path = os.path.join(SUCCESS_SCAN_DEST, related)
                if os.path.exists(path):
                    os.remove(path)
    except Exception as e:
        print(f"Failed to enforce scan retention: {e}")

def parse_card_output(stdout_text):
    """
    Parse card JSON from subprocess stdout.
    Accepts either pure JSON or noisy logs where the last JSON line is the payload.
    """
    text = (stdout_text or "").strip()
    if not text:
        raise json.JSONDecodeError("Empty output", "", 0)

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue

    raise json.JSONDecodeError("No JSON object found in output", text, 0)

def send_shutdown_summary_email(config):
            if not config.get("smtp", {}).get("enabled"):
                return
            try:
                body = f"""Raspberry Pi Sorter Shutdown Summary ({config.get("system_name", "Unnamed System")}):

            Lifetime Cards Processed: {move_count}
            Monthly Cards Processed: {monthly_move_count}
            Failed Reads: {get_failed_read_count()}

            Shutdown executed at: {time.strftime("%Y-%m-%d %H:%M:%S")}
            """

                msg = MIMEText(body)
                msg['Subject'] = f"[{config.get('system_name', 'Sorter')}] Shutdown Summary"
                msg['From'] = config["smtp"]["from_email"]
                msg['To'] = config["smtp"]["to_email"]

                with smtplib.SMTP(config["smtp"]["server"], config["smtp"]["port"]) as server:
                    server.starttls()
                    server.login(config["smtp"]["username"], config["smtp"]["password"])
                    server.sendmail(msg['From'], [msg['To']], msg.as_string())
                print("Shutdown summary email sent.")
            except Exception as e:
                print("Failed to send shutdown summary email:", e)

def sorting_loop():
    global move_count, monthly_move_count, sorting_active, sorting_thread, csv_enabled, card_identified_url, failed_read_count, do_credits
    global card_identified_name, card_identified_set, card_identified_collector_number, bin_counts
    # The LEDs light the card for the camera, so they stay at the configured
    # colour the whole time (no status colours, which would tint the scans).
    _live_config = read_config()
    # One game per run: snapshot it so a mid-run change can't mix rule sets.
    with lock:
        _game = active_game
    _is_do = _live_config.get("recognition_provider") == "do_serverless"
    if _is_do:
        _bal = fetch_do_balance()
        if _bal is not None:
            with stats_lock:
                do_credits = _bal
    while sorting_active:
        selected_box = None
        try:
            # Feed a new card.
            feed_result = subprocess.run(["python3", os.path.join(BASE_DIR, "scripts", "Feed-Card.py")], capture_output=True, text=True, timeout=90)
            if feed_result.returncode != 0:
                print("Initial feed failed, retrying after 2 seconds...")
                time.sleep(2)
                feed_result = subprocess.run(["python3", os.path.join(BASE_DIR, "scripts", "Feed-Card.py")], capture_output=True, text=True, timeout=90)
                if feed_result.returncode != 0:
                    print("Feed failed again (return code: {}), stopping sorting.".format(feed_result.returncode))
                    sorting_active = False
                    break

            servo_ctrl = ["python3", os.path.join(BASE_DIR, "scripts", "servo_controller.py")]

            if not sorting_active:
                # Stop was requested while this card was being physically fed. It's
                # already in the machine, so skip the slow OCR/Scryfall read and just
                # release it to the failsafe tray instead of running a full cycle.
                print("Stop requested during feed. Releasing in-flight card to bin 10 without reading.")
                subprocess.run(servo_ctrl + ["card_servo", "open"], check=True, timeout=10)
                time.sleep(2)
                subprocess.run(servo_ctrl + ["card_servo", "close"], check=True, timeout=10)
                update_images({})
                with stats_lock:
                    move_count += 1
                    monthly_move_count += 1
                    bin_counts[10] += 1
                save_move_count(move_count)
                save_monthly_move_count(monthly_move_count)
                break

            # Read the card info.
            read_env = os.environ.copy()
            read_env["SORTER_GAME"] = _game["id"]
            with lock:
                _criteria_snapshot = list(box_criteria.values())
            read_env["REQUIRE_RECOGNITION_KEYS"] = ",".join(
                games.required_recognition_keys(_game, _criteria_snapshot))
            read_env.pop("REQUIRE_SET_AND_COLLECTOR", None)
            read_stdout = ""
            read_stderr = ""
            try:
                result = subprocess.run(
                    ["python3", os.path.join(BASE_DIR, "scripts", "Read-Card.py")],
                    capture_output=True,
                    text=True,
                    env=read_env,
                    timeout=120
                )
                read_stdout = (result.stdout or "").strip()
                read_stderr = (result.stderr or "").strip()

                if result.returncode != 0:
                    print(f"Read-Card.py failed with return code {result.returncode}.")
                    card = {
                        "error": f"Read-Card.py exited with code {result.returncode}",
                        "raw_stderr": read_stderr
                    }
                else:
                    try:
                        card = parse_card_output(read_stdout)
                    except json.JSONDecodeError:
                        print("Error decoding card info. Using tray 10 as failover.")
                        card = {
                            "error": "Decoding error from Read-Card.py output",
                            "raw_stdout": read_stdout
                        }
            except subprocess.TimeoutExpired:
                print("Read-Card.py timed out after 120 seconds, routing to bin 10.")
                read_stderr = "Read-Card.py timed out after 120 seconds"
                card = {"error": "Read-Card.py timed out", "raw_stderr": read_stderr}

             
            if card.get("unsupported_game"):
                # The provider will reject every card of this game; release this one
                # to bin 10 and stop instead of failing the whole stack.
                print(f"Stopping sorting: {card['error']}")
                sorting_active = False
            if "error" in card:
                print(f"Error in card info: {card['error']}. Using tray 10 as failover.")
                selected_box = 10
                with stats_lock:
                    failed_read_count += 1
                save_failed_read_count(failed_read_count)

                # Save Failed Card Image
                timestamp = time.strftime("%Y%m%d-%H%M%S")
                save_failed_read_details(timestamp, card, read_stdout=read_stdout, read_stderr=read_stderr)
                failed_image_name = f"failed_{timestamp}.png"
                failed_image_path = os.path.join(FAILED_IMAGE_DEST, failed_image_name)
                try:
                    if os.path.exists(SCANNED_IMAGE_DEST):
                        shutil.copy(SCANNED_IMAGE_DEST, failed_image_path)
                        # shutil.copy(SCANNED_IMAGE_DEST, failed_image_path)
                        print(f"Saved failed read image: {failed_image_path}")
                except Exception as e:
                    print(f"Failed to save failed read image: {e}")
                # Save the cropped image as well
                combined_crop_src = os.path.join(BASE_DIR, "storage", "combined_crop.jpg")
                combined_crop_dest = os.path.join(FAILED_IMAGE_DEST, f"{timestamp}_combined_crop.jpg")
                try:
                    if os.path.exists(combined_crop_src):
                        shutil.copy(combined_crop_src, combined_crop_dest)
                        print(f"Saved combined crop image: {combined_crop_dest}")
                except Exception as e:
                    print(f"Failed to save combined crop image: {e}")
            else:
                if csv_enabled:
                    try:
                        append_card_to_csv(_game["id"], card)
                    except Exception as e:
                        print("Failed to append card to CSV:", e)
                
                # Update the identified-card image/name/set/number shown on the
                # Live Sorting tab, via the keys named in the game's "display" block.
                shown = card_display(_game, card)
                with stats_lock:
                    if shown["image"]:
                        card_identified_url = shown["image"]
                    card_identified_name = shown["name"]
                    card_identified_set = shown["set"]
                    card_identified_collector_number = shown["number"]
                if shown["image"]:
                    print("Updated card URL:", shown["image"])

                # Optionally save this successful scan's images/details for prompt-tuning reference.
                _scan_cfg = read_config().get("save_scans", {})
                if _scan_cfg.get("enabled"):
                    _scan_ts = time.strftime("%Y%m%d-%H%M%S")
                    save_successful_scan(_scan_ts, _game, card)
                    enforce_scan_retention(int(_scan_cfg.get("max_saved") or 200))

                # Determine the correct box.
                selected_box = None
                with lock:
                    for i in range(1, 11):
                        crit = box_criteria.get(i, {})
                        match = games.matches_criteria(_game, card, crit)
                        print(f"Checking Box {i} with criteria {crit}: match = {match}")
                        if match:
                            selected_box = i
                            break
                if selected_box is None:
                    selected_box = 10
            
            print(f"Selected Box: {selected_box} for card: {card}")

            # Process the card.
            if selected_box == 10:
                subprocess.run(servo_ctrl + ["card_servo", "open"], check=True, timeout=10)
                time.sleep(2)
                subprocess.run(servo_ctrl + ["card_servo", "close"], check=True, timeout=10)
            else:
                flapper_key = f"flapper_{selected_box}"
                subprocess.run(servo_ctrl + [flapper_key, "open"], check=True, timeout=10)
                subprocess.run(servo_ctrl + ["card_servo", "open"], check=True, timeout=10)
                time.sleep(2)
                subprocess.run(servo_ctrl + [flapper_key, "close"], check=True, timeout=10)
                subprocess.run(servo_ctrl + ["card_servo", "close"], check=True, timeout=10)
                
            
            # Update the scanned image.
            update_images(card)
        
        except subprocess.TimeoutExpired as e:
            print(f"Subprocess timed out, stopping sorting: {e}")
            sorting_active = False
            break
        except subprocess.CalledProcessError as e:
            print(f"Error during sorting process: {e}")
        except Exception as ex:
            print(f"Unexpected error: {ex}")
        
        with stats_lock:
            move_count += 1
            monthly_move_count += 1
            if selected_box is not None:
                bin_counts[selected_box] += 1
        save_move_count(move_count)
        save_monthly_move_count(monthly_move_count)
        if _is_do:
            _bal = fetch_do_balance()
            if _bal is not None:
                with stats_lock:
                    do_credits = _bal
        time.sleep(0.5)

PUBLIC_ENDPOINTS = {"login", "static"}

@app.before_request
def require_login():
    if request.endpoint in PUBLIC_ENDPOINTS or request.endpoint is None:
        return None
    if not session.get("logged_in"):
        return redirect(url_for("login", next=request.path))
    return None


@app.route("/login", methods=["GET", "POST"])
def login():
    config = read_config()
    password_hash = config["auth"]["password_hash"]
    first_run = not password_hash
    error = None

    if request.method == "POST":
        if first_run:
            new_password = request.form.get("new_password", "")
            confirm_password = request.form.get("confirm_password", "")
            if len(new_password) < 8:
                error = "Password must be at least 8 characters."
            elif new_password != confirm_password:
                error = "Passwords do not match."
            else:
                config["auth"]["password_hash"] = generate_password_hash(new_password)
                write_config(config)
                session["logged_in"] = True
                return redirect(url_for("index"))
        else:
            password = request.form.get("password", "")
            if check_password_hash(password_hash, password):
                session["logged_in"] = True
                return redirect(request.form.get("next") or url_for("index"))
            error = "Incorrect password."

    return render_template(
        "login.html",
        first_run=first_run,
        error=error,
        next=request.args.get("next", "")
    )


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/", methods=["GET", "POST"])
def index():
    global move_count, monthly_move_count, sorting_active, failed_read_count
    global sorting_thread, box_criteria, csv_enabled, card_identified_url
    global card_identified_name, card_identified_set, card_identified_collector_number, bin_counts

    error = None
    cards = {}

    if request.method == "POST":
        # Handle CSV checkbox
        csv_enabled = True if request.form.get("save_to_csv") else False

        if "start_sorting" in request.form:
            # Refuse up front if the provider can't recognize the active game
            # (e.g. a custom pack with the hosted DO Serverless service).
            with lock:
                error = games.unsupported_provider_message(active_game, read_config())
                should_start = not sorting_active and not error
                if should_start:
                    sorting_active = True
            if should_start:
                live_config = read_config()
                led_controller.apply_config(live_config.get("led"))
                sorting_thread = threading.Thread(target=sorting_loop, daemon=True)
                sorting_thread.start()

        elif "stop_sorting" in request.form:
            sorting_active = False
            if sorting_thread:
                sorting_thread.join(timeout=15)
                if sorting_thread.is_alive():
                    print("Warning: sorting thread did not stop within 15s of Stop Sorting request.")
                sorting_thread = None

        elif "reset_bins" in request.form:
            with lock:
                box_criteria = read_default_bins(active_game)
                write_bin_info(active_game, box_criteria)
                _reset_game = active_game["id"]
            print(f"Bin criteria for {_reset_game} reset to default.")


        elif "clear_csv" in request.form:
            # Clear the active game's CSV
            with lock:
                _csv = csv_path(active_game["id"])
            if os.path.exists(_csv):
                open(_csv, 'w').close()
            print("CSV file cleared.")

        elif "clear_monthly_count" in request.form:
            # Reset monthly count
            with stats_lock:
                monthly_move_count = 0
            save_monthly_move_count(0)
            print("Monthly count reset to 0.")

        elif "clear_failed_count" in request.form:
            save_failed_read_count(0)
            with stats_lock:
                failed_read_count = 0

            # Clear all files in /static/images/failed
            for filename in os.listdir(FAILED_IMAGE_DEST):
                file_path = os.path.join(FAILED_IMAGE_DEST, filename)
                try:
                    if os.path.isfile(file_path):
                        os.remove(file_path)
                except Exception as e:
                    print(f"Failed to delete {file_path}: {e}")

            print("Failed read count reset and failed images deleted.")

        elif "clear_bin_counts" in request.form:
            with stats_lock:
                bin_counts = {i: 0 for i in range(1, 11)}
            print("Bin counters reset to 0.")


    # GET or POST: Always render the index
    with stats_lock:
        _moves = move_count
        _monthly = monthly_move_count
        _url = card_identified_url
        _credits = do_credits
        _card_name = card_identified_name
        _card_set = card_identified_set
        _card_collector = card_identified_collector_number
        _bin_counts = dict(bin_counts)
    with lock:
        _sorting_active = sorting_active
        _box_criteria = box_criteria
        _game = active_game
        _games = list(game_registry.values())
    _live_config = read_config()
    return render_template(
        "index.html",
        game=_game,
        games_list=_games,
        bin_summaries={i: games.summarize_criteria(_game, _box_criteria.get(i, {})) for i in _box_criteria},
        provider_warning=games.unsupported_provider_message(_game, _live_config),
        form_field_name=games.form_field_name,
        moves=_moves,
        monthly_moves=_monthly,
        cards=cards,
        error=error,
        sorting_active=_sorting_active,
        box_criteria=_box_criteria,
        csv_enabled=csv_enabled,
        card_identified_url=_url,
        recognition_provider=_live_config.get("recognition_provider", "aws"),
        do_credits=_credits,
        card_identified_name=_card_name,
        card_identified_set=_card_set,
        card_identified_collector_number=_card_collector,
        bin_counts=_bin_counts,
    )

@app.route("/get_move_count", methods=["GET"])
def get_move_count_route():
    with stats_lock:
        _moves = move_count
        _monthly = monthly_move_count
        _failed = failed_read_count
        _url = card_identified_url
        _credits = do_credits
        _card_name = card_identified_name
        _card_set = card_identified_set
        _card_collector = card_identified_collector_number
        _bin_counts = dict(bin_counts)
    try:
        # Lets the page reload the (~100 KB) scanned image only when it changed.
        _scanned_version = os.stat(SCANNED_IMAGE_DEST).st_mtime_ns
    except OSError:
        _scanned_version = 0
    return jsonify({
        "moves": _moves,
        "monthly_moves": _monthly,
        "failed_reads": _failed,
        "card_identified_url": _url,
        "card_scanned_url": "/static/images/card_scanned.png",
        "card_scanned_version": _scanned_version,
        "credits": _credits,
        "card_identified_name": _card_name,
        "card_identified_set": _card_set,
        "card_identified_collector_number": _card_collector,
        "bin_counts": _bin_counts,
    })

@app.route("/update_bin_criteria", methods=["POST"])
def update_bin_criteria():
    global box_criteria

    try:
        bin_index = int(request.form.get("bin_index", ""))
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "invalid bin_index"}), 400
    if bin_index < 1 or bin_index > 10:
        return jsonify({"success": False, "error": "bin_index out of range"}), 400

    with lock:
        # The page posts the game it was rendered for; refuse if the active game
        # has changed since (another tab switched games), rather than writing one
        # game's form fields into another game's bins.
        if request.form.get("game_id", active_game["id"]) != active_game["id"]:
            return jsonify({"success": False, "error": "game changed, reload the page"}), 409
        box_criteria[bin_index] = games.criteria_from_form(active_game, request.form, bin_index)
        saved = write_bin_info(active_game, box_criteria)
        criteria_snapshot = box_criteria[bin_index]
        summary = games.summarize_criteria(active_game, criteria_snapshot)

    return jsonify({"success": saved, "bin_index": bin_index, "criteria": criteria_snapshot, "summary": summary})

@app.route("/set_game", methods=["POST"])
def set_game():
    """Switches the active game. Refused while sorting (one game per run)."""
    game_id = request.form.get("game_id", "")
    with lock:
        if sorting_active:
            return jsonify({"success": False, "error": "Stop sorting before switching games."}), 409
        if game_id not in game_registry:
            return jsonify({"success": False, "error": "Unknown game."}), 404
        config = read_config()
        config["active_game"] = game_id
        if not write_config(config):
            return jsonify({"success": False, "error": "Failed to save configuration."}), 500
        load_game_state(game_id)
    global card_identified_url, card_identified_name, card_identified_set, card_identified_collector_number
    with stats_lock:
        card_identified_url = ""
        card_identified_name = ""
        card_identified_set = ""
        card_identified_collector_number = ""
    return jsonify({"success": True, "game_id": game_id})

@app.route("/download_csv", methods=["GET"])
def download_csv():
    with lock:
        game_id = active_game["id"]
    path = csv_path(game_id)
    if os.path.exists(path):
        name = "card_info.csv" if game_id == games.DEFAULT_GAME_ID else f"card_info-{game_id}.csv"
        return send_file(path, as_attachment=True, download_name=name)
    else:
        return "CSV file not found.", 404


@app.route("/api/led", methods=["GET"])
def api_led_get():
    state = led_controller.get_state()
    if not state.get("enabled"):
        return jsonify(state)
    if not led_controller.is_available():
        return jsonify({
            **state,
            "error": led_controller.get_error() or "LED disabled: pigpiod unavailable",
        }), 503
    return jsonify(state)


@app.route("/api/led", methods=["POST"])
def api_led_post():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "Invalid JSON payload"}), 400

    try:
        config = read_config()
        led_cfg = config.get("led", {})
        led_cfg = normalize_led_config(led_cfg)

        if "enabled" in payload:
            led_cfg["enabled"] = bool(payload.get("enabled"))

        if "color" in payload:
            color_payload = payload.get("color")
            if isinstance(color_payload, str):
                led_cfg["color"] = parse_hex_color(color_payload)
            elif isinstance(color_payload, dict):
                if not all(k in color_payload for k in ("r", "g", "b")):
                    raise ValueError("color object must include r, g, b")
                led_cfg["color"] = {
                    "r": max(0, min(255, int(color_payload["r"]))),
                    "g": max(0, min(255, int(color_payload["g"]))),
                    "b": max(0, min(255, int(color_payload["b"]))),
                }
            else:
                raise ValueError("color must be #RRGGBB or an object with r,g,b")

        if "brightness" in payload:
            led_cfg["brightness"] = normalize_api_brightness(payload.get("brightness"))

        led_cfg = normalize_led_config(led_cfg)
        config["led"] = led_cfg
        if not write_config(config):
            return jsonify({"error": "Failed to save LED settings"}), 500

        led_controller.apply_config(led_cfg)
        if not led_cfg["enabled"]:
            led_controller.off()
        else:
            led_controller.set_color(
                led_cfg["color"]["r"],
                led_cfg["color"]["g"],
                led_cfg["color"]["b"],
                brightness=led_cfg["brightness"],
            )

        state = led_controller.get_state()
        if not state.get("enabled"):
            return jsonify(state)
        if not led_controller.is_available():
            return jsonify({
                **state,
                "error": led_controller.get_error() or "LED disabled: pigpiod unavailable",
            }), 503
        return jsonify(state)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": f"Failed to update LED settings: {exc}"}), 500


@app.route("/api/led/off", methods=["POST"])
def api_led_off():
    config = read_config()
    led_cfg = normalize_led_config(config.get("led"))
    led_cfg["enabled"] = False
    config["led"] = led_cfg
    if not write_config(config):
        return jsonify({"error": "Failed to save LED settings"}), 500

    led_controller.off()
    state = led_controller.get_state()
    if not state.get("enabled"):
        return jsonify(state)
    if not led_controller.is_available():
        return jsonify({
            **state,
            "error": led_controller.get_error() or "LED disabled: pigpiod unavailable",
        }), 503
    return jsonify(state)

@app.route("/api/test_recognition", methods=["POST"])
def api_test_recognition():
    """Runs the currently-saved Ollama prompt/parameters against the most
    recently captured card image, without needing a physical card feed.
    Uses last-saved settings — save first if you just changed something."""
    config = read_config()
    with lock:
        _game = active_game
    unsupported = games.unsupported_provider_message(_game, config)
    if unsupported:
        return jsonify({"success": False, "error": unsupported}), 400
    ollama_cfg = config.get("ollama", {})
    base_url = (ollama_cfg.get("base_url") or "").rstrip("/")
    if not base_url:
        return jsonify({"success": False, "error": "Ollama Base URL is not configured."}), 400
    if not os.path.exists(SCANNED_IMAGE_DEST):
        return jsonify({"success": False, "error": "No captured card image yet — run a scan first."}), 404

    with open(SCANNED_IMAGE_DEST, "rb") as f:
        img_b64 = base64.b64encode(f.read()).decode("utf-8")
    _prompt = games.resolve_prompt(_game, config)

    payload = {
        "model": ollama_cfg.get("model") or "minicpm-v:latest",
        "prompt": _prompt,
        "images": [img_b64],
        "stream": False,
        "format": "json",
        "options": {
            "temperature": float(ollama_cfg.get("temperature", 0.0)),
            "top_p": float(ollama_cfg.get("top_p", 0.1)),
            "top_k": int(ollama_cfg.get("top_k", 20)),
            "repeat_penalty": float(ollama_cfg.get("repeat_penalty", 1.2)),
            "num_predict": int(ollama_cfg.get("num_predict", 120)),
            "seed": int(ollama_cfg.get("seed", 42)),
        },
    }
    if config.get("recognition_provider") == "do_serverless":
        payload["game"] = _game["id"]  # DO uses its own server-side prompt per game
    api_key = ollama_cfg.get("api_key", "")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    timeout = int(ollama_cfg.get("timeout_seconds") or 60)

    try:
        r = requests.post(f"{base_url}/api/generate", json=payload, headers=headers, timeout=timeout)
    except requests.exceptions.RequestException as e:
        return jsonify({"success": False, "error": f"Request failed: {e}"}), 502

    if r.status_code >= 400:
        return jsonify({"success": False, "error": f"HTTP {r.status_code}: {r.text[:2000]}"}), 502

    try:
        data = r.json()
    except ValueError:
        return jsonify({"success": False, "error": "Response was not valid JSON.", "raw": r.text[:2000]}), 502

    return jsonify({"success": True, "response_text": data.get("response", ""), "raw": data})

@app.route("/api/clear_saved_scans", methods=["POST"])
def api_clear_saved_scans():
    try:
        for filename in os.listdir(SUCCESS_SCAN_DEST):
            file_path = os.path.join(SUCCESS_SCAN_DEST, filename)
            if os.path.isfile(file_path):
                os.remove(file_path)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

def render_settings(config, error=None, games_message=None):
    with lock:
        _game = active_game
        _games = list(game_registry.values())
    return render_template(
        "settings.html",
        config=config,
        error=error,
        game=_game,
        games_list=_games,
        pack_summaries=[game_packs.pack_summary(g) for g in _games],
        game_prompt=games.game_settings(config, _game["id"]).get("prompt")
            or (config.get("ollama", {}).get("prompt", "") if _game["id"] == games.DEFAULT_GAME_ID else ""),
        games_message=games_message,
    )

@app.route("/settings", methods=["GET", "POST"])
def settings():
    error = None
    if request.method == "POST":
        # Handle reboot/shutdown requests
        action = request.form.get("system_action")
        if action == "reboot":
            print("Reboot requested.")
            os.system("sudo reboot")
            return "Rebooting...", 200
        elif action == "shutdown":
            print("Shutdown requested.")
            config = read_config()
            send_shutdown_summary_email(config)
            os.system("sudo shutdown now")
            return "Shutting down...", 200

        if "save" in request.form:
            config = read_config()
            config.setdefault("aws", {})
            config.setdefault("ollama", {})
            config.setdefault("recognition_provider", "aws")
            config["system_name"] = request.form.get("system_name", "")
            config["aws"]["access_key_id"] = request.form.get("aws_access_key_id", "")
            config["aws"]["secret_access_key"] = request.form.get("aws_secret_access_key", "")
            config["aws"]["region_name"] = request.form.get("aws_region_name", "")
            config["scryfall_search_url"] = request.form.get("scryfall_search_url", "")
            config["recognition_provider"] = request.form.get("recognition_provider", "aws")
            config["ollama"]["base_url"] = request.form.get("ollama_base_url", "").strip()
            config["ollama"]["model"] = request.form.get("ollama_model", "").strip()
            config["ollama"]["api_key"] = request.form.get("ollama_api_key", "").strip()
            # The prompt box edits the active game's prompt override.
            with lock:
                _gid = active_game["id"]
            config.setdefault("games", {}).setdefault(_gid, {})["prompt"] = request.form.get("ollama_prompt", "").strip()
            if _gid == games.DEFAULT_GAME_ID:
                config["ollama"].pop("prompt", None)  # migrated into games.mtg.prompt
            config["ollama"]["debug"] = True if request.form.get("ollama_debug") else False
            try:
                config["ollama"]["timeout_seconds"] = int(request.form.get("ollama_timeout_seconds") or 60)
                config["ollama"]["min_confidence"] = float(request.form.get("ollama_min_confidence") or 0.80)
                config["ollama"]["temperature"] = float(request.form.get("ollama_temperature") or 0.0)
                config["ollama"]["top_p"] = float(request.form.get("ollama_top_p") or 0.1)
                config["ollama"]["top_k"] = int(request.form.get("ollama_top_k") or 20)
                config["ollama"]["repeat_penalty"] = float(request.form.get("ollama_repeat_penalty") or 1.2)
                config["ollama"]["num_predict"] = int(request.form.get("ollama_num_predict") or 120)
                config["ollama"]["seed"] = int(request.form.get("ollama_seed") or 42)
            except ValueError:
                error = "Recognition parameters must be numbers."
                return render_settings(config, error)

            if "save_scans" not in config or not isinstance(config["save_scans"], dict):
                config["save_scans"] = {}
            config["save_scans"]["enabled"] = True if request.form.get("save_scans_enabled") else False
            try:
                config["save_scans"]["max_saved"] = int(request.form.get("save_scans_max") or 200)
            except ValueError:
                error = "Max Saved Scans must be a whole number."
                return render_settings(config, error)

            if "smtp" not in config:
                config["smtp"] = {}  # Ensure smtp key exists in config
            config["smtp"]["server"] = request.form.get("smtp_server", "")
            config["smtp"]["port"] = int(request.form.get("smtp_port", 587))
            config["smtp"]["username"] = request.form.get("smtp_username", "")
            config["smtp"]["password"] = request.form.get("smtp_password", "")
            config["smtp"]["from_email"] = request.form.get("smtp_from", "")
            config["smtp"]["to_email"] = request.form.get("smtp_to", "")
            config["smtp"]["enabled"] = True if request.form.get("smtp_enabled") else False
            for flapper in config.get("flappers", {}):
                open_field = f"{flapper}_open_degrees"
                close_field = f"{flapper}_close_degrees"
                open_val = request.form.get(open_field, "")
                close_val = request.form.get(close_field, "")
                if open_val != "":
                    config["flappers"][flapper]["open_degrees"] = int(open_val)
                if close_val != "":
                    config["flappers"][flapper]["close_degrees"] = int(close_val)
            open_val = request.form.get("card_servo_open_degrees", "")
            close_val = request.form.get("card_servo_close_degrees", "")
            if open_val != "":
                config["card_servo"]["open_degrees"] = int(open_val)
            if close_val != "":
                config["card_servo"]["close_degrees"] = int(close_val)
            if "feed" not in config or not isinstance(config["feed"], dict):
                config["feed"] = {}
            motor2_extra = request.form.get("motor2_extra_feed_sec", "").strip()
            if motor2_extra != "":
                try:
                    config["feed"]["motor2_extra_feed_sec"] = float(motor2_extra)
                except ValueError:
                    error = "Motor 2 Extra Feed Time must be a number (example: 1.2)."
                    return render_settings(config, error)
            motor3_extra = request.form.get("motor3_extra_feed_sec", "").strip()
            if motor3_extra != "":
                try:
                    config["feed"]["motor3_extra_feed_sec"] = float(motor3_extra)
                except ValueError:
                    error = "Motor 3 Extra Feed Time must be a number (example: 0.0)."
                    return render_settings(config, error)
            sensor1_block_timeout = request.form.get("sensor1_block_timeout_sec", "").strip()
            if sensor1_block_timeout != "":
                try:
                    config["feed"]["sensor1_block_timeout_sec"] = float(sensor1_block_timeout)
                except ValueError:
                    error = "Phase 1 (Initial Feed-In) Timeout must be a number (example: 8.0)."
                    return render_settings(config, error)
            sensor1_clear_timeout = request.form.get("sensor1_clear_timeout_sec", "").strip()
            if sensor1_clear_timeout != "":
                try:
                    config["feed"]["sensor1_clear_timeout_sec"] = float(sensor1_clear_timeout)
                except ValueError:
                    error = "Phase 2 (Anti-Double-Feed Reverse) Timeout must be a number (example: 5.0)."
                    return render_settings(config, error)
            sensor2_block_timeout = request.form.get("sensor2_block_timeout_sec", "").strip()
            if sensor2_block_timeout != "":
                try:
                    config["feed"]["sensor2_block_timeout_sec"] = float(sensor2_block_timeout)
                except ValueError:
                    error = "Phase 3 (Exit Routing) Timeout must be a number (example: 6.0)."
                    return render_settings(config, error)
            feed_max_attempts = request.form.get("feed_max_attempts", "").strip()
            if feed_max_attempts != "":
                try:
                    max_attempts_val = int(feed_max_attempts)
                    if max_attempts_val < 1:
                        raise ValueError
                    config["feed"]["max_attempts"] = max_attempts_val
                except ValueError:
                    error = "Feed Cycle Max Attempts must be a whole number of 1 or more (example: 3)."
                    return render_settings(config, error)
            config["feed"]["debug_logging"] = True if request.form.get("feed_debug_logging") else False
            if write_config(config):
                return redirect(url_for("index"))
            else:
                error = "Failed to save configuration."
                return render_settings(config, error)
        elif "cancel" in request.form:
            return redirect(url_for("index"))
    else:
        config = read_config()
        return render_settings(config, error)
    
# ---------------------------------------------------------------------------
# Game packs (Settings -> Games): upload / export / delete
# ---------------------------------------------------------------------------
GAME_PACK_MAX_UPLOAD_BYTES = 50 * 1024 * 1024

@app.route("/games/upload", methods=["POST"])
def games_upload():
    upload = request.files.get("pack")
    if not upload or not upload.filename:
        return render_settings(read_config(), games_message="Choose a .zip game pack to upload.")
    if request.content_length and request.content_length > GAME_PACK_MAX_UPLOAD_BYTES + 1024 * 1024:
        return render_settings(read_config(), games_message="Game pack is larger than 50 MB.")
    replace = bool(request.form.get("replace"))
    # Decide the message while holding `lock`, but render after releasing it:
    # render_settings takes `lock` itself and it is not re-entrant.
    with lock:
        if sorting_active:
            message = "Stop sorting before installing a game pack."
        else:
            try:
                game_id = game_packs.install_pack(upload.stream, game_registry, replace=replace)
            except game_packs.PackError as e:
                message = f"Upload rejected: {e}"
            else:
                load_game_state(active_game["id"])
                message = f"Installed game pack: {game_registry.get(game_id, {}).get('name', game_id)}."
    return render_settings(read_config(), games_message=message)

@app.route("/games/<game_id>/export", methods=["GET"])
def games_export(game_id):
    with lock:
        game = game_registry.get(game_id)
    if not game:
        return "Unknown game.", 404
    data = game_packs.export_pack(game)
    return send_file(io.BytesIO(data), mimetype="application/zip", as_attachment=True,
                     download_name=f"{game_id}-game-pack.zip")

@app.route("/games/<game_id>/delete", methods=["POST"])
def games_delete(game_id):
    deleted = False
    with lock:  # render_settings re-takes `lock`, so only decide the message here
        if sorting_active:
            message = "Stop sorting before deleting a game pack."
        elif active_game["id"] == game_id:
            message = "Switch to another game before deleting this one."
        else:
            try:
                game_packs.delete_pack(game_id, game_registry)
            except game_packs.PackError as e:
                message = f"Delete failed: {e}"
            else:
                load_game_state(active_game["id"])
                deleted = True
                message = f"Deleted game pack: {game_id}."
    config = read_config()
    if deleted:
        # Remove the deleted game's saved bins and settings; its sorted-card CSV
        # is user data and is kept.
        if os.path.isfile(bin_info_path(game_id)):
            os.remove(bin_info_path(game_id))
        if isinstance(config.get("games"), dict) and config["games"].pop(game_id, None) is not None:
            write_config(config)
    return render_settings(config, games_message=message)

@app.route("/update_program", methods=["POST"])
def update_program():
    with lock:
        if sorting_active:
            return jsonify({"success": False, "message": "Stop sorting before updating the program."}), 409
    try:
        result = subprocess.check_output(["git", "-C", BASE_DIR, "pull"], stderr=subprocess.STDOUT)
        message = result.decode()
        if "Already up to date" in message:
            return jsonify({"success": True, "message": message})

        def _restart_service():
            time.sleep(1)
            os.system("sudo systemctl restart card-sorter.service")

        threading.Thread(target=_restart_service, daemon=True).start()
        message += "\n\nRestarting the app to apply the update..."
        return jsonify({"success": True, "message": message})
    except subprocess.CalledProcessError as e:
        return jsonify({"success": False, "message": e.output.decode()}), 500
    
@app.route("/failed")
def failed_gallery():
    try:
        files = os.listdir(FAILED_IMAGE_DEST)
        entries_by_ts = {}

        for filename in files:
            if not filename.startswith("failed_"):
                continue

            if filename.endswith(".png"):
                ts = filename.replace("failed_", "").replace(".png", "")
                entries_by_ts.setdefault(ts, {"timestamp": ts, "has_scanned": False, "has_crop": False, "details": {}})
                entries_by_ts[ts]["has_scanned"] = True
            elif filename.endswith(".json"):
                ts = filename.replace("failed_", "").replace(".json", "")
                entries_by_ts.setdefault(ts, {"timestamp": ts, "has_scanned": False, "has_crop": False, "details": {}})
                details_path = os.path.join(FAILED_IMAGE_DEST, filename)
                try:
                    with open(details_path, "r", encoding="utf-8") as f:
                        entries_by_ts[ts]["details"] = json.load(f)
                except Exception as e:
                    entries_by_ts[ts]["details"] = {"error": f"Failed to read details file: {e}"}

        for ts, entry in entries_by_ts.items():
            combined_crop_name = f"{ts}_combined_crop.jpg"
            combined_crop_path = os.path.join(FAILED_IMAGE_DEST, combined_crop_name)
            entry["has_crop"] = os.path.exists(combined_crop_path)

        entries = sorted(entries_by_ts.values(), key=lambda x: x["timestamp"], reverse=True)
    except Exception as e:
        print(f"Error loading failed images: {e}")
        entries = []

    return render_template("failed.html", entries=entries)

FEED_LOG_TAIL_LINES = 1000

@app.route("/feed_log")
def feed_log():
    lines = []
    if os.path.exists(FEED_LOG_FILE):
        try:
            with open(FEED_LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()[-FEED_LOG_TAIL_LINES:]
        except Exception as e:
            lines = [f"Error reading log file: {e}\n"]
    log_text = "".join(lines)
    config = read_config()
    logging_enabled = config.get("feed", {}).get("debug_logging", False)
    return render_template(
        "feed_log.html",
        log_text=log_text,
        logging_enabled=logging_enabled,
        tail_lines=FEED_LOG_TAIL_LINES,
    )

@app.route("/feed_log/download")
def feed_log_download():
    if not os.path.exists(FEED_LOG_FILE):
        return "No feed log file yet.", 404
    return send_file(FEED_LOG_FILE, as_attachment=True, download_name="feed_debug.log")

@app.route("/feed_log/clear", methods=["POST"])
def feed_log_clear():
    try:
        open(FEED_LOG_FILE, "w").close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/camera_test", methods=["GET", "POST"])
def camera_test():
    config = read_config()
    error = None

    # Ensure camera_crop and subkeys exist to prevent template errors
    if "camera_crop" not in config:
        config["camera_crop"] = {
            "top_crop": {"x1": 160, "y1": 155, "x2": 577, "y2": 235},
            "bottom_crop": {"x1": 160, "y1": 828, "x2": 577, "y2": 885}
        }

    # Whole-card crop (game-independent: the card sits in the same spot for
    # every game). Unset = Read-Card.py sends the full processed image, which
    # is 756x960 (1920x1080 capture, margins trimmed, rotated 90°).
    if not isinstance(config.get("card_crop"), dict):
        config["card_crop"] = {"x1": 0, "y1": 0, "x2": 756, "y2": 960}

    if request.method == "POST":
        try:
            config["card_crop"] = {
                "x1": int(request.form.get("card_x1", 0)),
                "y1": int(request.form.get("card_y1", 0)),
                "x2": int(request.form.get("card_x2", 756)),
                "y2": int(request.form.get("card_y2", 960))
            }
            config["camera_crop"] = {
                "top_crop": {
                    "x1": int(request.form.get("top_x1", 0)),
                    "y1": int(request.form.get("top_y1", 0)),
                    "x2": int(request.form.get("top_x2", 672)),
                    "y2": int(request.form.get("top_y2", 300))
                },
                "bottom_crop": {
                    "x1": int(request.form.get("bottom_x1", 0)),
                    "y1": int(request.form.get("bottom_y1", 0)),
                    "x2": int(request.form.get("bottom_x2", 672)),
                    "y2": int(request.form.get("bottom_y2", 300))
                }
            }

            write_config(config)
            result = subprocess.run(["python3", os.path.join(BASE_DIR, "scripts", "Test-Camera.py")],
                                    capture_output=True, text=True, timeout=30)
            if result.returncode != 0:
                detail = (result.stdout.strip() or result.stderr.strip())[-500:]
                try:
                    detail = json.loads(detail).get("error", detail)
                except (ValueError, AttributeError):
                    pass
                error = f"Settings saved, but the test capture failed: {detail}"
        except Exception as e:
            error = f"Failed to update camera settings: {str(e)}"

    # Cache-buster so the previews always show the latest capture
    return render_template("camera_test.html", config=config, error=error, cache_bust=int(time.time()))
    
@app.route("/run_script")
def run_script():
    raw_script = request.args.get("script")
    if not raw_script:
        return "No script specified", 400

    # Decode the script path
    script_rel_path = urllib.parse.unquote(raw_script)

    # Combine with base directory and resolve any ".." / symlink components
    # before checking containment, so a resolved path can't string-match its
    # way past a naive startswith() check.
    scripts_dir = os.path.realpath(os.path.join(BASE_DIR, "scripts"))
    script_path = os.path.realpath(os.path.join(scripts_dir, script_rel_path))

    # Security check: prevent escaping out of the scripts directory
    if os.path.commonpath([script_path, scripts_dir]) != scripts_dir:
        return "Invalid script path", 403

    # Check existence
    if not os.path.exists(script_path):
        return f"Script not found: {script_path}", 404

    # Optional positional args (space-separated, no shell expansion)
    raw_args = request.args.get("args", "").strip()
    cmd = ["python3", script_path] + (raw_args.split() if raw_args else [])

    # Execute the script
    try:
        output = subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=15)
        return output.decode()
    except subprocess.TimeoutExpired:
        return "Script timed out", 500
    except subprocess.CalledProcessError as e:
        return f"Script failed:\n{e.output.decode()}", 500

# Each open live view holds one of waitress's 8 threads for as long as it's open.
STREAM_VIEWER_LIMIT = 3
stream_slots = threading.BoundedSemaphore(STREAM_VIEWER_LIMIT)

@app.route("/camera/stream", methods=["GET"])
def camera_stream():
    """Live camera view, relayed from camera_service.py so it sits behind the login."""
    if not stream_slots.acquire(blocking=False):
        return "Too many live views open", 503
    try:
        upstream = requests.get(camera_client.CAMERA_SERVICE_URL + "/stream.mjpg", stream=True, timeout=(2, 10))
        upstream.raise_for_status()
    except requests.RequestException as e:
        stream_slots.release()
        return f"Camera service unavailable: {e}", 503

    def release():
        # Runs when the viewer disconnects (waitress closes the response), even
        # if the relay never started, so the slot and upstream aren't leaked.
        upstream.close()
        stream_slots.release()

    def relay():
        try:
            yield from upstream.iter_content(chunk_size=4096)
        except requests.RequestException:
            pass  # service stopped or stalled; the page shows "Live view unavailable"

    response = Response(relay(), content_type=upstream.headers.get("Content-Type"))
    response.headers["Cache-Control"] = "no-store"
    response.call_on_close(release)
    return response

@app.route("/sensor_status", methods=["GET"])
def sensor_status():
    return jsonify(read_sensor_status())

@app.route("/sensors", methods=["GET"])
def sensors_page():
    return render_template("sensors.html")


if __name__ == "__main__":
    # Served via waitress (multi-threaded, single process) instead of the
    # Flask/Werkzeug dev server, which handles only one request at a time and
    # would stall every client (including the 2s status-polling loop) while a
    # single slow request — a servo test, camera test, or git pull — runs.
    # Single process is kept deliberately so only one thread ever drives
    # pigpio/LEDs at a time internally (protected by the existing locks).
    from waitress import serve
    serve(app, host='0.0.0.0', port=5000, threads=8)
