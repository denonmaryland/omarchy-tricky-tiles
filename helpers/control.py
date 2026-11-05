#!/usr/bin/python3
"""Tricky Tiles: spring window and workspace switching for Omarchy.

Settings live in ~/.config/omarchy/tricky-tiles.json. With the user's consent,
one marked line in ~/.config/hypr/hyprland.lua loads a generated module,
~/.config/hypr/tricky-tiles.lua, which is the plugin's template (hypr/module.lua)
preceded by a table of the current settings. ~/.config is $XDG_CONFIG_HOME when set.

The panel speaks in duration and bounce (the SwiftUI convention). Hyprland 0.56's
native spring wants mass, stiffness and damping:

    stiffness = (2 * pi / duration) ** 2        (mass 1)
    damping   = 4 * pi * (1 - bounce) / duration

so the damping ratio is exactly 1 - bounce.
"""
import argparse
from collections import Counter
import fcntl
import glob
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import tempfile
import time

PLUGIN_ID = 'io.github.denonmaryland.tricky-tiles'
PLUGIN_DIR = Path(__file__).resolve().parent.parent
TEMPLATE = PLUGIN_DIR / 'hypr' / 'module.lua'


def paths(home=None):
    if home is None:
        home = Path(os.environ.get('HOME') or Path.home())
        config = home / '.config'
        xdg = os.environ.get('XDG_CONFIG_HOME', '')
        if os.path.isabs(xdg):
            config = Path(xdg)
        state = Path(os.environ.get('XDG_STATE_HOME') or home / '.local/state') / 'tricky-tiles'
    else:   # an explicit home (tests, other users) keeps everything under it
        home = Path(home)
        config = home / '.config'
        state = home / '.local/state/tricky-tiles'
    # Same search as Hyprland: $XDG_CONFIG_HOME/hypr first, then ~/.config/hypr.
    hypr_dir = config / 'hypr'
    if not (hypr_dir / 'hyprland.lua').exists() and (home / '.config/hypr/hyprland.lua').exists():
        hypr_dir = home / '.config/hypr'
    return dict(home=home, settings=config / 'omarchy/tricky-tiles.json',
                module=hypr_dir / 'tricky-tiles.lua',
                hyprland=hypr_dir / 'hyprland.lua', hypr_dir=hypr_dir, state=state,
                last_load=state / 'last-load.json')


MARK = '-- Tricky Tiles (remove via its panel)'
# Loads tricky-tiles.lua from the folder of the hyprland.lua Hyprland is running, so it
# matches whichever config folder was found and survives a dotfile sync to another home.
LOADER = ('do local d = debug.getinfo(1, "S").source:match("^@(.*/)") or os.getenv("HOME") .. "/.config/hypr/"; '
          'local p = d .. "tricky-tiles.lua"; local f = io.open(p); if f then f:close(); dofile(p) end end ' + MARK)

LIMITS = dict(duration=(150, 500), bounce=(0, 40), travel=(0, 100), sensitivity=(50, 200))
CHOICES = dict(scope=('switching', 'everything'), workspace_axis=('vertical', 'horizontal'),
               swipe_horizontal=('windows', 'workspaces', 'off'), swipe_vertical=('workspaces', 'off'),
               tracking=('follow', 'step'), power_saver=('off', 'snappy', 'reduced'), fingers=('3', '4'))
BOOLS = ('enabled', 'focus_fade', 'swipes', 'wrap', 'preview_windows')
MOTION_KEYS = ('duration', 'bounce', 'travel', 'focus_fade')
# The panel's swipe setups: both directions in one change, so one reload.
SETUPS = dict(both=('windows', 'workspaces'), side=('workspaces', 'off'), vertical=('off', 'workspaces'))
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

LAYOUTS = ('dwindle', 'scrolling', 'floating', 'master')


def layout_default_command():
    """Omarchy 4.5 keeps the default workspace layout outside the user's Hyprland files and
    reports it with this command (no arguments: print only). None on Omarchy 4.0."""
    return shutil.which('omarchy-hyprland-workspace-layout-default')


def hypr_layout(p):
    """The user's default tiling layout: Omarchy's answer where it has one (4.5), otherwise
    read from their Hyprland files (best effort)."""
    cmd = layout_default_command()
    if cmd:
        try:
            r = subprocess.run([cmd], capture_output=True, text=True, timeout=5, stdin=subprocess.DEVNULL)
            answer = r.stdout.strip().splitlines()[-1].strip() if r.returncode == 0 and r.stdout.strip() else ''
            if answer in LAYOUTS:
                return answer
        except (OSError, subprocess.SubprocessError):
            pass
    for name in ('looknfeel.lua', 'hyprland.lua'):
        try:
            text = (p['hypr_dir'] / name).read_text()
        except (OSError, UnicodeDecodeError):
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
    # Swipes only pick the workspace axis while they are on; otherwise the axis is the user's.
    if values['swipes'] and values['swipe_horizontal'] == 'workspaces':
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


def lua_str(text):
    """A Lua string literal for any text. Everything outside printable ASCII becomes a
    \\ddd byte escape, which every Lua version reads (JSON's \\uXXXX is not Lua)."""
    out = []
    for b in os.fsencode(str(text)):
        if b in b'"\\':
            out.append('\\' + chr(b))
        elif 32 <= b < 127:
            out.append(chr(b))
        else:
            out.append(f'\\{b:03d}')
    return '"' + ''.join(out) + '"'


LUA_NAME_RE = re.compile(r'^[A-Za-z_]\w*$')


def lua_value(v):
    if isinstance(v, bool):
        return 'true' if v else 'false'
    if isinstance(v, (int, float)):
        return repr(round(v, 3)) if isinstance(v, float) else str(v)
    if isinstance(v, dict):
        keys = (k if LUA_NAME_RE.match(str(k)) else f'[{lua_str(k)}]' for k in v)
        return '{ ' + ', '.join(f'{k} = {lua_value(x)}' for k, x in zip(keys, v.values())) + ' }'
    if isinstance(v, (list, tuple)):
        return '{ ' + ', '.join(lua_value(x) for x in v) + ' }'
    return lua_str(v)


def new_marker():
    """Fresh points for the module's ttLoaded curve, so a reload can prove this render ran.
    Hyprland takes curve points from -1 to 2 and `hyprctl animations` shows two decimals."""
    return [(secrets.randbelow(301) - 100) / 100 for _ in range(4)]


MARKER_RE = re.compile(r'\bloaded = \{ (-?[\d.]+), (-?[\d.]+), (-?[\d.]+), (-?[\d.]+) \}')


def module_marker(text):
    m = MARKER_RE.search(text or '')
    return [float(x) for x in m.groups()] if m else None


def module_settings(values, skip=(), marker=None):
    """Everything the Lua side needs, precomputed so the module stays simple. `skip` lists
    the swipe directions Hyprland refused at the last load (see reload_module); `marker` is
    the ttLoaded curve's points (new_marker)."""
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
        gestures=dict(enabled=values['enabled'] and values['swipes'], fingers=int(values['fingers']),
                      horizontal=values['swipe_horizontal'], vertical=values['swipe_vertical'],
                      tracking=values['tracking'], wrap=values['wrap'],
                      scale=round(1.4 * sens, 3), ws_distance=int(round(700 / sens)),
                      skip={d: True for d in skip}),
        loaded=list(marker or new_marker()))


def render(values, skip=(), marker=None):
    s = module_settings(values, skip, marker)
    head = [f'-- Generated by Tricky Tiles ({PLUGIN_ID}). Change it from the panel; edits here are replaced.',
            f'-- {s["preset"]}: {values["duration"]} ms, bounce {values["bounce"]}% '
            f'(damping ratio {1 - values["bounce"] / 100:.2f}), workspace travel {values["travel"]}%.',
            'local S = ' + lua_value(s)]
    return '\n'.join(head) + '\n' + TEMPLATE.read_text()


def stand_in(marker):
    """The module while Remove runs: it registers nothing but the ttLoaded proof."""
    return ('-- Tricky Tiles is being removed: this stand-in registers nothing but its load proof.\n'
            'local S = ' + lua_value(dict(loaded=list(marker))) + '\n'
            'hl.curve("ttLoaded", { type = "bezier", points = { { S.loaded[1], S.loaded[2] }, { S.loaded[3], S.loaded[4] } } })\n')


def module_enabled(text):
    return bool(re.search(r'^local S = \{ enabled = true\b', text or '', re.M))


def module_scope(text):
    m = re.search(r'\bscope = "(\w+)"', text or '')
    return m.group(1) if m else None


def gesture_part(text):
    """The part of a rendered module that only a reload can apply (its table nests one level)."""
    m = re.search(r'gestures = \{', text or '')
    if not m:
        return ''
    depth = 0
    for i in range(m.end() - 1, len(text)):
        depth += {'{': 1, '}': -1}.get(text[i], 0)
        if depth == 0:
            return text[m.start():i + 1]
    return text[m.start():]


def module_skip(text):
    """The swipe directions a rendered module leaves to the user's config."""
    m = re.search(r'\bskip = \{([^}]*)\}', gesture_part(text))
    return re.findall(r'(\w+) = true', m.group(1)) if m else []


# ---------------------------------------------------------------- system

def atomic(path, data, mode=None):
    # Write through symlinks so a dotfile manager's link (stow, chezmoi) stays a link.
    # Text is written as UTF-8; bytes are written exactly as given.
    path = Path(os.path.realpath(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(data if isinstance(data, bytes) else data.encode())
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


_real_hypr = hypr
LAST_ERRORS = None   # what the last reload_and_errors() saw, for this run's report


def reload_and_errors():
    """Reload and read the errors in one request. Any `hyprctl eval` or dispatch in between
    empties Hyprland's error list, and the Omarchy shell sends those all the time, so a separate
    `configerrors` call often finds nothing even though the red error bar just showed."""
    global LAST_ERRORS
    if hypr is not _real_hypr:   # tests replace hypr with a fake compositor
        hypr('reload', 'config-only')
        LAST_ERRORS = hypr('configerrors')
        return LAST_ERRORS
    out = hypr('--batch', 'reload config-only; configerrors')
    first, _, rest = out.partition('\n')
    if first.strip() != 'ok':
        raise RuntimeError(out.strip() or 'Hyprland did not reload')
    LAST_ERRORS = rest.strip('\n')
    return LAST_ERRORS


def installed(p):
    try:
        return MARK.encode() in p['hyprland'].read_bytes()
    except OSError:
        return False


def animations():
    """`hyprctl animations -j`: [leaves, bezier curves]. None when the answer cannot be read."""
    try:
        leaves, curves = json.loads(hypr('animations', '-j'))[:2]
        if isinstance(leaves, list) and isinstance(curves, list):
            return leaves, curves
    except (ValueError, TypeError, RuntimeError, OSError, subprocess.SubprocessError):
        pass   # the command that changes something reports a missing compositor itself
    return None


def same_marker(a, b):
    try:
        return a is not None and b is not None and [round(x * 100) for x in a] == [round(x * 100) for x in b]
    except TypeError:
        return False


def module_loaded(marker):
    """Whether this render of the module ran in Hyprland's current config. It sets the ttLoaded
    curve to points no earlier render used; curves outlive a reload, and a reload Hyprland refuses
    keeps the old config running, so only these points prove it. None when the answer cannot be
    read (a changed output format must not block every change)."""
    found = animations()
    if found is None or marker is None:
        return None
    try:
        curve = next((c for c in found[1] if c.get('name') == 'ttLoaded'), None)
        return curve is not None and same_marker([curve[k] for k in ('X0', 'Y0', 'X1', 'Y1')], marker)
    except (LookupError, TypeError, AttributeError, ValueError):
        return None


def module_running(text):
    """Whether Hyprland still runs this rendered module: its proof is the current ttLoaded curve
    and, with motion on, window moves still use its spring. Curves outlive reloads, so with motion
    off a later reload that stopped before the loader line cannot be seen here. None when the
    answer cannot be read."""
    proof = module_loaded(module_marker(text))
    if proof is not True or not module_enabled(text):
        return proof
    found = animations()
    if found is None:
        return None
    try:
        return any(a.get('name') == 'windowsMove' and a.get('bezier') == 'spring:ttSpring' for a in found[0])
    except (TypeError, AttributeError):
        return None


# ---------------------------------------------------------------- what Hyprland reports

def error_lines(text):
    """`hyprctl configerrors` (or `hyprctl eval`) output as a list of lines. A Lua error with a
    traceback spans several lines."""
    return [line.strip() for line in (text or '').split('\n') if line.strip()]


def config_errors():
    """What `hyprctl configerrors` lists now; [] when Hyprland cannot be asked. `hyprctl eval`
    empties the list, so after a live change it only holds that change's errors."""
    try:
        return error_lines(hypr('configerrors'))
    except Exception:
        return []


PATH_START = ' \t"\'(@'


def mentions(line, path, number=None):
    """Whether an error line points at `path` as `path:<line>` (at line `number` when given).
    Hyprland names the file in full; Lua's own messages may shorten it to "..." and its tail."""
    path = str(path)
    name = os.path.basename(path)
    for m in re.finditer(re.escape(name) + r':(\d+)', line):
        if number is not None and int(m.group(1)) != number:
            continue
        upto = line[:m.start() + len(name)]
        cut = upto.rfind('...')
        if cut >= 0 and len(upto) - cut - 3 > len(name) and path.endswith(upto[cut + 3:]):
            return True
        for start, ch in enumerate(upto):
            if ch == '/' and (start == 0 or upto[start - 1] in PATH_START):
                candidate = upto[start:]
                if candidate == path or os.path.realpath(candidate) == os.path.realpath(path):
                    return True
    return False


def loader_line(p):
    """The number of the line in hyprland.lua that loads the module, counted like Lua does."""
    try:
        raw = p['hyprland'].read_bytes()
    except OSError:
        return None
    i = raw.find(MARK.encode())
    return None if i < 0 else len(re.findall(rb'\r\n|\n\r|\r|\n', raw[:i])) + 1


# Hyprland 0.56.2 refuses a swipe that an earlier one with the same fingers and modifiers already
# covers (the same direction, its axis, or "swipe"). hl.gesture neither raises nor returns
# anything then: it only adds a config error naming the calling file and line
# (TrackpadGestures.cpp addGesture, LuaBindingsConfigRules.cpp hlGesture via configError).
REFUSAL_RE = re.compile(r'^(?P<source>.+):(?P<line>\d+): hl\.gesture: Gesture will be overshadowed by a previous '
                        r'gesture\. Previous (?P<previous>[A-Z_]+) shadows new (?P<new>[A-Z_]+)$')


def refusals(lines, p):
    """Every refused swipe in the error lines: the line, the earlier swipe's direction, the refused
    one's, and whether the refused one is ours."""
    out = []
    for line in lines:
        m = REFUSAL_RE.match(line)
        if m:
            out.append(dict(line=line, previous=m.group('previous').lower(), new=m.group('new').lower(),
                            ours=mentions(m.group('source') + ':' + m.group('line'), p['module'])))
    return out


def ours(line, p, number=None):
    """An error Tricky Tiles caused: it names the module, or the hyprland.lua line that loads it."""
    number = loader_line(p) if number is None else number
    return mentions(line, p['module']) or (number is not None and mentions(line, p['hyprland'], number))


def user_errors(lines, p):
    """The errors that are the user's own: they never block a change, the panel only mentions them."""
    number = loader_line(p)
    return [line for line in lines if not ours(line, p, number)]


def save_last_load(p, marker, skip):
    """What Hyprland refused at the last load, tied to that render, so `state` can say it later."""
    atomic(p['last_load'], json.dumps(dict(loaded=list(marker), skipped=list(skip))) + '\n', 0o644)


def last_skipped(values, p, lines):
    """Our swipe directions left to the user's config: what the last load gave up (while that
    render is still the module) and anything Hyprland refused in its current error list."""
    try:
        saved = json.loads(p['last_load'].read_text())
        text = p['module'].read_text()
        kept = saved['skipped'] if installed(p) and same_marker(saved['loaded'], module_marker(text)) else []
    except (OSError, ValueError, LookupError, TypeError):
        kept = []
    refused = [r['new'] for r in refusals(lines, p) if r['ours']]
    return [d for d in wanted(values) if d in kept or d in refused]


# ---------------------------------------------------------------- applying

def wanted(values):
    """The swipe directions the module registers, as Hyprland names them."""
    if not values['enabled'] or not values['swipes']:
        return []
    follow = values['tracking'] == 'follow'
    out = []
    if values['swipe_horizontal'] != 'off':
        out += ['horizontal'] if follow else ['left', 'right']
    if values['swipe_vertical'] != 'off':
        out += ['vertical'] if follow else ['up', 'down']
    return out


NOT_LOADED = 'Hyprland reloaded, but Tricky Tiles did not load.'


def reload_proved(p, marker, failure=NOT_LOADED):
    """Reload, then check that the render with `marker` ran in it and that no error is ours.
    Returns the error lines Hyprland reports."""
    lines = error_lines(reload_and_errors())
    proof = module_loaded(marker)
    if proof is None:   # no answer is not proof: keep every file as it was
        raise RuntimeError('Hyprland reloaded, but did not say whether Tricky Tiles loaded, so nothing was kept.')
    if proof is False:
        raise RuntimeError(failure + ' ' + ('Hyprland reports:\n' + '\n'.join(lines) if lines
                                            else 'An error earlier in your config may stop it.'))
    number = loader_line(p)
    mine = [line for line in lines if ours(line, p, number) and not REFUSAL_RE.match(line)]
    if mine:
        raise RuntimeError('Hyprland reported an error in Tricky Tiles:\n' + '\n'.join(mine))
    return lines


MAX_LOADS = 3


def reload_module(eff, p, skip=()):
    """Load the module with a reload and leave to the user's config every swipe Hyprland refuses
    next to theirs. A refusal is only a config error, so the module registers every swipe but
    `skip` (the last load's answer while the swipe settings stay the same, else nothing); the ones
    Hyprland refused, and ours that made it refuse one of the user's later swipes, go into
    gestures.skip for one more reload, which must come back without them. Returns the skipped
    directions."""
    # blamed: direction -> the refusal lines put down to ours, counted (a loop in the user's config
    # can repeat one line); innocent: how many of each line were shown not to be ours.
    skip, blamed, innocent = [d for d in wanted(eff) if d in skip], {}, Counter()
    for _ in range(MAX_LOADS):
        marker = new_marker()
        atomic(p['module'], render(eff, skip, marker), 0o644)
        lines = reload_proved(p, marker)
        found = refusals(lines, p)
        refused = [r['new'] for r in found if r['ours']]
        # The message has no finger count, so ours is only blamed for a later swipe's refusal. A
        # refusal that stays after ours stepped aside was never ours, but only that one line is
        # cleared: another refusal of the same direction may still be ours.
        listed = Counter(lines)
        stayed = Counter()
        for put in blamed.values():
            stayed |= put & listed
        innocent |= stayed
        wrong = {d for d, put in blamed.items() if not put - stayed}
        registered = [d for d in wanted(eff) if d not in skip and d not in refused]
        newly, spare = {}, Counter(innocent)
        for r in found:
            if r['ours'] or r['previous'] not in registered:
                continue
            if spare[r['line']] > 0:   # this copy is one already shown not to be ours
                spare[r['line']] -= 1
                continue
            newly.setdefault(r['previous'], Counter())[r['line']] += 1
        if not refused and not newly and not wrong:
            save_last_load(p, marker, skip)
            return skip
        for d in wrong:
            blamed.pop(d)
        for d, put in newly.items():
            blamed.setdefault(d, Counter()).update(put)
        skip = [d for d in wanted(eff) if (d in skip or d in refused or d in newly) and d not in wrong]
    raise RuntimeError('Hyprland kept refusing Tricky Tiles swipes:\n' + '\n'.join(r['line'] for r in found))


def eval_module(eff, p, skip):
    """Re-run the module in Hyprland's running Lua state: motion changes apply at once, gestures
    stay as registered. The new render must prove it ran."""
    marker = new_marker()
    atomic(p['module'], render(eff, skip, marker), 0o644)
    out = error_lines(hypr('eval', f'dofile({lua_str(p["module"])})'))
    failed = [line for line in out if line.startswith('error')]
    if failed:
        raise RuntimeError('Hyprland could not apply Tricky Tiles:\n' + '\n'.join(failed))
    if module_loaded(marker) is not True:   # no answer is not proof either
        raise RuntimeError('Hyprland did not run (or did not confirm) the new Tricky Tiles settings.')
    save_last_load(p, marker, skip)


def snapshot(*files):
    """The exact bytes and permissions of files, so a failed change can put them back."""
    saved = {}
    for path in files:
        try:
            saved[path] = (path.read_bytes(), os.stat(path).st_mode & 0o777)
        except FileNotFoundError:
            saved[path] = None
    return saved


def restore(saved):
    """Put files back as snapshot() found them; files that are already right are not rewritten."""
    for path, old in saved.items():
        if old is None:
            path.unlink(missing_ok=True)
            continue
        try:
            if path.read_bytes() == old[0]:
                continue
        except OSError:
            pass
        atomic(path, *old)


def rollback(saved, reload=True):
    """Restore files and reload so Hyprland runs them again; never mask the error that got us here."""
    try:
        restore(saved)
        if reload:
            hypr('reload', 'config-only')
    except Exception:
        pass


def apply_module(values, p, force=False):
    """Write the module and apply it: live with `hyprctl eval` when only motion changes and the
    module runs now, otherwise with a reload. Every change renders a new ttLoaded proof and checks
    it. `force` (the apply action) always reloads and asks Hyprland afresh which swipes it refuses.
    A change that leaves the module as it is reports "unchanged" only while module_running() says
    Hyprland still runs that render (or cannot say); otherwise it reloads."""
    if not installed(p):
        return 'not-installed'
    eff = effective(values, p)
    old = p['module'].read_text() if p['module'].exists() else None
    old_skip = module_skip(old)
    live = None if force or old is None else module_running(old)
    if not force and old is not None and live is not False and old == render(eff, old_skip, module_marker(old)):
        return 'unchanged'
    saved = snapshot(p['module'], p['last_load'])
    try:
        # Leaving "everything" needs a reload: only a fresh config restores the stock
        # opening, closing, layer and border animations.
        leaving_everything = module_scope(old) == 'everything' and eff['scope'] != 'everything'
        same_swipes = not force and old is not None and gesture_part(old) == gesture_part(render(eff, old_skip))
        if same_swipes and live is not False and eff['enabled'] and module_enabled(old) and not leaving_everything:
            eval_module(eff, p, old_skip)
            return 'live'
        # A swipe change or Apply asks Hyprland afresh, so directions the user has since freed are taken.
        reload_module(eff, p, old_skip if same_swipes else ())
        return 'reloaded'
    except Exception:
        rollback(saved)
        raise


def newline(raw):
    return b'\r\n' if b'\r\n' in raw else b'\r' if b'\r' in raw else b'\n'


def with_loader(raw):
    """hyprland.lua with the loader appended on a line of its own (after a blank line when the
    file ends in a newline), in the file's own line endings."""
    nl = newline(raw)
    return raw + nl + LOADER.encode() + nl


def without_loader(raw):
    """Take out exactly what with_loader() added, so remove gives back the original bytes. Older
    or hand-edited loader lines go line by line, with the blank line before them."""
    # The style is tried first, then the others: a file edited since may read differently now.
    for nl in dict.fromkeys((newline(raw), b'\r\n', b'\r', b'\n')):
        block = nl + LOADER.encode() + nl
        i = raw.rfind(block)
        if i >= 0:
            return raw[:i] + raw[i + len(block):]
    eol = rb'(?:\r\n|\r|\n)'
    return re.sub(rb'(?:(?<=[\r\n])' + eol + rb')?[^\r\n]*' + re.escape(MARK.encode()) + rb'[^\r\n]*(?:' + eol + rb'|$)',
                  b'', raw)


def install(values, p):
    """Consent given in the panel: add the module and one marked loader line."""
    if not p['hyprland'].exists():
        raise RuntimeError(f'{p["hyprland"]} was not found, so Tricky Tiles cannot load itself')
    saved = snapshot(p['hyprland'], p['module'], p['last_load'])
    eff = effective(values, p)
    try:
        atomic(p['module'], render(eff), 0o644)   # in place before the loader line can run it
        if not installed(p):
            raw, mode = saved[p['hyprland']]
            stamp = time.strftime('%Y%m%d-%H%M%S')
            atomic(p['hypr_dir'] / f'hyprland.lua.bak.{stamp}-tricky-tiles', raw, mode)
            atomic(p['hyprland'], with_loader(raw))
        reload_module(eff, p)
    except Exception:   # leave hyprland.lua and the module as they were
        rollback(saved)
        raise


def uninstall(p):
    """Remove the loader line and the module; settings stay so a reinstall remembers them.
    First a reload runs a stand-in module that registers nothing but a new proof: once that proof
    shows, Hyprland runs none of ours, so taking the line and file out changes nothing at runtime."""
    hypr('version')   # no compositor to reload: fail before touching any file
    override_file = p['state'] / 'override'
    saved = snapshot(p['hyprland'], p['module'], override_file, p['last_load'])
    try:
        if installed(p):
            marker = new_marker()
            atomic(p['module'], stand_in(marker), 0o644)
            reload_proved(p, marker, 'Hyprland did not reload, so Tricky Tiles still runs and nothing was removed.')
            raw = saved[p['hyprland']][0]
            kept = without_loader(raw)
            if kept != raw:
                atomic(p['hyprland'], kept)
        p['module'].unlink(missing_ok=True)
        override_file.unlink(missing_ok=True)
        p['last_load'].unlink(missing_ok=True)
    except Exception:   # the old module keeps running, so keep the files that load it
        rollback(saved)
        raise


# ---------------------------------------------------------------- hints
# Best effort, for the panel only: the user's own swipes, found by reading the Lua files their
# hyprland.lua loads, with file and line. What the module registers and whether a change worked
# come from Hyprland (reload_module), never from this reading, which cannot see conditions,
# custom package paths or anything decided at run time.

SIDES = dict(left=('left',), right=('right',), up=('up',), down=('down',), horizontal=('left', 'right'),
             vertical=('up', 'down'), swipe=('left', 'right', 'up', 'down'))
ALIASES = dict(l='left', r='right', u='up', t='up', top='up', d='down', b='down', bottom='down',
               horiz='horizontal', vert='vertical')
GESTURE_RE = re.compile(r'hl\.gesture\s*\(')
BLOCK_COMMENT_RE = re.compile(r'--\[(=*)\[.*?\]\1\]', re.S)
# loadfile() only compiles a file, so it is not followed.
LOAD_RE = re.compile(r'\b(require_optional\.module|require|dofile)\s*(?=[("\'])')
STRING_RE = re.compile(r'\s*(["\'])((?:\\.|(?!\1).)*)\1', re.S)
GETENV_RE = re.compile(r'os\.getenv\s*\(\s*(["\'])(\w+)\1\s*\)')
EOL_RE = re.compile(r'\r\n|\r|\n')


def lua_source(path):
    """A Lua file's text with CR, CRLF and LF line ends all read as "\\n"."""
    return EOL_RE.sub('\n', path.read_bytes().decode('utf-8', 'replace'))


def strip_comments(text):
    """Blank out Lua comments, keeping line numbers (strings holding "--" are rare in configs)."""
    text = EOL_RE.sub('\n', text)
    text = BLOCK_COMMENT_RE.sub(lambda m: '\n' * m.group(0).count('\n'), text)
    return '\n'.join(line.split('--', 1)[0] for line in text.split('\n'))


def call_body(code, start, keep_depth=2):
    """The text of a call from its opening parenthesis to the matching close, skipping strings.
    Anything nested deeper than keep_depth (the call's own table) is blanked, so an action's
    focus({ direction = "l" }) is not read as the swipe's direction."""
    depth, quote, i, out = 0, None, start, []
    while i < len(code):
        ch = code[i]
        keep = depth <= keep_depth
        if quote:
            if ch == '\\':
                out.append(code[i:i + 2] if keep else '  ')
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in '"\'':
            quote = ch
        elif ch in '({':
            depth += 1
            keep = depth <= keep_depth
        elif ch in ')}':
            depth -= 1
            if depth == 0:
                return ''.join(out) + ch
        out.append(ch if keep else ' ')
        i += 1
    return ''.join(out)


def top_level(expr):
    """expr with strings and anything in brackets blanked, so operators between them can be found."""
    out, depth, quote, escaped = [], 0, None, False
    for ch in expr:
        hidden = depth > 0 or quote is not None
        if escaped:
            escaped = False
        elif quote:
            escaped = ch == '\\'
            if ch == quote:
                quote = None
        elif ch in '"\'':
            quote, hidden = ch, True
        elif ch in '([{':
            depth, hidden = depth + 1, True
        elif ch in ')]}':
            depth, hidden = depth - 1, True
        out.append(' ' if hidden else ch)
    return ''.join(out)


def split_top(expr, pattern):
    parts, last = [], 0
    for m in re.finditer(pattern, top_level(expr)):
        parts.append(expr[last:m.start()])
        last = m.end()
    return parts + [expr[last:]]


NIL = object()


def path_expr(expr, env):
    """The value of a load call's argument built only from string literals, os.getenv() and `or`
    and `..` (as in Omarchy's bootstrap line); None for anything decided at run time."""
    expr = expr.strip()
    while expr.startswith('(') and expr.endswith(')') and not top_level(expr).strip():
        expr = expr[1:-1].strip()
    alternatives = split_top(expr, r'\bor\b')
    if len(alternatives) > 1:
        for alt in alternatives:
            value = path_expr(alt, env)
            if value is not NIL:
                return value   # None (unknown) or the first value that is set
        return NIL
    pieces = split_top(expr, r'\.\.')
    if len(pieces) > 1:
        values = [path_expr(piece, env) for piece in pieces]
        return None if any(v is None or v is NIL for v in values) else ''.join(values)
    m = STRING_RE.fullmatch(expr)
    if m:
        return re.sub(r'\\(.)', r'\1', m.group(2))
    m = GETENV_RE.fullmatch(expr)
    if m:
        value = env.get(m.group(2))
        return NIL if value is None else value
    return None


def config_sources(p):
    """The Lua files hyprland.lua probably runs, in load order (best effort): hyprland.lua, then
    every file a require() or dofile() with a fixed path reaches, found on package.path as Hyprland
    sets it (the config folder) and Omarchy's bootstrap extends it (~/.local/state, ~/.config, the
    Omarchy folder). When hyprland.lua cannot be read, every .lua file in its folder counts and the
    second value says why."""
    home, main = Path(p['home']), Path(p['hyprland'])
    folder = main.parent
    env = dict(os.environ, HOME=str(home))
    search = [str(folder / '?.lua'), str(folder / '?/init.lua')]
    files, seen = [], set()

    def first_file(*candidates):
        return next((Path(os.path.normpath(c)) for c in candidates if c and os.path.isfile(c)), None)

    def resolve(kind, name):
        if kind == 'dofile':
            return [first_file(os.path.join(home, name))]   # a relative path: from HOME, where Hyprland starts
        if name.startswith(('/', './', '../', '~/')):
            base = str(home) + name[1:] if name.startswith('~/') else os.path.join(folder, name)
            if any(c in base for c in '*?['):   # Hyprland expands these
                return [Path(os.path.normpath(f)) for f in sorted(glob.glob(base)) if os.path.isfile(f)]
            return [first_file(base, None if base.endswith('.lua') else base + '.lua', os.path.join(base, 'init.lua'))]
        rel = name.replace('.', '/')
        return [first_file(*(t.replace('?', rel) for t in search))]

    def visit(path):
        if str(path) in seen:
            return
        seen.add(str(path))
        files.append(path)
        try:
            text = lua_source(path)
        except OSError:
            if path == main:
                raise
            return   # Hyprland reports it; there is nothing to read
        code = strip_comments(text)
        for m in LOAD_RE.finditer(code):
            if code[m.end()] == '(':
                arg = call_body(code, m.end(), keep_depth=99)[1:].rsplit(')', 1)[0]
            else:
                string = STRING_RE.match(code, m.end())
                arg = string.group(0) if string else ''
            value = path_expr(arg, env)
            if not isinstance(value, str):
                continue
            for target in resolve(m.group(1), value):
                if target is None:
                    continue
                if m.group(1) == 'dofile' and target.match('default/hypr/bootstrap.lua'):
                    omarchy = target.parents[2]
                    search[:0] = [str(home / '.local/state/?.lua'), str(home / '.config/?.lua'), str(omarchy / '?.lua')]
                visit(target)

    try:
        visit(main)
        return files, None
    except (OSError, ValueError, RecursionError) as error:
        everything = [path for path in sorted(folder.glob('**/*.lua')) if path != p['module'] and '.bak' not in path.name]
        reason = error.strerror if isinstance(error, OSError) else str(error)
        return everything, f'{shown(main, home)} could not be read ({reason}), so every .lua file next to it counts'


def shown(path, home):
    """A path as the panel shows it: from HOME when under it."""
    try:
        return str(Path(path).relative_to(home))
    except ValueError:
        return str(path)


def user_swipes(p, sources=None):
    """Every swipe the user's Lua files seem to register without modifiers, with the sides it takes."""
    found = []
    for path in (sources or config_sources(p)[0]):
        if path == p['module']:
            continue
        try:
            text = lua_source(path)
        except OSError:
            continue
        lines, code = text.split('\n'), strip_comments(text)
        for m in GESTURE_RE.finditer(code):
            body = call_body(code, m.end() - 1)
            fingers = re.search(r'\bfingers\s*=\s*(\d+)\b', body)
            direction = re.search(r'\bdirection\s*=\s*["\'](\w+)["\']', body)
            mods = re.search(r'\bmods\s*=\s*(["\'])(.*?)\1', body)
            if not fingers or not direction or re.search(r'\baction\s*=\s*["\']unset["\']', body):
                continue
            if mods and mods.group(2).strip():
                continue   # a swipe with modifiers never meets ours
            name = ALIASES.get(direction.group(1).lower(), direction.group(1).lower())
            if name not in SIDES:
                continue   # pinches never meet swipes
            number = code.count('\n', 0, m.start()) + 1
            found.append(dict(file=shown(path, p['home']), line=number, text=lines[number - 1].strip()[:120],
                              fingers=fingers.group(1), direction=name, directions=list(SIDES[name])))
    return found


def conflicts_by_fingers(p, sources=None):
    by = {f: [] for f in CHOICES['fingers']}
    for swipe in user_swipes(p, sources):
        fingers = swipe.pop('fingers')
        if fingers in by:
            by[fingers].append(swipe)
    return by


def conflicts(values, p):
    """The user's own swipes with our finger count, as hints."""
    return conflicts_by_fingers(p)[values['fingers']]


# ---------------------------------------------------------------- panel

def demo():
    """Play one real window step and one workspace hop on the desktop, then come back."""
    hypr('eval', 'if TrickyTiles and TrickyTiles.demo then TrickyTiles.demo() end')


def animations_enabled():
    """Hyprland's animations:enabled (Omarchy 4.5's `omarchy toggle animations` turns it off).
    None when Hyprland cannot be asked."""
    try:
        answer = json.loads(hypr('getoption', 'animations:enabled', '-j'))
        value = answer.get('bool', answer.get('int'))
        return None if value is None else bool(value)
    except Exception:   # best effort: the panel then shows no note
        return None


def monitor_info():
    try:
        mons = json.loads(hypr('monitors', '-j'))
        mon = next((m for m in mons if m.get('focused')), mons[0] if mons else None)
        if not mon:
            return None
        # Transforms 1, 3, 5 and 7 turn the panel a quarter, so its logical width is the mode's height.
        span = mon['height'] if mon.get('transform', 0) % 2 else mon['width']
        return dict(name=mon['name'], width=round(span / mon['scale']), refresh=round(mon['refreshRate'], 1))
    except Exception:   # best effort: the panel falls back to a default width
        return None


def save(values, p):
    atomic(p['settings'], json.dumps({k: v for k, v in values.items() if k != 'preset'}, indent=2) + '\n', 0o644)


def report(ok, values, p, error=None, applied=None):
    out = dict(ok=ok, values=values, presets=PRESETS, limits=LIMITS, stock=STOCK, installed=installed(p),
               override=override(p) if values['power_saver'] != 'off' else None, code=encode(values),
               monitor=monitor_info(), layout=hypr_layout(p), animations=animations_enabled())
    # Hints: the user's own swipes per finger count, whether or not ours are on (best effort).
    sources, out['sources_error'] = config_sources(p)
    out['conflicts_by_fingers'] = conflicts_by_fingers(p, sources)
    out['conflicts'] = out['conflicts_by_fingers'][values['fingers']]
    # From Hyprland: our directions it refused at the last load, and the user's own config errors.
    # A separate configerrors call can miss what this run's reload saw (the shell's eval traffic
    # empties the list), so use what the reload itself returned when there was one.
    lines = error_lines(LAST_ERRORS) if LAST_ERRORS is not None else config_errors()
    out['skipped'] = last_skipped(values, p, lines)
    out['config_errors'] = user_errors(lines, p)
    if applied:
        out['applied'] = applied
    if error:
        out['error'] = error
    print(json.dumps(out))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('action', choices=['state', 'set', 'preset', 'setup', 'apply', 'install', 'uninstall',
                                           'import', 'override', 'demo'])
    parser.add_argument('key', nargs='?')
    parser.add_argument('value', nargs='?')
    args = parser.parse_args(argv)
    global LAST_ERRORS
    LAST_ERRORS = None   # each action reports only what its own reload saw
    p = paths()
    p['state'].mkdir(parents=True, exist_ok=True)
    with (p['state'] / 'lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        (p['state'] / 'known-errors').unlink(missing_ok=True)   # from 1.0.0 previews; no longer used
        values = read(p)
        applied = None
        # One transaction: if any stage fails, every file goes back and, if Hyprland already
        # runs the new module, a reload brings the old one back.
        files = [p['module'], p['settings'], p['state'] / 'override', p['last_load']]
        saved = {}
        try:
            saved = snapshot(*files, *([p['hyprland']] if args.action == 'install' else []))
            if args.action == 'set':
                values[args.key] = clamp(args.key, args.value)
                normalize(values)
                applied = apply_module(values, p)
                save(values, p)
            elif args.action == 'preset':
                if args.key not in PRESETS:
                    raise ValueError(f'unknown preset {args.key}; choose {", ".join(PRESETS)}')
                values.update({k: PRESETS[args.key][k] for k in MOTION_KEYS})
                normalize(values)
                applied = apply_module(values, p)
                save(values, p)
            elif args.action == 'setup':
                if args.key not in SETUPS:
                    raise ValueError(f'unknown setup {args.key}; choose {", ".join(SETUPS)}')
                values['swipe_horizontal'], values['swipe_vertical'] = SETUPS[args.key]
                normalize(values)
                applied = apply_module(values, p)
                save(values, p)
            elif args.action == 'import':
                values.update(decode(args.key))
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
                applied = apply_module(values, p, force=True)
            elif args.action == 'install':
                install(values, p)
                applied = 'installed'
                save(values, p)
            elif args.action == 'uninstall':
                uninstall(p)
                applied = 'removed'
            elif args.action == 'demo':
                demo()
        except Exception as error:  # report to the panel instead of a traceback
            rollback(saved, reload=applied in ('live', 'reloaded', 'installed'))
            # The saved settings are what is running again.
            report(False, read(p), p, error=str(error))
            raise SystemExit(1)
        report(True, values, p, applied=applied)


if __name__ == '__main__':
    main()
