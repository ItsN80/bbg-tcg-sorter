#!/usr/bin/env python3

import threading
import time

import pigpio

DEFAULT_LED_CONFIG = {
    "enabled": True,
    "gpio": 12,
    "count": 8,
    "brightness": 0.35,
    "color": {"r": 0, "g": 140, "b": 255},
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
    # Match known-working test script timings.
    T0H_US = 0.35
    T0L_US = 0.80
    T1H_US = 0.70
    T1L_US = 0.60
    RESET_US = 300

    def __init__(self, config_led=None, max_fps=30):
        normalized = normalize_led_config(config_led)
        self._lock = threading.Lock()
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

        self._pi = None
        self._available = False
        self._error = ""

        self._connect()
        self._worker = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker.start()
        self._mark_dirty()

    def _connect(self):
        if self._available and self._pi is not None and getattr(self._pi, "connected", False):
            return True
        if self._pi is not None:
            try:
                self._pi.stop()
            except Exception:
                pass
            self._pi = None
        try:
            pi = pigpio.pi()
        except Exception as exc:
            self._available = False
            self._pi = None
            self._error = f"pigpio connection error: {exc}"
            return False

        if not pi.connected:
            self._available = False
            self._pi = None
            self._error = "Could not connect to pigpiod. Is pigpiod running?"
            try:
                pi.stop()
            except Exception:
                pass
            return False

        self._pi = pi
        self._pi.set_mode(self._gpio, pigpio.OUTPUT)
        self._available = True
        self._error = ""
        return True

    def _mark_dirty(self):
        with self._lock:
            self._desired_seq += 1
            self._dirty_event.set()

    def _build_wave(self, grb_bytes):
        pulses = []
        mask = 1 << self._gpio

        def add_pulse(level_on, level_off, duration_us):
            pulses.append(pigpio.pulse(level_on, level_off, int(round(duration_us))))

        for byte in grb_bytes:
            for bit in range(7, -1, -1):
                if (byte >> bit) & 1:
                    add_pulse(mask, 0, self.T1H_US)
                    add_pulse(0, mask, self.T1L_US)
                else:
                    add_pulse(mask, 0, self.T0H_US)
                    add_pulse(0, mask, self.T0L_US)
        add_pulse(0, mask, self.RESET_US)
        return pulses

    def _render(self, enabled, color, brightness):
        if not self._connect():
            return False

        if enabled:
            r = clamp_u8(int(color[0] * brightness))
            g = clamp_u8(int(color[1] * brightness))
            b = clamp_u8(int(color[2] * brightness))
        else:
            r, g, b = 0, 0, 0

        data = bytes([g, r, b]) * self._count  # WS2812 GRB ordering
        pulses = self._build_wave(data)

        try:
            self._pi.wave_clear()
            self._pi.wave_add_generic(pulses)
            wave_id = self._pi.wave_create()
            if wave_id < 0:
                self._error = f"pigpio wave_create failed ({wave_id})"
                self._available = False
                return False
            send_res = self._pi.wave_send_once(wave_id)
            if send_res < 0:
                self._error = f"pigpio wave_send_once failed ({send_res})"
                self._pi.wave_delete(wave_id)
                self._available = False
                return False
            started = time.time()
            while self._pi.wave_tx_busy():
                if time.time() - started > 0.25:
                    break
                time.sleep(0.001)
            self._pi.wave_delete(wave_id)
            self._error = ""
            self._available = True
            return True
        except Exception as exc:
            self._error = f"LED render failed: {exc}"
            self._available = False
            if self._pi is not None:
                try:
                    self._pi.stop()
                except Exception:
                    pass
                self._pi = None
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
                    time.sleep(0.05)

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

        # Force an immediate black frame so "off" is deterministic and does not
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
        # Try to explicitly latch LEDs off before tearing down worker/pigpio.
        try:
            self._render(False, (0, 0, 0), self._brightness)
        except Exception:
            pass
        self._stop_event.set()
        self._dirty_event.set()
        if self._worker.is_alive():
            self._worker.join(timeout=1.0)
        if self._pi is not None:
            try:
                self._pi.stop()
            except Exception:
                pass
