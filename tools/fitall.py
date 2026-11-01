#!/usr/bin/python3
"""Fit the first 8 frames of each measured move to the spring (or bezier) model.

Prints the start delay (ms from animation start to first displayed frame) and RMS
error per move, so a preset can be validated even when edge tracking glitches later.
"""
import json, math, sys, os
sys.path.insert(0, os.path.dirname(__file__))
from springsim import advance

def spring_model(duration, bounce):
    k = (2 * math.pi / duration) ** 2; c = 4 * math.pi * (1 - bounce) / duration
    return lambda t: advance(0.0, 0.0, k, c, 1, t)[0] if t > 0 else 0.0

def bezier_model(p1, p2, dur):
    def bez(s, a, b): return 3 * (1 - s) ** 2 * s * a + 3 * (1 - s) * s * s * b + s ** 3
    def f(t):
        x = min(1, max(0, t / dur)); lo, hi = 0.0, 1.0
        for _ in range(40):
            mid = (lo + hi) / 2
            if bez(mid, p1[0], p2[0]) < x: lo = mid
            else: hi = mid
        return bez(lo, p1[1], p2[1])
    return f

def fit(label, model):
    d = json.load(open(f'/tmp/motion-bench/{label}.json'))
    out = []
    for m in d['moves'][:4]:
        prog = m['progress'][1:9]
        best = min(((sum((model((i * 1000 / 60 + dl) / 1000) - p) ** 2 for i, p in enumerate(prog)), dl) for dl in range(0, 130)))
        out.append((best[1], round(math.sqrt(best[0] / len(prog)) * 100, 1), round(prog[0] * 100)))
    return out

PRESETS = {'spring': (0.29, 0.15), 'glide': (0.33, 0), 'snappy': (0.21, 0.05), 'lively': (0.36, 0.25), 'reduced': (0.17, 0)}
if __name__ == '__main__':
    print('baseline macEase:', fit('base-full', bezier_model((0.25, 1), (0.5, 1), 0.28)), '(delay ms, rms %, first frame %)')
    print('baseline via key:', fit('base-key', bezier_model((0.25, 1), (0.5, 1), 0.28)))
    for name, (dur, b) in PRESETS.items():
        print(f'{name:8s}', fit(f'preset-{name}', spring_model(dur, b)))
