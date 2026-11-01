#!/usr/bin/python3
"""Tricky Tiles: spring window and workspace switching for Omarchy.

Settings live in ~/.config/omarchy/tricky-tiles.json. With the user's consent,
one marked line in ~/.config/hypr/hyprland.lua loads a generated module,
~/.config/hypr/tricky-tiles.lua, which is the plugin's template (hypr/module.lua)
preceded by a table of the current settings.

The panel speaks in duration and bounce (the SwiftUI convention). Hyprland 0.56's
native spring wants mass, stiffness and damping:

    stiffness = (2 * pi / duration) ** 2        (mass 1)
    damping   = 4 * pi * (1 - bounce) / duration

so the damping ratio is exactly 1 - bounce.
"""
import argparse
import fcntl
import json
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time

PLUGIN_ID = 'io.github.denonmaryland.tricky-tiles'
PLUGIN_DIR = Path(__file__).resolve().parent.parent
TEMPLATE = PLUGIN_DIR / 'hypr' / 'module.lua'


def paths(home=None):
    if home is None:
        home = Path(os.environ.get('HOME') or Path.home())
        state = Path(os.environ.get('XDG_STATE_HOME') or home / '.local/state') / 'tricky-tiles'
    else:   # an explicit home (tests, other users) keeps everything under it
        home = Path(home)
        state = home / '.local/state/tricky-tiles'
    return dict(home=home, settings=home / '.config/omarchy/tricky-tiles.json',
                module=home / '.config/hypr/tricky-tiles.lua',
                hyprland=home / '.config/hypr/hyprland.lua', hypr_dir=home / '.config/hypr', state=state)


MARK = '-- Tricky Tiles (remove via its panel)'
LOADER = ('do local p = os.getenv("HOME") .. "/.config/hypr/tricky-tiles.lua"; local f = io.open(p); '
          'if f then f:close(); dofile(p) end end ' + MARK)

LIMITS = dict(duration=(150, 500), bounce=(0, 40), travel=(0, 100), sensitivity=(50, 200))
CHOICES = dict(scope=('switching', 'everything'), workspace_axis=('vertical', 'horizontal'),
               swipe_horizontal=('windows', 'workspaces', 'off'), swipe_vertical=('workspaces', 'off'),
               tracking=('follow', 'step'), power_saver=('off', 'snappy', 'reduced'), fingers=('3', '4'))
BOOLS = ('enabled', 'focus_fade', 'swipes', 'wrap', 'preview_windows')
MOTION_KEYS = ('duration', 'bounce', 'travel', 'focus_fade')
GESTURE_KEYS = ('swipes', 'swipe_horizontal', 'swipe_vertical', 'tracking', 'wrap', 'sensitivity', 'fingers')

PRESETS = {
    'spring': dict(label='Spring', duration=290, bounce=15, travel=30, focus_fade=True,
                   caption='Soft settle, a short lift between workspaces'),
    'glide': dict(label='Glide', duration=330, bounce=0, travel=100, focus_fade=True,
                  caption='No overshoot, full workspace slide'),
    'snappy': dict(label='Snappy', duration=210, bounce=5, travel=15, focus_fade=True,
                   caption='Quick and crisp'),
    'lively': dict(label='Lively', duration=360, bounce=25, travel=45, focus_fade=True,
                   caption='A visible bounce that settles'),
    'reduced': dict(label='Reduced', duration=170, bounce=0, travel=0, focus_fade=False,
                    caption='Least motion: short moves, workspaces crossfade'),
}
DEFAULT_PRESET = 'spring'
# Stock Omarchy, for the panel's hold-to-compare preview.
STOCK = dict(window_bezier=[0.23, 1, 0.32, 1], window_ms=379, workspaces=False)

CODE_RE = re.compile(r'^TT1-(\d{2,3})-(\d{1,2})-(\d{1,3})-([01])$')


# ---------------------------------------------------------------- values

def hypr_layout(p):
    """The user's default tiling layout, read from their Hyprland files (best effort)."""
    for name in ('looknfeel.lua', 'hyprland.lua'):
        try:
            text = (p['hypr_dir'] / name).read_text()
        except OSError:
            continue
        m = re.findall(r'^[^-\n]*\blayout\s*=\s*"(\w+)"', text, re.M)
        if m:
            return m[-1]
    return 'dwindle'


def fresh(p=None):
    preset = PRESETS[DEFAULT_PRESET]
    scrolling = hypr_layout(p) == 'scrolling' if p else False
    return dict(enabled=True, preset=DEFAULT_PRESET, **{k: preset[k] for k in MOTION_KEYS},
                scope='switching', workspace_axis='vertical' if scrolling else 'horizontal',
                swipes=False, swipe_horizontal='windows' if scrolling else 'workspaces',
                swipe_vertical='workspaces' if scrolling else 'off', tracking='follow', wrap=False,
                sensitivity=100, power_saver='off', fingers='3', preview_windows=True)


def clamp(key, value):
    if key in BOOLS:
        return value if isinstance(value, bool) else str(value).lower() in ('1', 'true', 'on', 'yes')
    if key in CHOICES:
        value = str(value)
        if value not in CHOICES[key]:
            raise ValueError(f'{key} must be one of {", ".join(CHOICES[key])}')
        return value
    if key in LIMITS:
        lo, hi = LIMITS[key]
        return int(round(max(lo, min(hi, float(value)))))
    raise ValueError(f'unknown setting {key}')


def matching_preset(values):
    for name, preset in PRESETS.items():
        if all(values[k] == preset[k] for k in MOTION_KEYS):
            return name
    return 'custom'


def normalize(values):
    """Keep combinations that cannot work together out of the saved settings."""
    if values['swipe_horizontal'] == 'workspaces':
        values['swipe_vertical'] = 'off'
        values['workspace_axis'] = 'horizontal'
    elif values['swipes'] and values['swipe_vertical'] == 'workspaces':
        values['workspace_axis'] = 'vertical'
    values['preset'] = matching_preset(values)
    return values


def read(p):
    values = fresh(p)
    try:
        saved = json.loads(p['settings'].read_text())
    except (OSError, ValueError):
        saved = {}
    if isinstance(saved, dict):
        for key, value in saved.items():
            if key in values and key != 'preset':
                try:
                    values[key] = clamp(key, value)
                except (TypeError, ValueError):
                    pass
    return normalize(values)


def override(p):
    try:
        name = (p['state'] / 'override').read_text().strip()
    except OSError:
        return None
    return name if name in PRESETS else None


def effective(values, p):
    """What Hyprland runs: the saved values, or the power-saver preset while it is active."""
    out = dict(values)
    name = override(p) if values['power_saver'] != 'off' else None
    if name:
        out.update({k: PRESETS[name][k] for k in MOTION_KEYS}, preset=name + ' (power saver)')
    return out


# ---------------------------------------------------------------- codes

def encode(values):
    return f'TT1-{values["duration"]}-{values["bounce"]}-{values["travel"]}-{int(values["focus_fade"])}'


def decode(code):
    m = CODE_RE.match(str(code).strip())
    if not m:
        raise ValueError('not a Tricky Tiles code (expected something like TT1-290-15-30-1)')
    d, b, t, f = m.groups()
    return dict(duration=clamp('duration', d), bounce=clamp('bounce', b), travel=clamp('travel', t),
                focus_fade=f == '1')


# ---------------------------------------------------------------- module

def spring(values):
    duration = values['duration'] / 1000
    stiffness = (2 * math.pi / duration) ** 2
    damping = 4 * math.pi * (1 - values['bounce'] / 100) / duration
    return stiffness, damping


def workspace_style(travel, axis):
    vert = 'vert' if axis == 'vertical' else ''
    if travel <= 0:
        return 'fade'
    if travel >= 100:
        return 'slide' + vert
    return f'slidefade{vert} {travel}%'


def lua_value(v):
    if isinstance(v, bool):
        return 'true' if v else 'false'
    if isinstance(v, (int, float)):
        return repr(round(v, 3)) if isinstance(v, float) else str(v)
    if isinstance(v, dict):
        return '{ ' + ', '.join(f'{k} = {lua_value(x)}' for k, x in v.items()) + ' }'
    return json.dumps(str(v))


def module_settings(values):
    """Everything the Lua side needs, precomputed so the module stays simple."""
    stiffness, damping = spring(values)
    dur = values['duration'] / 100   # Hyprland speeds are in tenths of a second
    sens = values['sensitivity'] / 100
    return dict(
        enabled=values['enabled'], preset=values['preset'], scope=values['scope'],
        stiffness=stiffness, damping=damping, speed=round(dur, 2),
        fade_speed=round(max(1.2, dur * 0.8), 2), enter_speed=round(max(1.2, dur * 0.75), 2),
        exit_speed=round(max(1.0, dur * 0.5), 2), focus_fade=values['focus_fade'],
        ws_style=workspace_style(values['travel'], values['workspace_axis']),
        special_style='fade' if values['travel'] <= 0 else 'slidevert',
        gestures=dict(enabled=values['swipes'], fingers=int(values['fingers']),
                      horizontal=values['swipe_horizontal'], vertical=values['swipe_vertical'],
                      tracking=values['tracking'], wrap=values['wrap'],
                      scale=round(1.4 * sens, 3), ws_distance=int(round(700 / sens))))


def render(values):
    s = module_settings(values)
    head = [f'-- Generated by Tricky Tiles ({PLUGIN_ID}). Change it from the panel; edits here are replaced.',
            f'-- {s["preset"]}: {values["duration"]} ms, bounce {values["bounce"]}% '
            f'(damping ratio {1 - values["bounce"] / 100:.2f}), workspace travel {values["travel"]}%.',
            'local S = ' + lua_value(s)]
    return '\n'.join(head) + '\n' + TEMPLATE.read_text()


def module_enabled(text):
    return bool(re.search(r'^local S = \{ enabled = true\b', text or '', re.M))


def module_scope(text):
    m = re.search(r'\bscope = "(\w+)"', text or '')
    return m.group(1) if m else None


def gesture_part(text):
    """The part of a rendered module that only a reload can apply."""
    m = re.search(r'gestures = \{[^}]*\}', text or '')
    return m.group(0) if m else ''


# ---------------------------------------------------------------- system

def atomic(path, text, mode=None):
    # Write through symlinks so a dotfile manager's link (stow, chezmoi) stays a link.
    path = Path(os.path.realpath(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as out:
            out.write(text)
        if mode is not None:
            os.chmod(tmp, mode)
        elif path.exists():
            os.chmod(tmp, path.stat().st_mode & 0o777)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def hypr(*args):
    r = subprocess.run(['hyprctl', *args], capture_output=True, text=True, timeout=15)
    if r.returncode:
        raise RuntimeError(r.stderr.strip() or r.stdout.strip() or 'Hyprland command failed')
    return r.stdout.strip()


def installed(p):
    try:
        return MARK in p['hyprland'].read_text()
    except OSError:
        return False


def reload_checked():
    before = hypr('configerrors')
    hypr('reload', 'config-only')
    errors = hypr('configerrors')
    if errors and errors != before:
        raise RuntimeError(errors)


def restore_module(p, old):
    """Put the previous module back and reload; never mask the error that got us here."""
    if old is not None:
        atomic(p['module'], old)
    else:
        p['module'].unlink(missing_ok=True)
    try:
        hypr('reload', 'config-only')
    except Exception:
        pass


def apply_module(values, p):
    """Write the module and apply it live; only gesture changes and turning off need a reload."""
    if not installed(p):
        return 'not-installed'
    eff = effective(values, p)
    old = p['module'].read_text() if p['module'].exists() else None
    new = render(eff)
    if old == new:
        return 'unchanged'
    atomic(p['module'], new, 0o644)
    try:
        # Leaving "everything" needs a reload: only a fresh config restores the stock
        # opening, closing, layer and border animations.
        leaving_everything = module_scope(old) == 'everything' and eff['scope'] != 'everything'
        if eff['enabled'] and module_enabled(old) and gesture_part(old) == gesture_part(new) \
                and not leaving_everything:
            hypr('eval', f'dofile("{p["module"]}")')
            return 'live'
        reload_checked()
        return 'reloaded'
    except Exception:
        restore_module(p, old)
        raise


def install(values, p):
    """Consent given in the panel: add the module and one marked loader line."""
    if not p['hyprland'].exists():
        raise RuntimeError('~/.config/hypr/hyprland.lua was not found, so Tricky Tiles cannot load itself')
    old_module = p['module'].read_text() if p['module'].exists() else None
    original = None
    atomic(p['module'], render(effective(values, p)), 0o644)
    try:
        if not installed(p):
            text = p['hyprland'].read_text()
            stamp = time.strftime('%Y%m%d-%H%M%S')
            backup = p['hypr_dir'] / f'hyprland.lua.bak.{stamp}-tricky-tiles'
            atomic(backup, text, Path(os.path.realpath(p['hyprland'])).stat().st_mode & 0o777)
            original = text
            atomic(p['hyprland'], text.rstrip('\n') + '\n\n' + LOADER + '\n')
        reload_checked()
    except Exception:   # leave hyprland.lua and the module as they were
        if original is not None:
            atomic(p['hyprland'], original)
        restore_module(p, old_module)
        raise


def uninstall(p):
    """Remove the loader line and the module; settings stay so a reinstall remembers them."""
    if p['hyprland'].exists():
        lines = p['hyprland'].read_text().split('\n')
        kept = [line for line in lines if MARK not in line]
        while len(kept) > 1 and kept[-1] == '' and kept[-2] == '':
            kept.pop()
        if len(kept) != len(lines):
            atomic(p['hyprland'], '\n'.join(kept))
    p['module'].unlink(missing_ok=True)
    (p['state'] / 'override').unlink(missing_ok=True)
    reload_checked()


GESTURE_RE = re.compile(r'hl\.gesture\s*\(')
BLOCK_COMMENT_RE = re.compile(r'--\[(=*)\[.*?\]\1\]', re.S)


def strip_comments(text):
    """Blank out Lua comments, keeping line numbers (strings holding "--" are rare in configs)."""
    text = BLOCK_COMMENT_RE.sub(lambda m: '\n' * m.group(0).count('\n'), text)
    return '\n'.join(line.split('--', 1)[0] for line in text.split('\n'))


def call_body(code, start):
    """The text of a call from its opening parenthesis to the matching close, skipping strings."""
    depth, quote, i = 0, None, start
    while i < len(code):
        ch = code[i]
        if quote:
            if ch == '\\':
                i += 1
            elif ch == quote:
                quote = None
        elif ch in '"\'':
            quote = ch
        elif ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
            if depth == 0:
                return code[start:i + 1]
        i += 1
    return code[start:]


def conflicts(values, p):
    """Three-finger (or four-finger) swipes registered elsewhere win over ours (first match wins)."""
    found = []
    fingers = values['fingers']
    for path in sorted(p['hypr_dir'].glob('**/*.lua')):
        if path == p['module'] or '.bak' in path.name:
            continue
        try:
            text = path.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        lines, code = text.split('\n'), strip_comments(text)
        for m in GESTURE_RE.finditer(code):
            body = call_body(code, m.end() - 1)
            if re.search(r'fingers\s*=\s*' + fingers + r'\b', body) and \
               re.search(r'direction\s*=\s*["\'](horizontal|vertical|left|right|up|down)["\']', body):
                number = code.count('\n', 0, m.start()) + 1
                found.append(dict(file=str(path.relative_to(p['home'])), line=number,
                                  text=lines[number - 1].strip()[:120]))
    return found


def demo():
    """Play one real window step and one workspace hop on the desktop, then come back."""
    hypr('eval', 'if TrickyTiles and TrickyTiles.demo then TrickyTiles.demo() end')


def monitor_info():
    try:
        mons = json.loads(hypr('monitors', '-j'))
    except Exception:
        return None
    mon = next((m for m in mons if m.get('focused')), mons[0] if mons else None)
    if not mon:
        return None
    return dict(name=mon['name'], width=round(mon['width'] / mon['scale']), refresh=round(mon['refreshRate'], 1))


def save(values, p):
    atomic(p['settings'], json.dumps({k: v for k, v in values.items() if k != 'preset'}, indent=2) + '\n', 0o644)


def report(ok, values, p, error=None, applied=None):
    out = dict(ok=ok, values=values, presets=PRESETS, limits=LIMITS, stock=STOCK, installed=installed(p),
               override=override(p) if values['power_saver'] != 'off' else None, code=encode(values),
               conflicts=conflicts(values, p) if values['swipes'] else [], monitor=monitor_info(),
               layout=hypr_layout(p))
    if applied:
        out['applied'] = applied
    if error:
        out['error'] = error
    print(json.dumps(out))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('action', choices=['state', 'set', 'preset', 'apply', 'install', 'uninstall',
                                           'import', 'override', 'demo'])
    parser.add_argument('key', nargs='?')
    parser.add_argument('value', nargs='?')
    args = parser.parse_args(argv)
    p = paths()
    p['state'].mkdir(parents=True, exist_ok=True)
    with (p['state'] / 'lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        values = read(p)
        applied = None
        try:
            if args.action == 'set':
                values[args.key] = clamp(args.key, args.value)
                if args.key in MOTION_KEYS:
                    values['enabled'] = True
                normalize(values)
                applied = apply_module(values, p)
                save(values, p)
            elif args.action == 'preset':
                if args.key not in PRESETS:
                    raise ValueError(f'unknown preset {args.key}; choose {", ".join(PRESETS)}')
                values.update({k: PRESETS[args.key][k] for k in MOTION_KEYS}, enabled=True)
                normalize(values)
                applied = apply_module(values, p)
                save(values, p)
            elif args.action == 'import':
                values.update(decode(args.key), enabled=True)
                normalize(values)
                applied = apply_module(values, p)
                save(values, p)
            elif args.action == 'override':
                name = args.key if args.key in PRESETS else None
                if name:
                    atomic(p['state'] / 'override', name + '\n')
                else:
                    (p['state'] / 'override').unlink(missing_ok=True)
                applied = apply_module(values, p)
            elif args.action == 'apply':
                applied = apply_module(values, p)
            elif args.action == 'install':
                install(values, p)
                save(values, p)
                applied = 'installed'
            elif args.action == 'uninstall':
                uninstall(p)
                applied = 'removed'
            elif args.action == 'demo':
                demo()
            report(True, values, p, applied=applied)
        except Exception as error:  # report to the panel instead of a traceback
            # Settings are only saved once applied, so the saved ones are what is running.
            report(False, read(p), p, error=str(error))
            raise SystemExit(1)


if __name__ == '__main__':
    main()
