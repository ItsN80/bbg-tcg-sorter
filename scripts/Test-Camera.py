#!/usr/bin/env python3
import json
import os
from datetime import datetime
from picamera2 import Picamera2
from PIL import Image
import shutil  # Added for copying files
import sys

import image_crops  # scripts/: PIL crop helpers shared with Read-Card.py

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

# Previews shown on the Camera Testing page (separate from card_scanned.png,
# which a live scan overwrites with the image it sent for recognition)
ORIGINAL_PREVIEW = os.path.join(output_directory_scanned, "camera_test_original.png")
CARD_CROP_PREVIEW = os.path.join(output_directory_scanned, "camera_test_card_crop.jpg")
COMBINED_CROP_PREVIEW = os.path.join(output_directory_scanned, "camera_test_combined_crop.jpg")

# Suppress libcamera logs
os.environ["LIBCAMERA_LOG_LEVELS"] = "3"

def load_config(config_file):
    """Loads configuration settings from a JSON file."""
    with open(config_file, "r") as file:
        return json.load(file)

config = load_config(CONFIG_PATH)

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

def cleanup_images(*file_paths):
    """Deletes the specified image files."""
    for file_path in file_paths:
        if os.path.exists(file_path):
            os.remove(file_path)

def main():
    """
    1. Captures and processes an image.
    2. Crops the whole card (what vision providers are sent).
    3. Crops two regions (top for name, bottom for collector number & set code) and combines them.
    All crop coordinates are pixels in the processed image saved in step 1.
    """
    processed_image = None
    try:
        processed_image = capture_image()

        # Save full scan
        shutil.copy(processed_image, ORIGINAL_PREVIEW)

        image_crops.crop_card_area(processed_image, config.get("card_crop"), CARD_CROP_PREVIEW)
        image_crops.crop_combined_areas(processed_image, config.get("camera_crop"), COMBINED_CROP_PREVIEW)

    except Exception as e:
        # Non-zero exit so the Camera Testing page shows the error
        print(json.dumps({"error": f"Image capture/process error: {str(e)}"}))
        sys.exit(1)
    finally:
        # Clean up temp image file (but leave cropped & scanned versions)
        if processed_image:
            cleanup_images(processed_image)

if __name__ == "__main__":
    main()
