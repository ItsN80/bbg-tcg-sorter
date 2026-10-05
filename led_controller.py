#!/usr/bin/env python3

import json
import os
import socket
import threading
import time

# The strip itself is driven by led_service.py (root, rpi_ws281x hardware
# timing); this module is its client and keeps the state the web UI shows.
LED_SOCKET_PATH = os.environ.get("BBG_LED_SOCKET", "/run/bbg-led/led.sock")

DEFAULT_LED_CONFIG = {
    "enabled": True,
    "gpio": 12,
    "count": 8,
    "brightness": 0.6,
    "color": {"r": 255, "g": 240, "b": 184},
}


def clamp_u8(value):
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = 0
    return 0 if value < 0 else 255 if value > 255 else value


def clamp_brightness(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        value = DEFAULT_LED_CONFIG["brightness"]
    return 0.0 if value < 0 else 1.0 if value > 1 else value


def normalize_led_config(raw):
    cfg = DEFAULT_LED_CONFIG.copy()
    if not isinstance(raw, dict):
        raw = {}

    color_raw = raw.get("color", {})
    if not isinstance(color_raw, dict):
        color_raw = {}

    cfg["enabled"] = bool(raw.get("enabled", cfg["enabled"]))

    try:
        gpio = int(raw.get("gpio", cfg["gpio"]))
    except (TypeError, ValueError):
        gpio = cfg["gpio"]
    if gpio in (0, 1):
        gpio = cfg["gpio"]
    cfg["gpio"] = gpio

    try:
        count = int(raw.get("count", cfg["count"]))
    except (TypeError, ValueError):
        count = cfg["count"]
    cfg["count"] = 1 if count < 1 else 1024 if count > 1024 else count

    cfg["brightness"] = clamp_brightness(raw.get("brightness", cfg["brightness"]))
    cfg["color"] = {
        "r": clamp_u8(color_raw.get("r", cfg["color"]["r"])),
        "g": clamp_u8(color_raw.get("g", cfg["color"]["g"])),
        "b": clamp_u8(color_raw.get("b", cfg["color"]["b"])),
    }
    return cfg


class LEDController:
    def __init__(self, config_led=None, max_fps=30, socket_path=None):
        normalized = normalize_led_config(config_led)
        self._socket_path = socket_path or LED_SOCKET_PATH
        self._lock = threading.Lock()
        # One frame at a time: requests from web handlers and the worker must not
        # interleave on the service connection.
        self._render_lock = threading.Lock()
        self._sock = None
        self._sock_file = None
        self._stop_event = threading.Event()
        self._dirty_event = threading.Event()
        self._desired_seq = 0
        self._sent_seq = -1
        self._min_interval = 1.0 / float(max_fps if max_fps and max_fps > 0 else 30)
        self._last_send_ts = 0.0

        self._enabled = normalized["enabled"]
        self._gpio = normalized["gpio"]
        self._count = normalized["count"]
        self._brightness = normalized["brightness"]
        self._color = (
            normalized["color"]["r"],
            normalized["color"]["g"],
            normalized["color"]["b"],
        )

        self._available = False
        self._error = ""

        self._worker = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker.start()
        self._mark_dirty()

    def _mark_dirty(self):
        with self._lock:
            self._desired_seq += 1
            self._dirty_event.set()

    def _disconnect(self):
        for obj in (self._sock_file, self._sock):
            try:
                if obj is not None:
                    obj.close()
            except Exception:
                pass
        self._sock = None
        self._sock_file = None

    def _send(self, request):
        """Sends one frame request to led_service.py; reconnects once if the
        service was restarted. Caller holds _render_lock."""
        for attempt in (1, 2):
            try:
                if self._sock is None:
                    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    sock.settimeout(2.0)
                    sock.connect(self._socket_path)
                    self._sock = sock
                    self._sock_file = sock.makefile("rwb")
                self._sock_file.write((json.dumps(request) + "\n").encode())
                self._sock_file.flush()
                line = self._sock_file.readline()
                if not line:
                    raise ConnectionError("LED service closed the connection")
                return json.loads(line)
            except (OSError, ValueError, ConnectionError) as exc:
                self._disconnect()
                if attempt == 2:
                    raise ConnectionError(
                        f"LED service unavailable ({exc}). Is bbg-led.service running?") from exc

    def _render(self, enabled, color, brightness):
        with self._render_lock:
            with self._lock:
                gpio, count = self._gpio, self._count
            request = {
                "enabled": bool(enabled),
                "color": {"r": color[0], "g": color[1], "b": color[2]},
                "brightness": brightness,
                "gpio": gpio,
                "count": count,
            }
            try:
                reply = self._send(request)
            except ConnectionError as exc:
                with self._lock:
                    self._available = False
                    self._error = str(exc)
                return False
            with self._lock:
                if reply.get("ok"):
                    self._available = True
                    self._error = ""
                    return True
                self._available = False
                self._error = f"LED service error: {reply.get('error')}"
                return False

    def _worker_loop(self):
        while not self._stop_event.is_set():
            self._dirty_event.wait(timeout=0.1)
            if self._stop_event.is_set():
                break

            while True:
                with self._lock:
                    desired_seq = self._desired_seq
                    sent_seq = self._sent_seq
                    enabled = self._enabled
                    color = self._color
                    brightness = self._brightness

                if sent_seq == desired_seq:
                    self._dirty_event.clear()
                    break

                now = time.time()
                delta = now - self._last_send_ts
                if delta < self._min_interval:
                    time.sleep(self._min_interval - delta)

                rendered = self._render(enabled, color, brightness)
                self._last_send_ts = time.time()
                if rendered:
                    with self._lock:
                        self._sent_seq = desired_seq
                else:
                    # Service down: retry slowly instead of spinning.
                    time.sleep(1.0)
                    if self._stop_event.is_set():
                        break

    def is_available(self):
        with self._lock:
            return self._available

    def get_error(self):
        with self._lock:
            return self._error

    def get_state(self):
        with self._lock:
            return {
                "enabled": self._enabled,
                "gpio": self._gpio,
                "count": self._count,
                "brightness": self._brightness,
                "color": {
                    "r": self._color[0],
                    "g": self._color[1],
                    "b": self._color[2],
                },
            }

    def set_enabled(self, enabled):
        with self._lock:
            self._enabled = bool(enabled)
        self._mark_dirty()

    def set_color(self, r, g, b, brightness=None):
        with self._lock:
            self._color = (clamp_u8(r), clamp_u8(g), clamp_u8(b))
            if brightness is not None:
                self._brightness = clamp_brightness(brightness)
            self._enabled = True
        self._mark_dirty()

    def set_brightness(self, brightness):
        with self._lock:
            self._brightness = clamp_brightness(brightness)
        self._mark_dirty()

    def off(self):
        with self._lock:
            self._enabled = False
            target_seq = self._desired_seq + 1
        self._mark_dirty()

        # Render the off frame now so "off" is deterministic and does not
        # depend solely on worker scheduling.
        rendered = self._render(False, (0, 0, 0), self._brightness)
        if rendered:
            with self._lock:
                if self._sent_seq < target_seq:
                    self._sent_seq = target_seq

    def set_mode(self, name):
        # Stub for future effects/modes.
        return name

    def apply_config(self, config_led_section):
        normalized = normalize_led_config(config_led_section)
        with self._lock:
            self._enabled = normalized["enabled"]
            self._gpio = normalized["gpio"]
            self._count = normalized["count"]
            self._brightness = normalized["brightness"]
            self._color = (
                normalized["color"]["r"],
                normalized["color"]["g"],
                normalized["color"]["b"],
            )
        self._mark_dirty()

    def close(self):
        # The LED service keeps the strip lit across web-app restarts; just stop
        # the worker and drop the connection.
        self._stop_event.set()
        self._dirty_event.set()
        with self._render_lock:
            self._disconnect()
