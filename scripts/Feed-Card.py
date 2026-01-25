#!/usr/bin/env python3
import threading
import pigpio
import time
import sys
import os
import json

# -----------------------------
# Pin Definitions (BCM)
# -----------------------------
MOTOR_1_PINS = [19, 26, 4, 17]
MOTOR_2_PINS = [27, 22, 10, 9]
MOTOR_3_PINS = [11, 7, 5, 6]

sensor1_pin = 8
sensor2_pin = 14

# -----------------------------
# Tuning / Behavior
# -----------------------------
SENSOR_BLOCKED_LEVEL = 1  # blocked = 1, clear = 0

# After sensor1 triggers, keep feeding briefly before stopping Motor 2
EXTRA_PULL_DELAY_SEC = 0.20  # adjust 0.10–0.40
MOTOR2_STOP_SETTLE_SEC = 0.05  # small pause after stopping motor 2 (optional)

SENSOR1_BLOCK_TIMEOUT_SEC = 8.0
SENSOR1_CLEAR_TIMEOUT_SEC = 5.0
SENSOR2_BLOCK_TIMEOUT_SEC = 10.0

STABLE_TIME_MS = 40

MOTOR1_FWD_DELAY = 0.001
MOTOR2_FWD_DELAY = 0.001
MOTOR3_FWD_DELAY = 0.001

MOTOR1_REV_DELAY = 0.001
MOTOR2_REV_DELAY = 0.001

STEPPER_SEQ_FULLSPEED = [
    [1, 0, 0, 0],
    [1, 1, 0, 0],
    [0, 1, 0, 0],
    [0, 1, 1, 0],
    [0, 0, 1, 0],
    [0, 0, 1, 1],
    [0, 0, 0, 1],
    [1, 0, 0, 1],
]

STEPPER_SEQ_HALFSPEED = [
    [1, 0, 0, 0],
    [1, 0, 0, 0],
    [1, 1, 0, 0],
    [1, 1, 0, 0],
    [0, 1, 0, 0],
    [0, 1, 0, 0],
    [0, 1, 1, 0],
    [0, 1, 1, 0],
    [0, 0, 1, 0],
    [0, 0, 1, 0],
    [0, 0, 1, 1],
    [0, 0, 1, 1],
    [0, 0, 0, 1],
    [0, 0, 0, 1],
    [1, 0, 0, 1],
    [1, 0, 0, 1],
]

# -----------------------------
# pigpio init
# -----------------------------
pi = pigpio.pi()
if not pi.connected:
    print("pigpio not connected. Exiting.")
    print("1")
    sys.exit(1)

all_motor_pins = MOTOR_1_PINS + MOTOR_2_PINS + MOTOR_3_PINS
for pin in all_motor_pins:
    pi.set_mode(pin, pigpio.OUTPUT)
    pi.write(pin, 0)

pi.set_mode(sensor1_pin, pigpio.INPUT)
pi.set_mode(sensor2_pin, pigpio.INPUT)

pi.set_pull_up_down(sensor1_pin, pigpio.PUD_DOWN)
pi.set_pull_up_down(sensor2_pin, pigpio.PUD_DOWN)


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.normpath(os.path.join(BASE_DIR, "..", "storage", "config.json"))

def read_config_value_motor2_extra_feed(default=1.2):
    try:
        with open(CONFIG_FILE, "r") as f:
            cfg = json.load(f)
        return float(cfg.get("feed", {}).get("motor2_extra_feed_sec", default))
    except Exception:
        return default

MOTOR2_EXTRA_FEED_SEC = read_config_value_motor2_extra_feed()


# -----------------------------
# Stepper helpers
# -----------------------------
def motor_step(motor_pins, step):
    for pin, val in zip(motor_pins, step):
        pi.write(pin, val)

def motor_step_sequence(motor_pins, sequence, stop_event, speed_delay=0.001, reverse=False):
    seq = list(sequence)
    if reverse:
        seq = list(reversed(seq))

    while not stop_event.is_set():
        for step in seq:
            if stop_event.is_set():
                break
            motor_step(motor_pins, step)
            time.sleep(speed_delay)

def stop_motor(motor_pins, stop_event, thread_obj):
    stop_event.set()
    if thread_obj is not None and thread_obj.is_alive():
        thread_obj.join(timeout=2.0)
    for pin in motor_pins:
        pi.write(pin, 0)

# -----------------------------
# Sensor stability helper
# -----------------------------
def wait_for_level_stable(pin, target_level, timeout_sec, stable_ms=40, poll_ms=5):
    deadline = time.time() + timeout_sec
    stable_start = None
    stable_required = stable_ms / 1000.0
    poll = poll_ms / 1000.0

    while time.time() < deadline:
        val = pi.read(pin)
        if val == target_level:
            if stable_start is None:
                stable_start = time.time()
            elif (time.time() - stable_start) >= stable_required:
                return True
        else:
            stable_start = None
        time.sleep(poll)
    return False

# -----------------------------
# Main flow
# -----------------------------
motor1_thread = motor2_thread = motor3_thread = None
motor1_stop = threading.Event()
motor2_stop = threading.Event()
motor3_stop = threading.Event()

try:
    print("Starting motors 1, 2, 3 (Phase 1 feed)")
    motor1_thread = threading.Thread(
        target=motor_step_sequence,
        args=(MOTOR_1_PINS, STEPPER_SEQ_FULLSPEED, motor1_stop, MOTOR1_FWD_DELAY, False),
        daemon=True
    )
    # Phase 1: Motor 2 inverted (mounted opposite)
    motor2_thread = threading.Thread(
        target=motor_step_sequence,
        args=(MOTOR_2_PINS, STEPPER_SEQ_HALFSPEED, motor2_stop, MOTOR2_FWD_DELAY, True),
        daemon=True
    )
    motor3_thread = threading.Thread(
        target=motor_step_sequence,
        args=(MOTOR_3_PINS, STEPPER_SEQ_FULLSPEED, motor3_stop, MOTOR3_FWD_DELAY, False),
        daemon=True
    )

    motor1_thread.start()
    motor2_thread.start()
    motor3_thread.start()

    print("Waiting for Sensor 1 BLOCKED (high)...")
    ok = wait_for_level_stable(sensor1_pin, SENSOR_BLOCKED_LEVEL, SENSOR1_BLOCK_TIMEOUT_SEC, stable_ms=STABLE_TIME_MS)
    if not ok:
        print("Timeout: Sensor 1 did not go BLOCKED in time")
        stop_motor(MOTOR_1_PINS, motor1_stop, motor1_thread)
        stop_motor(MOTOR_2_PINS, motor2_stop, motor2_thread)
        stop_motor(MOTOR_3_PINS, motor3_stop, motor3_thread)
        pi.stop()
        print("1")
        sys.exit(1)

    print("Sensor 1 BLOCKED (front edge detected)")

    # Stop Motor 1 promptly so it doesn't continue pulling a second card
    print("Stopping Motor 1 (immediately after sensor1 triggers)")
    stop_motor(MOTOR_1_PINS, motor1_stop, motor1_thread)

    # Keep Motor 2 running in its CURRENT (Phase 1) direction for 1.2 seconds
    # to push more of the card through before we stop & reverse for anti-double-feed
    print(f"Keeping Motor 2 running for {MOTOR2_EXTRA_FEED_SEC:.1f}s after sensor1 triggers")
    time.sleep(MOTOR2_EXTRA_FEED_SEC)

    print("Stopping Motor 2 (after extra feed time)")
    stop_motor(MOTOR_2_PINS, motor2_stop, motor2_thread)

    # Now proceed to Phase 2 (reverse) as before...


    # Phase 2: reverse to prevent double-feed until sensor1 clears
    print("Starting Phase 2 anti-double-feed (motors 1 & 2 reverse until sensor1 clears)")
    motor1_stop.clear()
    motor2_stop.clear()

    motor1_thread = threading.Thread(
        target=motor_step_sequence,
        args=(MOTOR_1_PINS, STEPPER_SEQ_HALFSPEED, motor1_stop, MOTOR1_REV_DELAY, True),
        daemon=True
    )
    # Phase 2: motor2 must FLIP direction vs Phase 1 (so reverse=False here)
    motor2_thread = threading.Thread(
        target=motor_step_sequence,
        args=(MOTOR_2_PINS, STEPPER_SEQ_HALFSPEED, motor2_stop, MOTOR2_REV_DELAY, False),
        daemon=True
    )

    motor1_thread.start()
    motor2_thread.start()

    print("Waiting for Sensor 1 CLEAR (low)...")
    ok = wait_for_level_stable(
        sensor1_pin,
        0 if SENSOR_BLOCKED_LEVEL == 1 else 1,
        SENSOR1_CLEAR_TIMEOUT_SEC,
        stable_ms=STABLE_TIME_MS
    )
    if not ok:
        print("Timeout: Sensor 1 did not CLEAR in time")
        stop_motor(MOTOR_1_PINS, motor1_stop, motor1_thread)
        stop_motor(MOTOR_2_PINS, motor2_stop, motor2_thread)
        stop_motor(MOTOR_3_PINS, motor3_stop, motor3_thread)
        pi.stop()
        print("1")
        sys.exit(1)

    print("Sensor 1 CLEAR - stopping motors 1 & 2")
    stop_motor(MOTOR_1_PINS, motor1_stop, motor1_thread)
    stop_motor(MOTOR_2_PINS, motor2_stop, motor2_thread)

    # Motor 3 continues until sensor2 trips
    print("Waiting for Sensor 2 BLOCKED (high)...")
    ok = wait_for_level_stable(sensor2_pin, SENSOR_BLOCKED_LEVEL, SENSOR2_BLOCK_TIMEOUT_SEC, stable_ms=STABLE_TIME_MS)
    if not ok:
        print("Timeout: Sensor 2 did not go BLOCKED in time")
        stop_motor(MOTOR_3_PINS, motor3_stop, motor3_thread)
        pi.stop()
        print("1")
        sys.exit(1)

    print("Sensor 2 BLOCKED - stopping Motor 3")
    stop_motor(MOTOR_3_PINS, motor3_stop, motor3_thread)

    pi.stop()
    print("0")
    sys.exit(0)

except KeyboardInterrupt:
    print("Stopping motors due to KeyboardInterrupt...")
    try:
        stop_motor(MOTOR_1_PINS, motor1_stop, motor1_thread)
        stop_motor(MOTOR_2_PINS, motor2_stop, motor2_thread)
        stop_motor(MOTOR_3_PINS, motor3_stop, motor3_thread)
    except Exception:
        pass
    pi.stop()
    print("1")
    sys.exit(1)
