"""Unit tests for scripts/image_crops.py (PIL only, no camera)."""
import os
import sys

import pytest
from PIL import Image

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))
import image_crops  # noqa: E402


@pytest.fixture
def capture(tmp_path):
    """A 756x960 frame (the processed capture size) with a red 'card'."""
    img = Image.new("RGB", (756, 960), (0, 0, 0))
    img.paste((255, 0, 0), (170, 110, 735, 890))
    path = tmp_path / "capture.jpg"
    img.save(path)
    return str(path)


def test_card_crop_uses_configured_box(capture, tmp_path):
    out = image_crops.crop_card_area(capture, {"x1": 170, "y1": 110, "x2": 735, "y2": 890},
                                     str(tmp_path / "card.jpg"))
    with Image.open(out) as card:
        assert card.size == (565, 780)
        r, g, b = card.getpixel((282, 390))
        assert r > 200 and g < 50 and b < 50


@pytest.mark.parametrize("card_crop", [None, {}, {"x1": 500, "y1": 0, "x2": 100, "y2": 960}])
def test_unset_or_empty_card_crop_keeps_full_image(capture, tmp_path, card_crop):
    out = image_crops.crop_card_area(capture, card_crop, str(tmp_path / "card.jpg"))
    with Image.open(out) as card:
        assert card.size == (756, 960)


def test_card_crop_is_clamped_to_image(capture, tmp_path):
    out = image_crops.crop_card_area(capture, {"x1": -50, "y1": 100, "x2": 9999, "y2": 9999},
                                     str(tmp_path / "card.jpg"))
    with Image.open(out) as card:
        assert card.size == (756, 860)


def test_combined_crop_stacks_regions(capture, tmp_path):
    cfg = {"top_crop": {"x1": 0, "y1": 0, "x2": 400, "y2": 80},
           "bottom_crop": {"x1": 0, "y1": 800, "x2": 300, "y2": 850}}
    out, split = image_crops.crop_combined_areas(capture, cfg, str(tmp_path / "combined.jpg"))
    assert split == 80
    with Image.open(out) as combined:
        assert combined.size == (400, 130)


def test_combined_crop_defaults_and_empty_region(capture, tmp_path):
    out, split = image_crops.crop_combined_areas(capture, {}, str(tmp_path / "combined.jpg"))
    assert split == 235 - 155
    with pytest.raises(ValueError, match="top_crop"):
        image_crops.crop_combined_areas(capture, {"top_crop": {"x1": 10, "y1": 10, "x2": 10, "y2": 50}},
                                        str(tmp_path / "combined.jpg"))
