# BBG TCG Sorter

An automated trading card sorter built on a Raspberry Pi 4. Cards are fed through a
physical mechanism, a camera captures each card, an OCR/vision provider (AWS
Rekognition or an Ollama-compatible vision model) reads the card name/set, Scryfall
identifies the card, and servo-controlled flappers route it into one of 10 bins. A
Flask web UI runs the whole thing and exposes live status, bin rules, and settings.

3D-printable design files for the physical build: **[Etsy listing](https://www.etsy.com/listing/4357367379/mtg-card-sorter-3d-print-stl-files-ikea)**

Build Instructions located at: https://itsn80.github.io/bbg-tcg-sorter/assembly-guide/

This is a hobbyist project shared as-is under the AGPLv3 license (see [LICENSE](LICENSE)).
It assumes you're comfortable with basic Raspberry Pi setup, soldering/wiring, and a
little troubleshooting — it is not a polished consumer product.

---

## Hardware You'll Need

- Raspberry Pi 4 Model B (4GB+ recommended) and a microSD card
- Raspberry Pi Camera Module (CSI ribbon connector, Picamera2-compatible)
- PCA9685 16-channel PWM/servo driver board (I2C)
- 10x micro hobby servos (SG90-class): 9 for the bin flappers, 1 for card release/capture
- 3x 4-wire unipolar stepper motors with ULN2003-style driver boards (card feed, guide, and exit)
- 2x IR break-beam or photointerrupter sensors (card-present detection at feed and exit)
- WS2812B addressable LED strip (8 LEDs by default; length is configurable in Settings)
- The 3D-printed housing, bin, and flapper parts (see link above)
- A 5V power supply sized for your combined servo/motor/LED load, plus wiring,
  standoffs, and fasteners

The exact pin/channel wiring this software expects is documented in
[Pin Reference](#pin-reference) below.

---

## Software Setup

These steps assume a fresh Raspberry Pi OS (64-bit, Bookworm or newer) install,
flashed with Raspberry Pi Imager, with SSH enabled.

### 1. Enable I2C and the camera

```bash
sudo raspi-config
```
Under **Interface Options**, enable **I2C** and **Camera**, then reboot.

### 2. Install system packages

```bash
sudo apt update
sudo apt install -y git python3-pip python3-dev i2c-tools python3-picamera2 build-essential
```

Verify the PCA9685 is detected once it's wired up:
```bash
i2cdetect -y 1
```
You should see a device at address `0x40`.

### 3. Build and install pigpio

`pigpio` provides GPIO/PWM access for the stepper motors, sensors, and LED strip.
It's no longer packaged in Debian/Raspberry Pi OS apt repos, so it needs to be built
from source:

```bash
cd ~
wget https://github.com/joan2937/pigpio/archive/master.zip
unzip master.zip
cd pigpio-master
make
sudo make install
```

Create a systemd service so the daemon starts on boot (there's no packaged unit file
since it wasn't installed via apt):

```bash
sudo tee /etc/systemd/system/pigpiod-custom.service > /dev/null <<'EOF'
[Unit]
Description=PIGPIO Daemon (Custom Build)
After=network.target

[Service]
ExecStart=/home/YOUR_USERNAME/pigpio-master/pigpiod
Type=forking
User=root
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now pigpiod-custom.service
```

Replace `YOUR_USERNAME` with your actual Pi username, and double check the `ExecStart`
path matches where you built pigpio.

### 4. Clone the repo and install Python dependencies

```bash
cd ~
git clone https://github.com/ItsN80/bbg-tcg-sorter.git
cd bbg-tcg-sorter
pip install -r requirements.txt --break-system-packages
```

`--break-system-packages` is required because Raspberry Pi OS (Bookworm+) marks the
system Python as externally managed. This only affects this one `pip install` command.

### 5. Run it once to generate config files

```bash
python3 Basic-Website.py
```

On first run this creates `storage/config.json` and `storage/bin-info.json` from their
`-default.json` templates. Stop it with `Ctrl+C` once you see it serving — you'll run it
as a service from here on.

### 6. Install it as a systemd service

```bash
sudo tee /etc/systemd/system/card-sorter.service > /dev/null <<'EOF'
[Unit]
Description=Card Sorter Flask App
After=network-online.target
Wants=network-online.target

[Service]
User=YOUR_USERNAME
WorkingDirectory=/home/YOUR_USERNAME/bbg-tcg-sorter/
ExecStart=/usr/bin/python3 /home/YOUR_USERNAME/bbg-tcg-sorter/Basic-Website.py
Restart=always
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now card-sorter.service
```

Again, replace `YOUR_USERNAME` with your actual Pi username in both `User=` and the paths.

### 7. Allow the app to reboot/shutdown/update the Pi (optional but recommended)

The web UI has buttons to reboot, shut down, and self-update the Pi, which need
passwordless `sudo` for exactly those three commands. Rather than granting broad
`sudo` access, scope it to just what's needed:

```bash
sudo visudo -f /etc/sudoers.d/card-sorter
```
Add this line (replacing `YOUR_USERNAME`):
```
YOUR_USERNAME ALL=(root) NOPASSWD: /usr/sbin/reboot, /usr/sbin/shutdown, /usr/bin/systemctl restart card-sorter.service
```

If you skip this, the Reboot/Shutdown/Update Program buttons in Settings won't work,
but everything else will.

### 8. First login and configuration

Browse to `http://<your-pi-ip>:5000/`. On first visit you'll be asked to set a
password — this gates the entire app, since it controls physical hardware and can
reboot/shut down the Pi. Choose a real password here, not a placeholder.

Then go to **Settings** and configure:
- **Recognition Provider** — Amazon Rekognition, Ollama, or DO Serverless (see notes below)
- Servo open/close degrees per flapper and the card servo, once your mechanism is assembled
- LED strip color/brightness/enabled state
- Optional shutdown-summary email (SMTP)

---

## Important Notes

- **Recognition provider costs money (usually).** AWS Rekognition bills per API call.
  DO Serverless or any remotely-hosted Ollama endpoint you point at will likely have
  its own cost/credit system. Running Ollama locally on the Pi itself is free but will
  be slow and less accurate without a GPU. Pick based on your budget and accuracy needs.
- **This app is not designed for internet exposure.** It's gated by a single shared
  password suitable for a trusted home LAN. Don't port-forward it to the public
  internet — if you want remote access, put it behind a VPN (e.g. Tailscale or
  WireGuard) instead.
- **`pigpiod` must be running before the app starts.** `Basic-Website.py` connects to
  the pigpio daemon on startup and will exit immediately if it can't. If the service
  fails to start, check `sudo systemctl status pigpiod-custom` first.
- **I2C and the camera must be enabled** (step 1 above) or the PCA9685/servo and
  camera calls will fail outright.
- **Your user account needs hardware group membership** — `gpio`, `i2c`, `spi`,
  `video`, and `dialout`. The default first-user account on Raspberry Pi OS already
  has these; if you created a separate service account, add it to these groups.
- **Updating via the UI requires a clean git checkout.** The Settings page's
  "Update Program" button runs `git pull` and restarts the service automatically. If
  you've hand-edited tracked files on the Pi, the pull can fail — commit or discard
  those changes first. `storage/config.json` and `storage/bin-info.json` are
  gitignored and untouched by updates.
- **License is AGPLv3.** If you modify and distribute this (including running a
  modified version as a network service others use), the license requires making your
  source available. See [LICENSE](LICENSE).

---

## Pin Reference

### PCA9685 (I2C, address `0x40`, wired to GPIO 2/3)

| Channel | Device |
|---------|--------|
| 0–8 | Flappers 1–9 |
| 9 | Card servo (release/capture) |
| 10–15 | Unused |

### Raspberry Pi GPIO (BCM numbering)

| GPIO | Device |
|------|--------|
| 2 | I2C SDA → PCA9685 |
| 3 | I2C SCL → PCA9685 |
| 4, 17, 27, 22 | Motor 1 (card feeder input) |
| 5, 9, 10, 11 | Motor 2 (card guide/pusher) |
| 6, 13, 19, 26 | Motor 3 (card exit guide) |
| 8 | Sensor 1 (card feed) |
| 12 | LED strip (WS2812) |
| 14 | Sensor 2 (card exit) — shares the pin with UART TX; works in practice but be aware if you also need the serial console |

Flapper/card-servo open and close angles, and per-flapper channel assignments, are
stored in `storage/config.json` and editable from the Settings page once the app is
running.

---

## Updating

Via the UI: **Settings → Update Program**. This pulls the latest commit and restarts
the app automatically.

Via SSH:
```bash
cd ~/bbg-tcg-sorter
git pull
sudo systemctl restart card-sorter.service
```

## Support

Found a bug or have a question? Open an issue on the
[GitHub repo](https://github.com/ItsN80/bbg-tcg-sorter/issues).

## License

[GNU AGPLv3](LICENSE)
