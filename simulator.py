#!/usr/bin/env python3
"""
ZenDesk desktop simulator (no Pi hardware).
  Space      = B1
  Down arrow = B2
  q / Esc    = quit

  python simulator.py
"""

import sys
import types
import threading

gpio = types.ModuleType("RPi.GPIO")
gpio.BCM, gpio.OUT, gpio.IN = "BCM", 0, 1
gpio.LOW, gpio.HIGH, gpio.PUD_UP = 0, 1, 0
_btn = {"b1": False, "b2": False}

gpio.setup = lambda *a, **k: None
gpio.output = lambda *a, **k: None
gpio.setwarnings = lambda *a: None
gpio.setmode = lambda *a: None
gpio.cleanup = lambda: None
gpio.input = lambda pin: gpio.LOW if (
    (_btn["b1"] and pin == 16) or (_btn["b2"] and pin == 20)
) else gpio.HIGH

spidev = types.ModuleType("spidev")
class _Spi:
    max_speed_hz = 16000000
    mode = 0
    def open(self, *a): pass
    def writebytes(self, x): pass
    def writebytes2(self, x): pass
spidev.SpiDev = _Spi

smbus = types.ModuleType("smbus2")
class _Bus:
    def write_byte_data(self, *a): pass
    def read_byte_data(self, *a): return 0
    def read_i2c_block_data(self, *a): return [0] * 6
smbus.SMBus = lambda _: _Bus()

sys.modules["RPi.GPIO"] = gpio
sys.modules["spidev"] = spidev
sys.modules["smbus2"] = smbus

import tkinter as tk
from PIL import ImageTk
import zendesk as z

z.SENSOR_OK = True
z.init_display = lambda: None
z.init_sensor = lambda: setattr(z, "SENSOR_OK", True) or None
_fake_ir = 28000
z.read_sample_blocking = lambda timeout=0.5: (50000, _fake_ir)
z.check_finger = lambda: _fake_ir > z.ir_threshold()

_root = None
_label = None

def _show(img):
    global _label
    if img.mode != "RGB":
        img = img.convert("RGB")
    photo = ImageTk.PhotoImage(img)
    _label.configure(image=photo)
    _label.image = photo
    _root.update()

z.show_image = _show

def _run_zendesk():
    z.load_data()
    z.init_assets()
    z.init_sensor()
    z.main()

def main():
    global _root, _label
    _root = tk.Tk()
    _root.title("ZenDesk Simulator")
    _label = tk.Label(_root)
    _label.pack()
    _root.geometry(f"{z.WIDTH}x{z.HEIGHT+40}")

    def down(e):
        if e.keysym == "space":
            _btn["b1"] = True
        elif e.keysym == "Down":
            _btn["b2"] = True

    def up(e):
        if e.keysym == "space":
            _btn["b1"] = False
        elif e.keysym == "Down":
            _btn["b2"] = False
        elif e.keysym in ("q", "Escape"):
            z.SENSOR_OK = False
            _root.destroy()
            sys.exit(0)

    _root.bind("<KeyPress>", down)
    _root.bind("<KeyRelease>", up)
    threading.Thread(target=_run_zendesk, daemon=True).start()
    _root.mainloop()

if __name__ == "__main__":
    main()
