#!/usr/bin/python3
"""A virtual multi-touch touchpad (uinput) for scripted trackpad swipes.

libinput sees it as a real touchpad, so Hyprland's gesture code runs exactly as
for physical fingers. Size: 129 x 77 mm (31 units/mm), like a laptop pad.

  vtouchpad.py swipe DX_MM DY_MM [--ms 180] [--fingers 3] [--hold-ms 0]
  (as a module) with VirtualTouchpad() as pad: pad.swipe(...)

Positive DX moves the fingers right, positive DY moves them down. The device is
destroyed on exit, so no touches can stay stuck.
"""
import argparse
import fcntl
import os
import struct
import time

EV_SYN, EV_KEY, EV_ABS = 0x00, 0x01, 0x03
SYN_REPORT = 0
ABS_X, ABS_Y = 0x00, 0x01
ABS_MT_SLOT, ABS_MT_POSITION_X, ABS_MT_POSITION_Y, ABS_MT_TOOL_TYPE, ABS_MT_TRACKING_ID = 0x2f, 0x35, 0x36, 0x37, 0x39
BTN_LEFT, BTN_TOOL_FINGER, BTN_TOUCH = 0x110, 0x145, 0x14a
BTN_TOOL_DOUBLETAP, BTN_TOOL_TRIPLETAP, BTN_TOOL_QUADTAP = 0x14d, 0x14e, 0x14f
TOOLS = {1: BTN_TOOL_FINGER, 2: BTN_TOOL_DOUBLETAP, 3: BTN_TOOL_TRIPLETAP, 4: BTN_TOOL_QUADTAP}
INPUT_PROP_POINTER, INPUT_PROP_BUTTONPAD = 0x00, 0x02

UI_SET_EVBIT, UI_SET_KEYBIT, UI_SET_ABSBIT, UI_SET_PROPBIT = 0x40045564, 0x40045565, 0x40045567, 0x4004556e
UI_DEV_SETUP, UI_ABS_SETUP, UI_DEV_CREATE, UI_DEV_DESTROY = 0x405c5503, 0x401c5504, 0x5501, 0x5502

RES = 31                      # units per mm
XMAX, YMAX = 4000, 2400       # 129 x 77 mm
NAME = b'motion-bench virtual touchpad'


class VirtualTouchpad:
    def __enter__(self):
        self.fd = os.open('/dev/uinput', os.O_WRONLY | os.O_NONBLOCK)
        for ev in (EV_SYN, EV_KEY, EV_ABS):
            fcntl.ioctl(self.fd, UI_SET_EVBIT, ev)
        for key in (BTN_LEFT, BTN_TOUCH, *TOOLS.values()):
            fcntl.ioctl(self.fd, UI_SET_KEYBIT, key)
        for prop in (INPUT_PROP_POINTER, INPUT_PROP_BUTTONPAD):
            fcntl.ioctl(self.fd, UI_SET_PROPBIT, prop)
        axes = [(ABS_X, 0, XMAX, RES), (ABS_Y, 0, YMAX, RES), (ABS_MT_SLOT, 0, 4, 0),
                (ABS_MT_POSITION_X, 0, XMAX, RES), (ABS_MT_POSITION_Y, 0, YMAX, RES),
                (ABS_MT_TOOL_TYPE, 0, 2, 0), (ABS_MT_TRACKING_ID, 0, 65535, 0)]
        for code, lo, hi, res in axes:
            fcntl.ioctl(self.fd, UI_SET_ABSBIT, code)
            fcntl.ioctl(self.fd, UI_ABS_SETUP, struct.pack('HH6i', code, 0, 0, lo, hi, 0, 0, res))
        fcntl.ioctl(self.fd, UI_DEV_SETUP, struct.pack('4H80sI', 0x18, 0x1234, 0x5678, 1, NAME, 0))
        fcntl.ioctl(self.fd, UI_DEV_CREATE)
        self.tracking = 100
        time.sleep(0.8)  # let udev, libinput and Hyprland pick the device up
        return self

    def __exit__(self, *exc):
        try:
            self.lift(range(5))
        finally:
            fcntl.ioctl(self.fd, UI_DEV_DESTROY)
            os.close(self.fd)

    def emit(self, etype, code, value):
        now = time.time()
        os.write(self.fd, struct.pack('llHHi', int(now), int((now % 1) * 1e6), etype, code, value))

    def syn(self):
        self.emit(EV_SYN, SYN_REPORT, 0)

    def lift(self, slots):
        for s in slots:
            self.emit(EV_ABS, ABS_MT_SLOT, s)
            self.emit(EV_ABS, ABS_MT_TRACKING_ID, -1)
        for key in (BTN_TOUCH, *TOOLS.values()):
            self.emit(EV_KEY, key, 0)
        self.syn()

    def frame(self, points, first=False):
        for s, (x, y) in enumerate(points):
            self.emit(EV_ABS, ABS_MT_SLOT, s)
            if first:
                self.tracking += 1
                self.emit(EV_ABS, ABS_MT_TRACKING_ID, self.tracking)
            self.emit(EV_ABS, ABS_MT_POSITION_X, int(x))
            self.emit(EV_ABS, ABS_MT_POSITION_Y, int(y))
        if first:
            self.emit(EV_KEY, BTN_TOUCH, 1)
            self.emit(EV_KEY, TOOLS[len(points)], 1)
        self.emit(EV_ABS, ABS_X, int(points[0][0]))
        self.emit(EV_ABS, ABS_Y, int(points[0][1]))
        self.syn()

    def swipe(self, dx_mm, dy_mm, ms=180, fingers=3, hold_ms=0, ease='out', hz=125):
        """Put fingers down mid-pad, move them together by (dx, dy) mm, optionally hold, lift."""
        cx, cy = XMAX / 2 - dx_mm * RES / 2, YMAX / 2 - dy_mm * RES / 2
        base = [(cx + (i - (fingers - 1) / 2) * 18 * RES, cy + (6 if i % 2 else 0) * RES) for i in range(fingers)]
        self.frame(base, first=True)
        time.sleep(1 / hz)
        steps = max(2, int(ms / 1000 * hz))
        for i in range(1, steps + 1):
            t = i / steps
            p = 1 - (1 - t) ** 2 if ease == 'out' else t
            self.frame([(x + dx_mm * RES * p, y + dy_mm * RES * p) for x, y in base])
            time.sleep(1 / hz)
        if hold_ms:
            end = [(x + dx_mm * RES, y + dy_mm * RES) for x, y in base]
            t_end = time.monotonic() + hold_ms / 1000
            while time.monotonic() < t_end:
                self.frame(end)
                time.sleep(1 / hz)
        self.lift(range(fingers))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('action', choices=['swipe'])
    ap.add_argument('dx', type=float)
    ap.add_argument('dy', type=float)
    ap.add_argument('--ms', type=int, default=180)
    ap.add_argument('--fingers', type=int, default=3)
    ap.add_argument('--hold-ms', type=int, default=0)
    ap.add_argument('--ease', default='out', choices=['out', 'linear'])
    a = ap.parse_args()
    with VirtualTouchpad() as pad:
        pad.swipe(a.dx, a.dy, a.ms, a.fingers, a.hold_ms, a.ease)
        time.sleep(0.2)


if __name__ == '__main__':
    main()
