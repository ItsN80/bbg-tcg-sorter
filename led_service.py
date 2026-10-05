#!/usr/bin/env python3
"""
WS2812 LED strip service (runs as root via systemd: bbg-led.service).

The strip is driven with rpi_ws281x, which generates the WS2812 bit timing in
hardware (PWM + DMA). The previous pigpio-waveform driver could only time in
whole microseconds, so the 0.35 us "0" bit pulse rounded to 0 us and frames
were decoded unreliably (random colours, partial "off").

rpi_ws281x needs /dev/mem (root), so this small service owns the strip and the
web app (non-root) talks to it over a Unix socket with one JSON object per line:

  request:  {"enabled": bool, "color": {"r","g","b"}, "brightness": 0..1,
             "gpio": 12, "count": 8}
  response: {"ok": true} or {"ok": false, "error": "..."}

The Pi's onboard analog audio also uses the PWM hardware, so it must be
disabled (dtparam=audio=off in /boot/firmware/config.txt).
"""
import json
import os
import signal
import socket
import sys
import threading

from rpi_ws281x import PixelStrip, Color, ws

SOCKET_PATH = os.environ.get("BBG_LED_SOCKET", "/run/bbg-led/led.sock")
SOCKET_GROUP = os.environ.get("BBG_LED_GROUP", "gpio")  # the web app's user is in this group
LED_FREQ_HZ = 800000
LED_DMA = 10            # pigpiod uses DMA 14 (and the PCM clock), so no clash
PWM_CHANNELS = {12: 0, 18: 0, 13: 1, 19: 1}  # PWM-capable pins rpi_ws281x can drive

_strip = None
_strip_key = None
_lock = threading.Lock()


def _clamp_u8(v):
    return max(0, min(255, int(v)))


def _get_strip(gpio, count):
    global _strip, _strip_key
    key = (gpio, count)
    if _strip is not None and _strip_key == key:
        return _strip
    if gpio not in PWM_CHANNELS:
        raise ValueError(f"GPIO {gpio} is not a PWM pin (use 12 or 18)")
    if _strip is not None:
        _blank(_strip)
    strip = PixelStrip(count, gpio, LED_FREQ_HZ, LED_DMA, False, 255,
                       PWM_CHANNELS[gpio], ws.WS2811_STRIP_GRB)
    strip.begin()
    _strip, _strip_key = strip, key
    return strip


def _blank(strip):
    for i in range(strip.numPixels()):
        strip.setPixelColor(i, 0)
    strip.show()


def apply(req):
    gpio = int(req.get("gpio", 12))
    count = max(1, min(1024, int(req.get("count", 8))))
    with _lock:
        strip = _get_strip(gpio, count)
        if req.get("enabled"):
            c = req.get("color") or {}
            brightness = max(0.0, min(1.0, float(req.get("brightness", 1.0))))
            color = Color(_clamp_u8(c.get("r", 0) * brightness),
                          _clamp_u8(c.get("g", 0) * brightness),
                          _clamp_u8(c.get("b", 0) * brightness))
        else:
            color = 0
        for i in range(strip.numPixels()):
            strip.setPixelColor(i, color)
        strip.show()


def handle(conn):
    with conn, conn.makefile("rwb") as f:
        for line in f:
            try:
                apply(json.loads(line))
                reply = {"ok": True}
            except Exception as e:  # report, keep serving
                reply = {"ok": False, "error": str(e)}
            f.write((json.dumps(reply) + "\n").encode())
            f.flush()


def shutdown(*_):
    with _lock:
        if _strip is not None:
            try:
                _blank(_strip)
            except Exception:
                pass
    sys.exit(0)


def main():
    os.makedirs(os.path.dirname(SOCKET_PATH), exist_ok=True)
    if os.path.exists(SOCKET_PATH):
        os.remove(SOCKET_PATH)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(SOCKET_PATH)
    try:
        import grp
        os.chown(SOCKET_PATH, 0, grp.getgrnam(SOCKET_GROUP).gr_gid)
    except (KeyError, PermissionError):
        pass
    os.chmod(SOCKET_PATH, 0o660)
    server.listen(4)
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    print(f"LED service listening on {SOCKET_PATH}", flush=True)
    while True:
        conn, _ = server.accept()
        threading.Thread(target=handle, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    main()
