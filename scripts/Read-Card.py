#!/usr/bin/env python3
import json
import os
import re
from datetime import datetime
from picamera2 import Picamera2
from PIL import Image
import boto3
import requests
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

config = load_config(CONFIG_PATH)
aws_config = config.get("aws", {})
aws_access_key_id = aws_config.get("access_key_id")
aws_secret_access_key = aws_config.get("secret_access_key")
region_name = aws_config.get("region_name")

# Set up the camera
camera = Picamera2()

def get_filename():
    """Generate a timestamped filename."""
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    return f"image_{timestamp}.jpg"

def capture_image():
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

    # Crop and rotate the captured image (if needed)
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

def crop_combined_areas(image_path):
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


def detect_text_combined(image_path, crop1_height):
    """
    Runs AWS Rekognition on the combined image and separates OCR results
    into top (card name) and bottom (collector number & set code) parts.
    """
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
    try:
        # Convert to int to remove any leading zeros, then back to str.
        collector_number_clean = str(int(collector_number))
    except ValueError:
        collector_number_clean = collector_number  # fallback if conversion fails

    # Use Scryfall's named endpoint with set code and collector number
    url = f"https://api.scryfall.com/cards/named?fuzzy={card_name}&set={set_code.lower()}"
    response = requests.get(url)
    if response.status_code == 200:
        data = response.json()
        return {
            "name": data.get("name", "Unknown"),
            "type": data.get("type_line", "Type not found"),
            "colors": data.get("colors", []),
            "cmc": data.get("cmc", "CMC not found"),
            "set_symbol": data.get("set", "Set not found"),
            "card_identified_url": data.get("image_uris", {}).get("normal", "")
        }
    else:
        # Fallback: try searching by exact card name
        fallback_url = f"https://api.scryfall.com/cards/named?fuzzy={card_name}"
        fallback_response = requests.get(fallback_url)
        if fallback_response.status_code == 200:
            data = fallback_response.json()
            return {
                "name": data.get("name", "Unknown"),
                "type": data.get("type_line", "Type not found"),
                "colors": data.get("colors", []),
                "cmc": data.get("cmc", "CMC not found"),
                "set_symbol": data.get("set", "Set not found"),
                "card_identified_url": data.get("image_uris", {}).get("normal", "")
            }
    return None

def cleanup_images(*file_paths):
    """Deletes the specified image files."""
    for file_path in file_paths:
        if os.path.exists(file_path):
            os.remove(file_path)

def main():
    """
    1. Captures and processes an image.
    2. Crops two regions (top for name, bottom for collector number & set code) and combines them.
    3. Runs one Rekognition call to separate OCR results.
    4. Uses the detected values to query Scryfall.
    5. Prints Scryfall card details as JSON.
    """
    try:
        processed_image = capture_image()
        # Create a permanent copy called "card_scanned.png" in the same directory.
        scanned_copy = os.path.join(output_directory_scanned, "card_scanned.png")
        shutil.copy(processed_image, scanned_copy)
        
        combined_image, crop1_height = crop_combined_areas(processed_image)
        card_name, collector_number, set_code = detect_text_combined(combined_image, crop1_height)
    except Exception as e:
        print(json.dumps({"error": f"Image capture/process error: {str(e)}"}))
        return

    card_info = fetch_card_info(card_name, set_code, collector_number)
    if card_info:
        print(json.dumps(card_info))
    else:
        print(json.dumps({"error": f"No information found for card: {card_name}"}))

    # Optionally cleanup images (do not include scanned_copy & combined_image so it persists)
    cleanup_images(processed_image)

if __name__ == "__main__":
    main()
