import pigpio
import time
import json
import os

SERVO_PIN = 16

# Dynamic path to config.json
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "..", "storage", "config.json")

# Create a pigpio instance
pi = pigpio.pi()

# Set the GPIO pin to output mode
pi.set_mode(SERVO_PIN, pigpio.OUTPUT)

# Function to set the servo angle (0-180 degrees)
def set_servo_angle(angle):
    if 0 <= angle <= 180:
        # Calculate the pulse width in microseconds
        pulse_width = (angle / 180) * 1000 + 500  # Typical range: 500-2500µs
        pi.set_servo_pulsewidth(SERVO_PIN, pulse_width)
        print(f"Setting servo to {angle} degrees (Pulse Width: {pulse_width}µs)")

try:
    # Read the configuration from config.json
    with open(CONFIG_PATH, "r") as f:
        config = json.load(f)
    
    # Retrieve the open degree value for the card servo.
    # If not found, default to 90.
    open_degrees = config.get("flappers", {}).get("flapper_6", {}).get("open_degrees", 90)
    
    # Open the servo to the specified angle.
    set_servo_angle(open_degrees)
    time.sleep(1)  # Hold position for 1 second

except KeyboardInterrupt:
    print("\nExiting...")
finally:
    # Stop the servo and release the GPIO pin
    pi.set_servo_pulsewidth(SERVO_PIN, 0)  # Stop the servo from jittering