import threading
import pigpio
import time
import sys

# Pin Definitions
MOTOR_1_PINS = [19, 26, 4, 17]
MOTOR_2_PINS = [27, 22, 10, 9]
MOTOR_3_PINS = [11, 7, 5, 6]
sensor1_pin = 8
sensor2_pin = 14

# Stepper motor sequences
STEPPER_SEQ_FULLSPEED = [
    [1, 0, 0, 0],
    [1, 1, 0, 0],
    [0, 1, 0, 0],
    [0, 1, 1, 0],
    [0, 0, 1, 0],
    [0, 0, 1, 1],
    [0, 0, 0, 1],
    [1, 0, 0, 1],
    [1, 0, 0, 0],
    [1, 1, 0, 0],
    [0, 1, 0, 0],
    [0, 1, 1, 0],
    [0, 0, 1, 0],
    [0, 0, 1, 1],
    [0, 0, 0, 1],
    [1, 0, 0, 1]
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
    [1, 0, 0, 1]
]

# Initialize pigpio
pi = pigpio.pi()
if not pi.connected:
    print("pigpio not connected. Exiting.")
    sys.exit(1)

all_motor_pins = MOTOR_1_PINS + MOTOR_2_PINS + MOTOR_3_PINS
for pin in all_motor_pins:
    pi.set_mode(pin, pigpio.OUTPUT)
    pi.write(pin, 0)

# Create stop events for each motor
motor1_stop_event = threading.Event()
motor2_stop_event = threading.Event()
motor3_stop_event = threading.Event()

def motor_step(motor_pins, step):
    for pin, val in zip(motor_pins, step):
        pi.write(pin, val)

def motor_step_sequence(motor_pins, sequence, stop_event, reverse=False, speed_delay=0.001):
    while not stop_event.is_set():
        for step in sequence:
            if stop_event.is_set():
                break
            # Reverse the step if needed
            motor_step(motor_pins, step[::-1] if reverse else step)
            time.sleep(speed_delay)

# The stop_motor functions use the global thread variables.
def stop_motor_1():
    global motor1_thread
    motor1_stop_event.set()  # Signal thread to stop
    motor1_thread.join()     # Wait for thread to stop
    for pin in MOTOR_1_PINS:
        pi.write(pin, 0)     # Turn off motor

def stop_motor_2():
    global motor2_thread
    motor2_stop_event.set()
    motor2_thread.join()
    for pin in MOTOR_2_PINS:
        pi.write(pin, 0)

def stop_motor_3():
    global motor3_thread
    motor3_stop_event.set()
    motor3_thread.join()
    for pin in MOTOR_3_PINS:
        pi.write(pin, 0)

try:
    #print("Run until Sensor 1 goes high, then stop motor 2")
    # Start Motor 1, 2, and 3 with initial settings.
    motor1_thread = threading.Thread(target=motor_step_sequence, args=(MOTOR_1_PINS, STEPPER_SEQ_FULLSPEED, motor1_stop_event, False, 0.001))
    motor2_thread = threading.Thread(target=motor_step_sequence, args=(MOTOR_2_PINS, STEPPER_SEQ_HALFSPEED, motor2_stop_event, True, 0.001))
    motor3_thread = threading.Thread(target=motor_step_sequence, args=(MOTOR_3_PINS, STEPPER_SEQ_FULLSPEED, motor3_stop_event, False, 0.001))
    motor1_thread.start()
    motor2_thread.start()
    motor3_thread.start()

    # Wait until Sensor 1 goes high, but time out after 5 seconds.
    start_time = time.time()
    while pi.read(sensor1_pin) == 0:
        if time.time() - start_time > 8:
            #print("Timeout: Sensor 1 did not go high within 5 seconds")
            stop_motor_1()
            stop_motor_2()
            stop_motor_3()
            pi.stop()
            print("1")  # Output failure code
            sys.exit(1)
        time.sleep(0.05)

    #print("Sensor 1 High")
    time.sleep(1)
    stop_motor_2()
    stop_motor_1()

    #print("Restarting Motor 1 & 2 with different settings")
    # Clear the stop events before restarting.
    motor1_stop_event.clear()
    motor2_stop_event.clear()
    motor1_thread = threading.Thread(target=motor_step_sequence, args=(MOTOR_1_PINS, STEPPER_SEQ_HALFSPEED, motor1_stop_event, True, 0.001))
    motor1_thread.start()
    motor2_thread = threading.Thread(target=motor_step_sequence, args=(MOTOR_2_PINS, STEPPER_SEQ_HALFSPEED, motor2_stop_event, False, 0.001))
    motor2_thread.start()
        
    # Wait until Sensor 1 goes low, with a timeout of 3 seconds.
    start_time = time.time()
    while pi.read(sensor1_pin) == 1:
        if time.time() - start_time > 5:
            #print("Timeout: Sensor 1 did not go low within 3 seconds")
            stop_motor_1()
            stop_motor_2()
            stop_motor_3()
            pi.stop()
            print("1")
            sys.exit(1)
        time.sleep(0.05)
    
    #print("Sensor 1 Low")
    time.sleep(1)

    #print("Stopping Motor 2")
    stop_motor_2()
    #print("Stopping Motor 1")
    stop_motor_1()

    # Continue to run Motor 3 until Sensor 2 goes high.
    while pi.read(sensor2_pin) == 0:
        time.sleep(0.1)

    #print("Stopping Motor 3")
    stop_motor_3()
    pi.stop()

    # If we reach here, all operations were successful.
    print("0")  # Output success code
    sys.exit(0)

except KeyboardInterrupt:
    print("Stopping motors due to KeyboardInterrupt...")
    try:
        motor2_stop_event.set()
        stop_motor_2()
    except Exception:
        pass
    pi.stop()
    print("1")
    sys.exit(1)
