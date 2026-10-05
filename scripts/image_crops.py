"""Crop helpers shared by Read-Card.py and Test-Camera.py (PIL only, no camera).

All coordinates are pixels in the processed (cropped + rotated) camera image,
i.e. the "Original Image" shown on the Camera Testing page."""
from PIL import Image

DEFAULT_TOP_CROP = {"x1": 160, "y1": 155, "x2": 577, "y2": 235}
DEFAULT_BOTTOM_CROP = {"x1": 160, "y1": 828, "x2": 577, "y2": 885}


def _box(region, defaults, width, height):
    """(x1, y1, x2, y2) clamped to the image; None if the box is empty."""
    region = region if isinstance(region, dict) else {}
    try:
        x1, y1, x2, y2 = (int(region.get(k, defaults[k])) for k in ("x1", "y1", "x2", "y2"))
    except (TypeError, ValueError):
        return None
    x1, x2 = max(0, min(x1, width)), max(0, min(x2, width))
    y1, y2 = max(0, min(y1, height)), max(0, min(y2, height))
    if x2 <= x1 or y2 <= y1:
        return None
    return x1, y1, x2, y2


def crop_card_area(image_path, card_crop, output_path):
    """Saves the whole-card region of image_path to output_path. An unset or
    empty card_crop keeps the full image, so recognition still gets a picture."""
    with Image.open(image_path) as img:
        full = {"x1": 0, "y1": 0, "x2": img.width, "y2": img.height}
        box = _box(card_crop, full, img.width, img.height) if card_crop else None
        result = img.crop(box) if box else img.copy()
    result.convert("RGB").save(output_path)
    return output_path


def crop_combined_areas(image_path, crop_cfg, output_path):
    """crop_cfg: {"top_crop": {x1,y1,x2,y2}, "bottom_crop": {...}}; missing
    values fall back to the defaults above. Stacks the two regions and returns
    (output_path, height of the top region) so OCR lines can be split."""
    crop_cfg = crop_cfg or {}
    with Image.open(image_path) as img:
        crops = []
        for key, defaults in (("top_crop", DEFAULT_TOP_CROP), ("bottom_crop", DEFAULT_BOTTOM_CROP)):
            box = _box(crop_cfg.get(key), defaults, img.width, img.height)
            if box is None:
                raise ValueError(f"{key} is empty or outside the {img.width}x{img.height} image")
            crops.append(img.crop(box))
    crop1, crop2 = crops

    combined_img = Image.new("RGB", (max(crop1.width, crop2.width), crop1.height + crop2.height),
                             color=(255, 255, 255))
    combined_img.paste(crop1, (0, 0))
    combined_img.paste(crop2, (0, crop1.height))
    combined_img.save(output_path)
    return output_path, crop1.height
