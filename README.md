# ZenDesk

Raspberry Pi wellness desk: **heart rate & SpO2** (MAX30102), mood insights, guided breathing when stressed, plus mini-games.

## Hardware

| Part | Connection |
|------|------------|
| 240×320 SPI TFT | DC=GPIO24, RST=GPIO25, CS=GPIO8 |
| MAX30102 | I2C bus 1, address `0x57` |
| Button B1 (select) | GPIO16 → GND (internal pull-up) |
| Button B2 (down/back) | GPIO20 → GND |

Edit pins in [`config.json`](config.json) if your wiring differs.

## Install on Raspberry Pi

```bash
sudo apt update
sudo apt install -y python3-pip python3-pil python3-spidev python3-smbus \
  python3-rpi-lgpio python3-lgpio
sudo raspi-config   # enable SPI and I2C, reboot

cd ~/zendesk   # use the whole folder, not only zendesk.py
pip3 install -r requirements.txt
pip3 uninstall -y RPi.GPIO 2>/dev/null || true   # avoid conflict with apt lgpio

# Allow GPIO without sudo (log out & back in after this)
sudo usermod -aG gpio,spi,i2c $USER

python3 zendesk.py
# If permission errors:  sudo python3 zendesk.py
```

### `lgpio.error: GPIO not allocated`

1. Use **`python3-rpi-lgpio`** (commands above), not pip `RPi.GPIO`.
2. Run from **`~/zendesk`** (needs `config.json` beside `zendesk.py`).
3. Reboot the Pi after enabling SPI/I2C.
4. Do not run two copies of the app at once.

## Auto-start on boot (optional)

```bash
sudo cp zendesk.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable zendesk
sudo systemctl start zendesk
# Logs: journalctl -u zendesk -f
```

## Data files (created automatically)

| File | Purpose |
|------|---------|
| `~/.zendesk_data.json` | Settings, high scores, vitals log, streak |
| `~/.zendesk_data.json.bak` | Backup before each save |
| `~/zendesk_vitals.csv` | One row per successful reading (for graphs) |

## Main feature: Heart rate

1. Menu → **Heart Rate** → place finger on MAX30102.
2. Hold still ~10 seconds (progress bars on screen; **B2** skips back).
3. Vitals → mood → tips (or **breathing** if excited, **rest timer** if sleepy).
4. Readings appear on the menu (“Last: 72 bpm · calm · 2h ago”) and in **Settings → View vitals log**.

If the sensor is missing, HR shows an error but **games still work**.

**Finger not detected?** Settings → **Finger IR level** (15000 / 20000 / 25000).

## Games

- Flappy Bird, Pong vs CPU, Reaction test, Chrome-style **Dino Run** (B1 jump).
- High scores in Settings; game speed and screen dim in Settings.

## Develop on PC (no Pi)

```bash
pip install Pillow
python simulator.py
```

Space = B1, Down arrow = B2. No real sensor (HR uses fake readings).

## Tips

- Display glitches: lower `spi_speed_hz` in `config.json` to `16000000`.
- CSV export: open `~/zendesk_vitals.csv` in Excel or Google Sheets.
- Wellness streak: check vitals on consecutive days (shown on menu).
