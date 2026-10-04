# TODO

## LED lighting shifts to blue while scanning

During a sort the card is lit (or photographed) blue/cyan instead of the configured
warm white, which hurts recognition accuracy (seen 2026-10-04 on "Scour the
Laboratory": the whole capture has a strong blue cast).

What's known:
- Config colour is warm white (`led.color` = 255, 240, 184), and `sorting_loop()`
  reads it correctly via `led_controller.get_state()`.
- That colour with red and blue swapped is (184, 240, 255), a light cyan, which
  matches the cast in the capture. So the likely cause is a channel-order mix-up.

Where to look:
- `led_controller.py`: the WS2812 byte order (the strips are usually GRB) used
  when building the pigpio wave. Check whether r and b are swapped.
- `scripts/Read-Card.py` `capture_image()`: Picamera2 `RGB888` is BGR in memory.
  Check whether the saved JPEG has its channels swapped, and whether auto white
  balance is fighting the LEDs (consider fixed `AwbEnable`/`ColourGains`).
- Watch the strip itself during a sort, so you know whether the LEDs or only the
  photo turn blue.
- Bonus: `led_controller.set_color(255, 30, 30)` on a failed read and the idle
  restore would be affected by the same swap.
