#!/usr/bin/python3
"""Measure window and workspace switch motion on the live compositor.

Opens disposable, solid-coloured foot windows on spare workspaces, records the
monitor at 60 fps while the real window-step / workspace actions run, then tracks
one window edge per frame. Restores the original workspace, focus and cursor.

  bench.py windows    --label NAME [--widths 1,1,1]
  bench.py workspaces --label NAME

Writes /tmp/motion-bench/NAME.{mp4,json}; prints a per-step summary.
"""
import argparse
import json
import os
import signal
import subprocess
import time
from pathlib import Path

OUT = Path('/tmp/motion-bench')
COLORS = {'r': 'ff0000', 'g': '00ff00', 'b': '0000ff'}
APP = 'motionbench'


def hypr(*args):
    return subprocess.run(['hyprctl', *args], capture_output=True, text=True, timeout=10).stdout


def lua(code):
    r = subprocess.run(['hyprctl', 'eval', code], capture_output=True, text=True, timeout=10)
    if r.returncode:
        raise RuntimeError(r.stdout + r.stderr)


def clients():
    return json.loads(hypr('clients', '-j'))


def bench_windows(ws):
    return sorted((c for c in clients() if c['class'] == APP and c['workspace']['id'] == ws),
                  key=lambda c: c['at'][0])


def spawn(ws, colors):
    lua(f'hl.dispatch(hl.dsp.focus({{ workspace = "{ws}" }}))')
    time.sleep(0.15)
    for name in colors:
        subprocess.Popen(['foot', '--app-id', APP, '-o', f'colors-dark.background={COLORS[name]}',
                          '-o', f'colors-light.background={COLORS[name]}', 'sleep', '120'],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        for _ in range(60):
            time.sleep(0.05)
            if len(bench_windows(ws)) >= colors.index(name) + 1:
                break
        time.sleep(0.15)


def cleanup():
    for c in clients():
        if c['class'] == APP:
            lua(f'hl.dispatch(hl.dsp.window.close({{ window = "address:{c["address"]}" }}))')
    time.sleep(0.4)


class Recorder:
    def __init__(self, path):
        self.path = path
        self.proc = subprocess.Popen(
            ['gpu-screen-recorder', '-w', 'eDP-1', '-f', '60', '-fm', 'cfr', '-k', 'h264',
             '-q', 'very_high', '-cursor', 'no', '-c', 'mp4', '-o', str(path)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.2)

    def stop(self):
        time.sleep(0.3)
        self.proc.send_signal(signal.SIGINT)
        self.proc.wait(timeout=10)


def frames(path, crop):
    """Yield (w, h, bytes) for each frame of a cropped strip, as rgb24."""
    w, h, x, y = crop
    raw = subprocess.run(['ffmpeg', '-v', 'error', '-i', str(path), '-vf', f'crop={w}:{h}:{x}:{y}',
                          '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'], capture_output=True).stdout
    size = w * h * 3
    return [raw[i:i + size] for i in range(0, len(raw) - size + 1, size)]


def classify(px):
    r, g, b = px
    if r > g + 60 and r > b + 60: return 'r'
    if g > r + 60 and g > b + 60: return 'g'
    if b > r + 60 and b > g + 60: return 'b'
    return '.'


def runs(strip, n):
    """Runs of one colour class along a strip of n pixels: (cls, start, end)."""
    out, cur, start = [], None, 0
    for i in range(n):
        c = classify(strip[i * 3:i * 3 + 3])
        if c != cur:
            if cur and cur != '.' and i - start >= 24:
                out.append((cur, start, i))
            cur, start = c, i
    if cur and cur != '.' and n - start >= 24:
        out.append((cur, start, n))
    return out


def track(path, rest, axis):
    """Viewport offset per frame (physical px), from any unclipped leading edge."""
    if axis == 'x':
        data = [f[:1920 * 3] for f in frames(path, (1920, 2, 0, 640))]
        n = 1920
    else:
        data = [b''.join(f[i * 6:i * 6 + 3] for i in range(1200)) for f in frames(path, (2, 1200, 1100, 0))]
        n = 1200
    offsets = []
    for strip in data:
        found = None
        for cls, start, end in runs(strip, n):
            if start > 4 and cls in rest:
                found = start - rest[cls]
                break
            if end < n - 4 and cls in rest and rest.get(cls + '_end') is not None:
                found = end - rest[cls + '_end']
                break
        offsets.append(found)
    return offsets


def summarize(offsets, fps=60):
    """Split the trace into moves and report shape numbers for each."""
    vals = [o for o in offsets]
    moves, i, n = [], 1, len(vals)
    while i < n:
        if vals[i] is not None and vals[i - 1] is not None and abs(vals[i] - vals[i - 1]) >= 2:
            s = i - 1
            j = i
            quiet = 0
            while j < n and quiet < 12:
                if vals[j] is not None and vals[j - 1] is not None and abs(vals[j] - vals[j - 1]) < 1:
                    quiet += 1
                else:
                    quiet = 0
                j += 1
            seg = [v for v in vals[s:j] if v is not None]
            start, end = seg[0], seg[-1]
            dist = end - start
            if abs(dist) > 20:
                prog = [(v - start) / dist for v in seg]
                steps = [abs(seg[k] - seg[k - 1]) for k in range(1, len(seg))]
                t90 = next((k for k, p in enumerate(prog) if p >= 0.9), None)
                settle = next((k for k in range(len(seg)) if all(abs(v - end) <= 1 for v in seg[k:])), None)
                moves.append(dict(start_frame=s, distance=round(dist), overshoot_pct=round(max(0, max(prog) - 1) * 100, 1),
                                  t90_ms=None if t90 is None else round(t90 * 1000 / fps),
                                  settle_ms=None if settle is None else round(settle * 1000 / fps),
                                  max_px_per_frame=max(steps) if steps else 0,
                                  progress=[round(p, 3) for p in prog]))
            i = j
        else:
            i += 1
    return moves


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('mode', choices=['windows', 'workspaces'])
    ap.add_argument('--label', required=True)
    ap.add_argument('--widths', default='1,1,1')
    ap.add_argument('--gap', type=float, default=0.75, help='seconds between steps')
    ap.add_argument('--action', choices=['step', 'native', 'key', 'move', 'keyheld', 'keybindnative', 'keybindlua'], default='step')
    ap.add_argument('--setup', default='', help='Lua run before recording')
    ap.add_argument('--teardown', default='', help='Lua run after recording')
    args = ap.parse_args()
    OUT.mkdir(exist_ok=True)
    video = OUT / f'{args.label}.mp4'
    before_ws = json.loads(hypr('activeworkspace', '-j'))['id']
    before_win = json.loads(hypr('activewindow', '-j') or '{}').get('address')
    cursor = hypr('cursorpos').strip().split(', ')
    cleanup()
    try:
        if args.mode == 'windows':
            spawn(98, ['r', 'g', 'b'])
            widths = [float(w) for w in args.widths.split(',')]
            for win, width in zip(bench_windows(98), widths):
                lua(f'hl.dispatch(hl.dsp.focus({{ window = "address:{win["address"]}" }}))')
                time.sleep(0.05)
                lua(f'hl.dispatch(hl.dsp.layout("colresize {width}"))')
                time.sleep(0.1)
            first = bench_windows(98)[0]
            lua(f'hl.dispatch(hl.dsp.focus({{ window = "address:{first["address"]}" }}))')
            time.sleep(0.9)
            mon = json.loads(hypr('monitors', '-j'))[0]
            scale = mon['scale']
            rest = {}
            for win, name in zip(bench_windows(98), 'rgb'):
                rest[name] = round((win['at'][0] - mon['x']) * scale)
                rest[name + '_end'] = round((win['at'][0] + win['size'][0] - mon['x']) * scale)
            if args.setup:
                lua(args.setup)
            rec = Recorder(video)
            def go(d):
                if args.action == 'step':
                    lua(f'TrickyTiles.step("{d}")')
                elif args.action == 'move':  # viewport only, focus unchanged
                    lua(f'hl.dispatch(hl.dsp.layout("move {-1880 if d == "r" else 1880}"))')
                elif args.action == 'native':
                    lua(f'hl.dispatch(hl.dsp.layout("focus {d}"))')
                elif args.action in ('keybindnative', 'keybindlua'):  # temporary test binds
                    # Super+Ctrl+Shift+F11/F12 (never Ctrl+Alt+Fn: that switches the VT).
                    fkey = 87 if d == 'r' else 88
                    subprocess.run(['ydotool', 'key', '125:1', '29:1', '42:1', f'{fkey}:1', f'{fkey}:0',
                                    '42:0', '29:0', '125:0'], capture_output=True)
                elif args.action == 'keyheld':  # Super stays down; only the arrow is pressed
                    code = 106 if d == 'r' else 105
                    subprocess.run(['ydotool', 'key', f'{code}:1', f'{code}:0'], capture_output=True)
                else:  # the real Super+arrow binding, through uinput
                    code = 106 if d == 'r' else 105
                    subprocess.run(['ydotool', 'key', '125:1', f'{code}:1', f'{code}:0', '125:0'],
                                   capture_output=True)
            if args.action == 'keyheld':
                subprocess.run(['ydotool', 'key', '125:1'], capture_output=True)
                time.sleep(0.5)
            for d in ['r', 'r', 'l', 'l']:
                go(d)
                time.sleep(args.gap)
            # Interrupt: two quick steps 120 ms apart.
            go('r'); time.sleep(0.12); go('r')
            time.sleep(args.gap + 0.3)
            if args.action == 'keyheld':
                subprocess.run(['ydotool', 'key', '125:0'], capture_output=True)
            rec.stop()
            if args.teardown:
                lua(args.teardown)
            offsets = track(video, rest, 'x')
        else:
            spawn(98, ['r'])
            spawn(99, ['b'])
            lua('hl.dispatch(hl.dsp.focus({ workspace = "98" }))')
            time.sleep(0.9)
            rest = {'r': 0, 'r_end': None, 'b': 0, 'b_end': None}
            if args.setup:
                lua(args.setup)
            rec = Recorder(video)
            for target in ['99', '98', '99', '98']:
                lua(f'hl.dispatch(hl.dsp.focus({{ workspace = "{target}" }}))')
                time.sleep(args.gap)
            rec.stop()
            if args.teardown:
                lua(args.teardown)
            offsets = None
    finally:
        cleanup()
        lua(f'hl.dispatch(hl.dsp.focus({{ workspace = "{before_ws}" }}))')
        if before_win:
            lua(f'hl.dispatch(hl.dsp.focus({{ window = "address:{before_win}" }}))')
        lua(f'hl.dispatch(hl.dsp.cursor.move({{ x = {cursor[0]}, y = {cursor[1]} }}))')
    result = dict(label=args.label, mode=args.mode, widths=args.widths, video=str(video))
    if offsets is not None:
        result['offsets'] = offsets
        result['moves'] = summarize(offsets)
    (OUT / f'{args.label}.json').write_text(json.dumps(result, indent=1))
    for m in result.get('moves', []):
        print({k: v for k, v in m.items() if k != 'progress'})
    print('video', video)


if __name__ == '__main__':
    main()
