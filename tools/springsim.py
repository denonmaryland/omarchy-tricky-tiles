#!/usr/bin/python3
"""Simulate hyprutils' closed-form spring at 60 Hz for a full-width (1880 px) move.

response/bounce follow the SwiftUI convention used by the plugin:
  stiffness = (2*pi/response)^2 * mass, damping = 4*pi*(1-bounce)/response * mass
"""
import math
import sys


def advance(x, v, k, c, m, dt):
    d, w0, g = x - 1, math.sqrt(k / m), c / (2 * m)
    if g < w0:
        wd = math.sqrt(w0 * w0 - g * g); e = math.exp(-g * dt)
        nd = e * (d * math.cos(wd * dt) + (v + g * d) / wd * math.sin(wd * dt))
        nv = e * (v * math.cos(wd * dt) - (g * v + w0 * w0 * d) / wd * math.sin(wd * dt))
        return 1 + nd, nv
    e = math.exp(-g * dt); b = v + g * d
    return 1 + e * (d + b * dt), e * (v - g * b * dt)


def run(response, bounce, dist=1880, delay=0.0):
    k = (2 * math.pi / response) ** 2; c = 4 * math.pi * (1 - bounce) / response
    x, v, t, dt = 0.0, 0.0, 0.0, 1 / 60
    frames, prev = [], 0.0
    x, v = advance(x, v, k, c, 1, dt + delay)
    frames.append(x)
    for _ in range(80):
        x, v = advance(x, v, k, c, 1, dt); frames.append(x)
    steps = [abs(frames[0])] + [abs(frames[i] - frames[i - 1]) for i in range(1, len(frames))]
    t90 = next(i for i, p in enumerate(frames) if p >= 0.9)
    t99 = next(i for i in range(len(frames)) if all(abs(p - 1) * dist <= 2 for p in frames[i:]))
    return dict(k=round(k, 1), c=round(c, 2), first_px=round(frames[0] * dist), peak_px=round(max(steps) * dist),
                t90_ms=round((t90 + 1) * 1000 / 60 + delay * 1000), within2px_ms=round((t99 + 1) * 1000 / 60),
                overshoot_px=round(max(0, max(frames) - 1) * dist))


if __name__ == '__main__':
    for r, b in [(0.22, 0.0), (0.26, 0.0), (0.28, 0.1), (0.30, 0.0), (0.30, 0.15), (0.32, 0.15), (0.32, 0.2), (0.34, 0.25), (0.36, 0.3), (0.407, 0.216), (0.18, 0.0)]:
        print(f'r={r} b={b}', run(r, b), '| +50ms delay first_px', run(r, b, delay=0.05)['first_px'])
