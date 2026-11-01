#!/usr/bin/python3
"""Per-frame vertical runs of the red (ws 98) and blue (ws 99) bench windows."""
import sys
sys.path.insert(0, __import__('os').path.dirname(__file__))
import bench
video = sys.argv[1]
data = [b''.join(f[i * 6:i * 6 + 3] for i in range(1200)) for f in bench.frames(video, (2, 1200, 1100, 0))]
prev = None
for n, strip in enumerate(data):
    r = bench.runs(strip, 1200)
    sig = ' '.join(f'{c}:{a}-{b}' for c, a, b in r)
    if sig != prev:
        print(n, sig)
    prev = sig
