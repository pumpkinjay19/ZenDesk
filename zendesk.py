"""
ZenDesk — Integrated Launcher
==============================
B1 (GPIO16) = Select / Confirm / Action
B2 (GPIO20) = Down / Back / Quit

Main Menu:
  → Heart Rate Monitor (+ daily streak)
  → Flappy Bird / Pong / Reaction / Dino Run
  → Settings (speed, dim display, high scores)
  → Excited HR mood → guided breathing timer
"""

import json
import os
import shutil
import spidev
import RPi.GPIO as GPIO
from smbus2 import SMBus
from PIL import Image, ImageDraw, ImageFont
from datetime import date, timedelta
import time
import math
import random

# ══════════════════════════════════════════════════════════════
# CONFIG & PATHS
# ══════════════════════════════════════════════════════════════
WIDTH, HEIGHT = 240, 320
_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
DATA_FILE = os.path.expanduser("~/.zendesk_data.json")
CSV_FILE = os.path.expanduser("~/zendesk_vitals.csv")
DEFAULT_DATA = {
    "scores": {
        "flappy": 0, "dino": 0, "reaction_avg": 9999, "reaction_best": 9999,
        "pong_wins": 0, "pong_best_rally": 0,
    },
    "vitals_days": [],
    "vitals_log": [],
    "last_vitals": None,
    "meta": {"streak_popup_date": ""},
    "settings": {
        "speed_pct": 100, "brightness": 100, "breathing": "box", "ir_threshold": 20000,
    },
}
DATA = {}
SENSOR_OK = False
IDLE_TIMEOUT_S = 60
ZENDESK_VERSION = "2.1-mochi"

# ══════════════════════════════════════════════════════════════
# COLOUR PALETTE
# ══════════════════════════════════════════════════════════════
BG_DARK      = (18,  12,  35)
BG_MID       = (32,  20,  58)
PURPLE_DEEP  = (88,  44, 130)
PURPLE_MID   = (140, 82, 200)
PURPLE_LIGHT = (196, 154, 240)
PINK_HOT     = (255,  80, 160)
PINK_SOFT    = (255, 160, 210)
PINK_PALE    = (255, 220, 240)
MINT         = (100, 230, 180)
MINT_DARK    = ( 40, 180, 120)
TEAL         = ( 60, 200, 200)
GOLD         = (255, 210,  80)
WHITE        = (255, 255, 255)
OFF_WHITE    = (240, 230, 255)
RED          = (220,  60,  60)

# Mochi bot face (expressions only — no frame/box)
MOCHI_CREAM  = (255, 247, 240)
MOCHI_SHADOW = (242, 224, 214)
MOCHI_BLUSH  = (255, 175, 192)
MOCHI_INK    = (58,  48,  72)
MOCHI_MOOD   = {
    "calm":    PURPLE_LIGHT,
    "happy":   MINT,
    "excited": PINK_HOT,
    "sleepy":  (168, 148, 228),
}

def _mochi_eye_dots(draw, lx, rx, ey, r=5):
    for ex in (lx, rx):
        draw.ellipse([ex - r, ey - r, ex + r, ey + r], fill=MOCHI_INK)
        draw.ellipse([ex + 1, ey - 2, ex + 3, ey], fill=WHITE)

def _mochi_eye_blink(draw, lx, rx, ey):
    for ex in (lx, rx):
        draw.arc([ex - 9, ey - 5, ex + 9, ey + 7], start=0, end=180, fill=MOCHI_INK, width=3)

def _mochi_zzz(draw, cx, cy, frame):
    for i, (dx, dy, sc) in enumerate([(28, -42, 1.0), (42, -58, 0.75), (54, -72, 0.55)]):
        drift = int(6 * math.sin(frame * 0.08 + i * 1.2))
        x = cx + dx + drift
        y = cy + dy - int(frame * 0.4) % 20
        s = int(7 * sc)
        col = (PURPLE_MID, PURPLE_LIGHT, PINK_SOFT)[i]
        draw.arc([x - s, y - s, x + s, y + s], start=200, end=340, fill=col, width=2)
        draw.line([(x + s - 2, y), (x + s + 4, y)], fill=col, width=2)

# Flappy / Pong specific
SKY_TOP     = (30,  60, 120)
SKY_BOT     = (60, 130, 200)
GRASS       = (52, 168,  72)
GRASS_DARK  = (34, 120,  48)
PIPE_GREEN  = (46, 175,  68)
PIPE_LITE   = (72, 205,  88)
PIPE_DARK   = (28, 105,  42)
PIPE_RIM    = (20,  75,  30)
BIRD_YELLOW = (255, 220,  60)
BIRD_ORANGE = (255, 155,  40)
BIRD_CHEEK  = (255, 120,  90)
FB_FRAME_S  = 1.0 / 30
MENU_FRAME_S = 1.0 / 20

def _load_config():
    cfg = {
        "pins": {"dc": 24, "rst": 25, "cs": 8, "btn1": 16, "btn2": 20},
        "spi_speed_hz": 32_000_000,
        "spi_speed_fallback_hz": 16_000_000,
        "i2c_bus": 1,
        "sensor_addr": "0x57",
        "idle_timeout_s": 60,
    }
    try:
        if os.path.isfile(_CONFIG_PATH):
            with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
                user = json.load(f)
            for k, v in user.items():
                if isinstance(v, dict) and k in cfg and isinstance(cfg[k], dict):
                    cfg[k].update(v)
                else:
                    cfg[k] = v
    except (OSError, json.JSONDecodeError) as e:
        print(f"Config: {e}")
    return cfg

CFG = _load_config()
DC, RST, CS = CFG["pins"]["dc"], CFG["pins"]["rst"], CFG["pins"]["cs"]
BTN1, BTN2 = CFG["pins"]["btn1"], CFG["pins"]["btn2"]
IDLE_TIMEOUT_S = CFG.get("idle_timeout_s", 60)

spi = None
bus = None
_HW_READY = False

GAME_FRAME_S = 1.0 / 42
addr = int(CFG.get("sensor_addr", "0x57"), 16)

def init_hardware():
    """Init GPIO/SPI/I2C once (Bookworm lgpio needs setmode before setup)."""
    global spi, bus, _HW_READY
    if _HW_READY:
        return
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    for _p in (DC, RST, CS):
        GPIO.setup(_p, GPIO.OUT, initial=GPIO.LOW)
    for _p in (BTN1, BTN2):
        GPIO.setup(_p, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    spi = spidev.SpiDev()
    spi.open(0, 0)
    spi.mode = 0
    for _mhz in (CFG.get("spi_speed_hz", 32_000_000), CFG.get("spi_speed_fallback_hz", 16_000_000)):
        try:
            spi.max_speed_hz = _mhz
            break
        except Exception:
            continue
    bus = SMBus(CFG.get("i2c_bus", 1))
    _HW_READY = True
    print("Hardware OK (GPIO/SPI/I2C)")

# ══════════════════════════════════════════════════════════════
# DISPLAY DRIVER
# ══════════════════════════════════════════════════════════════
def _cmd(c):
    GPIO.output(CS, 0); GPIO.output(DC, 0); spi.writebytes([c]); GPIO.output(CS, 1)

def _data(d):
    GPIO.output(CS, 0); GPIO.output(DC, 1); spi.writebytes([d]); GPIO.output(CS, 1)

def init_display():
    GPIO.output(RST, 0); time.sleep(0.1)
    GPIO.output(RST, 1); time.sleep(0.2)
    _cmd(0x01); time.sleep(0.2)
    _cmd(0x11); time.sleep(0.12)
    _cmd(0x3A); _data(0x55)
    _cmd(0x36); _data(0xC8)
    _cmd(0x29)

_SPI_RGB565 = bytearray(WIDTH * HEIGHT * 2)
_SCRATCH_RGB = Image.new("RGB", (WIDTH, HEIGHT))
_LUT_R = None
_LUT_G = None
_LUT_B = None
_LUT_BR = -1

def _rebuild_rgb565_lut():
    global _LUT_R, _LUT_G, _LUT_B, _LUT_BR
    br = int(DATA.get("settings", {}).get("brightness", 100))
    if _LUT_R is not None and _LUT_BR == br:
        return
    _LUT_BR = br
    f = br / 100.0
    if f >= 0.995:
        _LUT_R = [((i & 0xF8) << 8) for i in range(256)]
        _LUT_G = [((i & 0xFC) << 3) for i in range(256)]
        _LUT_B = [(i >> 3) for i in range(256)]
    else:
        _LUT_R = [((int(i * f) & 0xF8) << 8) for i in range(256)]
        _LUT_G = [((int(i * f) & 0xFC) << 3) for i in range(256)]
        _LUT_B = [(int(i * f) >> 3) for i in range(256)]

def _rgb_to_565(raw, buf):
    lr, lg, lb = _LUT_R, _LUT_G, _LUT_B
    j = 0
    for i in range(0, len(raw), 3):
        c = lr[raw[i]] | lg[raw[i + 1]] | lb[raw[i + 2]]
        buf[j] = c >> 8
        buf[j + 1] = c
        j += 2

def show_image(img):
    if img.mode != "RGB":
        img = img.convert("RGB")
    _rebuild_rgb565_lut()
    raw = img.tobytes()
    buf = _SPI_RGB565
    _rgb_to_565(raw, buf)
    j = len(buf)
    _cmd(0x2A)
    for b in (0x00, 0x00, 0x00, 0xEF):
        _data(b)
    _cmd(0x2B)
    for b in (0x00, 0x00, 0x01, 0x3F):
        _data(b)
    _cmd(0x2C)
    GPIO.output(CS, 0)
    GPIO.output(DC, 1)
    mv = memoryview(buf)
    for off in range(0, j, 32768):
        spi.writebytes2(mv[off:off + 32768])
    GPIO.output(CS, 1)

def new_frame(bg=BG_DARK):
    img = Image.new("RGB", (WIDTH, HEIGHT), bg)
    return img, ImageDraw.Draw(img)

_BG_CACHE = {}
_FONTS = {}

def font(size):
    if size in _FONTS:
        return _FONTS[size]
    for path in [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ]:
        try:
            _FONTS[size] = ImageFont.truetype(path, size)
            return _FONTS[size]
        except OSError:
            pass
    _FONTS[size] = ImageFont.load_default()
    return _FONTS[size]

def init_assets():
    """Pre-render static backgrounds (call once at startup)."""
    if _BG_CACHE:
        return
    for key, top, bot in (
        ("menu", BG_DARK, BG_MID),
        ("sky", SKY_TOP, SKY_BOT),
        ("dark", BG_DARK, BG_MID),
    ):
        img = Image.new("RGB", (WIDTH, HEIGHT))
        gradient(ImageDraw.Draw(img), top, bot)
        _BG_CACHE[key] = img
    _BG_CACHE["sky_play"] = _make_fb_play_bg()
    _BG_CACHE["fb_menu"] = _make_fb_menu_bg()

def _make_fb_play_bg():
    img = Image.new("RGB", (WIDTH, HEIGHT))
    d = ImageDraw.Draw(img)
    gradient(d, (18, 45, 95), (72, 145, 220), step=2)
    d.ellipse([175, 28, 215, 68], fill=(255, 245, 160))
    d.ellipse([183, 36, 207, 60], fill=(255, 252, 200))
    gh = 36
    for col, yb, h in ((42, 58, 50), (55, 72, 42)):
        pts = [(0, HEIGHT - gh), (WIDTH, HEIGHT - gh)]
        for x in range(0, WIDTH + 60, 60):
            pts.append((x, HEIGHT - gh - h - int(10 * math.sin(x * 0.07 + yb))))
        d.polygon(pts, fill=col)
    return img

def _make_fb_menu_bg():
    img = Image.new("RGB", (WIDTH, HEIGHT))
    d = ImageDraw.Draw(img)
    gradient(d, (22, 50, 105), (88, 165, 230), step=2)
    for cx, cy, w in [(55, 55, 36), (175, 42, 44), (110, 95, 50)]:
        d.ellipse([cx - w, cy - 14, cx + w, cy + 14], fill=(235, 245, 255))
        d.ellipse([cx - w - 8, cy - 8, cx + w + 8, cy + 8], fill=(245, 250, 255))
    return img

def frame_bg(key):
    _SCRATCH_RGB.paste(_BG_CACHE[key], (0, 0))
    return _SCRATCH_RGB, ImageDraw.Draw(_SCRATCH_RGB)

def wait_frame(t0, target=None):
    if target is None:
        target = game_frame_target()
    elapsed = time.perf_counter() - t0
    if elapsed < target:
        time.sleep(target - elapsed)

# ══════════════════════════════════════════════════════════════
# SAVE DATA / SETTINGS
# ══════════════════════════════════════════════════════════════
def load_data():
    global DATA, _LUT_BR
    DATA = json.loads(json.dumps(DEFAULT_DATA))
    _LUT_BR = -1
    try:
        if os.path.isfile(DATA_FILE):
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            for k, v in saved.items():
                if k == "settings" and isinstance(v, dict):
                    DATA["settings"].update(v)
                elif k in DATA and isinstance(DATA[k], dict) and isinstance(v, dict):
                    DATA[k].update(v)
                else:
                    DATA[k] = v
            sc = DATA.get("scores", {})
            if sc.get("dino", 0) == 0 and sc.get("snake", 0):
                sc["dino"] = sc["snake"]
    except (OSError, json.JSONDecodeError) as e:
        print(f"Data load: {e}")

def save_data():
    try:
        if os.path.isfile(DATA_FILE):
            shutil.copy2(DATA_FILE, DATA_FILE + ".bak")
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(DATA, f, indent=2)
    except OSError as e:
        print(f"Data save: {e}")

def ir_threshold():
    return int(DATA.get("settings", {}).get("ir_threshold", 20000))

def ir_lost_threshold():
    return max(5000, ir_threshold() // 2)

def record_vitals_reading(hr, spo2, mood):
    record_vitals_day()
    entry = {
        "ts": time.time(),
        "date": date.today().isoformat(),
        "hr": hr, "spo2": spo2, "mood": mood,
    }
    DATA["last_vitals"] = entry
    log = DATA.setdefault("vitals_log", [])
    log.append(entry)
    DATA["vitals_log"] = log[-14:]
    save_data()
    export_vitals_csv(entry)
    invalidate_menu_cache()

def export_vitals_csv(entry):
    try:
        new_file = not os.path.isfile(CSV_FILE)
        with open(CSV_FILE, "a", encoding="utf-8") as f:
            if new_file:
                f.write("datetime,hr,spo2,mood\n")
            t = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(entry["ts"]))
            f.write(f"{t},{entry['hr']},{entry['spo2']},{entry['mood']}\n")
    except OSError as e:
        print(f"CSV export: {e}")

def format_last_session():
    lv = DATA.get("last_vitals")
    if not lv:
        return None
    ago = int(time.time() - lv["ts"])
    if ago < 3600:
        when = f"{ago // 60}m ago" if ago >= 60 else "just now"
    elif ago < 86400:
        when = f"{ago // 3600}h ago"
    else:
        when = lv["date"]
    return f"Last: {lv['hr']} bpm · {lv['mood']} · {when}"

def reset_all_scores():
    DATA["scores"] = json.loads(json.dumps(DEFAULT_DATA["scores"]))
    save_data()

def speed_mult():
    return DATA.get("settings", {}).get("speed_pct", 100) / 100.0

def game_frame_target():
    return GAME_FRAME_S / max(0.5, speed_mult())

def record_score(key, value, higher_better=True):
    scores = DATA.setdefault("scores", {})
    cur = scores.get(key, 0 if higher_better else 9999)
    if higher_better and value > cur:
        scores[key] = value
        save_data()
        return True
    if not higher_better and value < cur:
        scores[key] = value
        save_data()
        return True
    return False

def record_vitals_day():
    today = date.today().isoformat()
    days = DATA.setdefault("vitals_days", [])
    if today not in days:
        days.append(today)
        save_data()

def _fmt_reaction(ms):
    return f"{ms}ms" if ms < 9999 else "—"

def vitals_streak():
    days_set = set(DATA.get("vitals_days", []))
    if not days_set:
        return 0
    d = date.today()
    streak = 0
    while d.isoformat() in days_set:
        streak += 1
        d -= timedelta(days=1)
    return streak

# ══════════════════════════════════════════════════════════════
# BUTTONS
# ══════════════════════════════════════════════════════════════
def b1(): return GPIO.input(BTN1) == GPIO.LOW
def b2(): return GPIO.input(BTN2) == GPIO.LOW

def poll(n=8, gap=0.003):
    for _ in range(n):
        if b1(): return 'b1'
        if b2(): return 'b2'
        time.sleep(gap)
    return None

def poll_btn():
    if b1(): return 'b1'
    if b2(): return 'b2'
    return None

def debounce(): time.sleep(0.05)

def wait_release():
    while b1() or b2(): time.sleep(0.01)

# ══════════════════════════════════════════════════════════════
# SHARED DRAWING HELPERS
# ══════════════════════════════════════════════════════════════
def gradient(draw, top, bot, y0=0, y1=HEIGHT, step=1):
    span = max(1, y1 - y0)
    for y in range(y0, y1, step):
        t = (y - y0) / span
        c = tuple(int(top[i] + (bot[i] - top[i]) * t) for i in range(3))
        if step > 1:
            draw.rectangle([0, y, WIDTH, min(y1, y + step)], fill=c)
        else:
            draw.line([(0, y), (WIDTH, y)], fill=c)

def draw_score_hud(draw, text, y=14, accent=WHITE):
    tw = len(text) * 11 + 24
    cx = WIDTH // 2
    rrect(draw, cx - tw // 2, y - 10, cx + tw // 2, y + 14, 8,
          fill=(12, 8, 28), outline=PURPLE_MID, width=1)
    draw.text((cx, y + 1), text, fill=accent, font=font(20), anchor="mm")

def rrect(draw, x1, y1, x2, y2, r=10, fill=None, outline=None, width=1):
    draw.rounded_rectangle([(x1, y1), (x2, y2)], radius=r,
                            fill=fill, outline=outline, width=width)

def glow_circle(draw, cx, cy, r, color, layers=4):
    for i in range(layers, 0, -1):
        cr = min(255, color[0] + 30)
        cg = min(255, color[1] + 30)
        cb = min(255, color[2] + 30)
        draw.ellipse([cx-r-i*3, cy-r-i*3, cx+r+i*3, cy+r+i*3],
                     outline=(cr, cg, cb), width=1)
    draw.ellipse([cx-r, cy-r, cx+r, cy+r], fill=color)

def draw_sparkle(draw, x, y, size, color):
    draw.polygon([(x, y-size), (x+size//3, y), (x, y+size), (x-size//3, y)], fill=color)
    draw.polygon([(x-size, y), (x, y-size//3), (x+size, y), (x, y+size//3)], fill=color)

# ══════════════════════════════════════════════════════════════
# SENSOR (MAX30102)
# ══════════════════════════════════════════════════════════════
def init_sensor():
    global SENSOR_OK
    try:
        bus.write_byte_data(addr, 0x09, 0x40)
        time.sleep(0.5)
        bus.write_byte_data(addr, 0x04, 0x00)
        bus.write_byte_data(addr, 0x05, 0x00)
        bus.write_byte_data(addr, 0x06, 0x00)
        bus.write_byte_data(addr, 0x08, 0x5F)
        bus.write_byte_data(addr, 0x09, 0x03)
        time.sleep(0.05)
        bus.write_byte_data(addr, 0x0A, 0x27)
        bus.write_byte_data(addr, 0x0C, 0x1F)
        bus.write_byte_data(addr, 0x0D, 0x1F)
        time.sleep(0.1)
        flush_fifo()
        time.sleep(0.05)
        SENSOR_OK = True
        print("Sensor initialised")
    except OSError as e:
        SENSOR_OK = False
        print(f"Sensor init failed: {e}")

def flush_fifo():
    bus.write_byte_data(addr, 0x04, 0x00)
    bus.write_byte_data(addr, 0x05, 0x00)
    bus.write_byte_data(addr, 0x06, 0x00)

def read_sample_blocking(timeout=0.5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        wr = bus.read_byte_data(addr, 0x04) & 0x1F
        rd = bus.read_byte_data(addr, 0x06) & 0x1F
        if (wr - rd) & 0x1F > 0:
            raw = bus.read_i2c_block_data(addr, 0x07, 6)
            red = (raw[0] << 16 | raw[1] << 8 | raw[2]) & 0x3FFFF
            ir  = (raw[3] << 16 | raw[4] << 8 | raw[5]) & 0x3FFFF
            return red, ir
        time.sleep(0.005)
    return None

def check_finger():
    flush_fifo()
    time.sleep(0.05)
    s = read_sample_blocking(timeout=0.5)
    if s is None: return False
    _, ir = s
    print(f"  check_finger IR={ir}")
    return ir > ir_threshold()

# ══════════════════════════════════════════════════════════════
# SIGNAL PROCESSING
# ══════════════════════════════════════════════════════════════
def bandpass_simple(signal, window=5):
    if len(signal) < window * 2: return signal
    dc = []
    for i in range(len(signal)):
        lo = max(0, i - window); hi = min(len(signal), i + window + 1)
        dc.append(sum(signal[lo:hi]) / (hi - lo))
    return [signal[i] - dc[i] for i in range(len(signal))]

def detect_peaks(signal, min_distance=10, threshold_ratio=0.4):
    if len(signal) < 4: return []
    mn, mx = min(signal), max(signal)
    threshold = mn + (mx - mn) * threshold_ratio
    peaks, last_peak = [], -min_distance
    for i in range(1, len(signal) - 1):
        if (signal[i] > threshold and signal[i] > signal[i-1] and
                signal[i] > signal[i+1] and i - last_peak >= min_distance):
            peaks.append(i); last_peak = i
    return peaks

def calculate_hr(ir_signal, sample_rate=25):
    if len(ir_signal) < 20: return 72
    ac = bandpass_simple(ir_signal, window=6)
    peaks = detect_peaks(ac, min_distance=10, threshold_ratio=0.35)
    if len(peaks) < 2:
        peaks = detect_peaks(ac, min_distance=8, threshold_ratio=0.2)
    if len(peaks) >= 2:
        intervals = [peaks[i+1] - peaks[i] for i in range(len(peaks)-1)]
        hr = int(60 / (sum(intervals)/len(intervals) / sample_rate))
        return max(45, min(150, hr))
    return 72

def calculate_spo2(red_signal, ir_signal):
    if len(red_signal) < 10 or len(ir_signal) < 10: return 98
    def ac_dc(sig):
        mn, mx = min(sig), max(sig)
        return (mx - mn) / 2.0, sum(sig) / len(sig)
    ac_r, dc_r   = ac_dc(red_signal)
    ac_ir, dc_ir = ac_dc(ir_signal)
    if dc_r < 1 or dc_ir < 1 or ac_ir < 1: return 98
    R = (ac_r / dc_r) / (ac_ir / dc_ir)
    return max(85, min(100, int(110 - 25 * R)))

def get_mood(hr):
    if hr < 60:   return "sleepy"
    elif hr < 75: return "calm"
    elif hr < 95: return "happy"
    else:         return "excited"

def draw_pulse_wave(draw, ir_signal, x0, y0, w, h, color=MINT):
    rrect(draw, x0-2, y0-2, x0+w+2, y0+h+2, 6, fill=BG_DARK, outline=PURPLE_MID, width=1)
    if len(ir_signal) < 4:
        draw.line([(x0, y0+h//2), (x0+w, y0+h//2)], fill=PURPLE_MID, width=1); return
    ac = bandpass_simple(ir_signal, window=8)
    display = ac[-60:] if len(ac) > 60 else ac
    mn, mx = min(display), max(display)
    rng = mx - mn
    if rng < 10:
        draw.line([(x0, y0+h//2), (x0+w, y0+h//2)], fill=PURPLE_MID, width=1)
        draw.text((x0+w//2, y0+h//2-8), "Keep still...",
                  fill=PURPLE_LIGHT, font=font(9), anchor="mm"); return
    n = len(display)
    pts = []
    for i, v in enumerate(display):
        px = x0 + int(i * w / max(1, n-1))
        py = max(y0+1, min(y0+h-1, y0 + h - int((v-mn)/rng*(h-4)) - 2))
        pts.append((px, py))
    gc = tuple(min(255, c+40) for c in color[:3])
    for i in range(len(pts)-1):
        draw.line([pts[i], pts[i+1]], fill=(gc[0]//3, gc[1]//3, gc[2]//3), width=4)
    for i in range(len(pts)-1):
        draw.line([pts[i], pts[i+1]], fill=color, width=2)
    for p in detect_peaks(display, min_distance=8, threshold_ratio=0.35):
        if 0 < p < n:
            draw.ellipse([pts[p][0]-3, pts[p][1]-3, pts[p][0]+3, pts[p][1]+3], fill=PINK_HOT)

def draw_face(draw, mood, cx, cy, blink=False, frame=0):
    """Soft mochi-bot expression — lively, no surrounding box."""
    accent = MOCHI_MOOD.get(mood, PURPLE_LIGHT)
    bob = int(2.5 * math.sin(frame * 0.13))
    cy += bob
    r = 44

    draw.ellipse([cx - r, cy - r + 3, cx + r, cy + r + 5], fill=MOCHI_SHADOW)
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=MOCHI_CREAM, outline=MOCHI_INK, width=2)

    sway = int(2.5 * math.sin(frame * 0.15))
    ax = cx + sway
    draw.line([(cx, cy - r + 6), (ax, cy - r - 14)], fill=MOCHI_INK, width=2)
    pr = 5 + (2 if mood == "excited" and int(frame * 0.2) % 2 else 0)
    draw.ellipse([ax - pr, cy - r - 24, ax + pr, cy - r - 10], fill=accent)
    draw.ellipse([ax - 2, cy - r - 20, ax + 2, cy - r - 14], fill=WHITE)

    blush_pulse = int(2 * math.sin(frame * 0.18)) if mood in ("happy", "excited") else 0
    for bx in (cx - 26, cx + 26):
        draw.ellipse([bx - 8 - blush_pulse, cy + 4, bx + 8 + blush_pulse, cy + 15 + blush_pulse],
                     fill=MOCHI_BLUSH)

    lx, rx = cx - 14, cx + 14
    ey, my = cy - 4, cy + 18

    if blink:
        _mochi_eye_blink(draw, lx, rx, ey)
    elif mood == "excited":
        for ex in (lx, rx):
            wr = 7 + int(math.sin(frame * 0.25))
            draw.ellipse([ex - wr, ey - wr, ex + wr, ey + wr], fill=MOCHI_INK)
            draw.ellipse([ex + 2, ey - 4, ex + 5, ey - 1], fill=WHITE)
    elif mood == "happy":
        for ex in (lx, rx):
            draw.arc([ex - 9, ey - 2, ex + 9, ey + 12], start=200, end=340, fill=MOCHI_INK, width=3)
            draw.ellipse([ex + 2, ey + 1, ex + 5, ey + 4], fill=WHITE)
    elif mood == "sleepy":
        for ex in (lx, rx):
            draw.ellipse([ex - 6, ey - 2, ex + 6, ey + 8], fill=MOCHI_INK)
            draw.rectangle([ex - 7, ey - 8, ex + 7, ey - 1], fill=MOCHI_CREAM)
            draw.line([(ex - 7, ey - 1), (ex + 7, ey - 1)], fill=MOCHI_INK, width=2)
    else:
        _mochi_eye_dots(draw, lx, rx, ey, r=4)

    if mood == "excited":
        wob = int(2 * math.sin(frame * 0.3))
        draw.ellipse([cx - 10, my - 6 + wob, cx + 10, my + 8 + wob], fill=MOCHI_INK)
        draw.ellipse([cx - 6, my - 2 + wob, cx + 6, my + 4 + wob], fill=(255, 120, 140))
        if frame % 18 < 9:
            draw.ellipse([cx + 32, cy - 28, cx + 38, cy - 20], fill=(120, 190, 255))
    elif mood == "happy":
        smile = 14 + int(2 * math.sin(frame * 0.12))
        draw.arc([cx - smile, my - 8, cx + smile, my + 10], start=0, end=180, fill=MOCHI_INK, width=3)
    elif mood == "sleepy":
        draw.ellipse([cx - 8, my - 2, cx + 8, my + 10], fill=MOCHI_INK)
        draw.ellipse([cx - 5, my + 1, cx + 5, my + 7], fill=(255, 210, 220))
        _mochi_zzz(draw, cx, cy, frame)
    else:
        draw.arc([cx - 11, my - 4, cx + 11, my + 6], start=0, end=180, fill=MOCHI_INK, width=2)
        if frame % 40 < 20:
            draw.ellipse([cx - 1, my + 1, cx + 1, my + 3], fill=accent)


# ══════════════════════════════════════════════════════════════
# MAIN MENU
# ══════════════════════════════════════════════════════════════
MENU_ITEMS = [
    ("Heart Rate",    PINK_HOT,     "♥"),
    ("Flappy Bird",   GOLD,         "✦"),
    ("Pong vs CPU",   MINT,         "■"),
    ("Reaction Test", PURPLE_LIGHT, "★"),
    ("Dino Run",      TEAL,         "▲"),
    ("Settings",      OFF_WHITE,    "⚙"),
]
MENU_HINTS = (
    "★ main · vitals log", "high score", "vs CPU", "5 rounds",
    "jump obstacles", "speed & scores",
)
MENU_VISIBLE = 5
MENU_ROW_H   = 38
MENU_TOP     = 68

def _menu_scroll(selected):
    n = len(MENU_ITEMS)
    if n <= MENU_VISIBLE:
        return 0
    return max(0, min(selected - MENU_VISIBLE + 1, n - MENU_VISIBLE))

_MENU_CACHE = None

def invalidate_menu_cache():
    global _MENU_CACHE
    _MENU_CACHE = None

def _render_main_menu(selected):
    img = _BG_CACHE["menu"].copy()
    draw = ImageDraw.Draw(img)
    scroll = _menu_scroll(selected)
    rrect(draw, 0, 0, WIDTH, 56, 0, fill=(38, 22, 68))
    draw.line([(0, 56), (WIDTH, 56)], fill=PINK_HOT, width=2)
    draw.text((WIDTH // 2, 17), "ZEN DESK", fill=PINK_HOT, font=font(19), anchor="mm")
    streak = vitals_streak()
    last = format_last_session()
    if last:
        draw.text((WIDTH // 2, 38), last, fill=PURPLE_LIGHT, font=font(8), anchor="mm")
    elif streak:
        draw.text((WIDTH // 2, 38), f"streak {streak} day{'s' if streak != 1 else ''}",
                  fill=MINT, font=font(9), anchor="mm")
    else:
        draw.text((WIDTH // 2, 38), "place finger for vitals", fill=PURPLE_LIGHT, font=font(9), anchor="mm")
    for vi, idx in enumerate(range(scroll, min(scroll + MENU_VISIBLE, len(MENU_ITEMS)))):
        label, color, icon = MENU_ITEMS[idx]
        y = MENU_TOP + vi * MENU_ROW_H
        is_sel = idx == selected
        bg = (58, 36, 98) if is_sel else (26, 16, 48)
        rrect(draw, 10, y, 230, y + 32, 10, fill=bg,
              outline=color if is_sel else (58, 42, 95), width=2 if is_sel else 1)
        if is_sel:
            draw.rectangle([10, y + 5, 14, y + 27], fill=color)
        draw.ellipse([18, y + 6, 40, y + 26], fill=color if is_sel else PURPLE_DEEP)
        draw.text((29, y + 16), icon, fill=BG_DARK if is_sel else color, font=font(11), anchor="mm")
        draw.text((48, y + 11), label, fill=WHITE if is_sel else OFF_WHITE, font=font(12), anchor="lm")
        draw.text((48, y + 23), MENU_HINTS[idx], fill=color if is_sel else PURPLE_MID,
                  font=font(7), anchor="lm")
    if len(MENU_ITEMS) > MENU_VISIBLE:
        bar_h = 38 * MENU_VISIBLE
        by0 = MENU_TOP + 4
        thumb = max(12, bar_h * MENU_VISIBLE // len(MENU_ITEMS))
        tpos = by0 + (bar_h - thumb) * scroll // max(1, len(MENU_ITEMS) - MENU_VISIBLE)
        draw.rectangle([234, by0, 238, by0 + bar_h], fill=PURPLE_DEEP)
        draw.rectangle([234, tpos, 238, tpos + thumb], fill=PINK_HOT)
    rrect(draw, 0, 302, WIDTH, HEIGHT, 0, fill=PURPLE_DEEP)
    draw.text((WIDTH // 2, 313), "B1 select   B2 next", fill=PURPLE_LIGHT, font=font(8), anchor="mm")
    return img

def build_menu_cache():
    global _MENU_CACHE
    _MENU_CACHE = [_render_main_menu(i) for i in range(len(MENU_ITEMS))]

def screen_main_menu(selected, frame):
    if _MENU_CACHE is None:
        build_menu_cache()
    return _MENU_CACHE[selected]

# ══════════════════════════════════════════════════════════════
# ❶  HEART RATE MONITOR
# ══════════════════════════════════════════════════════════════
def screen_hr_place_finger(frame=0):
    img, draw = frame_bg("menu")
    rrect(draw, 0, 0, WIDTH, 52, 0, fill=PURPLE_DEEP)
    draw.line([(0,52),(WIDTH,52)], fill=PINK_HOT, width=2)
    draw.text((WIDTH//2, 15), "ZEN DESK",         fill=PINK_HOT,     font=font(18), anchor="mm")
    draw.text((WIDTH//2, 36), "heart rate monitor", fill=PURPLE_LIGHT, font=font(10), anchor="mm")
    cx, cy = 120, 175
    pulse = int(10 * abs(math.sin(frame * 0.12)))
    for r, c in [(72 + pulse, PURPLE_DEEP), (62, PURPLE_MID), (52, PURPLE_LIGHT)]:
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=c, width=1)
    draw_face(draw, "calm", cx, cy, frame=frame)
    draw.text((WIDTH//2, 82),  "Place your finger", fill=OFF_WHITE,    font=font(14), anchor="mm")
    draw.text((WIDTH//2, 100), "on the sensor",     fill=PURPLE_LIGHT, font=font(12), anchor="mm")
    draw.line([(40,115),(200,115)], fill=PURPLE_MID, width=1)
    draw.text((WIDTH//2, 252), "keep still for best results", fill=PURPLE_LIGHT, font=font(10), anchor="mm")
    draw.text((WIDTH//2, 272), f"IR sense > {ir_threshold()}", fill=PURPLE_MID, font=font(9), anchor="mm")
    draw.text((WIDTH//2, 288), "adjust in Settings if needed", fill=PURPLE_DEEP, font=font(8), anchor="mm")
    rrect(draw, 0, 294, WIDTH, HEIGHT, 0, fill=PURPLE_DEEP)
    draw.text((WIDTH//2, 308), "B2 = back to menu",
              fill=PURPLE_LIGHT, font=font(9), anchor="mm")
    return img

def screen_hr_measuring(sec_left, reds=None, irs=None, frame=0, sample_n=0, sample_goal=250):
    img, draw = frame_bg("menu")
    rrect(draw, 0, 0, WIDTH, 52, 0, fill=PURPLE_DEEP)
    draw.line([(0,52),(WIDTH,52)], fill=MINT, width=2)
    draw.text((WIDTH//2, 15), "MEASURING",           fill=MINT,         font=font(18), anchor="mm")
    draw.text((WIDTH//2, 36), "hold still · B2 skip", fill=PURPLE_LIGHT, font=font(9), anchor="mm")
    draw_face(draw, "calm", 120, 118, frame=frame)
    cx, cy = 120, 168
    draw.text((cx, cy),    str(sec_left), fill=MINT,         font=font(36), anchor="mm")
    draw.text((cx, cy+28), "sec left",    fill=PURPLE_LIGHT, font=font(10), anchor="mm")
    progress = max(0, min(200, int((10 - sec_left) / 10 * 200)))
    rrect(draw, 20, 198, 220, 212, 6, fill=BG_MID, outline=PURPLE_MID, width=1)
    if progress > 4:
        rrect(draw, 20, 198, 20 + progress, 212, 6, fill=MINT)
    sp = max(0, min(200, int(sample_n / max(1, sample_goal) * 200)))
    draw.text((20, 186), "signal strength", fill=PURPLE_LIGHT, font=font(8))
    rrect(draw, 20, 208, 220, 220, 4, fill=BG_MID, outline=PURPLE_MID, width=1)
    if sp > 4:
        rrect(draw, 20, 208, 20 + sp, 220, 4, fill=PINK_HOT)
    draw.text((20, 224), "live pulse signal", fill=PURPLE_LIGHT, font=font(9))
    if irs and len(irs) >= 4:
        draw_pulse_wave(draw, irs, 10, 220, 220, 60, color=MINT)
    else:
        rrect(draw, 8, 218, 232, 282, 6, fill=BG_DARK, outline=PURPLE_MID, width=1)
        draw.text((WIDTH//2, 250), "collecting...", fill=PURPLE_MID, font=font(10), anchor="mm")
    draw.text((WIDTH//2, 292), "breathe normally", fill=PURPLE_MID, font=font(9), anchor="mm")
    rrect(draw, 0, 303, WIDTH, HEIGHT, 0, fill=PURPLE_DEEP)
    draw.text((WIDTH//2, 312), "analysing your vitals...", fill=PURPLE_LIGHT, font=font(9), anchor="mm")
    return img

def screen_hr_vitals(hr, spo2, ir_vals, blink=False):
    img, draw = frame_bg("menu")
    rrect(draw, 0, 0, WIDTH, 52, 0, fill=PURPLE_DEEP)
    draw.line([(0,52),(WIDTH,52)], fill=PINK_HOT, width=2)
    draw.text((WIDTH//2, 15), "YOUR VITALS",    fill=PINK_HOT,     font=font(18), anchor="mm")
    draw.text((WIDTH//2, 36), "health snapshot", fill=PURPLE_LIGHT, font=font(10), anchor="mm")
    streak = vitals_streak()
    if streak:
        draw.text((WIDTH - 12, 48), f"{streak}d streak", fill=GOLD, font=font(8), anchor="rm")
    hr_color  = MINT if hr < 80 else GOLD if hr < 100 else PINK_HOT
    rrect(draw, 8, 58, 118, 158, 12, fill=BG_MID, outline=hr_color, width=2)
    hx, hy = 63, 82
    bsz = 10 + (2 if blink else 0)
    draw.ellipse([hx-bsz,hy-bsz//2,hx,hy+bsz//2],   fill=PINK_HOT)
    draw.ellipse([hx,hy-bsz//2,hx+bsz,hy+bsz//2],    fill=PINK_HOT)
    draw.polygon([(hx-bsz,hy),(hx+bsz//2,hy+bsz+4),(hx+bsz,hy)], fill=PINK_HOT)
    draw.text((63, 108), str(hr), fill=hr_color,     font=font(30), anchor="mm")
    draw.text((63, 132), "BPM",   fill=PURPLE_LIGHT, font=font(11), anchor="mm")
    hr_lbl = "normal" if 60<=hr<=100 else ("low" if hr<60 else "high")
    lc = MINT if hr_lbl=="normal" else (GOLD if hr_lbl=="low" else PINK_HOT)
    rrect(draw, 28, 140, 98, 154, 5, fill=lc)
    draw.text((63, 147), hr_lbl, fill=BG_DARK, font=font(9), anchor="mm")
    spo2_color = MINT if spo2>=95 else GOLD if spo2>=90 else PINK_HOT
    rrect(draw, 122, 58, 232, 158, 12, fill=BG_MID, outline=spo2_color, width=2)
    draw.text((177, 80), "SpO2", fill=PURPLE_LIGHT, font=font(11), anchor="mm")
    pct     = (spo2 - 85) / 15
    arc_end = int(180 + pct * 180)
    draw.arc([147,88,207,148], start=180, end=360,    fill=BG_DARK,    width=8)
    draw.arc([147,88,207,148], start=180, end=arc_end, fill=spo2_color, width=8)
    draw.text((177, 120), f"{spo2}%", fill=spo2_color,  font=font(20), anchor="mm")
    draw.text((177, 140), "oxygen",   fill=PURPLE_LIGHT, font=font(10), anchor="mm")
    s_lbl = "normal" if spo2>=95 else ("fair" if spo2>=90 else "low")
    sc = MINT if s_lbl=="normal" else (GOLD if s_lbl=="fair" else PINK_HOT)
    rrect(draw, 142, 143, 212, 156, 5, fill=sc)
    draw.text((177, 150), s_lbl, fill=BG_DARK, font=font(9), anchor="mm")
    draw.text((14, 164), "pulse waveform (IR signal)", fill=PURPLE_LIGHT, font=font(9))
    draw_pulse_wave(draw, ir_vals, 8, 176, 224, 72, color=MINT)
    draw.text((14, 257), "SpO2 level", fill=PURPLE_LIGHT, font=font(9))
    bar_x, bar_y, bar_w, bar_h = 8, 268, 224, 16
    for xi in range(bar_w):
        t = xi / bar_w
        r = int(200*(1-t)+40*t); g = int(80*(1-t)+200*t); b = int(200*(1-t)+120*t)
        draw.line([(bar_x+xi,bar_y),(bar_x+xi,bar_y+bar_h)], fill=(r,g,b))
    rrect(draw, bar_x, bar_y, bar_x+bar_w, bar_y+bar_h, 6, outline=PURPLE_MID, width=1)
    needle_x = bar_x + int((spo2-85)/15 * bar_w)
    draw.polygon([(needle_x,bar_y-2),(needle_x-4,bar_y-9),(needle_x+4,bar_y-9)], fill=WHITE)
    draw.text((needle_x, bar_y-12), f"{spo2}%", fill=WHITE, font=font(8), anchor="mm")
    for pv in [85,90,95,100]:
        tx = bar_x + int((pv-85)/15 * bar_w)
        draw.line([(tx,bar_y+bar_h),(tx,bar_y+bar_h+4)], fill=PURPLE_LIGHT, width=1)
        draw.text((tx, bar_y+bar_h+10), str(pv), fill=PURPLE_MID, font=font(7), anchor="mm")
    rrect(draw, 0, 303, WIDTH, HEIGHT, 0, fill=PURPLE_DEEP)
    draw.text((WIDTH//2, 312), "next: mood analysis →", fill=PURPLE_LIGHT, font=font(9), anchor="mm")
    return img

def screen_hr_mood(hr, spo2, mood, blink=False, frame=0):
    img, draw = frame_bg("menu")
    accent = {"calm":PURPLE_LIGHT,"happy":MINT,"excited":PINK_HOT,
              "sleepy":(120,100,200)}.get(mood, PURPLE_MID)
    rrect(draw, 0, 0, WIDTH, 52, 0, fill=PURPLE_DEEP)
    draw.line([(0,52),(WIDTH,52)], fill=accent, width=2)
    draw.text((WIDTH//2, 15), "MOOD",                   fill=accent,       font=font(18), anchor="mm")
    draw.text((WIDTH//2, 36), f"based on HR: {hr} bpm", fill=PURPLE_LIGHT, font=font(10), anchor="mm")
    rrect(draw, 8, 58, 232, 215, 14, fill=BG_MID, outline=accent, width=2)
    draw_face(draw, mood, 120, 135, blink=blink, frame=frame)
    labels = {"calm":"Calm  ✦","happy":"Happy  ♡","excited":"Excited  ★","sleepy":"Sleepy  zzz"}
    draw.text((WIDTH//2, 220), labels.get(mood, mood.capitalize()),
              fill=accent, font=font(16), anchor="mm")
    descs = {"calm":"relaxed and focused","happy":"great energy levels!",
             "excited":"high stress — breathe","sleepy":"low energy, rest up"}
    draw.text((WIDTH//2, 238), descs.get(mood,""), fill=PURPLE_LIGHT, font=font(10), anchor="mm")
    draw.text((14, 252), "stress index", fill=PURPLE_LIGHT, font=font(9))
    stress = min(100, max(0, int((hr-55)/50*100)))
    rrect(draw, 8, 262, 232, 278, 6, fill=BG_DARK, outline=PURPLE_MID, width=1)
    if stress > 0:
        sc = MINT if stress<40 else (GOLD if stress<70 else PINK_HOT)
        rrect(draw, 8, 262, 8+int(stress/100*222), 278, 6, fill=sc)
    draw.text((235, 270), f"{stress}%", fill=PURPLE_LIGHT, font=font(8), anchor="lm")
    rrect(draw, 8, 284, 115, 300, 6, fill=PURPLE_DEEP, outline=PINK_HOT, width=1)
    draw.text((61, 292), f"♥ {hr} bpm", fill=PINK_HOT, font=font(9), anchor="mm")
    rrect(draw, 120, 284, 232, 300, 6, fill=PURPLE_DEEP, outline=MINT, width=1)
    draw.text((176, 292), f"O₂ {spo2}%", fill=MINT, font=font(9), anchor="mm")
    rrect(draw, 0, 303, WIDTH, HEIGHT, 0, fill=PURPLE_DEEP)
    nxt = "next: calm breathing →" if mood == "excited" else "next: zen tips →"
    draw.text((WIDTH//2, 312), nxt, fill=PINK_HOT if mood == "excited" else PURPLE_LIGHT,
              font=font(9), anchor="mm")
    return img

# ── Breathing (used when HR mood is excited) ─────────────────
BREATH_PATTERNS = {
    "box": [("Inhale", 4), ("Hold", 4), ("Exhale", 4), ("Hold", 4)],
    "478": [("Inhale", 4), ("Hold", 7), ("Exhale", 8)],
}

def screen_hr_breath_invite(hr):
    img, draw = frame_bg("menu")
    rrect(draw, 0, 0, WIDTH, 52, 0, fill=PURPLE_DEEP)
    draw.line([(0, 52), (WIDTH, 52)], fill=PINK_HOT, width=2)
    draw.text((WIDTH // 2, 16), "STRESS RELIEF", fill=PINK_HOT, font=font(17), anchor="mm")
    draw.text((WIDTH // 2, 36), f"HR {hr} bpm — excited", fill=PURPLE_LIGHT, font=font(10), anchor="mm")
    draw_face(draw, "excited", 120, 130, frame=0)
    draw.text((WIDTH // 2, 200), "Let's calm down with", fill=OFF_WHITE, font=font(12), anchor="mm")
    draw.text((WIDTH // 2, 220), "guided breathing", fill=MINT, font=font(14), anchor="mm")
    pat = DATA["settings"].get("breathing", "box")
    draw.text((WIDTH // 2, 248), f"{pat} pattern · 3 cycles", fill=PURPLE_LIGHT, font=font(10), anchor="mm")
    rrect(draw, 0, 288, WIDTH, HEIGHT, 0, fill=PURPLE_DEEP)
    draw.text((WIDTH // 2, 302), "B1 = start breathing", fill=MINT, font=font(10), anchor="mm")
    draw.text((WIDTH // 2, 316), "B2 = skip to menu", fill=PURPLE_MID, font=font(9), anchor="mm")
    return img

def screen_breath_phase_hr(label, sec_left, phase_t, radius, cycle, total_cycles):
    img, draw = frame_bg("menu")
    draw.text((WIDTH // 2, 20), "CALM BREATHING", fill=PINK_HOT, font=font(14), anchor="mm")
    draw.text((WIDTH // 2, 38), f"Cycle {cycle}/{total_cycles}", fill=PURPLE_LIGHT, font=font(9), anchor="mm")
    cx, cy = WIDTH // 2, 150
    draw.ellipse([cx - 72, cy - 72, cx + 72, cy + 72], outline=PURPLE_DEEP, width=2)
    r = int(radius)
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=MINT, outline=PINK_SOFT, width=2)
    draw.text((cx, cy), str(sec_left), fill=BG_DARK, font=font(34), anchor="mm")
    draw.text((WIDTH // 2, 242), label.upper(), fill=OFF_WHITE, font=font(18), anchor="mm")
    bw = int(phase_t * 200)
    rrect(draw, 20, 268, 220, 280, 4, fill=BG_MID, outline=PURPLE_MID, width=1)
    if bw > 0:
        rrect(draw, 20, 268, 20 + bw, 280, 4, fill=MINT)
    draw.text((WIDTH // 2, 302), "B2 = stop early", fill=PURPLE_MID, font=font(9), anchor="mm")
    return img

def screen_breath_done_hr():
    img, draw = frame_bg("menu")
    draw.text((WIDTH // 2, 110), "Nice work", fill=MINT, font=font(26), anchor="mm")
    draw.text((WIDTH // 2, 148), "You should feel calmer", fill=PURPLE_LIGHT, font=font(11), anchor="mm")
    draw_face(draw, "calm", 120, 210, frame=0)
    draw.text((WIDTH // 2, 300), "B2 = back to menu", fill=PURPLE_LIGHT, font=font(10), anchor="mm")
    return img

def screen_hr_no_sensor():
    img, draw = frame_bg("menu")
    rrect(draw, 0, 0, WIDTH, 52, 0, fill=PURPLE_DEEP)
    draw.text((WIDTH // 2, 18), "SENSOR", fill=PINK_HOT, font=font(18), anchor="mm")
    draw.text((WIDTH // 2, 100), "MAX30102 not found", fill=OFF_WHITE, font=font(13), anchor="mm")
    draw.text((WIDTH // 2, 130), "Check I2C wiring", fill=PURPLE_LIGHT, font=font(11), anchor="mm")
    draw.text((WIDTH // 2, 155), "Games still work!", fill=MINT, font=font(11), anchor="mm")
    draw.text((WIDTH // 2, 300), "B2 = menu", fill=PURPLE_LIGHT, font=font(10), anchor="mm")
    return img

def screen_hr_finger_lost():
    img, draw = frame_bg("menu")
    draw.text((WIDTH // 2, 100), "Finger lost", fill=PINK_HOT, font=font(22), anchor="mm")
    draw.text((WIDTH // 2, 140), "Cover the sensor again", fill=PURPLE_LIGHT, font=font(12), anchor="mm")
    draw.text((WIDTH // 2, 170), "Press B1 to retry", fill=MINT, font=font(11), anchor="mm")
    draw.text((WIDTH // 2, 300), "B2 = menu", fill=PURPLE_MID, font=font(10), anchor="mm")
    return img

def screen_vitals_history():
    img, draw = frame_bg("menu")
    draw.text((WIDTH // 2, 18), "VITALS LOG", fill=PINK_HOT, font=font(16), anchor="mm")
    draw.text((WIDTH // 2, 34), f"streak {vitals_streak()} days", fill=MINT, font=font(9), anchor="mm")
    log = list(reversed(DATA.get("vitals_log", [])[-7:]))
    if not log:
        draw.text((WIDTH // 2, 140), "No readings yet", fill=PURPLE_LIGHT, font=font(12), anchor="mm")
    else:
        for i, e in enumerate(log):
            y = 58 + i * 36
            draw.text((16, y), e["date"][5:], fill=PURPLE_MID, font=font(9), anchor="lm")
            draw.text((52, y), f"{e['hr']} bpm", fill=PINK_HOT, font=font(11), anchor="lm")
            draw.text((120, y), f"O₂ {e['spo2']}%", fill=MINT, font=font(10), anchor="lm")
            draw.text((200, y), e["mood"], fill=GOLD, font=font(9), anchor="lm")
    draw.text((WIDTH // 2, 302), "B2 = back", fill=PURPLE_LIGHT, font=font(9), anchor="mm")
    return img

def screen_sleepy_rest_invite(hr):
    img, draw = frame_bg("menu")
    draw.text((WIDTH // 2, 16), "REST MODE", fill=PURPLE_LIGHT, font=font(17), anchor="mm")
    draw_face(draw, "sleepy", 120, 120, frame=0)
    draw.text((WIDTH // 2, 200), f"Low HR ({hr} bpm)", fill=PURPLE_LIGHT, font=font(11), anchor="mm")
    draw.text((WIDTH // 2, 222), "2 min eye rest?", fill=OFF_WHITE, font=font(12), anchor="mm")
    draw.text((WIDTH // 2, 290), "B1 = start   B2 = skip", fill=MINT, font=font(9), anchor="mm")
    return img

def screen_rest_timer(sec_left):
    img, draw = frame_bg("menu")
    draw.text((WIDTH // 2, 24), "REST YOUR EYES", fill=PURPLE_LIGHT, font=font(14), anchor="mm")
    cx, cy = WIDTH // 2, 150
    draw.ellipse([cx - 55, cy - 55, cx + 55, cy + 55], outline=PURPLE_MID, width=2)
    draw.text((cx, cy), f"{sec_left // 60}:{sec_left % 60:02d}", fill=MINT, font=font(32), anchor="mm")
    draw.text((WIDTH // 2, 250), "B2 = stop early", fill=PURPLE_MID, font=font(9), anchor="mm")
    return img

def run_rest_session(seconds=120):
    t_end = time.time() + seconds
    while time.time() < t_end:
        if b2():
            debounce()
            wait_release()
            return
        left = int(t_end - time.time())
        show_image(screen_rest_timer(left))
        time.sleep(0.25)

def screen_streak_celebration(streak):
    img, draw = frame_bg("menu")
    draw.text((WIDTH // 2, 90), "STREAK!", fill=GOLD, font=font(28), anchor="mm")
    draw.text((WIDTH // 2, 135), f"{streak} days in a row", fill=MINT, font=font(18), anchor="mm")
    draw.text((WIDTH // 2, 175), "Keep checking vitals", fill=PURPLE_LIGHT, font=font(11), anchor="mm")
    draw.text((WIDTH // 2, 280), "B1 = continue", fill=OFF_WHITE, font=font(10), anchor="mm")
    return img

def screen_screensaver(frame):
    img, draw = frame_bg("menu")
    br = DATA.get("settings", {}).get("brightness", 100)
    if br > 40:
        draw.rectangle([0, 0, WIDTH, HEIGHT], fill=(8, 5, 18))
    t = time.strftime("%H:%M")
    draw.text((WIDTH // 2, 120), t, fill=PURPLE_LIGHT, font=font(36), anchor="mm")
    draw.text((WIDTH // 2, 165), "ZEN DESK", fill=PINK_HOT, font=font(14), anchor="mm")
    s = vitals_streak()
    if s:
        draw.text((WIDTH // 2, 190), f"{s}-day wellness streak", fill=MINT, font=font(10), anchor="mm")
    pulse = int(4 * math.sin(frame * 0.1))
    draw.text((WIDTH // 2, 270 + pulse), "press any button", fill=PURPLE_MID, font=font(10), anchor="mm")
    return img

def run_breathing_session():
    """Guided breathing (no menu). Returns when finished or user presses B2."""
    pat_key = DATA["settings"].get("breathing", "box")
    phases = BREATH_PATTERNS.get(pat_key, BREATH_PATTERNS["box"])
    total_cycles = 3
    for cycle in range(1, total_cycles + 1):
        for label, duration in phases:
            inhale = "inhale" in label.lower()
            for left in range(duration, 0, -1):
                if b2():
                    debounce()
                    wait_release()
                    return
                elapsed = duration - left
                phase_t = elapsed / max(1, duration)
                radius = (18 + phase_t * 42) if inhale else (60 - phase_t * 42)
                show_image(screen_breath_phase_hr(label, left, phase_t, radius, cycle, total_cycles))
                time.sleep(1.0)
    while True:
        show_image(screen_breath_done_hr())
        if b2():
            debounce()
            wait_release()
            return
        time.sleep(0.05)

def screen_hr_tip(mood, hr):
    img, draw = frame_bg("menu")
    rrect(draw, 0, 0, WIDTH, 52, 0, fill=PURPLE_DEEP)
    draw.line([(0,52),(WIDTH,52)], fill=GOLD, width=2)
    draw.text((WIDTH//2, 15), "ZEN TIP",                fill=GOLD,         font=font(18), anchor="mm")
    draw.text((WIDTH//2, 36), "personalised just for you", fill=PURPLE_LIGHT, font=font(10), anchor="mm")
    tips = {
        "calm":    ["You are in harmony ✦","Stay hydrated.","Maintain this calm",
                    "with 5 min of mindful","breathing."],
        "happy":   ["Great energy! ♡","Channel it creatively.","Take a short walk",
                    "or stretch to boost","your mood further."],
        "excited": ["Stress detected ★","You completed","guided breathing.","Rest a moment."],
        "sleepy":  ["Low energy detected zzz","Consider a short rest.","Splash cold water",
                    "on your face, or do","a light stretch."],
    }
    rrect(draw, 10, 58, 230, 230, 12, fill=BG_MID, outline=GOLD, width=1)
    for i, line in enumerate(tips.get(mood, tips["calm"])):
        fc = OFF_WHITE if i == 0 else PURPLE_LIGHT
        draw.text((WIDTH//2, 78+i*22), line,
                  fill=fc, font=font(11 if i==0 else 10), anchor="mm")
    draw.text((WIDTH//2, 242), "breathing guide", fill=PURPLE_LIGHT, font=font(9), anchor="mm")
    phases = [("inhale",4,MINT),("hold",4,PURPLE_LIGHT),("exhale",6,PINK_SOFT),("hold",2,PURPLE_MID)]
    px_ = 8
    total_dur = sum(d for _,d,_ in phases)
    for label, dur, color in phases:
        bw = int(dur / total_dur * 222)
        rrect(draw, px_, 254, px_+bw-2, 274, 5, fill=color)
        draw.text((px_+bw//2-1, 264), f"{dur}s", fill=BG_DARK,     font=font(8), anchor="mm")
        draw.text((px_+bw//2-1, 280), label,      fill=PURPLE_LIGHT, font=font(7), anchor="mm")
        px_ += bw
    rrect(draw, 20, 290, 110, 305, 6, fill=PURPLE_DEEP, outline=PINK_HOT, width=1)
    draw.text((65, 298), f"HR: {hr} bpm", fill=PINK_HOT, font=font(9), anchor="mm")
    rrect(draw, 120, 290, 220, 305, 6, fill=PURPLE_DEEP, outline=MINT, width=1)
    draw.text((170, 298), mood.upper(), fill=MINT, font=font(9), anchor="mm")
    rrect(draw, 0, 308, WIDTH, HEIGHT, 0, fill=PURPLE_DEEP)
    draw.text((WIDTH//2, 315), "B2 = back to menu", fill=PURPLE_LIGHT, font=font(8), anchor="mm")
    return img

def run_heart_rate():
    """Heart rate monitor — main ZenDesk feature."""
    if not SENSOR_OK:
        while True:
            show_image(screen_hr_no_sensor())
            if b2():
                debounce()
                wait_release()
                return
            time.sleep(0.05)
        return

    frame = 0
    thresh = ir_threshold()
    lost_th = ir_lost_threshold()
    sample_goal = 250

    while True:
        print("Waiting for finger...")
        while True:
            show_image(screen_hr_place_finger(frame))
            frame = (frame + 1) % 200
            if b2():
                debounce()
                wait_release()
                return
            s = read_sample_blocking(timeout=0.3)
            if s is not None and s[1] > thresh:
                print(f"Finger detected IR={s[1]}")
                break

        flush_fifo()
        t0 = time.time()
        while time.time() - t0 < 1.5:
            if b2():
                debounce()
                wait_release()
                return
            show_image(screen_hr_measuring(10, None, None, frame, 0, sample_goal))
            frame += 1
            time.sleep(0.12)
        flush_fifo()

        all_reds, all_irs = [], []
        finger_lost = False
        for sec in range(10, 0, -1):
            for sub in range(25):
                if b2():
                    debounce()
                    wait_release()
                    return
                t0 = time.time()
                s = read_sample_blocking(timeout=0.08)
                if s is None:
                    if sub % 4 == 0:
                        show_image(screen_hr_measuring(sec, all_reds, all_irs, frame,
                                                       len(all_irs), sample_goal))
                        frame += 1
                    continue
                red, ir = s
                if ir < lost_th:
                    finger_lost = True
                    break
                all_reds.append(red)
                all_irs.append(ir)
                if sub % 4 == 0:
                    show_image(screen_hr_measuring(sec, all_reds, all_irs, frame,
                                                   len(all_irs), sample_goal))
                    frame += 1
                time.sleep(max(0, 0.04 - (time.time() - t0)))
            if finger_lost:
                break

        if finger_lost:
            while True:
                show_image(screen_hr_finger_lost())
                p = poll()
                if p == "b1":
                    debounce()
                    wait_release()
                    break
                if p == "b2":
                    debounce()
                    wait_release()
                    return
                time.sleep(0.03)
            continue

        if len(all_irs) < 20:
            print(f"Not enough samples ({len(all_irs)})")
            continue

        hr = calculate_hr(all_irs, sample_rate=25)
        spo2 = calculate_spo2(all_reds, all_irs)
        mood = get_mood(hr)
        print(f"HR={hr} SpO2={spo2} mood={mood}")
        record_vitals_reading(hr, spo2, mood)

        t = time.time()
        blink = False
        while time.time() - t < 12:
            if b2():
                debounce()
                wait_release()
                break
            show_image(screen_hr_vitals(hr, spo2, all_irs, blink))
            blink = not blink
            time.sleep(0.55)

        t = time.time()
        blink = False
        frame = 0
        while time.time() - t < 8:
            if b2():
                debounce()
                wait_release()
                break
            show_image(screen_hr_mood(hr, spo2, mood, blink, frame))
            blink = not blink
            frame += 1
            time.sleep(0.45)

        if mood == "excited":
            while True:
                show_image(screen_hr_breath_invite(hr))
                p = poll()
                if p == "b1":
                    debounce()
                    wait_release()
                    run_breathing_session()
                    break
                if p == "b2":
                    debounce()
                    wait_release()
                    return
                time.sleep(0.02)
            return

        if mood == "sleepy":
            while True:
                show_image(screen_sleepy_rest_invite(hr))
                p = poll()
                if p == "b1":
                    debounce()
                    wait_release()
                    run_rest_session(120)
                    break
                if p == "b2":
                    debounce()
                    wait_release()
                    break
                time.sleep(0.02)

        while True:
            show_image(screen_hr_tip(mood, hr))
            if b2():
                debounce()
                wait_release()
                return
            time.sleep(0.15)

# ══════════════════════════════════════════════════════════════
# ❷  FLAPPY BIRD
# ══════════════════════════════════════════════════════════════
GRAVITY      = 0.48
FLAP_VEL     = -6.2
MAX_FALL     = 8.0
BIRD_X       = 58
BIRD_R       = 13
GROUND_H     = 36
PIPE_W       = 38
BASE_SPEED   = 3.8
BASE_GAP     = 108
MIN_GAP      = 72
SPD_PER_PIPE = 0.12
GAP_PER_PIPE = 0.75

def pipe_params(score):
    sm = speed_mult()
    return (min((BASE_SPEED + score * SPD_PER_PIPE) * sm, 8.5),
            max(BASE_GAP - score * GAP_PER_PIPE, MIN_GAP))

_FB_CANVAS = None
_FB_MENU_FRAMES = None
def _fb_canvas():
    global _FB_CANVAS
    if _FB_CANVAS is None:
        _FB_CANVAS = _BG_CACHE["sky_play"].copy()
    else:
        _FB_CANVAS.paste(_BG_CACHE["sky_play"], (0, 0))
    return _FB_CANVAS, ImageDraw.Draw(_FB_CANVAS)

def draw_bird(draw, y, vel, anim=0):
    cx, cy = BIRD_X, int(y)
    draw.ellipse([cx - 12, cy - 11, cx + 14, cy + 11], fill=BIRD_YELLOW, outline=BIRD_ORANGE)
    draw.ellipse([cx + 5, cy - 5, cx + 10, cy], fill=WHITE)
    if anim & 1:
        draw.polygon([(cx - 6, cy + 5), (cx - 16, cy + 12), (cx - 2, cy + 10)], fill=BIRD_ORANGE)
    draw.polygon([(cx + 12, cy + 1), (cx + 20, cy + 4), (cx + 12, cy + 6)], fill=(255, 130, 50))

def draw_pipe(draw, px, gap_y, gap):
    sky_bot = HEIGHT - GROUND_H
    top_end = gap_y - gap // 2
    bot_start = gap_y + gap // 2
    cap = 8
    px = int(px)
    if top_end > cap + 2:
        draw.rectangle([px, 0, px + PIPE_W, top_end - cap], fill=PIPE_GREEN)
        draw.rectangle([px - 2, top_end - cap, px + PIPE_W + 2, top_end], fill=PIPE_LITE)
    if bot_start + cap < sky_bot:
        draw.rectangle([px, bot_start + cap, px + PIPE_W, sky_bot], fill=PIPE_GREEN)
        draw.rectangle([px - 2, bot_start, px + PIPE_W + 2, bot_start + cap], fill=PIPE_LITE)

def draw_ground_fb(draw, scroll):
    y = HEIGHT - GROUND_H
    draw.rectangle([0, y, WIDTH, HEIGHT], fill=GRASS)
    draw.rectangle([0, y, WIDTH, y + 4], fill=GRASS_DARK)
    off = int(scroll) % 32
    for x in range(-32, WIDTH + 32, 32):
        draw.rectangle([x - off + 4, y + 7, x - off + 16, y + 12], fill=(62, 185, 78))

def collide_fb(bird_y, pipes, gap):
    sky_bot = HEIGHT - GROUND_H
    if bird_y+BIRD_R >= sky_bot or bird_y-BIRD_R <= 0: return True
    for px, gy, _ in pipes:
        if BIRD_X+BIRD_R > px and BIRD_X-BIRD_R < px+PIPE_W:
            if bird_y-BIRD_R < gy-gap//2 or bird_y+BIRD_R > gy+gap//2:
                return True
    return False

def score_tick(pipes):
    added = 0
    for i, (px, gy, sc) in enumerate(pipes):
        if not sc and px+PIPE_W < BIRD_X:
            pipes[i] = (px, gy, True); added += 1
    return added

def _render_fb_menu(frame):
    img = _BG_CACHE["fb_menu"].copy()
    draw = ImageDraw.Draw(img)
    draw_ground_fb(draw, frame * 3)
    draw_bird(draw, 200 + int(6 * math.sin(frame * 0.15)), -2, frame)
    rrect(draw, 24, 90, 216, 162, 12, fill=(255, 220, 70), outline=(255, 150, 40), width=2)
    draw.text((WIDTH // 2, 118), "FLAPPY", fill=(120, 70, 10), font=font(22), anchor="mm")
    draw.text((WIDTH // 2, 142), "BIRD", fill=(160, 90, 20), font=font(20), anchor="mm")
    draw.text((WIDTH // 2, 252), "B1 flap  B2 back", fill=WHITE, font=font(10), anchor="mm")
    draw.text((WIDTH // 2, 298), "Press B1", fill=MINT, font=font(12), anchor="mm")
    return img

def build_fb_menu_frames():
    global _FB_MENU_FRAMES
    _FB_MENU_FRAMES = [_render_fb_menu(i * 2) for i in range(4)]

def screen_fb_game(bird_y, vel, pipes, score, scroll, gap, anim=0):
    img, draw = _fb_canvas()
    draw_ground_fb(draw, scroll)
    for px, gy, _ in pipes:
        if -PIPE_W < px < WIDTH:
            draw_pipe(draw, px, gy, gap)
    draw_bird(draw, bird_y, vel, anim)
    draw.rectangle([WIDTH - 54, 6, WIDTH - 4, 24], fill=(28, 48, 72))
    draw.text((WIDTH - 28, 15), str(score), fill=WHITE, font=font(16), anchor="mm")
    return img

def screen_fb_dead(score, best, new_record=False, session_best=0):
    img, draw = frame_bg("dark")
    draw_bird(draw, 120, 95, 4, 0)
    draw.text((WIDTH//2, 58), "GAME", fill=PINK_HOT, font=font(26), anchor="mm")
    draw.text((WIDTH//2, 88), "OVER", fill=GOLD, font=font(26), anchor="mm")
    rrect(draw, 28, 118, 212, 228, 14, fill=(40, 28, 65), outline=GOLD if new_record else PURPLE_MID, width=2)
    draw.text((WIDTH//2,152), "Score",        fill=PURPLE_LIGHT, font=font(11), anchor="mm")
    draw.text((WIDTH//2,176), str(score),      fill=WHITE,       font=font(24), anchor="mm")
    rec = "NEW BEST!" if new_record else f"Best: {best}"
    draw.text((WIDTH//2,202), rec, fill=GOLD, font=font(11), anchor="mm")
    draw.text((WIDTH//2,218), f"This run: {session_best}", fill=PURPLE_LIGHT, font=font(9), anchor="mm")
    draw.text((WIDTH//2,252), "B1 = Play again", fill=MINT,        font=font(11), anchor="mm")
    draw.text((WIDTH//2,272), "B2 = Back",       fill=PURPLE_LIGHT, font=font(10), anchor="mm")
    return img

def run_flappy():
    if _FB_MENU_FRAMES is None:
        build_fb_menu_frames()
    fi = 0
    while True:
        show_image(_FB_MENU_FRAMES[fi % 4])
        fi += 1
        p = poll(6, 0.01)
        if p == 'b1': debounce(); break
        if p == 'b2': return
        time.sleep(0.06)

    session_best = 0
    while True:
        bird_y = float(HEIGHT // 2)
        bird_vel = 0.0
        pipes = []
        score = 0
        scroll = 0.0
        next_pipe = 0
        quit_flag = False
        anim = 0

        while True:
            t0 = time.perf_counter()
            anim += 1
            btn = poll_btn()
            if btn == 'b1':
                bird_vel = FLAP_VEL * speed_mult()
            elif btn == 'b2':
                quit_flag = True
                break

            sm = speed_mult()
            bird_vel = min(bird_vel + GRAVITY * sm, MAX_FALL * sm)
            bird_y += bird_vel
            speed, gap = pipe_params(score)
            pipes = [(px - speed, gy, sc) for px, gy, sc in pipes]
            pipes = [(px, gy, sc) for px, gy, sc in pipes if px > -PIPE_W - 10]
            next_pipe -= 1
            if next_pipe <= 0:
                next_pipe = max(48, 72 - score)
                pipes.append((float(WIDTH + PIPE_W),
                              random.randint(80, HEIGHT - GROUND_H - 80), False))
            score += score_tick(pipes)
            session_best = max(session_best, score)
            scroll += speed
            if collide_fb(bird_y, pipes, gap):
                break
            show_image(screen_fb_game(bird_y, bird_vel, pipes, score, scroll, gap, anim))
            wait_frame(t0, target=FB_FRAME_S)

        if quit_flag:
            return

        saved = record_score("flappy", score)
        best = DATA["scores"].get("flappy", score)
        show_image(screen_fb_dead(score, best, saved, session_best))
        while True:
            p = poll(5, 0.015)
            if p == 'b1': debounce(); break
            if p == 'b2': return

# ══════════════════════════════════════════════════════════════
# ❸  PONG VS CPU
# ══════════════════════════════════════════════════════════════
CT       = 30
CB       = HEIGHT - 4
CH       = CB - CT
PAD_W    = 10
PAD_H    = 50
P_X      = 8
C_X      = WIDTH - 8 - PAD_W
PAD_SPD  = 3.5
BALL_SZ  = 8
BVX0     = 4.8
BVY0     = 3.4
CPU_SPD0 = 3.0
CPU_MAX  = 6.5
CPU_STEP = 0.15
WIN_PTS  = 7

def reset_ball(direction=1):
    return (float(WIDTH//2),
            float(random.randint(CT+40, CB-40)),
            BVX0 * direction,
            random.choice([-1, 1]) * BVY0)

def cpu_move(cy, ball_y, spd):
    mid = cy + PAD_H // 2
    if   ball_y < mid - 5: cy -= spd
    elif ball_y > mid + 5: cy += spd
    return max(float(CT), min(float(CB-PAD_H), cy))

def screen_pong_menu(frame):
    img, draw = new_frame(BG_DARK)
    for y in range(0, HEIGHT, 20):
        if (y//20 + frame//6) % 2 == 0:
            draw.rectangle([WIDTH//2-1,y,WIDTH//2+1,y+12], fill=PURPLE_MID)
    draw.text((WIDTH//2, 75),  "PONG",   fill=WHITE,        font=font(32), anchor="mm")
    draw.text((WIDTH//2, 115), "vs CPU", fill=PURPLE_LIGHT, font=font(14), anchor="mm")
    rrect(draw, 25, 140, 215, 248, 10, fill=PURPLE_DEEP, outline=PURPLE_MID, width=1)
    for txt, col, y in [
        ("B1 = Paddle UP",      MINT,         165),
        ("B2 = Paddle DOWN",    PINK_HOT,     193),
        ("First to 7 wins",     PURPLE_LIGHT, 222),
    ]:
        draw.text((WIDTH//2, y), txt, fill=col, font=font(11), anchor="mm")
    draw.text((WIDTH//2, 272), "Rally → CPU gets faster!", fill=PURPLE_LIGHT, font=font(9), anchor="mm")
    draw.text((WIDTH//2, 312), "Press B1 to start",        fill=MINT,         font=font(12), anchor="mm")
    return img

def screen_pong_game(py, cy, bx, by, ps, cs, rally):
    img, draw = new_frame((14, 10, 28))
    rrect(draw, 8, 6, 72, 28, 6, fill=PURPLE_DEEP, outline=MINT, width=1)
    rrect(draw, WIDTH - 72, 6, WIDTH - 8, 28, 6, fill=PURPLE_DEEP, outline=PINK_HOT, width=1)
    draw.text((40, 17), str(ps), fill=MINT, font=font(16), anchor="mm")
    draw.text((WIDTH - 40, 17), str(cs), fill=PINK_HOT, font=font(16), anchor="mm")
    draw.text((WIDTH // 2, 17), "vs", fill=PURPLE_MID, font=font(9), anchor="mm")
    draw.line([(0,CT),(WIDTH,CT)], fill=PURPLE_MID, width=1)
    for y in range(CT, CB, 20):
        draw.rectangle([WIDTH//2-1,y,WIDTH//2+1,y+10], fill=PURPLE_DEEP)
    bc = MINT if rally<5 else GOLD if rally<10 else PINK_HOT
    draw.rounded_rectangle([P_X,int(py),P_X+PAD_W,int(py)+PAD_H], 4, fill=MINT)
    draw.rounded_rectangle([C_X,int(cy),C_X+PAD_W,int(cy)+PAD_H], 4, fill=PINK_HOT)
    draw.rectangle([int(bx),int(by),int(bx)+BALL_SZ,int(by)+BALL_SZ], fill=bc)
    if rally >= 5:
        draw.text((WIDTH//2, CT+14), f"Rally x{rally}!", fill=bc, font=font(9), anchor="mm")
    return img

def screen_pong_point(who, ps, cs):
    img, draw = frame_bg("dark")
    c = MINT if who == "player" else PINK_HOT
    draw.text((WIDTH//2, 130), "You scored!" if who=="player" else "CPU scored!",
              fill=c, font=font(16), anchor="mm")
    draw.text((WIDTH//2, 170), f"{ps}  —  {cs}", fill=WHITE, font=font(20), anchor="mm")
    show_image(img)
    time.sleep(0.55)

def screen_pong_win(winner, ps, cs):
    img, draw = frame_bg("dark")
    c = MINT if winner == "player" else PINK_HOT
    draw.text((WIDTH//2, 80),  "YOU WIN!" if winner=="player" else "CPU WINS",
              fill=c, font=font(26), anchor="mm")
    rrect(draw, 40, 130, 200, 202, 10, fill=PURPLE_DEEP, outline=PURPLE_MID, width=2)
    draw.text((WIDTH//2, 152), "Final score",      fill=PURPLE_LIGHT, font=font(10), anchor="mm")
    draw.text((WIDTH//2, 177), f"{ps}  —  {cs}",  fill=WHITE,        font=font(18), anchor="mm")
    draw.text((WIDTH//2, 232), "B1 = Again  B2 = Back", fill=PURPLE_LIGHT, font=font(10), anchor="mm")
    return img

def run_pong():
    frame = 0
    while True:
        show_image(screen_pong_menu(frame))
        frame += 1
        p = poll()
        if p == 'b1': debounce(); break
        if p == 'b2': return

    while True:
        ps = cs = rally = 0
        mid = float(CT + (CH - PAD_H) // 2)
        py = cy = mid
        bx, by, vx, vy = reset_ball(1)

        while ps < WIN_PTS and cs < WIN_PTS:
            t0 = time.perf_counter()
            sm = speed_mult()
            if b1():
                py = max(float(CT), py - PAD_SPD * sm)
            if b2():
                py = min(float(CB - PAD_H), py + PAD_SPD * sm)

            cpu_spd = min((CPU_SPD0 + rally * CPU_STEP) * sm, CPU_MAX * sm)
            cy = cpu_move(cy, by + BALL_SZ // 2, cpu_spd)
            sf = min(1.0 + rally * 0.05, 2.2) * sm
            bx += vx * sf
            by += vy * sf

            if by <= CT:
                by = float(CT)
                vy = abs(vy)
            if by + BALL_SZ >= CB:
                by = float(CB - BALL_SZ)
                vy = -abs(vy)

            if bx <= P_X + PAD_W and by + BALL_SZ >= py and by <= py + PAD_H and vx < 0:
                vx = abs(vx)
                hit = ((by + BALL_SZ // 2) - py) / PAD_H
                vy = (hit - 0.5) * 7
                rally += 1

            if bx + BALL_SZ >= C_X and by + BALL_SZ >= cy and by <= cy + PAD_H and vx > 0:
                vx = -abs(vx)
                hit = ((by + BALL_SZ // 2) - cy) / PAD_H
                vy = (hit - 0.5) * 7
                rally += 1

            if bx + BALL_SZ < 0:
                cs += 1
                screen_pong_point("cpu", ps, cs)
                bx, by, vx, vy = reset_ball(-1)
                rally = 0
                py = cy = mid
                continue

            if bx > WIDTH:
                ps += 1
                screen_pong_point("player", ps, cs)
                bx, by, vx, vy = reset_ball(1)
                rally = 0
                py = cy = mid
                continue

            show_image(screen_pong_game(py, cy, bx, by, ps, cs, rally))
            wait_frame(t0, target=1.0 / 48)

        winner = "player" if ps >= WIN_PTS else "cpu"
        if winner == "player":
            DATA.setdefault("scores", {})["pong_wins"] = DATA["scores"].get("pong_wins", 0) + 1
            save_data()
        show_image(screen_pong_win(winner, ps, cs))
        while True:
            p = poll(5, 0.015)
            if p == 'b1': debounce(); break
            if p == 'b2': return

# ══════════════════════════════════════════════════════════════
# ❹  REACTION TEST
# ══════════════════════════════════════════════════════════════
ROUNDS   = 5
MIN_WAIT = 2.0
MAX_WAIT = 5.0
GREAT_MS = 220
GOOD_MS  = 380
OK_MS    = 550

def rating(ms):
    if ms < GREAT_MS: return "GREAT!",   MINT
    if ms < GOOD_MS:  return "Good",     GOLD
    if ms < OK_MS:    return "OK",       PURPLE_LIGHT
    return "Too slow",                    RED

def screen_rt_menu(frame):
    img, draw = frame_bg("menu")
    draw.rectangle([0,0,WIDTH,50], fill=PURPLE_DEEP)
    draw.line([(0,50),(WIDTH,50)], fill=GOLD, width=2)
    draw.text((WIDTH//2, 15), "REACTION",          fill=GOLD,         font=font(18), anchor="mm")
    draw.text((WIDTH//2, 34), "TEST",              fill=PURPLE_LIGHT, font=font(10), anchor="mm")
    pulse = int(6 * abs(math.sin(frame * 0.08)))
    pts = [(120,88-pulse),(100,138),(118,138),(108,183+pulse),(140,126),(122,126)]
    draw.polygon(pts, fill=GOLD)
    draw.text((WIDTH//2,205), f"{ROUNDS} rounds",       fill=WHITE,        font=font(13), anchor="mm")
    draw.text((WIDTH//2,228), "Wait for gold flash",    fill=PURPLE_LIGHT, font=font(11), anchor="mm")
    draw.text((WIDTH//2,248), "then press B1 fast!",    fill=PURPLE_LIGHT, font=font(11), anchor="mm")
    draw.text((WIDTH//2,272), "Press early = retry",    fill=PINK_HOT,     font=font(10), anchor="mm")
    draw.text((WIDTH//2,300), "B1=Start  B2=Back",      fill=MINT,         font=font(10), anchor="mm")
    return img

def screen_rt_waiting(rnd):
    img, draw = new_frame(BG_DARK)
    draw.text((WIDTH//2, 40),  f"Round {rnd}/{ROUNDS}", fill=PURPLE_MID,  font=font(12), anchor="mm")
    draw.text((WIDTH//2, 155), "Wait...",               fill=PURPLE_LIGHT, font=font(20), anchor="mm")
    draw.text((WIDTH//2, 195), "Don't press yet!",      fill=PURPLE_MID,  font=font(10), anchor="mm")
    return img

def screen_rt_flash(rnd):
    img, draw = new_frame(GOLD)
    draw.text((WIDTH//2,130), "PRESS!",                fill=BG_DARK, font=font(34), anchor="mm")
    draw.text((WIDTH//2,175), f"Round {rnd}/{ROUNDS}", fill=BG_MID,  font=font(12), anchor="mm")
    show_image(img)
    return time.time()

def screen_rt_early():
    img, draw = new_frame(RED)
    draw.text((WIDTH//2,120), "TOO",   fill=WHITE, font=font(30), anchor="mm")
    draw.text((WIDTH//2,158), "EARLY", fill=WHITE, font=font(30), anchor="mm")
    draw.text((WIDTH//2,210), "Wait for the gold flash!", fill=WHITE, font=font(11), anchor="mm")
    show_image(img); time.sleep(1.5)

def screen_rt_result(rnd, ms, label, color):
    img, draw = frame_bg("menu")
    draw.text((WIDTH//2, 50), f"Round {rnd}/{ROUNDS}", fill=PURPLE_LIGHT, font=font(12), anchor="mm")
    rrect(draw, 25, 78, 215, 200, 14, fill=PURPLE_DEEP, outline=color, width=2)
    draw.text((WIDTH//2,114), f"{ms} ms", fill=WHITE,  font=font(28), anchor="mm")
    draw.text((WIDTH//2,154), label,       fill=color, font=font(18), anchor="mm")
    bw = min(180, int(ms/600*180))
    rrect(draw, 30, 216, 210, 232, 5, fill=BG_DARK, outline=PURPLE_MID, width=1)
    if bw > 0: rrect(draw, 30, 216, 30+bw, 232, 5, fill=color)
    draw.text((WIDTH//2,252), "Press B1 to continue", fill=PURPLE_LIGHT, font=font(10), anchor="mm")
    return img

def screen_rt_summary(times):
    avg   = int(sum(times)/len(times))
    best  = min(times); worst = max(times)
    label, color = rating(avg)
    img, draw = frame_bg("menu")
    draw.rectangle([0,0,WIDTH,50], fill=PURPLE_DEEP)
    draw.line([(0,50),(WIDTH,50)], fill=GOLD, width=2)
    draw.text((WIDTH//2,15), "RESULTS", fill=GOLD,         font=font(18), anchor="mm")
    draw.text((WIDTH//2,34), "summary", fill=PURPLE_LIGHT, font=font(10), anchor="mm")
    rrect(draw, 15, 58, 225, 178, 12, fill=PURPLE_DEEP, outline=color, width=2)
    draw.text((WIDTH//2, 80),  "Average",   fill=PURPLE_LIGHT, font=font(10), anchor="mm")
    draw.text((WIDTH//2,108),  f"{avg} ms", fill=WHITE,        font=font(26), anchor="mm")
    draw.text((WIDTH//2,140),  label,       fill=color,         font=font(16), anchor="mm")
    hs = DATA["scores"].get("reaction_avg", 9999)
    hs_txt = f"Record {hs}ms" if hs < 9999 else ""
    draw.text((WIDTH//2,162),  f"Best {best}ms  Worst {worst}ms",
              fill=PURPLE_LIGHT, font=font(9), anchor="mm")
    if hs_txt:
        draw.text((WIDTH//2, 174), hs_txt, fill=GOLD, font=font(8), anchor="mm")
    draw.text((WIDTH//2,186), "Per round:", fill=PURPLE_LIGHT, font=font(9), anchor="mm")
    bmax = max(times)
    for i, t in enumerate(times):
        bx_ = 20 + i*44; bh = int(t/bmax*30); by_ = 222+30-bh
        _, bc = rating(t)
        draw.rectangle([bx_, by_, bx_+34, 252], fill=bc)
        draw.text((bx_+17, 260), str(i+1), fill=PURPLE_LIGHT, font=font(9), anchor="mm")
    draw.text((WIDTH//2,280), "B1 = Again  B2 = Back", fill=PURPLE_LIGHT, font=font(9), anchor="mm")
    return img

def run_reaction():
    frame = 0
    while True:
        show_image(screen_rt_menu(frame))
        frame += 1
        if b1(): time.sleep(0.12); break
        if b2(): return

    while True:
        times = []; rnd = 1
        while rnd <= ROUNDS:
            delay = random.uniform(MIN_WAIT, MAX_WAIT)
            show_image(screen_rt_waiting(rnd))
            t0 = time.time(); early = False
            while time.time() - t0 < delay:
                if b1(): early = True; break
                time.sleep(0.01)
            if early:
                screen_rt_early()
                while b1(): time.sleep(0.01)
                continue
            flash_t = screen_rt_flash(rnd)
            pressed = False; deadline = flash_t + 3.0
            while time.time() < deadline:
                if b1():
                    ms = int((time.time() - flash_t) * 1000)
                    pressed = True; break
                time.sleep(0.001)
            if not pressed: ms = 3000
            while b1(): time.sleep(0.01)
            time.sleep(0.05)
            label, color = rating(ms)
            times.append(ms)
            record_score("reaction_best", ms, higher_better=False)
            show_image(screen_rt_result(rnd, ms, label, color))
            # Wait B1 to continue (B2 = abort to menu)
            while True:
                if b1(): time.sleep(0.12); break
                if b2(): return
                time.sleep(0.02)
            rnd += 1

        avg = int(sum(times) / len(times))
        record_score("reaction_avg", avg, higher_better=False)
        show_image(screen_rt_summary(times))
        while True:
            if b2(): return
            if b1(): time.sleep(0.15); break
            time.sleep(0.02)

# ══════════════════════════════════════════════════════════════
# ❺  DINO RUN (Chrome offline style)
# ══════════════════════════════════════════════════════════════
DINO_X       = 42
DINO_W       = 24
DINO_H       = 28
DINO_GROUND  = HEIGHT - 52
DINO_GRAV    = 0.95
DINO_JUMP    = -12.5
DINO_MAX_FALL = 14.0
CACTUS_MIN_W = 14
CACTUS_MAX_W = 22
CACTUS_H_MIN = 28
CACTUS_H_MAX = 42
DINO_BASE_SPEED = 5.0
DINO_SPEED_INC  = 0.04
SKY_DINO    = (248, 248, 248)
GROUND_DINO = (220, 220, 220)
_DINO_CANVAS = None
_DINO_BG_DAY = None
_DINO_BG_NIGHT = None
DINO_BODY   = (83, 83, 83)
CACTUS_COL  = (92, 168, 92)

def _spawn_cactus(scroll):
    w = random.randint(CACTUS_MIN_W, CACTUS_MAX_W)
    h = random.randint(CACTUS_H_MIN, CACTUS_H_MAX)
    return [float(WIDTH + 10), DINO_GROUND - h, w, h]

def draw_dino(draw, y, leg_frame):
    x = DINO_X
    yi = int(y)
    draw.rectangle([x, yi + 8, x + DINO_W, yi + DINO_H], fill=DINO_BODY)
    draw.rectangle([x + 14, yi, x + 22, yi + 12], fill=DINO_BODY)
    leg = 2 if leg_frame % 2 else 0
    draw.rectangle([x + 4, yi + DINO_H, x + 10, yi + DINO_H + 6 + leg], fill=DINO_BODY)
    draw.rectangle([x + 14, yi + DINO_H, x + 20, yi + DINO_H + 6 - leg], fill=DINO_BODY)
    draw.rectangle([x + 18, yi + 4, x + 21, yi + 7], fill=WHITE)

def draw_cactus(draw, obs):
    ox, oy, w, h = obs
    ox, oy, w, h = int(ox), int(oy), int(w), int(h)
    draw.rectangle([ox, oy + h // 2, ox + w, oy + h], fill=CACTUS_COL)
    arm = max(6, w // 3)
    draw.rectangle([ox - arm // 2, oy + h // 3, ox + arm, oy + h // 3 + 10], fill=CACTUS_COL)
    draw.rectangle([ox + w - arm // 2, oy + h // 4, ox + w + arm, oy + h // 4 + 12], fill=CACTUS_COL)

def dino_collide(dino_y, obs):
    ox, oy, w, h = obs
    if ox > DINO_X + DINO_W or ox + w < DINO_X:
        return False
    if dino_y + DINO_H > oy + 4:
        return True
    return False

def screen_dino_menu():
    img, draw = new_frame(SKY_DINO)
    draw.rectangle([0, DINO_GROUND, WIDTH, HEIGHT], fill=GROUND_DINO)
    draw.line([(0, DINO_GROUND), (WIDTH, DINO_GROUND)], fill=(180, 180, 180), width=2)
    draw_dino(draw, DINO_GROUND - DINO_H, 0)
    draw.text((WIDTH // 2, 36), "DINO RUN", fill=DINO_BODY, font=font(24), anchor="mm")
    best = DATA["scores"].get("dino", 0)
    draw.text((WIDTH // 2, 64), f"HI {best:05d}", fill=(120, 120, 120), font=font(14), anchor="mm")
    draw.text((WIDTH // 2, 120), "B1 = jump", fill=(100, 100, 100), font=font(11), anchor="mm")
    draw.text((WIDTH // 2, 260), "Press B1 to start", fill=DINO_BODY, font=font(13), anchor="mm")
    draw.text((WIDTH // 2, 290), "B2 = back", fill=(140, 140, 140), font=font(10), anchor="mm")
    return img

def _build_dino_bg(night=False):
    sky, ground = ((40, 40, 50), (60, 60, 70)) if night else (SKY_DINO, GROUND_DINO)
    img = Image.new("RGB", (WIDTH, HEIGHT), sky)
    d = ImageDraw.Draw(img)
    d.rectangle([0, DINO_GROUND, WIDTH, HEIGHT], fill=ground)
    d.line([(0, DINO_GROUND), (WIDTH, DINO_GROUND)], fill=(180, 180, 180), width=2)
    line_c = (90, 90, 100) if night else (200, 200, 200)
    for x in range(0, WIDTH, 24):
        d.line([(x, DINO_GROUND + 8), (x + 12, DINO_GROUND + 8)], fill=line_c, width=2)
    return img

def init_dino_cache():
    global _DINO_BG_DAY, _DINO_BG_NIGHT
    if _DINO_BG_DAY is None:
        _DINO_BG_DAY = _build_dino_bg(False)
        _DINO_BG_NIGHT = _build_dino_bg(True)

def _dino_canvas(night=False):
    global _DINO_CANVAS
    bg = _DINO_BG_NIGHT if night else _DINO_BG_DAY
    if _DINO_CANVAS is None:
        _DINO_CANVAS = bg.copy()
    else:
        _DINO_CANVAS.paste(bg, (0, 0))
    return _DINO_CANVAS, ImageDraw.Draw(_DINO_CANVAS)

def screen_dino_game(dino_y, obstacles, score, scroll, leg_frame, night=False):
    img, draw = _dino_canvas(night)
    off = int(scroll) % 24
    for x in range(-24, WIDTH + 24, 48):
        draw.line([(x - off, DINO_GROUND + 8), (x - off + 12, DINO_GROUND + 8)],
                  fill=(120, 120, 130) if night else (200, 200, 200), width=2)
    for obs in obstacles:
        if obs[0] < WIDTH + 40:
            draw_cactus(draw, obs)
    draw_dino(draw, dino_y, leg_frame)
    draw.text((WIDTH - 12, 14), f"{score:05d}", fill=(180, 180, 180) if night else (80, 80, 80),
              font=font(16), anchor="rm")
    return img

def screen_dino_over(score, new_record):
    img, draw = new_frame(SKY_DINO)
    draw.rectangle([0, DINO_GROUND, WIDTH, HEIGHT], fill=GROUND_DINO)
    draw.text((WIDTH // 2, 90), "GAME OVER", fill=DINO_BODY, font=font(22), anchor="mm")
    draw.text((WIDTH // 2, 130), f"SCORE {score:05d}", fill=(80, 80, 80), font=font(20), anchor="mm")
    best = DATA["scores"].get("dino", score)
    msg = "NEW HI!" if new_record else f"HI {best:05d}"
    draw.text((WIDTH // 2, 165), msg, fill=(200, 140, 60) if new_record else (120, 120, 120),
              font=font(14), anchor="mm")
    draw.text((WIDTH // 2, 250), "B1 = play again", fill=DINO_BODY, font=font(11), anchor="mm")
    draw.text((WIDTH // 2, 275), "B2 = menu", fill=(140, 140, 140), font=font(10), anchor="mm")
    return img

def run_dino():
    while True:
        show_image(screen_dino_menu())
        p = poll()
        if p == "b1":
            debounce()
            break
        if p == "b2":
            return

    sm = speed_mult()
    while True:
        for cd in (3, 2, 1):
            img, draw = new_frame(SKY_DINO)
            draw.text((WIDTH // 2, HEIGHT // 2), str(cd), fill=DINO_BODY, font=font(48), anchor="mm")
            show_image(img)
            time.sleep(0.55)

        dino_y = float(DINO_GROUND - DINO_H)
        dino_vy = 0.0
        on_ground = True
        obstacles = []
        scroll = 0.0
        score = 0
        speed = DINO_BASE_SPEED * sm
        spawn_timer = 0
        leg_frame = 0
        quit_flag = False

        while True:
            t0 = time.perf_counter()
            if b1() and on_ground:
                dino_vy = DINO_JUMP * sm
                on_ground = False
            if b2():
                quit_flag = True
                break

            if not on_ground:
                dino_vy = min(dino_vy + DINO_GRAV * sm, DINO_MAX_FALL)
            dino_y += dino_vy
            if dino_y >= DINO_GROUND - DINO_H:
                dino_y = float(DINO_GROUND - DINO_H)
                dino_vy = 0.0
                on_ground = True

            speed = min((DINO_BASE_SPEED + score * DINO_SPEED_INC) * sm, 12.0 * sm)
            scroll += speed
            score = int(scroll // 6)

            spawn_timer -= 1
            if spawn_timer <= 0:
                obstacles.append(_spawn_cactus(scroll))
                gap = max(55, 95 - score // 8)
                spawn_timer = max(28, int(gap / speed * 8))

            obstacles = [[o[0] - speed, o[1], o[2], o[3]] for o in obstacles]
            obstacles = [o for o in obstacles if o[0] + o[2] > -10]

            for obs in obstacles:
                if dino_collide(dino_y, obs):
                    quit_flag = False
                    break
            else:
                leg_frame += 1
                show_image(screen_dino_game(dino_y, obstacles, score, scroll, leg_frame,
                                            night=score >= 300))
                wait_frame(t0, target=game_frame_target() * 0.9)
                continue
            break

        if quit_flag:
            return

        saved = record_score("dino", score)
        show_image(screen_dino_over(score, saved))
        while True:
            p = poll(5, 0.015)
            if p == "b1":
                debounce()
                break
            if p == "b2":
                return

# ══════════════════════════════════════════════════════════════
# ❻  SETTINGS (software only)
# ══════════════════════════════════════════════════════════════
SETTINGS_ROWS = [
    ("Game speed", "speed_pct", (80, 100, 120)),
    ("Screen dim", "brightness", (60, 80, 100)),
    ("HR breathing", "breathing", ("box", "478")),
    ("Finger IR level", "ir_threshold", (15000, 20000, 25000)),
]
SETTINGS_ACTIONS = ("View vitals log", "Reset all scores")

def _settings_count():
    return len(SETTINGS_ROWS) + len(SETTINGS_ACTIONS)

def screen_settings(sel):
    img, draw = frame_bg("menu")
    draw.text((WIDTH // 2, 20), "SETTINGS", fill=OFF_WHITE, font=font(18), anchor="mm")
    s = DATA["settings"]
    for i, (title, key, options) in enumerate(SETTINGS_ROWS):
        y = 48 + i * 44
        is_sel = i == sel
        rrect(draw, 14, y, 226, y + 42, 8,
              fill=PURPLE_DEEP if is_sel else (28, 18, 50),
              outline=PINK_HOT if is_sel else PURPLE_MID, width=2 if is_sel else 1)
        val = s.get(key, options[0])
        if key == "speed_pct":
            disp = f"{val}%"
        elif key == "brightness":
            disp = f"{val}% dim"
        else:
            disp = str(val)
        draw.text((24, y + 14), title, fill=WHITE if is_sel else PURPLE_LIGHT, font=font(11), anchor="lm")
        draw.text((210, y + 14), disp, fill=MINT if is_sel else GOLD, font=font(11), anchor="rm")
    base_y = 48 + len(SETTINGS_ROWS) * 44
    for j, act in enumerate(SETTINGS_ACTIONS):
        i = len(SETTINGS_ROWS) + j
        y = base_y + j * 36
        is_sel = i == sel
        rrect(draw, 14, y, 226, y + 30, 8,
              fill=PURPLE_DEEP if is_sel else (28, 18, 50),
              outline=PINK_HOT if is_sel else PURPLE_MID, width=2 if is_sel else 1)
        draw.text((24, y + 15), act, fill=WHITE if is_sel else PURPLE_LIGHT, font=font(11), anchor="lm")
    sc = DATA["scores"]
    draw.text((WIDTH // 2, 278),
              f"Hi Flappy {sc.get('flappy',0)} Dino {sc.get('dino',0):05d} "
              f"React {_fmt_reaction(sc.get('reaction_best',9999))}",
              fill=PURPLE_MID, font=font(7), anchor="mm")
    draw.text((WIDTH // 2, 292), "B2 = next   hold B1 on Reset to clear",
              fill=PURPLE_MID, font=font(7), anchor="mm")
    draw.text((WIDTH // 2, 312), "B2 on last item = menu", fill=PURPLE_LIGHT, font=font(8), anchor="mm")
    return img

def run_settings():
    sel = 0
    n = _settings_count()
    while True:
        show_image(screen_settings(sel))
        p = poll()
        if p == "b2":
            debounce()
            wait_release()
            if sel >= n - 1:
                return
            sel += 1
            continue
        if p == "b1":
            debounce()
            if sel < len(SETTINGS_ROWS):
                _t, key, options = SETTINGS_ROWS[sel]
                cur = DATA["settings"].get(key, options[0])
                idx = options.index(cur) if cur in options else 0
                DATA["settings"][key] = options[(idx + 1) % len(options)]
                save_data()
                if key == "brightness":
                    global _LUT_BR
                    _LUT_BR = -1
            elif sel == len(SETTINGS_ROWS):
                while True:
                    show_image(screen_vitals_history())
                    if b2():
                        debounce()
                        wait_release()
                        break
                    time.sleep(0.05)
            else:
                reset_all_scores()
            wait_release()
        time.sleep(0.02)

# ══════════════════════════════════════════════════════════════
# WELCOME SPLASH
# ══════════════════════════════════════════════════════════════
def screen_welcome(frame=0):
    img, draw = frame_bg("menu")
    for i, (sx, sy) in enumerate([(30,60),(200,80),(50,200),(190,250),(120,40),(60,290),(210,180)]):
        s = int(2 + 2 * math.sin(frame * 0.15 + i))
        c = [PURPLE_LIGHT, PINK_SOFT, MINT][i % 3]
        draw.ellipse([sx-s,sy-s,sx+s,sy+s], fill=c)
    for r in range(60, 0, -10):
        draw.ellipse([WIDTH//2-r,85-r//2,WIDTH//2+r,85+r//2], outline=PURPLE_MID, width=1)
    draw.text((WIDTH//2, 55), "ZEN",  fill=PURPLE_LIGHT, font=font(38), anchor="mm")
    draw.text((WIDTH//2, 95), "DESK", fill=PINK_HOT,     font=font(38), anchor="mm")
    draw.line([(50,112),(190,112)], fill=PURPLE_MID, width=1)
    draw.text((WIDTH//2,124), "your wellness companion", fill=PURPLE_LIGHT, font=font(10), anchor="mm")
    draw_face(draw, "happy", 120, 210, frame=frame)
    for i, (sx, sy) in enumerate([(50,160),(195,155),(35,250),(215,255)]):
        s = int(3 + 2 * math.sin(frame*0.2 + i*1.5))
        c = [PINK_HOT, MINT, GOLD, PURPLE_LIGHT][i]
        draw_sparkle(draw, sx, sy, s, c)
    draw.text((WIDTH//2,295), "initialising...", fill=PURPLE_MID, font=font(10), anchor="mm")
    return img

# ══════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════
def main():
    load_data()
    try:
        init_hardware()
    except Exception as e:
        print(f"GPIO/SPI init failed: {e}")
        print("On Pi OS Bookworm run:")
        print("  sudo apt install python3-rpi-lgpio python3-lgpio")
        print("  pip3 uninstall -y RPi.GPIO")
        print("  sudo usermod -aG gpio,spi,i2c $USER  # then log out & back in")
        raise
    init_display()
    init_assets()
    init_sensor()
    print(f"ZenDesk {ZENDESK_VERSION} started!")

    streak = vitals_streak()
    today = date.today().isoformat()
    if streak >= 3 and DATA.get("meta", {}).get("streak_popup_date") != today:
        while True:
            show_image(screen_streak_celebration(streak))
            if b1() or b2():
                debounce()
                wait_release()
                break
            time.sleep(0.05)
        DATA.setdefault("meta", {})["streak_popup_date"] = today
        save_data()

    for f in range(36):
        show_image(screen_welcome(f * 2))
        time.sleep(0.07)

    selected = 0
    saver_frame = 0
    last_input = time.time()
    menu_shown_sel = -1
    build_menu_cache()
    init_dino_cache()
    _rebuild_rgb565_lut()

    try:
        while True:
            if time.time() - last_input >= IDLE_TIMEOUT_S:
                show_image(screen_screensaver(saver_frame))
                saver_frame += 1
                if poll_btn():
                    last_input = time.time()
                    debounce()
                    wait_release()
                time.sleep(0.12)
                continue

            p = poll(10, 0.006)
            if p:
                last_input = time.time()

            if p == 'b2':
                selected = (selected + 1) % len(MENU_ITEMS)
                debounce()
                wait_release()

            elif p == 'b1':
                debounce()
                wait_release()
                if selected == 0:
                    run_heart_rate()
                elif selected == 1:
                    run_flappy()
                elif selected == 2:
                    run_pong()
                elif selected == 3:
                    run_reaction()
                elif selected == 4:
                    run_dino()
                elif selected == 5:
                    run_settings()
                invalidate_menu_cache()
                build_menu_cache()
                menu_shown_sel = -1

            if selected != menu_shown_sel:
                show_image(screen_main_menu(selected, 0))
                menu_shown_sel = selected

            time.sleep(0.01)

    except KeyboardInterrupt:
        pass
    finally:
        try: GPIO.cleanup()
        except: pass
        try: spi.close()
        except: pass
        print("ZenDesk stopped.")

if __name__ == "__main__":
    main()
