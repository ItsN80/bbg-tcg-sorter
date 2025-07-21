#!/usr/bin/env python3
import json
import os
from datetime import datetime
from picamera2 import Picamera2
from PIL import Image
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

        combined_path = os.path.join(output_directory_scanned, "combined_crop.jpg")
        combined_img.save(combined_path)

        return combined_path, crop1.height

def cleanup_images(*file_paths):
    """Deletes the specified image files."""
    for file_path in file_paths:
        if os.path.exists(file_path):
            os.remove(file_path)

def main():
    """
    1. Captures and processes an image.
    2. Crops two regions (top for name, bottom for collector number & set code) and combines them.
    """
    try:
        processed_image = capture_image()

        # Save full scan
        scanned_copy = os.path.join(output_directory_scanned, "card_scanned.png")
        shutil.copy(processed_image, scanned_copy)

        # ✅ Generate the cropped & combined image
        crop_combined_areas(processed_image)

    except Exception as e:
        print(json.dumps({"error": f"Image capture/process error: {str(e)}"}))
        return

    # Clean up temp image file (but leave cropped & scanned versions)
    cleanup_images(processed_image)


if __name__ == "__main__":
    main()
