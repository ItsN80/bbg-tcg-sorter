#!/usr/bin/env python3
import json
import os
import re
import time
from datetime import datetime
from picamera2 import Picamera2
from PIL import Image
import boto3
import requests
import base64
import shutil  # Added for copying files
import sys

# Base directory of the script
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Paths relative to the script location
output_directory = os.path.normpath(os.path.join(BASE_DIR, "..", "storage"))
output_directory_scanned = os.path.normpath(os.path.join(BASE_DIR, "..", "static", "images"))

# Ensure output directories exist
os.makedirs(output_directory, exist_ok=True)
os.makedirs(output_directory_scanned, exist_ok=True)

# Path to config.json
CONFIG_PATH = os.path.join(output_directory, "config.json")

# Suppress libcamera logs
os.environ["LIBCAMERA_LOG_LEVELS"] = "3"

def load_config(config_file):
    """Loads configuration settings from a JSON file."""
    with open(config_file, "r") as file:
        return json.load(file)

def parse_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        v = value.strip().lower()
        if v in {"1", "true", "yes", "y", "on"}:
            return True
        if v in {"0", "false", "no", "n", "off"}:
            return False
    return default

def debug_enabled(config):
    """
    Global debug toggle for step-by-step logs.
    Priority: env DEBUG_READ_CARD, then config.debug, default False.
    """
    env_debug = os.environ.get("DEBUG_READ_CARD")
    if env_debug is not None:
        return parse_bool(env_debug, default=False)
    return parse_bool(config.get("debug"), default=False)

def debug_log(enabled, message):
    if enabled:
        print(f"[READ-CARD DEBUG] {message}", file=sys.stderr)

# Set up the camera
camera = Picamera2()

def get_filename():
    """Generate a timestamped filename."""
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    return f"image_{timestamp}.jpg"


def clean_collector_number(raw: str) -> str:
    """
    Extracts the first digit group from a collector number string.
    Examples:
      'A123' -> '123'
      '123/287' -> '123'
      '0123' -> '123'
      '123a' -> '123'
    """
    if not raw:
        return "Unknown"

    s = str(raw).strip()

    m = re.search(r'(\d+)', s)
    if not m:
        return "Unknown"

    digits = m.group(1)

    # Normalize leading zeros: '000' -> '0', '0123' -> '123'
    try:
        return str(int(digits))
    except ValueError:
        return digits

def clean_set_code(raw: str) -> str:
    """
    Normalize set code strings coming from OCR/LLM.
    Examples:
      'BLC-EN' -> 'BLC'
      ' blc '  -> 'BLC'
      'BLC/EN' -> 'BLC'
      None/'Unknown' -> 'Unknown'
    """
    if not raw:
        return "Unknown"

    s = str(raw).strip().upper()
    if s in ("UNKNOWN", "N/A", "NONE", "NULL", ""):
        return "Unknown"

    # Keep only the first alphanumeric token (split on - / space etc.)
    token = re.split(r'[^A-Z0-9]+', s)[0].strip()

    # Scryfall set codes are typically 3–5 chars; keep within that range
    if len(token) < 3:
        return "Unknown"
    if len(token) > 5:
        token = token[:5]

    # Reject all-digit tokens (common OCR/LLM artifact like "304")
    if not re.search(r"[A-Z]", token):
        return "Unknown"

    return token

def looks_like_type_line(name: str) -> bool:
    """
    Heuristic filter for cases where the model reads the type line
    (e.g. "Creature - Human Wizard") instead of card name.
    """
    if not name:
        return False

    n = name.strip().lower()
    type_words = {
        "artifact", "battle", "conspiracy", "creature", "dungeon",
        "emblem", "enchantment", "instant", "kindred", "land",
        "phenomenon", "plane", "planeswalker", "scheme", "sorcery",
        "tribal", "vanguard"
    }

    # Typical MTG type-line separators
    if " - " in n or " — " in n:
        first = re.split(r"\s[-—]\s", n, maxsplit=1)[0].strip()
        if first in type_words:
            return True

    # Also reject direct single-type outputs like "Creature"
    return n in type_words

def image_to_base64(image_path: str) -> str:
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")

def parse_first_json_object(text: str):
    """
    Parse the first JSON object from a string.
    Handles model outputs that include prose before/after JSON.
    """
    if not isinstance(text, str):
        raise ValueError("Response text is not a string")

    s = text.strip()
    if not s:
        raise ValueError("Empty response text")

    decoder = json.JSONDecoder()

    # Fast path: pure JSON payload.
    try:
        return decoder.decode(s)
    except json.JSONDecodeError:
        pass

    # Fallback: scan for the first '{' and attempt raw_decode from there.
    for i, ch in enumerate(s):
        if ch != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(s[i:])
            return obj
        except json.JSONDecodeError:
            continue

    raise ValueError("No valid JSON object found in response")

def capture_image(config):
    """Captures an image using Picamera2 and processes it."""
    dbg = debug_enabled(config)
    raw_file = os.path.join(output_directory, "raw_image.jpg")
    processed_file = os.path.join(output_directory, get_filename())
    debug_log(dbg, f"Starting image capture. raw_file={raw_file} processed_file={processed_file}")

    # Ensure the camera is initialized
    camera_info = Picamera2.global_camera_info()
    if not camera_info:
        raise RuntimeError("No cameras found!")
    debug_log(dbg, f"Cameras detected: {len(camera_info)}")

    # Configure, capture, then stop the camera
    camera.configure(camera.create_preview_configuration(
        main={"format": "RGB888", "size": (1920, 1080)}))
    debug_log(dbg, "Camera configured for 1920x1080 RGB preview capture")
    camera.start()
    camera.capture_file(raw_file)
    camera.stop()
    debug_log(dbg, "Image captured and camera stopped")

    provider = (config.get("recognition_provider") or "aws").lower().strip()
    debug_log(dbg, f"Recognition provider selected: {provider}")

    if provider == "ollama":
        # Keep the full captured image (no crop/rotate)
        crop_and_rotate_image(raw_file, processed_file)
        #shutil.copy(raw_file, processed_file)
        os.remove(raw_file)
        return processed_file
 

    # AWS path: keep your existing crop+rotate behavior
    crop_and_rotate_image(raw_file, processed_file)
    os.remove(raw_file)
    return processed_file

def crop_and_rotate_image(input_file, output_file):
    """Crops and rotates the image (adjust margins as needed)."""
    with Image.open(input_file) as img:
        width, height = img.size
        crop_margin_w = int(width * 0.25)
        crop_margin_h = int(height * 0.15)
        cropped_img = img.crop((crop_margin_w, crop_margin_h,
                                width - crop_margin_w,
                                height - crop_margin_h))
        rotated_img = cropped_img.rotate(90, expand=True)
        rotated_img.save(output_file)

def rotate_image(input_file, output_file):
    with Image.open(input_file) as img:
        rotated_img = img.rotate(90, expand=True)
        rotated_img.save(output_file)

def crop_combined_areas(image_path, config):
    with Image.open(image_path) as img:
        crop_cfg = config.get("camera_crop", {})
        top = crop_cfg.get("top_crop", {})
        bot = crop_cfg.get("bottom_crop", {})

        crop1 = img.crop((top.get("x1", 160), top.get("y1", 155),
                          top.get("x2", 577), top.get("y2", 235)))

        crop2 = img.crop((bot.get("x1", 160), bot.get("y1", 828),
                          bot.get("x2", 577), bot.get("y2", 885)))

        combined_width = max(crop1.width, crop2.width)
        combined_height = crop1.height + crop2.height
        combined_img = Image.new("RGB", (combined_width, combined_height), color=(255, 255, 255))

        combined_img.paste(crop1, (0, 0))
        combined_img.paste(crop2, (0, crop1.height))

        combined_path = os.path.join(output_directory, "combined_crop.jpg")
        combined_img.save(combined_path)

        return combined_path, crop1.height


def detect_text_combined(image_path, crop1_height, aws_config):
    """
    Runs AWS Rekognition on the combined image and separates OCR results
    into top (card name) and bottom (collector number & set code) parts.
    """
    aws_access_key_id = aws_config.get("access_key_id")
    aws_secret_access_key = aws_config.get("secret_access_key")
    region_name = aws_config.get("region_name")

    client = boto3.client(
        'rekognition',
        aws_access_key_id=aws_access_key_id,
        aws_secret_access_key=aws_secret_access_key,
        region_name=region_name
    )

    with open(image_path, 'rb') as f:
        image_bytes = f.read()

    response = client.detect_text(Image={'Bytes': image_bytes})
    text_detections = response.get('TextDetections', [])

    top_lines = []
    bottom_lines = []
    with Image.open(image_path) as combined_img:
        combined_height = combined_img.height

    for detection in text_detections:
        if detection['Type'] == 'LINE':
            bbox = detection['Geometry']['BoundingBox']
            y_top = bbox['Top'] * combined_height
            if y_top < crop1_height:
                top_lines.append(detection['DetectedText'])
            else:
                bottom_lines.append(detection['DetectedText'])

    # Card name from the top region
    card_name = " ".join(top_lines).strip() if top_lines else "Unknown"

    # For the bottom region, join the lines and use regex to get collector number and set code
    bottom_text = " ".join(bottom_lines)

    # Adjust the regex pattern as needed. Here we look for an optional letter and a 3- or 4-digit number.
    collector_pattern = r'[A-Za-z]?\s*(\d{3,4})'
    collector_match = re.search(collector_pattern, bottom_text)
    collector_number = collector_match.group(1) if collector_match else "Unknown"

    # Extract 3-letter set code
    set_match = re.search(r'\b([A-Za-z]{3})\b', bottom_text)
    set_code = set_match.group(1).upper() if set_match else "Unknown"

    return card_name, collector_number, set_code

def fetch_card_info(card_name, set_code, collector_number, dbg=False):
    scryfall_attempts = []
    scryfall_timeout_seconds = 30
    scryfall_max_attempts = 3

    def record_attempt(stage, url, status_code=None, details=None, error=None):
        scryfall_attempts.append({
            "stage": stage,
            "url": url,
            "status_code": status_code,
            "details": details,
            "error": error
        })

    def scryfall_get_with_retries(stage, url, params=None):
        last_error = None
        for attempt in range(1, scryfall_max_attempts + 1):
            try:
                debug_log(
                    dbg,
                    (
                        f"Scryfall request [{stage}] attempt {attempt}/{scryfall_max_attempts}: "
                        f"url={url} params={params}"
                    )
                )
                response = requests.get(url, params=params, timeout=scryfall_timeout_seconds)
                return response, None
            except requests.RequestException as e:
                last_error = str(e)
                debug_log(
                    dbg,
                    (
                        f"Scryfall request [{stage}] attempt {attempt}/{scryfall_max_attempts} failed: "
                        f"{last_error}"
                    )
                )
                if attempt < scryfall_max_attempts:
                    sleep_seconds = attempt
                    debug_log(dbg, f"Scryfall retry sleep: {sleep_seconds}s")
                    time.sleep(sleep_seconds)
        return None, last_error

    # Normalize inputs
    set_code_clean = clean_set_code(set_code)
    collector_number_clean = clean_collector_number(collector_number)
    debug_log(
        dbg,
        (
            "Scryfall lookup inputs: "
            f"card_name='{card_name}', set_code_raw='{set_code}', collector_raw='{collector_number}', "
            f"set_code_clean='{set_code_clean}', collector_clean='{collector_number_clean}'"
        )
    )

    # 1) Best match: exact by set + collector number
    if set_code_clean and set_code_clean != "Unknown" and collector_number_clean != "Unknown":
        exact_url = f"https://api.scryfall.com/cards/{set_code_clean}/{collector_number_clean}"
        #print(
        #    f"[SCRYFALL EXACT] name='{card_name}', set='{set_code_clean}', "
        #    f"collector='{collector_number_clean}', url={exact_url}"
        #)

        try:
            r, req_error = scryfall_get_with_retries("exact", exact_url)
            if req_error:
                record_attempt("exact", exact_url, error=req_error)
                r = None
            if r is None:
                raise requests.RequestException(req_error or "Unknown request error")
            if r.status_code == 200:
                data = r.json()
                record_attempt("exact", exact_url, status_code=r.status_code, details="ok")
                return {
                    "name": data.get("name", "Unknown"),
                    "type": data.get("type_line", "Type not found"),
                    "colors": data.get("colors", []),
                    "color_identity": data.get("color_identity", data.get("colors", [])),
                    "cmc": data.get("cmc", "CMC not found"),
                    "set_symbol": data.get("set", "Set not found"),
                    "collector_number": data.get("collector_number", collector_number_clean),
                    "card_identified_url": data.get("image_uris", {}).get("normal", "")
                }, scryfall_attempts

            # If exact lookup fails (bad set/collector), fall through to fuzzy
            try:
                err = r.json()
                record_attempt("exact", exact_url, status_code=r.status_code, details=err.get("details", ""))
                print(f"[SCRYFALL EXACT FAILED] status={r.status_code} details={err.get('details', '')}", file=sys.stderr)
            except Exception:
                record_attempt("exact", exact_url, status_code=r.status_code, details="non-json error body")
                print(f"[SCRYFALL EXACT FAILED] status={r.status_code}", file=sys.stderr)
        except requests.RequestException:
            pass

    # 2) Fallback: fuzzy by name (optionally constrain by set)
    fuzzy_base_url = "https://api.scryfall.com/cards/named"
    if set_code_clean and set_code_clean != "Unknown":
        fuzzy_params = {"fuzzy": card_name, "set": set_code_clean}
        stage = "fuzzy_set"
    else:
        fuzzy_params = {"fuzzy": card_name}
        stage = "fuzzy"

    #print(
    #    f"[SCRYFALL FUZZY] name='{card_name}', set='{set_code_clean or 'None'}', "
    #    f"collector='{collector_number_clean}', url={url}"
    #)

    try:
        response, req_error = scryfall_get_with_retries(stage, fuzzy_base_url, params=fuzzy_params)
        if req_error:
            record_attempt(stage, fuzzy_base_url, error=req_error)
            response = None
        if response is None:
            raise requests.RequestException(req_error or "Unknown request error")
        if response.status_code == 200:
            data = response.json()
            record_attempt(stage, response.url, status_code=response.status_code, details="ok")
            return {
                "name": data.get("name", "Unknown"),
                "type": data.get("type_line", "Type not found"),
                "colors": data.get("colors", []),
                "color_identity": data.get("color_identity", data.get("colors", [])),
                "cmc": data.get("cmc", "CMC not found"),
                "set_symbol": data.get("set", "Set not found"),
                "collector_number": data.get("collector_number", "Unknown"),
                "card_identified_url": data.get("image_uris", {}).get("normal", "")
            }, scryfall_attempts
        try:
            err = response.json()
            record_attempt(stage, response.url, status_code=response.status_code, details=err.get("details", ""))
        except Exception:
            record_attempt(stage, response.url, status_code=response.status_code, details="non-json error body")
    except requests.RequestException:
        pass

    # 3) Last fallback: fuzzy without set constraint (if set-constrained fuzzy failed)
    fallback_url = "https://api.scryfall.com/cards/named"
    fallback_params = {"fuzzy": card_name}
    if stage == "fuzzy_set":
        #print(f"[SCRYFALL FUZZY FALLBACK] url={fallback_url}")
        try:
            fallback_response, req_error = scryfall_get_with_retries(
                "fuzzy_fallback",
                fallback_url,
                params=fallback_params
            )
            if req_error:
                record_attempt("fuzzy_fallback", fallback_url, error=req_error)
                fallback_response = None
            if fallback_response is None:
                raise requests.RequestException(req_error or "Unknown request error")
            if fallback_response.status_code == 200:
                data = fallback_response.json()
                record_attempt("fuzzy_fallback", fallback_response.url, status_code=fallback_response.status_code, details="ok")
                return {
                    "name": data.get("name", "Unknown"),
                    "type": data.get("type_line", "Type not found"),
                    "colors": data.get("colors", []),
                    "color_identity": data.get("color_identity", data.get("colors", [])),
                    "cmc": data.get("cmc", "CMC not found"),
                    "set_symbol": data.get("set", "Set not found"),
                    "collector_number": data.get("collector_number", "Unknown"),
                    "card_identified_url": data.get("image_uris", {}).get("normal", "")
                }, scryfall_attempts
            try:
                err = fallback_response.json()
                record_attempt("fuzzy_fallback", fallback_response.url, status_code=fallback_response.status_code, details=err.get("details", ""))
            except Exception:
                record_attempt("fuzzy_fallback", fallback_response.url, status_code=fallback_response.status_code, details="non-json error body")
        except requests.RequestException:
            pass

    return None, scryfall_attempts


def cleanup_images(*file_paths):
    """Deletes the specified image files."""
    for file_path in file_paths:
        if os.path.exists(file_path):
            os.remove(file_path)

def recognize_with_aws(processed_image, config):
    dbg = debug_enabled(config)
    debug_log(dbg, f"Running AWS recognition with processed_image={processed_image}")
    aws_config = config.get("aws", {})
    combined_image, crop1_height = crop_combined_areas(processed_image, config)
    debug_log(dbg, f"Combined crop image created: {combined_image} (split at y={crop1_height})")
    card_name, collector_number, set_code = detect_text_combined(combined_image, crop1_height, aws_config)
    debug_log(
        dbg,
        (
            "AWS recognition result: "
            f"card_name='{card_name}', set_code='{set_code}', collector_number='{collector_number}'"
        )
    )
    return card_name, collector_number, set_code


def recognize_with_ollama(processed_image, config):
    dbg = debug_enabled(config)
    debug_log(dbg, f"Running Ollama recognition with processed_image={processed_image}")
    ollama_cfg = config.get("ollama", {})
    base_url = (ollama_cfg.get("base_url") or "http://localhost:11434").rstrip("/")
    model = ollama_cfg.get("model") or "minicpm-v:latest"
    timeout = int(ollama_cfg.get("timeout_seconds") or 60)
    min_confidence = float(ollama_cfg.get("min_confidence") or 0.80)
    debug_ollama = parse_bool(ollama_cfg.get("debug"), default=False)
    require_set_and_collector = parse_bool(
        ollama_cfg.get("require_set_and_collector"),
        default=False
    )
    env_debug_ollama = os.environ.get("DEBUG_OLLAMA")
    if env_debug_ollama is not None:
        debug_ollama = parse_bool(env_debug_ollama, default=debug_ollama)
    env_require_set = os.environ.get("REQUIRE_SET_AND_COLLECTOR")
    if env_require_set is not None:
        require_set_and_collector = parse_bool(
            env_require_set,
            default=require_set_and_collector
        )

    # Encode image
    img_b64 = image_to_base64(processed_image)
    debug_log(dbg, f"Image encoded to base64 ({len(img_b64)} chars)")

    # Prompt: strict JSON and conservative fail behavior
    prompt = (
        "You are identifying a Magic: The Gathering card from an image.\n"
        "Return ONLY valid JSON with these keys:\n"
        "card_name (string or null), set_code (string or null), "
        "collector_number (string or null), confidence (number 0.0 to 1.0).\n"
        "card_name must be the printed card title, not the type line.\n"
        "If text is unclear or confidence is low, return null values.\n"
        "Do NOT guess.\n"
        "Do not include any extra text.\n"
    )

    payload = {
        "model": model,
        "prompt": prompt,
        "images": [img_b64],
        "stream": False,
        "format": "json",
        "options": {
            "temperature": float(ollama_cfg.get("temperature", 0.0)),
            "top_p": float(ollama_cfg.get("top_p", 0.1)),
            "top_k": int(ollama_cfg.get("top_k", 20)),
            "repeat_penalty": float(ollama_cfg.get("repeat_penalty", 1.2)),
            "num_predict": int(ollama_cfg.get("num_predict", 120)),
            "seed": int(ollama_cfg.get("seed", 42))
        }
    }

    if debug_ollama:
        payload_debug = dict(payload)
        image_list = payload.get("images") or []
        if image_list:
            payload_debug["images"] = [
                f"<omitted base64 image #{idx + 1}, length={len(img)} chars>"
                for idx, img in enumerate(image_list)
            ]
        print("[OLLAMA DEBUG] Request URL:", f"{base_url}/api/generate", file=sys.stderr)
        print("[OLLAMA DEBUG] Request payload (images redacted):", file=sys.stderr)
        print(json.dumps(payload_debug, indent=2), file=sys.stderr)

    r = None
    try:
        r = requests.post(f"{base_url}/api/generate", json=payload, timeout=timeout)
        if debug_ollama:
            print("[OLLAMA DEBUG] HTTP status:", r.status_code, file=sys.stderr)
            print("[OLLAMA DEBUG] Raw response body:", file=sys.stderr)
            print(r.text, file=sys.stderr)
        r.raise_for_status()
        data = r.json()
        debug_log(dbg, "Received JSON response from Ollama API")

        # Ollama generate responses typically include a "response" string
        response_text = data.get("response", "").strip()
        if not response_text:
            raise RuntimeError("Ollama returned an empty response")

        parsed = parse_first_json_object(response_text)
        debug_log(dbg, f"Parsed model JSON: {json.dumps(parsed)}")

        confidence_raw = parsed.get("confidence", None)
        confidence = None
        if confidence_raw is not None:
            try:
                confidence = float(confidence_raw)
            except (TypeError, ValueError):
                confidence = None

        card_name_raw = parsed.get("card_name")
        if isinstance(card_name_raw, str):
            card_name = card_name_raw.strip() or "Unknown"
        else:
            card_name = "Unknown"

        set_code = parsed.get("set_code") or "Unknown"
        collector_number = parsed.get("collector_number") or "Unknown"

        if isinstance(collector_number, str):
            collector_number = collector_number.strip() or "Unknown"
        else:
            collector_number = "Unknown"

        collector_number = clean_collector_number(collector_number)
        set_code = clean_set_code(set_code)

        # Normalize
        if isinstance(set_code, str):
            set_code = set_code.strip().upper() or "Unknown"
        else:
            set_code = "Unknown"

        if isinstance(collector_number, str):
            collector_number = collector_number.strip() or "Unknown"
        else:
            collector_number = "Unknown"

        # Conservative fail policy: if confidence is present and low, reject.
        # If confidence is omitted, allow name-only matching.
        if confidence is not None and confidence < min_confidence:
            debug_log(dbg, f"Rejecting result: confidence {confidence} < min_confidence {min_confidence}")
            return "Unknown", "Unknown", "Unknown"
        if confidence is None:
            debug_log(dbg, "Confidence missing/unparseable; continuing with name-based matching")

        if not card_name or card_name.lower() in {"unknown", "null", "none", "n/a"}:
            debug_log(dbg, "Rejecting result: card_name missing/unknown")
            return "Unknown", "Unknown", "Unknown"

        if looks_like_type_line(card_name):
            debug_log(dbg, f"Rejecting result: card_name looks like a type line ('{card_name}')")
            return "Unknown", "Unknown", "Unknown"

        if require_set_and_collector and (set_code == "Unknown" or collector_number == "Unknown"):
            debug_log(
                dbg,
                "Rejecting result: REQUIRE_SET_AND_COLLECTOR is enabled and set/collector is Unknown"
            )
            return "Unknown", "Unknown", "Unknown"

        debug_log(
            dbg,
            (
                "Accepted Ollama result: "
                f"card_name='{card_name}', set_code='{set_code}', collector_number='{collector_number}', "
                f"confidence={confidence}"
            )
        )
        return card_name, collector_number, set_code
        


    except Exception as e:
        if debug_ollama:
            print(f"[OLLAMA DEBUG] Exception: {str(e)}", file=sys.stderr)
            if r is not None:
                print("[OLLAMA DEBUG] Response body at exception:", file=sys.stderr)
                print(r.text, file=sys.stderr)
        # Fail gracefully; your fetch_card_info will fallback if card_name is usable
        return "Unknown", "Unknown", "Unknown"


def recognize_card(processed_image, config):
    provider = (config.get("recognition_provider") or "aws").lower().strip()
    debug_log(debug_enabled(config), f"Dispatching recognition provider: {provider}")
    if provider == "ollama":
        return recognize_with_ollama(processed_image, config)
    return recognize_with_aws(processed_image, config)


def main():
    """
    1. Captures and processes an image.
    2. Crops two regions (top for name, bottom for collector number & set code) and combines them.
    3. Runs one Rekognition call to separate OCR results.
    4. Uses the detected values to query Scryfall.
    5. Prints Scryfall card details as JSON.
    """
    try:
        config = load_config(CONFIG_PATH)
        dbg = debug_enabled(config)
        debug_log(dbg, f"Loaded config from {CONFIG_PATH}")
        processed_image = capture_image(config)
        debug_log(dbg, f"Processed image ready: {processed_image}")

        # Create a permanent copy called "card_scanned.png" in the same directory.
        scanned_copy = os.path.join(output_directory_scanned, "card_scanned.png")
        shutil.copy(processed_image, scanned_copy)
        debug_log(dbg, f"Copied processed image to UI path: {scanned_copy}")

        # Provider-aware recognition
        card_name, collector_number, set_code = recognize_card(processed_image, config)
        debug_log(
            dbg,
            (
                "Recognition output: "
                f"card_name='{card_name}', set_code='{set_code}', collector_number='{collector_number}'"
            )
        )

    except Exception as e:
        print(json.dumps({"error": f"Image capture/process error: {str(e)}"}))
        return

    card_info, scryfall_attempts = fetch_card_info(card_name, set_code, collector_number, dbg=dbg)
    debug_log(dbg, f"Scryfall attempts: {json.dumps(scryfall_attempts)}")
    if card_info:
        debug_log(dbg, f"Final card match: {json.dumps(card_info)}")
        print(json.dumps(card_info))
    else:
        provider = (config.get("recognition_provider") or "aws")
        debug_log(dbg, "No card match found after recognition + Scryfall")
        print(json.dumps({
            "error": "Unable to identify card from recognition + Scryfall query",
            "provider": provider,
            "recognition": {
                "card_name": card_name,
                "set_code": set_code,
                "collector_number": collector_number
            },
            "scryfall_responses": scryfall_attempts
        }))

    cleanup_images(processed_image)
    debug_log(dbg, f"Cleaned up processed image: {processed_image}")

if __name__ == "__main__":
    main()
