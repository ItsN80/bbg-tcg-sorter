"""
Full-resolution stills from the shared camera.

camera_service.py (bbg-camera.service) keeps the camera open for the live
view, and only one process can hold it, so Read-Card.py and Test-Camera.py
ask the service for a frame instead of opening the camera themselves. When
the service isn't running they fall back to opening the camera directly, as
they did before the service existed.
"""
import os
import urllib.error
import urllib.request

CAMERA_SERVICE_URL = os.environ.get("BBG_CAMERA_URL", "http://127.0.0.1:5001")

# Read-Card.py's crop coordinates (card_crop, camera_crop) are pixels in an
# image derived from a capture of exactly this size.
STILL_SIZE = (1920, 1080)


class CameraServiceError(RuntimeError):
    """The camera service is running but could not take the picture."""


def capture_to(path, timeout=10):
    """Saves one full-resolution JPEG to `path`. Returns "service" or "direct"
    (which camera path took it); raises on failure."""
    try:
        with urllib.request.urlopen(CAMERA_SERVICE_URL + "/capture", timeout=timeout) as response:
            data = response.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace").strip()
        raise CameraServiceError(f"camera service capture failed ({e.code}): {detail}") from e
    except urllib.error.URLError as e:
        if not isinstance(e.reason, ConnectionRefusedError):
            raise CameraServiceError(f"camera service unreachable: {e.reason}") from e
        _capture_direct(path)
        return "direct"
    with open(path, "wb") as f:
        f.write(data)
    return "service"


def _capture_direct(path):
    os.environ.setdefault("LIBCAMERA_LOG_LEVELS", "3")
    from picamera2 import Picamera2  # slow (~1s) import, only needed without the service

    if not Picamera2.global_camera_info():
        raise RuntimeError("No cameras found!")
    camera = Picamera2()
    try:
        camera.configure(camera.create_preview_configuration(
            main={"format": "RGB888", "size": STILL_SIZE}))
        camera.start()
        camera.capture_file(path)
        camera.stop()
    finally:
        camera.close()
