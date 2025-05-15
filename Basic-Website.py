from flask import Flask, render_template, request, jsonify, redirect, url_for, send_file
import subprocess
import os
import threading
import time
import json  # for parsing card info output
import csv   # for writing CSV files
import shutil  # for copying files
import requests  # for downloading images
import pigpio
import urllib.parse

app = Flask(__name__)

# Global variables
sorting_active = False      # Whether the sorting loop is active
sorting_thread = None       # Thread running the sorting loop
box_criteria = {}           # Dictionary mapping box numbers (1-10) to criteria
lock = threading.Lock()     # Protects access to box_criteria

# Global File Path
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Global variables for CSV saving
csv_enabled = False  # Whether to append card output to CSV (set via a checkbox on the main page)
CSV_FILE = os.path.join(BASE_DIR, "storage", "card_info.csv")
csv_lock = threading.Lock()  # Protects CSV file access

# File Paths
COUNTER_FILE = os.path.join(BASE_DIR, "counters", "move_count.txt")
MONTHLY_COUNTER_FILE = os.path.join(BASE_DIR, "counters", "monthly_move_count.txt")
CONFIG_FILE = os.path.join(BASE_DIR, "storage", "config.json")
SCANNED_IMAGE_SRC = os.path.join(BASE_DIR, "storage", "scanned_card.png")
SCANNED_IMAGE_DEST = os.path.join(BASE_DIR, "static", "images", "card_scanned.png")
IDENTIFIED_IMAGE_DEST = os.path.join(BASE_DIR, "static", "images", "card_identified.png")

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

# Initialize pigpio globally
pi = pigpio.pi()
if not pi.connected:
    print("Failed to connect to pigpio daemon.")
    exit()

def get_move_count():
    try:
        with open(COUNTER_FILE, "r") as f:
            return int(f.read())
    except FileNotFoundError:
        return 0

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

def read_config():
    try:
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    except Exception as e:
        print("Error reading config file:", e)
        return {}

def write_config(config):
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(config, f, indent=4)
        return True
    except Exception as e:
        print("Error writing config file:", e)
        return False

def matches_criteria(card, criteria):
    # If no criteria specified, do not consider it a match.
    if not (criteria.get("name") or 
            (criteria.get("type") and criteria.get("type").lower() != "-none-") or 
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

    if criteria.get("type") and criteria["type"].lower() != "-none-":
        if criteria["type"].lower() not in card.get("type", "").lower():
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
        card_colors = set(card.get("colors", []))
        if crit_colors == {"C"}:
            if card_colors:
                return False
        else:
            if crit_colors != card_colors:
                return False

    return True

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

def sorting_loop():
    global move_count, monthly_move_count, sorting_active, sorting_thread, csv_enabled, card_identified_url
    while sorting_active:
        try:
            # Feed a new card.
            feed_result = subprocess.run(["python3", os.path.join(BASE_DIR, "scripts", "Feed-Card.py")], capture_output=True, text=True)
            if feed_result.returncode != 0:
                print("Feed failed (return code: {}), stopping sorting.".format(feed_result.returncode))
                sorting_active = False
                break
            
            # Read the card info.
            result = subprocess.run(["python3", os.path.join(BASE_DIR, "scripts", "Read-Card.py")],capture_output=True, text=True, check=True)
            try:
                card = json.loads(result.stdout)
            except json.JSONDecodeError:
                print("Error decoding card info. Using tray 10 as failover.")
                card = {"error": "Decoding error"}
            
            if "error" in card:
                print(f"Error in card info: {card['error']}. Using tray 10 as failover.")
                selected_box = 10
            else:
                if csv_enabled:
                    try:
                        append_card_to_csv(card)
                    except Exception as e:
                        print("Failed to append card to CSV:", e)
                
                # Update the global URL from the API data.
                if "card_identified_url" in card and card["card_identified_url"]:
                    card_identified_url = card["card_identified_url"]
                    print("Updated card URL:", card_identified_url)
                
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
                subprocess.run(["python3", os.path.join(BASE_DIR, "scripts", "Card-Release.py")], check=True)
                time.sleep(2)
                subprocess.run(["python3", os.path.join(BASE_DIR, "scripts", "Card-Capture.py")], check=True)
            else:
                flapper_open_script = os.path.join(BASE_DIR, "scripts", f"Flapper-{selected_box}_Open.py")
                subprocess.run(["python3", flapper_open_script], check=True)
                subprocess.run(["python3", os.path.join(BASE_DIR, "scripts", "Card-Release.py")], check=True)
                time.sleep(2)
                flapper_closed_script = os.path.join(BASE_DIR, "scripts", f"Flapper-{selected_box}_Closed.py")
                subprocess.run(["python3", flapper_closed_script], check=True)
                subprocess.run(["python3", os.path.join(BASE_DIR, "scripts", "Card-Capture.py")], check=True)
                
            
            # Update the scanned image.
            update_images(card)
        
        except subprocess.CalledProcessError as e:
            print(f"Error during sorting process: {e}")
        except Exception as ex:
            print(f"Unexpected error: {ex}")
        
        move_count += 1
        monthly_move_count += 1
        save_move_count(move_count)
        save_monthly_move_count(monthly_move_count)
        time.sleep(0.5)

@app.route("/", methods=["GET", "POST"])
def index():
    global move_count, monthly_move_count, sorting_active
    global sorting_thread, box_criteria, csv_enabled, card_identified_url

    error = None
    cards = {}

    if request.method == "POST":
        # Handle CSV checkbox
        csv_enabled = True if request.form.get("save_to_csv") else False

        if "start_sorting" in request.form:
            # ...
            if not sorting_active:
                sorting_active = True
                sorting_thread = threading.Thread(target=sorting_loop, daemon=True)
                sorting_thread.start()

        elif "stop_sorting" in request.form:
            sorting_active = False
            if sorting_thread:
                sorting_thread.join(timeout=5)
                sorting_thread = None

        elif "submit_card" in request.form:
            # (Save box criteria for each of the 10 trays)
            new_criteria = {}
            for i in range(1, 11):
                crit = {}
                crit["name"] = request.form.get(f"name{i}", "").strip()
                crit["type"] = request.form.get(f"type{i}", "").strip()
                colors = []
                for color in ["W", "U", "B", "R", "G", "C"]:
                    if request.form.get(f"{color}{i}"):
                        colors.append(color)
                crit["colors"] = colors
                crit["cmc"] = request.form.get(f"cmc{i}", "").strip()
                crit["set_symbol"] = request.form.get(f"set_symbol{i}", "").strip()
                new_criteria[i] = crit
            with lock:
                box_criteria = new_criteria
            print("Submitted Card Criteria:")
            print("{")
            for key, criteria in new_criteria.items():
                # Use separators to keep the inner dictionary on one line.
                print(f'  "{key}": {json.dumps(criteria, separators=(", ", ": "))},')
            print("}")


        elif "clear_csv" in request.form:
            # Clear CSV
            if os.path.exists(CSV_FILE):
                open(CSV_FILE, 'w').close()
            print("CSV file cleared.")

        elif "clear_monthly_count" in request.form:
            # Reset monthly count
            monthly_move_count = 0
            save_monthly_move_count(monthly_move_count)
            print("Monthly count reset to 0.")

    # GET or POST: Always render the index
    return render_template(
        "index.html",
        moves=move_count,
        monthly_moves=monthly_move_count,
        cards=cards,
        error=error,
        sorting_active=sorting_active,
        box_criteria=box_criteria,
        csv_enabled=csv_enabled,
        card_identified_url=card_identified_url
    )

@app.route("/get_move_count", methods=["GET"])
def get_move_count_route():
    return jsonify({"moves": move_count,"monthly_moves": monthly_move_count,"card_identified_url": card_identified_url,"card_scanned_url": "/static/images/card_scanned.png"})

@app.route("/download_csv", methods=["GET"])
def download_csv():
    if os.path.exists(CSV_FILE):
        return send_file(CSV_FILE, as_attachment=True, download_name="card_info.csv")
    else:
        return "CSV file not found.", 404

@app.route("/settings", methods=["GET", "POST"])
def settings():
    error = None
    if request.method == "POST":
        if "save" in request.form:
            config = read_config()
            config["aws"]["access_key_id"] = request.form.get("aws_access_key_id", "")
            config["aws"]["secret_access_key"] = request.form.get("aws_secret_access_key", "")
            config["aws"]["region_name"] = request.form.get("aws_region_name", "")
            config["scryfall_search_url"] = request.form.get("scryfall_search_url", "")
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
    
@app.route("/run_script")
def run_script():
    raw_script = request.args.get("script")
    if not raw_script:
        return "No script specified", 400

    # Decode the script path
    script_rel_path = urllib.parse.unquote(raw_script)

    # Combine with base directory
    script_path = os.path.join(BASE_DIR, script_rel_path)

    # Security check: prevent escaping out of the scripts directory
    scripts_dir = os.path.join(BASE_DIR, "scripts")
    if not script_path.startswith(scripts_dir):
        return "Invalid script path", 403

    # Check existence
    if not os.path.exists(script_path):
        return f"Script not found: {script_path}", 404

    # Execute the script
    try:
        output = subprocess.check_output(["python3", script_path], stderr=subprocess.STDOUT)
        return output.decode()
    except subprocess.CalledProcessError as e:
        return f"Script failed:\n{e.output.decode()}", 500

if __name__ == "__main__":
    app.run(debug=True, host='0.0.0.0')
