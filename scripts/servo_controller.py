#!/usr/bin/env python3
"""
servo_controller.py <servo_key>[,<servo_key>...] <action>

servo_key : flapper_1 .. flapper_15  or  card_servo. Several comma-separated
            keys move together (e.g. centering every tower flapper at once).
action    : open | close | left | center | right

Reads <action>_degrees (open_degrees, close_degrees, ...) from config.json
(flappers.<key> or card_servo). open/close are the ramp positions;
left/center/right are the tower positions. Both are set on the Calibrate
Ramp / Tower Flappers pages (Settings -> Sorter Design).
PCA9685 channel is taken from config channel field if present, otherwise falls back
to the hardcoded defaults below.
"""

import sys
import json
import os
import time

try:
    import board
    import busio
    from adafruit_pca9685 import PCA9685
except ImportError as e:
    print(f"Missing dependency: {e}. Install with: pip install adafruit-circuitpython-pca9685", file=sys.stderr)
    sys.exit(1)

PCA9685_FREQUENCY = 50   # Hz — standard servo frequency (20ms period)
PCA9685_ADDRESS   = 0x40 # default I2C address

ACTIONS = ("open", "close", "left", "center", "right")

# Default PCA9685 channel assignments. Override by adding "channel" to the
# matching section in config.json. Tower flappers 10-15 use the spare channels.
DEFAULT_CHANNELS = {
    "flapper_1": 0,
    "flapper_2": 1,
    "flapper_3": 2,
    "flapper_4": 3,
    "flapper_5": 4,
    "flapper_6": 5,
    "flapper_7": 6,
    "flapper_8": 7,
    "flapper_9": 8,
    "card_servo": 9,
    "flapper_10": 10,
    "flapper_11": 11,
    "flapper_12": 12,
    "flapper_13": 13,
    "flapper_14": 14,
    "flapper_15": 15,
}

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "..", "storage", "config.json")


def angle_to_duty_cycle(angle):
    """Convert servo angle (0–180°) to a 16-bit duty cycle at 50 Hz.

    At 50 Hz the period is 20 000 µs.  Standard servos expect a pulse of
    500–1 500 µs, mapping to 0–180°.
    """
    pulse_us = (angle / 180.0) * 1000.0 + 500.0
    return int(pulse_us / 20000.0 * 65535)


def main():
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <servo_key> <{'|'.join(ACTIONS)}>", file=sys.stderr)
        sys.exit(1)

    servo_keys = sys.argv[1].split(",")
    action     = sys.argv[2].lower()

    for servo_key in servo_keys:
        if servo_key not in DEFAULT_CHANNELS:
            print(f"Unknown servo key: {servo_key!r}. Valid keys: {list(DEFAULT_CHANNELS)}", file=sys.stderr)
            sys.exit(1)

    if action not in ACTIONS:
        print(f"Unknown action: {action!r}. Must be one of {list(ACTIONS)}.", file=sys.stderr)
        sys.exit(1)

    with open(CONFIG_PATH) as f:
        config = json.load(f)

    moves = []  # (servo_key, channel, angle)
    for servo_key in servo_keys:
        if servo_key == "card_servo":
            section = config.get("card_servo", {})
        else:
            section = config.get("flappers", {}).get(servo_key, {})
        moves.append((servo_key,
                      section.get("channel", DEFAULT_CHANNELS[servo_key]),
                      section.get(f"{action}_degrees", 90)))

    i2c = busio.I2C(board.SCL, board.SDA)
    pca = PCA9685(i2c, address=PCA9685_ADDRESS)
    pca.frequency = PCA9685_FREQUENCY

    try:
        for servo_key, channel_num, angle in moves:
            duty = angle_to_duty_cycle(angle)
            pca.channels[channel_num].duty_cycle = duty
            print(f"{servo_key} {action}: channel {channel_num} → {angle}° (duty={duty})")
        time.sleep(1)
    finally:
        for _, channel_num, _ in moves:
            pca.channels[channel_num].duty_cycle = 0  # stop servo jitter
        pca.deinit()

if __name__ == "__main__":
    main()
