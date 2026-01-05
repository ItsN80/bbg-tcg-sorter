#!/usr/bin/env python3
import json
import os
import re
from datetime import datetime
from picamera2 import Picamera2
from PIL import Image
import boto3
import requests
import base64
import shutil  # Added for copying files

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

    return token

def image_to_base64(image_path: str) -> str:
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")

def capture_image(config):
    """Captures an image using Picamera2 and processes it."""
    raw_file = os.path.join(output_directory, "raw_image.jpg")
    processed_file = os.path.join(output_directory, get_filename())

    # Ensure the camera is initialized
    camera_info = Picamera2.global_camera_info()
    if not camera_info:
        raise RuntimeError("No cameras found!")

    # Configure, capture, then stop the camera
    camera.configure(camera.create_preview_configuration(
        main={"format": "RGB888", "size": (1920, 1080)}))
    camera.start()
    camera.capture_file(raw_file)
    camera.stop()

    provider = (config.get("recognition_provider") or "aws").lower().strip()

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

def fetch_card_info(card_name, set_code, collector_number):
    # Normalize inputs
    set_code_clean = clean_set_code(set_code)
    collector_number_clean = clean_collector_number(collector_number)

    # 1) Best match: exact by set + collector number
    if set_code_clean and set_code_clean != "unknown" and collector_number_clean != "Unknown":
        exact_url = f"https://api.scryfall.com/cards/{set_code_clean}/{collector_number_clean}"
        #print(
        #    f"[SCRYFALL EXACT] name='{card_name}', set='{set_code_clean}', "
        #    f"collector='{collector_number_clean}', url={exact_url}"
        #)

        r = requests.get(exact_url)
        if r.status_code == 200:
            data = r.json()
            return {
                "name": data.get("name", "Unknown"),
                "type": data.get("type_line", "Type not found"),
                "colors": data.get("colors", []),
                "cmc": data.get("cmc", "CMC not found"),
                "set_symbol": data.get("set", "Set not found"),
                "collector_number": data.get("collector_number", collector_number_clean),
                "card_identified_url": data.get("image_uris", {}).get("normal", "")
            }
        else:
            # If exact lookup fails (bad set/collector), fall through to fuzzy
            try:
                err = r.json()
                print(f"[SCRYFALL EXACT FAILED] status={r.status_code} details={err.get('details', '')}")
            except Exception:
                print(f"[SCRYFALL EXACT FAILED] status={r.status_code}")

    # 2) Fallback: fuzzy by name (optionally constrain by set)
    if set_code_clean and set_code_clean != "unknown":
        url = f"https://api.scryfall.com/cards/named?fuzzy={card_name}&set={set_code_clean}"
    else:
        url = f"https://api.scryfall.com/cards/named?fuzzy={card_name}"

    #print(
    #    f"[SCRYFALL FUZZY] name='{card_name}', set='{set_code_clean or 'None'}', "
    #    f"collector='{collector_number_clean}', url={url}"
    #)

    response = requests.get(url)
    if response.status_code == 200:
        data = response.json()
        return {
            "name": data.get("name", "Unknown"),
            "type": data.get("type_line", "Type not found"),
            "colors": data.get("colors", []),
            "cmc": data.get("cmc", "CMC not found"),
            "set_symbol": data.get("set", "Set not found"),
            "collector_number": data.get("collector_number", "Unknown"),
            "card_identified_url": data.get("image_uris", {}).get("normal", "")
        }

    # 3) Last fallback: fuzzy without set constraint (if set-constrained fuzzy failed)
    fallback_url = f"https://api.scryfall.com/cards/named?fuzzy={card_name}"
    if fallback_url != url:
        #print(f"[SCRYFALL FUZZY FALLBACK] url={fallback_url}")
        fallback_response = requests.get(fallback_url)
        if fallback_response.status_code == 200:
            data = fallback_response.json()
            return {
                "name": data.get("name", "Unknown"),
                "type": data.get("type_line", "Type not found"),
                "colors": data.get("colors", []),
                "cmc": data.get("cmc", "CMC not found"),
                "set_symbol": data.get("set", "Set not found"),
                "collector_number": data.get("collector_number", "Unknown"),
                "card_identified_url": data.get("image_uris", {}).get("normal", "")
            }

    return None


def cleanup_images(*file_paths):
    """Deletes the specified image files."""
    for file_path in file_paths:
        if os.path.exists(file_path):
            os.remove(file_path)

def recognize_with_aws(processed_image, config):
    aws_config = config.get("aws", {})
    combined_image, crop1_height = crop_combined_areas(processed_image, config)
    card_name, collector_number, set_code = detect_text_combined(combined_image, crop1_height, aws_config)
    return card_name, collector_number, set_code


def recognize_with_ollama(processed_image, config):
    ollama_cfg = config.get("ollama", {})
    base_url = (ollama_cfg.get("base_url") or "http://localhost:11434").rstrip("/")
    model = ollama_cfg.get("model") or "minicpm-v:latest"
    timeout = int(ollama_cfg.get("timeout_seconds") or 60)

    # Encode image
    img_b64 = image_to_base64(processed_image)

    # Prompt: keep it strict and MTG-specific
    prompt = (
        "You are identifying a Magic: The Gathering card from an image.\n"
        "Return ONLY valid JSON with these keys:\n"
        "card_name (string), set_code (string or null), collector_number (string or null).\n"
        "If you are not confident, still provide best-guess card_name.\n"
        "Do not include any extra text.\n"
    )

    payload = {
        "model": model,
        "prompt": prompt,
        "images": [img_b64],
        "stream": False,
        "format": "json"
    }

    try:
        r = requests.post(f"{base_url}/api/generate", json=payload, timeout=timeout)
        r.raise_for_status()
        data = r.json()

        # Ollama generate responses typically include a "response" string
        response_text = data.get("response", "").strip()
        if not response_text:
            raise RuntimeError("Ollama returned an empty response")

        parsed = json.loads(response_text)

        card_name = (parsed.get("card_name") or "Unknown").strip()
        set_code = (parsed.get("set_code") or "Unknown")
        collector_number = (parsed.get("collector_number") or "Unknown")

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

        return card_name, collector_number, set_code
        


    except Exception as e:
        # Fail gracefully; your fetch_card_info will fallback if card_name is usable
        return "Unknown", "Unknown", "Unknown"


def recognize_card(processed_image, config):
    provider = (config.get("recognition_provider") or "aws").lower().strip()
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

        config = load_config(CONFIG_PATH)
        processed_image = capture_image(config)

        # Create a permanent copy called "card_scanned.png" in the same directory.
        scanned_copy = os.path.join(output_directory_scanned, "card_scanned.png")
        shutil.copy(processed_image, scanned_copy)

        # Provider-aware recognition
        card_name, collector_number, set_code = recognize_card(processed_image, config)

    except Exception as e:
        print(json.dumps({"error": f"Image capture/process error: {str(e)}"}))
        return

    card_info = fetch_card_info(card_name, set_code, collector_number)
    if card_info:
        print(json.dumps(card_info))
    else:
        provider = (config.get("recognition_provider") or "aws")
        print(json.dumps({"error": "...", "provider": provider}))

    cleanup_images(processed_image)

if __name__ == "__main__":
    main()

