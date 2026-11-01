#!/usr/bin/python3
"""Record finger-tracked swipes made with the virtual touchpad and track the strip per frame.

  swipebench.py LABEL [--setup LUA] [--mods super+alt] [--profile drag|flick|short]
"""
import argparse, json, subprocess, sys, time, os
sys.path.insert(0, os.path.dirname(__file__))
import bench
from vtouchpad import VirtualTouchpad

PROFILES = {
    'drag': dict(dx=-55, ms=420, hold_ms=120),   # slow, deliberate drag then lift
    'flick': dict(dx=-30, ms=90, hold_ms=0),     # quick flick
    'short': dict(dx=-12, ms=200, hold_ms=60),   # small, slow nudge: should spring back
    'long': dict(dx=-110, ms=500, hold_ms=0),    # drag across more than one window
}
MODS = {'super+alt': ['125', '56'], 'none': []}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('label')
    ap.add_argument('--setup', default='')
    ap.add_argument('--mods', default='super+alt', choices=list(MODS))
    ap.add_argument('--profiles', default='drag,flick,short')
    a = ap.parse_args()
    video = bench.OUT / f'{a.label}.mp4'
    ws_before = json.loads(bench.hypr('activeworkspace', '-j'))['id']
    bench.cleanup()
    events = []
    try:
        bench.spawn(98, ['r', 'g', 'b'])
        for w in bench.bench_windows(98):
            bench.lua(f'hl.dispatch(hl.dsp.focus({{ window = "address:{w["address"]}" }}))'); time.sleep(0.05)
            bench.lua('hl.dispatch(hl.dsp.layout("colresize 1"))'); time.sleep(0.1)
        wins = bench.bench_windows(98)
        order = [w['address'] for w in wins]
        mon = json.loads(bench.hypr('monitors', '-j'))[0]
        rest = {}
        for win, name in zip(wins, 'rgb'):
            rest[name] = round((win['at'][0] - mon['x']) * mon['scale'])
            rest[name + '_end'] = round((win['at'][0] + win['size'][0] - mon['x']) * mon['scale'])
        if a.setup:
            bench.lua(a.setup)
        with VirtualTouchpad() as pad:
            rec = bench.Recorder(video)
            for prof in a.profiles.split(','):
                bench.lua(f'hl.dispatch(hl.dsp.focus({{ window = "address:{order[0]}" }}))')
                time.sleep(0.9)
                p = PROFILES[prof]
                down = [f'{k}:1' for k in MODS[a.mods]]
                up = [f'{k}:0' for k in reversed(MODS[a.mods])]
                if down: subprocess.run(['ydotool', 'key', *down])
                t0 = time.monotonic()
                pad.swipe(p['dx'], 0, ms=p['ms'], hold_ms=p['hold_ms'])
                if up: subprocess.run(['ydotool', 'key', *up])
                time.sleep(1.0)
                act = json.loads(bench.hypr('activewindow', '-j'))['address']
                events.append(dict(profile=prof, landed=order.index(act) if act in order else None))
            rec.stop()
        offsets = bench.track(video, rest, 'x')
    finally:
        bench.cleanup()
        bench.lua(f'hl.dispatch(hl.dsp.focus({{ workspace = "{ws_before}" }}))')
    (bench.OUT / f'{a.label}.json').write_text(json.dumps(dict(events=events, offsets=offsets)))
    print(a.label, events)

if __name__ == '__main__':
    main()
