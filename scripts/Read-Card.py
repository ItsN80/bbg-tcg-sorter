#!/usr/bin/env python3
import contextlib
import json
import os
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
REPO_ROOT = os.path.normpath(os.path.join(BASE_DIR, ".."))
for _p in (BASE_DIR, REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import games  # noqa: E402  (repo root: game registry, no hardware imports)
import recognition  # noqa: E402  (scripts/: pure recognition helpers)
from recognition import (  # noqa: E402,F401  (re-exported for backwards compatibility)
    UNKNOWN,
    clean_collector_number,
    clean_set_code,
    looks_like_type_line,
    parse_first_json_object,
)
from lookups import lookup_card  # noqa: E402

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

def load_active_game():
    """
    Active game from env SORTER_GAME (default "mtg"), via the games registry.
    games.load_games() may print warnings about broken packs; keep those off
    stdout, which is reserved for the single JSON result line.
    """
    requested = (os.environ.get("SORTER_GAME") or games.DEFAULT_GAME_ID).strip()
    with contextlib.redirect_stdout(sys.stderr):
        # strict: an unknown/broken pack must fail the read (card goes to bin 10)
        # rather than silently looking the card up as another game.
        return games.get_game(requested, strict=True)

# Camera is created lazily (first capture) so this module can be imported
# without opening the camera.
camera = None

def get_camera():
    global camera
    if camera is None:
        camera = Picamera2()
    return camera

def get_filename():
    """Generate a timestamped filename."""
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    return f"image_{timestamp}.jpg"


def image_to_base64(image_path: str) -> str:
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")

def capture_image(config):
    """Captures an image using Picamera2 and processes it."""
    dbg = debug_enabled(config)
    raw_file = os.path.join(output_directory, "raw_image.jpg")
    processed_file = os.path.join(output_directory, get_filename())
    debug_log(dbg, f"Starting image capture. raw_file={raw_file} processed_file={processed_file}")

    # Ensure the camera is initialized
    cam = get_camera()
    camera_info = Picamera2.global_camera_info()
    if not camera_info:
        raise RuntimeError("No cameras found!")
    debug_log(dbg, f"Cameras detected: {len(camera_info)}")

    # Configure, capture, then stop the camera
    cam.configure(cam.create_preview_configuration(
        main={"format": "RGB888", "size": (1920, 1080)}))
    debug_log(dbg, "Camera configured for 1920x1080 RGB preview capture")
    cam.start()
    cam.capture_file(raw_file)
    cam.stop()
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

def crop_combined_areas(image_path, crop_cfg):
    """crop_cfg: {"top_crop": {x1,y1,x2,y2}, "bottom_crop": {...}} from
    games.resolve_camera_crop(); missing values fall back to the defaults below."""
    with Image.open(image_path) as img:
        crop_cfg = crop_cfg or {}
        top = crop_cfg.get("top_crop", {}) or {}
        bot = crop_cfg.get("bottom_crop", {}) or {}

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
    Runs AWS Rekognition on the combined image and separates OCR LINE results
    into top (card name) and bottom (set / number etc.) lists.
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

    return top_lines, bottom_lines


def cleanup_images(*file_paths):
    """Deletes the specified image files."""
    for file_path in file_paths:
        if os.path.exists(file_path):
            os.remove(file_path)

def recognize_with_aws(processed_image, config, game):
    dbg = debug_enabled(config)
    debug_log(dbg, f"Running AWS recognition with processed_image={processed_image}")
    aws_config = config.get("aws", {})
    crop_cfg = games.resolve_camera_crop(game, config)
    combined_image, crop1_height = crop_combined_areas(processed_image, crop_cfg)
    debug_log(dbg, f"Combined crop image created: {combined_image} (split at y={crop1_height})")
    top_lines, bottom_lines = detect_text_combined(combined_image, crop1_height, aws_config)
    debug_log(dbg, f"AWS text: top={top_lines} bottom={bottom_lines}")
    result = recognition.extract_aws_fields(top_lines, bottom_lines, game)
    debug_log(dbg, f"AWS recognition result: {json.dumps(result)}")
    return result


def recognize_with_ollama(processed_image, config, game):
    dbg = debug_enabled(config)
    debug_log(dbg, f"Running Ollama recognition with processed_image={processed_image}")
    ollama_cfg = config.get("ollama", {})
    base_url = (ollama_cfg.get("base_url") or "http://localhost:11434").rstrip("/")
    model = ollama_cfg.get("model") or "minicpm-v:latest"
    timeout = int(ollama_cfg.get("timeout_seconds") or 60)
    min_confidence = float(ollama_cfg.get("min_confidence") or 0.80)
    debug_ollama = parse_bool(ollama_cfg.get("debug"), default=False)
    env_debug_ollama = os.environ.get("DEBUG_OLLAMA")
    if env_debug_ollama is not None:
        debug_ollama = parse_bool(env_debug_ollama, default=debug_ollama)
    # REQUIRE_RECOGNITION_KEYS=a,b (or legacy REQUIRE_SET_AND_COLLECTOR=1,
    # defaulting to config ollama.require_set_and_collector)
    required_keys = recognition.required_keys_from_env(
        game,
        legacy_default=parse_bool(ollama_cfg.get("require_set_and_collector"), default=False)
    )
    debug_log(dbg, f"Required recognition keys: {required_keys}")

    # Encode image
    img_b64 = image_to_base64(processed_image)
    debug_log(dbg, f"Image encoded to base64 ({len(img_b64)} chars)")

    # Prompt: strict JSON and conservative fail behavior. Per-game override
    # from the Settings page > legacy ollama.prompt (MTG) > game.json prompt.
    prompt = games.resolve_prompt(game, config)

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

    api_key = ollama_cfg.get("api_key", "")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    r = None
    try:
        r = requests.post(f"{base_url}/api/generate", json=payload, headers=headers, timeout=timeout)
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

        # Normalize the game's keys + conservative accept/reject policy
        # (low confidence, unknown name, type-line name, required keys).
        result, reject_reason = recognition.evaluate_model_output(
            parsed, game, min_confidence, required_keys, dbg=dbg
        )
        if reject_reason:
            debug_log(dbg, f"Rejecting result: {reject_reason}")
            return result

        debug_log(
            dbg,
            f"Accepted Ollama result: {json.dumps(result)}, confidence={recognition.parse_confidence(parsed)}"
        )
        return result

    except Exception as e:
        if debug_ollama:
            print(f"[OLLAMA DEBUG] Exception: {str(e)}", file=sys.stderr)
            if r is not None:
                print("[OLLAMA DEBUG] Response body at exception:", file=sys.stderr)
                print(r.text, file=sys.stderr)
        # Fail gracefully; the lookup will skip / fall back as appropriate
        return recognition.unknown_result(game)


def recognize_card(processed_image, config, game):
    provider = (config.get("recognition_provider") or "aws").lower().strip()
    debug_log(debug_enabled(config), f"Dispatching recognition provider: {provider}")
    if provider in ("ollama", "do_serverless"):
        return recognize_with_ollama(processed_image, config, game)
    return recognize_with_aws(processed_image, config, game)


def main():
    """
    1. Loads the active game (env SORTER_GAME, default "mtg").
    2. Captures and processes an image.
    3. Recognizes the game's keys (AWS Rekognition crops or Ollama vision model).
    4. Looks the card up with the game's lookup (Scryfall, local JSON, ...).
    5. Prints ONE JSON line to stdout: the card (plus "game") or an error object.
    """
    game_id = (os.environ.get("SORTER_GAME") or games.DEFAULT_GAME_ID).strip()
    try:
        config = load_config(CONFIG_PATH)
        dbg = debug_enabled(config)
        debug_log(dbg, f"Loaded config from {CONFIG_PATH}")
        game = load_active_game()
        game_id = game["id"]
        debug_log(dbg, f"Active game: {game_id} (lookup={game.get('lookup', {}).get('type')})")
        processed_image = capture_image(config)
        debug_log(dbg, f"Processed image ready: {processed_image}")

        # Create a permanent copy called "card_scanned.png" in the same directory.
        scanned_copy = os.path.join(output_directory_scanned, "card_scanned.png")
        shutil.copy(processed_image, scanned_copy)
        debug_log(dbg, f"Copied processed image to UI path: {scanned_copy}")

        # Provider-aware recognition
        recognized = recognize_card(processed_image, config, game)
        debug_log(dbg, f"Recognition output: {json.dumps(recognized)}")

    except Exception as e:
        print(json.dumps({"error": f"Image capture/process error: {str(e)}", "game": game_id}))
        return

    card_info, lookup_attempts = lookup_card(game, recognized, dbg=dbg)
    debug_log(dbg, f"Lookup attempts: {json.dumps(lookup_attempts)}")
    if card_info:
        card_info = dict(card_info)
        card_info["game"] = game_id
        debug_log(dbg, f"Final card match: {json.dumps(card_info)}")
        print(json.dumps(card_info))
    else:
        provider = (config.get("recognition_provider") or "aws")
        lookup_type = game.get("lookup", {}).get("type")
        debug_log(dbg, f"No card match found after recognition + {lookup_type} lookup")
        print(json.dumps({
            "error": f"Unable to identify card from recognition + {lookup_type} lookup",
            "provider": provider,
            "game": game_id,
            "recognition": recognized,
            "lookup_responses": lookup_attempts
        }))

    cleanup_images(processed_image)
    debug_log(dbg, f"Cleaned up processed image: {processed_image}")

if __name__ == "__main__":
    main()
