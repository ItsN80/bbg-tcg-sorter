#!/usr/bin/env python3

from flask import Flask, render_template, request, jsonify, redirect, url_for, send_file, session
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
import pigpio
import urllib.parse
import smtplib
from email.mime.text import MIMEText
from werkzeug.security import generate_password_hash, check_password_hash
from led_controller import LEDController, normalize_led_config

app = Flask(__name__)

# Global variables
sorting_active = False      # Whether the sorting loop is active
sorting_thread = None       # Thread running the sorting loop
box_criteria = {}           # Dictionary mapping box numbers (1-10) to criteria
lock = threading.Lock()       # Protects sorting_active, box_criteria
stats_lock = threading.Lock() # Protects move_count, monthly_move_count, failed_read_count, card_identified_url

# Global File Path
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Global variables for CSV saving
csv_enabled = False  # Whether to append card output to CSV (set via a checkbox on the main page)
CSV_FILE = os.path.join(BASE_DIR, "storage", "card_info.csv")
csv_lock = threading.Lock()  # Protects CSV file access

# File Paths
COUNTER_FILE = os.path.join(BASE_DIR, "counters", "move_count.txt")
MONTHLY_COUNTER_FILE = os.path.join(BASE_DIR, "counters", "monthly_move_count.txt")
FAILED_READS_FILE = os.path.join(BASE_DIR, "counters", "failed_reads.txt")
CONFIG_FILE = os.path.join(BASE_DIR, "storage", "config.json")
BIN_INFO_FILE = os.path.join(BASE_DIR, "storage", "bin-info.json")
BIN_INFO_DEFAULT_FILE = os.path.join(BASE_DIR, "storage", "bin-info-default.json")
SCANNED_IMAGE_SRC = os.path.join(BASE_DIR, "storage", "scanned_card.png")
SCANNED_IMAGE_DEST = os.path.join(BASE_DIR, "static", "images", "card_scanned.png")
IDENTIFIED_IMAGE_DEST = os.path.join(BASE_DIR, "static", "images", "card_identified.png")
FAILED_IMAGE_DEST = os.path.join(BASE_DIR, "static", "images", "failed")

#Create Paths if they do not exist
os.makedirs(FAILED_IMAGE_DEST, exist_ok=True)

# Check if config.json exists; if not, copy config-default.json as config.json
if not os.path.isfile(CONFIG_FILE):
    default_config_file = os.path.join(BASE_DIR, "storage", "config-default.json")
    if os.path.exists(default_config_file):
        shutil.copy(default_config_file, CONFIG_FILE)
        print("Default configuration file created from config-default.json.")
    else:
        print("Default configuration file config-default.json not found. Please create one.")

# Check if bin-info.json exists; if not, copy bin-info-default.json as bin-info.json
if not os.path.isfile(BIN_INFO_FILE):
    if os.path.exists(BIN_INFO_DEFAULT_FILE):
        shutil.copy(BIN_INFO_DEFAULT_FILE, BIN_INFO_FILE)
        print("Default bin info file created from bin-info-default.json.")
    else:
        print("Default bin info file bin-info-default.json not found. Please create one.")
        
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

TYPE_MODIFIER_TAGS = ["Legendary", "Snow", "Token", "Basic"]
TYPE_BASE_TAGS = ["Artifact", "Enchantment", "Creature", "Instant", "Sorcery", "Land", "Planeswalker", "Battle"]
ALL_TYPE_TAGS = TYPE_MODIFIER_TAGS + TYPE_BASE_TAGS

def default_box_criteria():
    return {
        i: {"name": "", "type_tags": [], "colors": [], "cmc": "", "set_symbol": ""}
        for i in range(1, 11)
    }

def normalize_criteria_value(raw_value):
    if not isinstance(raw_value, dict):
        raw_value = {}
    colors_raw = raw_value.get("colors", [])
    colors = []
    if isinstance(colors_raw, list):
        colors = [c for c in colors_raw if c in ["W", "U", "B", "R", "G", "C"]]

    type_tags_raw = raw_value.get("type_tags")
    if type_tags_raw is None:
        # Legacy migration: bins saved before the type-tag checkboxes used a single
        # combined string (e.g. "Legendary Creature") — split it into recognized tags.
        legacy_type = str(raw_value.get("type", "")).strip()
        type_tags_raw = legacy_type.split()
    if not isinstance(type_tags_raw, list):
        type_tags_raw = []
    type_tags = [t for t in ALL_TYPE_TAGS if t in type_tags_raw]

    return {
        "name": str(raw_value.get("name", "")).strip(),
        "type_tags": type_tags,
        "colors": colors,
        "cmc": str(raw_value.get("cmc", "")).strip(),
        "set_symbol": str(raw_value.get("set_symbol", "")).strip(),
    }

def normalize_box_criteria(raw):
    normalized = default_box_criteria()
    if not isinstance(raw, dict):
        return normalized

    for raw_key, raw_value in raw.items():
        try:
            box_num = int(raw_key)
        except (TypeError, ValueError):
            continue

        if box_num < 1 or box_num > 10 or not isinstance(raw_value, dict):
            continue

        normalized[box_num] = normalize_criteria_value(raw_value)

    return normalized

def read_bin_info():
    try:
        with open(BIN_INFO_FILE, "r", encoding="utf-8") as f:
            return normalize_box_criteria(json.load(f))
    except Exception as e:
        print("Error reading bin info file:", e)
        return default_box_criteria()

def write_bin_info(criteria):
    try:
        normalized = normalize_box_criteria(criteria)
        serialized = {str(i): normalized.get(i, {}) for i in range(1, 11)}
        with open(BIN_INFO_FILE, "w", encoding="utf-8") as f:
            json.dump(serialized, f, indent=4)
        return True
    except Exception as e:
        print("Error writing bin info file:", e)
        return False

with lock:
    box_criteria = read_bin_info()

def matches_criteria(card, criteria):
    # If no criteria specified, do not consider it a match.
    if not (criteria.get("name") or
            criteria.get("type_tags") or
            criteria.get("cmc") or
            criteria.get("set_symbol") or
            criteria.get("colors")):
        return False

    if criteria.get("name"):
        name_crit = criteria["name"].strip()
        card_name = card.get("name", "").strip()
        if "-" in name_crit:
            parts = name_crit.split("-")
            if len(parts) == 2 and len(parts[0].strip()) == 1 and len(parts[1].strip()) == 1:
                start_letter = parts[0].strip().upper()
                end_letter = parts[1].strip().upper()
                if not card_name:
                    return False
                first_letter = card_name[0].upper()
                if first_letter < start_letter or first_letter > end_letter:
                    return False
            else:
                if name_crit.lower() not in card_name.lower():
                    return False
        else:
            if name_crit.lower() not in card_name.lower():
                return False

    if criteria.get("type_tags"):
        card_type_line = card.get("type", "").lower()
        for tag in criteria["type_tags"]:
            if tag.lower() not in card_type_line:
                return False

    if criteria.get("cmc"):
        try:
            if float(criteria["cmc"]) != float(card.get("cmc", 0)):
                return False
        except ValueError:
            return False

    if criteria.get("set_symbol"):
        if criteria["set_symbol"].lower() not in card.get("set_symbol", "").lower():
            return False

    if criteria.get("colors"):
        crit_colors = set(criteria["colors"])
        # Match by color identity first, then fallback to colors for older payloads.
        card_colors = set(card.get("color_identity", card.get("colors", [])))
        if crit_colors == {"C"}:
            if card_colors:
                return False
        else:
            if crit_colors != card_colors:
                return False

    return True

def any_box_has_set_symbol():
    with lock:
        criteria_snapshot = list(box_criteria.values())
    return any(str((crit or {}).get("set_symbol", "")).strip() for crit in criteria_snapshot)

def append_card_to_csv(card):
    # Use card keys as field names.
    fieldnames = list(card.keys())
    file_exists = os.path.isfile(CSV_FILE) and os.path.getsize(CSV_FILE) > 0
    with csv_lock:
        with open(CSV_FILE, 'a', newline='') as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            if not file_exists:
                writer.writeheader()
            writer.writerow(card)

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
        "recognition_response": card.get("recognition", {}),
        "scryfall_responses": card.get("scryfall_responses", []),
        "raw_read_stdout": read_stdout,
        "raw_read_stderr": read_stderr
    }
    details_path = os.path.join(FAILED_IMAGE_DEST, f"failed_{timestamp}.json")
    try:
        with open(details_path, "w", encoding="utf-8") as f:
            json.dump(details, f, indent=2)
    except Exception as e:
        print(f"Failed to save failed read details: {e}")

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
    led_controller.set_mode("sorting")
    _led_state = led_controller.get_state()
    _idle_color = (
        _led_state.get("color", {}).get("r", 0),
        _led_state.get("color", {}).get("g", 140),
        _led_state.get("color", {}).get("b", 255),
    )
    _live_config = read_config()
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
            read_env["REQUIRE_SET_AND_COLLECTOR"] = "1" if any_box_has_set_symbol() else "0"
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

             
            if "error" in card:
                print(f"Error in card info: {card['error']}. Using tray 10 as failover.")
                led_controller.set_color(255, 30, 30)
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
                        append_card_to_csv(card)
                    except Exception as e:
                        print("Failed to append card to CSV:", e)
                
                # Update the global URL from the API data.
                if "card_identified_url" in card and card["card_identified_url"]:
                    with stats_lock:
                        card_identified_url = card["card_identified_url"]
                    print("Updated card URL:", card["card_identified_url"])

                # Update the global identified-card name/set/collector number.
                with stats_lock:
                    card_identified_name = card.get("name", "")
                    card_identified_set = card.get("set_symbol", "")
                    card_identified_collector_number = card.get("collector_number", "")
                
                # Determine the correct box.
                selected_box = None
                with lock:
                    for i in range(1, 11):
                        crit = box_criteria.get(i, {})
                        match = matches_criteria(card, crit)
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
        
        led_controller.set_color(*_idle_color)
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
    led_controller.set_color(*_idle_color)

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
    global move_count, monthly_move_count, sorting_active
    global sorting_thread, box_criteria, csv_enabled, card_identified_url
    global card_identified_name, card_identified_set, card_identified_collector_number, bin_counts

    error = None
    cards = {}

    if request.method == "POST":
        # Handle CSV checkbox
        csv_enabled = True if request.form.get("save_to_csv") else False

        if "start_sorting" in request.form:
            # ...
            with lock:
                should_start = not sorting_active
                if should_start:
                    sorting_active = True
            if should_start:
                live_config = read_config()
                led_controller.apply_config(live_config.get("led"))
                sorting_thread = threading.Thread(target=sorting_loop, daemon=True)
                sorting_thread.start()

        elif "stop_sorting" in request.form:
            sorting_active = False
            led_controller.off()
            if sorting_thread:
                sorting_thread.join(timeout=15)
                if sorting_thread.is_alive():
                    print("Warning: sorting thread did not stop within 15s of Stop Sorting request.")
                sorting_thread = None

        elif "reset_bins" in request.form:
            with lock:
                if os.path.exists(BIN_INFO_DEFAULT_FILE):
                    try:
                        shutil.copy(BIN_INFO_DEFAULT_FILE, BIN_INFO_FILE)
                    except Exception as e:
                        print(f"Failed to reset bin info from default file: {e}")
                        box_criteria = default_box_criteria()
                        write_bin_info(box_criteria)
                    else:
                        box_criteria = read_bin_info()
                else:
                    print("bin-info-default.json not found. Resetting to empty criteria.")
                    box_criteria = default_box_criteria()
                    write_bin_info(box_criteria)
            print("Bin criteria reset to default.")


        elif "clear_csv" in request.form:
            # Clear CSV
            if os.path.exists(CSV_FILE):
                open(CSV_FILE, 'w').close()
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
    _live_config = read_config()
    return render_template(
        "index.html",
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
    return jsonify({
        "moves": _moves,
        "monthly_moves": _monthly,
        "failed_reads": _failed,
        "card_identified_url": _url,
        "card_scanned_url": "/static/images/card_scanned.png",
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

    raw_crit = {
        "name": request.form.get(f"name{bin_index}", ""),
        "type_tags": [t for t in ALL_TYPE_TAGS if request.form.get(f"type_{t}{bin_index}")],
        "colors": [c for c in ["W", "U", "B", "R", "G", "C"] if request.form.get(f"{c}{bin_index}")],
        "cmc": request.form.get(f"cmc{bin_index}", ""),
        "set_symbol": request.form.get(f"set_symbol{bin_index}", ""),
    }

    with lock:
        box_criteria[bin_index] = normalize_criteria_value(raw_crit)
        saved = write_bin_info(box_criteria)
        criteria_snapshot = box_criteria[bin_index]

    return jsonify({"success": saved, "bin_index": bin_index, "criteria": criteria_snapshot})

@app.route("/download_csv", methods=["GET"])
def download_csv():
    if os.path.exists(CSV_FILE):
        return send_file(CSV_FILE, as_attachment=True, download_name="card_info.csv")
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
                    return render_template("settings.html", config=config, error=error)
            if write_config(config):
                return redirect(url_for("index"))
            else:
                error = "Failed to save configuration."
                return render_template("settings.html", config=config, error=error)
        elif "cancel" in request.form:
            return redirect(url_for("index"))
    else:
        config = read_config()
        return render_template("settings.html", config=config, error=error)
    
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

    if request.method == "POST":
        try:
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
            subprocess.run(["python3", os.path.join(BASE_DIR, "scripts", "Test-Camera.py")], check=True, timeout=30)
        except Exception as e:
            error = f"Failed to update camera settings: {str(e)}"

    return render_template("camera_test.html", config=config, error=error)
    
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
