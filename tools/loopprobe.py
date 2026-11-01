#!/usr/bin/python3
"""Is Hyprland's main loop blocked after a window switch? Hammer the IPC socket
with tiny requests in a thread and report the slowest round-trips around each switch."""
import json, os, socket, sys, threading, time
sys.path.insert(0, os.path.dirname(__file__))
import bench
SOCK = f"{os.environ['XDG_RUNTIME_DIR']}/hypr/{os.environ['HYPRLAND_INSTANCE_SIGNATURE']}/.socket.sock"

def ask(cmd):
    s = socket.socket(socket.AF_UNIX); s.connect(SOCK); s.sendall(cmd.encode())
    out = b''
    while chunk := s.recv(65536): out += chunk
    s.close(); return out

def hammer(log, stop):
    while not stop.is_set():
        t0 = time.monotonic(); ask('j/activeworkspace'); log.append((t0, time.monotonic() - t0))

ws_before = json.loads(bench.hypr('activeworkspace', '-j'))['id']
bench.cleanup()
try:
    bench.spawn(98, ['r', 'g', 'b'])
    for w in bench.bench_windows(98):
        bench.lua(f'hl.dispatch(hl.dsp.focus({{ window = "address:{w["address"]}" }}))'); time.sleep(0.05)
        bench.lua('hl.dispatch(hl.dsp.layout("colresize 1"))'); time.sleep(0.1)
    bench.lua(f'hl.dispatch(hl.dsp.focus({{ window = "address:{bench.bench_windows(98)[0]["address"]}" }}))')
    for label, cmd in [('focus step', 'TrickyTiles.step("{}")'), ('viewport  ', 'hl.dispatch(hl.dsp.layout("move {}"))')]:
        for d in ('r', 'l', 'r', 'l'):
            time.sleep(1.0)
            log, stop = [], threading.Event()
            th = threading.Thread(target=hammer, args=(log, stop)); th.start(); time.sleep(0.1)
            arg = d if 'focus' in label else ('-1880' if d == 'r' else '1880')
            t_sw = time.monotonic(); ask('eval ' + cmd.format(arg)); t_done = time.monotonic()
            time.sleep(0.25); stop.set(); th.join()
            after = [(round((t - t_sw) * 1000), round(lat * 1000, 1)) for t, lat in log if t >= t_sw - 0.005]
            worst = sorted(after, key=lambda x: -x[1])[:3]
            base = sorted(lat for t, lat in log if t < t_sw - 0.01)
            print(f'{label} eval took {round((t_done - t_sw) * 1000, 1)} ms; idle median {round(base[len(base)//2]*1000, 2)} ms; slowest after (start ms, latency ms): {worst}')
finally:
    bench.cleanup()
    bench.lua(f'hl.dispatch(hl.dsp.focus({{ workspace = "{ws_before}" }}))')
