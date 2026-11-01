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
import fcntl
import glob
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
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
                hyprland=hypr_dir / 'hyprland.lua', hypr_dir=hypr_dir, state=state)


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
    the swipe directions the user's own config already takes (see conflicts); `marker` is
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
        gestures=dict(enabled=values['swipes'], fingers=int(values['fingers']),
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


def installed(p):
    try:
        return MARK.encode() in p['hyprland'].read_bytes()
    except OSError:
        return False


def animations():
    """`hyprctl animations -j`: [leaves, bezier curves]. Read-only (`hyprctl eval` would also
    clear Hyprland's error list). None when the answer cannot be read."""
    try:
        leaves, curves = json.loads(hypr('animations', '-j'))[:2]
        if isinstance(leaves, list) and isinstance(curves, list):
            return leaves, curves
    except (ValueError, TypeError):
        pass
    return None


def module_loaded(marker):
    """Whether this render of the module ran in Hyprland's current config. It sets the ttLoaded
    curve to points no earlier render used; curves outlive a reload, and a reload whose
    hyprland.lua does not parse keeps the old config running, so only these points prove it.
    None when the answer cannot be read (a changed output format must not block every change)."""
    found = animations()
    if found is None or marker is None:
        return None
    try:
        curve = next((c for c in found[1] if c.get('name') == 'ttLoaded'), None)
        return curve is not None and \
            [round(curve[k] * 100) for k in ('X0', 'Y0', 'X1', 'Y1')] == [round(v * 100) for v in marker]
    except (LookupError, TypeError, AttributeError, ValueError):
        return None


OUR_CURVES = ('spring:ttSpring', 'ttFade', 'ttEnter', 'ttExit')


def module_gone():
    """Whether no animation still uses the module's curves. A reload resets every animation,
    so one left on ours means Hyprland kept the old config. None when it cannot be read."""
    found = animations()
    if found is None:
        return None
    try:
        return not any(a.get('bezier') in OUR_CURVES for a in found[0])
    except (TypeError, AttributeError):
        return None


# A reload whose hyprland.lua does not parse changes nothing and leaves just this error, with no
# traceback (Lua shortens a long path with "...").
SYNTAX_RE = re.compile(r'^(?:\S*/|\.\.\.)?hyprland\.lua:\d+: ')


def kept_old_config(errors):
    lines = error_lines(errors)
    return len(lines) == 1 and bool(SYNTAX_RE.match(next(iter(lines))))


def error_lines(text):
    return {line.strip() for line in (text or '').split('\n') if line.strip()}


def config_fingerprint(p):
    """A hash of the config Hyprland loads (finding the files like user_swipes does), without our
    module and loader line, so remembered errors only count for the config that had them."""
    digest = hashlib.sha256()
    for source in config_sources(p)[0]:
        path = source['path']
        if path == p['module']:
            continue
        try:
            data = path.read_bytes()
        except OSError:
            data = b'(unreadable)'
        if path == p['hyprland']:
            data = without_loader(data)
        digest.update(os.fsencode(str(path)) + b'\0' + hashlib.sha256(data).digest())
    return digest.hexdigest()


def remembered_errors(known, fingerprint):
    try:
        saved = json.loads(known.read_text())
        if saved.get('config') == fingerprint:
            return error_lines(saved.get('errors'))
    except (OSError, ValueError, AttributeError):
        pass
    return set()   # another config (or an older format): forget them


def reload_checked(loads=False, p=None):
    """Reload and fail on any error the reload added. With `loads` the module must have run in
    this reload; without, none of it may still run."""
    # `hyprctl eval` (live changes) empties Hyprland's error list, so errors the config already
    # had are remembered from the last good reload and are not blamed on Tricky Tiles, as long as
    # the config is still the one that had them.
    known = p['state'] / 'known-errors' if p else None
    fingerprint = config_fingerprint(p) if p else None
    before = error_lines(hypr('configerrors'))
    if known:
        before |= remembered_errors(known, fingerprint)
    hypr('reload', 'config-only')
    errors = hypr('configerrors')
    if error_lines(errors) - before:
        raise RuntimeError(errors)
    # An error that was already in the config can stop Lua before the loader line runs, or stop
    # the reload altogether, so check what Hyprland really runs now.
    if loads:
        marker = module_marker(p['module'].read_text()) if p and p['module'].exists() else None
        if module_loaded(marker) is False:
            raise RuntimeError('Hyprland reloaded, but Tricky Tiles did not load. An error earlier in your '
                               'config may stop it' + (': ' + errors if errors else '.'))
    elif p and (kept_old_config(errors) or module_gone() is False):
        raise RuntimeError('Hyprland kept running Tricky Tiles: your config did not reload'
                           + (': ' + errors if errors else '.'))
    if known:
        try:
            atomic(known, json.dumps(dict(config=fingerprint, errors=errors)) + '\n')
        except OSError:
            pass


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


def apply_module(values, p):
    """Write the module and apply it live; only gesture changes and turning off need a reload."""
    if not installed(p):
        return 'not-installed'
    eff = effective(values, p)
    old = p['module'].read_text() if p['module'].exists() else None
    skip = skipped(eff, conflicts(eff, p))
    if old is not None and old == render(eff, skip, module_marker(old)):
        return 'unchanged'
    new = render(eff, skip)
    saved = snapshot(p['module'])
    atomic(p['module'], new, 0o644)
    try:
        # Leaving "everything" needs a reload: only a fresh config restores the stock
        # opening, closing, layer and border animations.
        leaving_everything = module_scope(old) == 'everything' and eff['scope'] != 'everything'
        if eff['enabled'] and module_enabled(old) and gesture_part(old) == gesture_part(new) \
                and not leaving_everything:
            hypr('eval', f'dofile({lua_str(p["module"])})')
            return 'live'
        reload_checked(loads=True, p=p)
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
    saved = snapshot(p['hyprland'], p['module'])
    eff = effective(values, p)
    atomic(p['module'], render(eff, skipped(eff, conflicts(eff, p))), 0o644)
    try:
        if not installed(p):
            raw, mode = saved[p['hyprland']]
            stamp = time.strftime('%Y%m%d-%H%M%S')
            atomic(p['hypr_dir'] / f'hyprland.lua.bak.{stamp}-tricky-tiles', raw, mode)
            atomic(p['hyprland'], with_loader(raw))
        reload_checked(loads=True, p=p)
    except Exception:   # leave hyprland.lua and the module as they were
        rollback(saved)
        raise


def uninstall(p):
    """Remove the loader line and the module; settings stay so a reinstall remembers them."""
    hypr('version')   # no compositor to reload: fail before touching any file
    override_file = p['state'] / 'override'
    saved = snapshot(p['hyprland'], p['module'], override_file)
    try:
        if saved[p['hyprland']] is not None:
            raw = saved[p['hyprland']][0]
            kept = without_loader(raw)
            if kept != raw:
                atomic(p['hyprland'], kept)
        p['module'].unlink(missing_ok=True)
        override_file.unlink(missing_ok=True)
        reload_checked(p=p)
    except Exception:   # the old animations keep running, so keep the files that load them
        rollback(saved)
        raise


# Hyprland 0.56 refuses a new swipe when an earlier one with the same fingers and modifiers
# has the same direction, is the axis that direction lies on, or is "swipe" ("overshadowed", a
# config error); at swipe time the first match wins (TrackpadGestures.cpp). So an earlier "left"
# leaves our "horizontal" alone (left swipes stay theirs, right ones are ours), while an earlier
# "horizontal" takes left, right and horizontal. The module loads after the user's config, so it
# skips what their earlier swipes take, and what would take a swipe they register after it.
SIDES = dict(left=('left',), right=('right',), up=('up',), down=('down',), horizontal=('left', 'right'),
             vertical=('up', 'down'), swipe=('left', 'right', 'up', 'down'))
AXIS = dict(left='horizontal', right='horizontal', horizontal='horizontal', up='vertical', down='vertical',
            vertical='vertical', swipe='swipe')
ALIASES = dict(l='left', r='right', u='up', t='up', top='up', d='down', b='down', bottom='down',
               horiz='horizontal', vert='vertical')
GESTURE_RE = re.compile(r'hl\.gesture\s*\(')
BLOCK_COMMENT_RE = re.compile(r'--\[(=*)\[.*?\]\1\]', re.S)
LOAD_RE = re.compile(r'\b(require_optional\.module|require|dofile|loadfile)\s*(?=[("\'])')
STRING_RE = re.compile(r'\s*(["\'])((?:\\.|(?!\1).)*)\1', re.S)
GETENV_RE = re.compile(r'os\.getenv\s*\(\s*(["\'])(\w+)\1\s*\)')


def blocks(earlier, later):
    """Whether Hyprland refuses the later of two swipes with the same fingers (see AXIS)."""
    return earlier in (later, AXIS[later], 'swipe')


def strip_comments(text):
    """Blank out Lua comments, keeping line numbers (strings holding "--" are rare in configs)."""
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
    """The Lua files Hyprland runs, in load order: hyprland.lua, then every file a require() or
    dofile() with a fixed path reaches, found on package.path as Hyprland sets it (the config
    folder) and Omarchy's bootstrap extends it (~/.local/state, ~/.config, the Omarchy folder).
    Each comes with whether it loads after the Tricky Tiles line, and hyprland.lua with that
    line's number. When hyprland.lua cannot be read, every .lua file in its folder counts and
    the second value says why."""
    home, main = Path(p['home']), Path(p['hyprland'])
    folder = main.parent
    env = dict(os.environ, HOME=str(home))
    search = [str(folder / '?.lua'), str(folder / '?/init.lua')]
    files, seen = [], set()

    def first_file(*candidates):
        return next((Path(os.path.normpath(c)) for c in candidates if c and os.path.isfile(c)), None)

    def resolve(kind, name):
        if kind in ('dofile', 'loadfile'):
            return [first_file(os.path.join(home, name))]   # a relative path: from HOME, where Hyprland starts
        if name.startswith(('/', './', '../', '~/')):
            base = str(home) + name[1:] if name.startswith('~/') else os.path.join(folder, name)
            if any(c in base for c in '*?['):   # Hyprland expands these
                return [Path(os.path.normpath(f)) for f in sorted(glob.glob(base)) if os.path.isfile(f)]
            return [first_file(base, None if base.endswith('.lua') else base + '.lua', os.path.join(base, 'init.lua'))]
        rel = name.replace('.', '/')
        return [first_file(*(t.replace('?', rel) for t in search))]

    def visit(path, late):
        if str(path) in seen:
            return
        seen.add(str(path))
        entry = dict(path=path, late=late, cut=None)
        files.append(entry)
        try:
            text = path.read_bytes().decode('utf-8', 'replace')
        except OSError:
            if path == main:
                raise
            return   # Hyprland reports it; there is nothing to read
        if path == main:
            entry['cut'] = next((i for i, line in enumerate(text.split('\n'), 1) if MARK in line), None)
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
            after = late or (entry['cut'] is not None and code.count('\n', 0, m.start()) + 1 > entry['cut'])
            for target in resolve(m.group(1), value):
                if target is None:
                    continue
                if m.group(1) == 'dofile' and target.match('default/hypr/bootstrap.lua'):
                    omarchy = target.parents[2]
                    search[:0] = [str(home / '.local/state/?.lua'), str(home / '.config/?.lua'), str(omarchy / '?.lua')]
                visit(target, after)

    try:
        visit(main, False)
        return files, None
    except (OSError, ValueError, RecursionError) as error:
        everything = [dict(path=path, late=False, cut=None) for path in sorted(folder.glob('**/*.lua'))
                      if path != p['module'] and '.bak' not in path.name]
        reason = error.strerror if isinstance(error, OSError) else str(error)
        return everything, f'{shown(main, home)} could not be read ({reason}), so every .lua file next to it counts'


def shown(path, home):
    """A path as the panel shows it: from HOME when under it."""
    try:
        return str(Path(path).relative_to(home))
    except ValueError:
        return str(path)


def user_swipes(p):
    """Every swipe the user's loaded Lua files register without modifiers: the sides it takes,
    and whether it registers after the module (its loader line comes first)."""
    found = []
    for source in config_sources(p)[0]:
        path = source['path']
        if path == p['module']:
            continue
        try:
            text = path.read_bytes().decode('utf-8', 'replace')
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
                              fingers=fingers.group(1), direction=name, directions=list(SIDES[name]),
                              after=source['late'] or (source['cut'] is not None and number > source['cut'])))
    return found


def conflicts_by_fingers(p):
    by = {f: [] for f in CHOICES['fingers']}
    for swipe in user_swipes(p):
        fingers = swipe.pop('fingers')
        if fingers in by:
            by[fingers].append(swipe)
    return by


def conflicts(values, p):
    """The user's own swipes with our finger count; skipped() says which of ours they take."""
    return conflicts_by_fingers(p)[values['fingers']]


def wanted(values):
    """The swipe directions the module registers, as Hyprland names them."""
    if not values['swipes']:
        return []
    follow = values['tracking'] == 'follow'
    out = []
    if values['swipe_horizontal'] != 'off':
        out += ['horizontal'] if follow else ['left', 'right']
    if values['swipe_vertical'] != 'off':
        out += ['vertical'] if follow else ['up', 'down']
    return out


def skipped(values, found):
    """Our swipe directions Hyprland would refuse next to the user's (see AXIS), or that would
    make it refuse one of theirs that loads after ours."""
    return [d for d in wanted(values)
            if any(blocks(d, s['direction']) if s.get('after') else blocks(s['direction'], d) for s in found)]


def demo():
    """Play one real window step and one workspace hop on the desktop, then come back."""
    hypr('eval', 'if TrickyTiles and TrickyTiles.demo then TrickyTiles.demo() end')


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
               monitor=monitor_info(), layout=hypr_layout(p))
    # The user's own swipes per finger count, whether or not ours are on, and which of our
    # directions the module leaves to them.
    out['conflicts_by_fingers'] = conflicts_by_fingers(p)
    out['conflicts'] = out['conflicts_by_fingers'][values['fingers']]
    out['skipped'] = skipped(values, out['conflicts'])
    # Set when hyprland.lua could not be followed and every .lua file was checked instead.
    out['sources_error'] = config_sources(p)[1]
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
        # One transaction: if any stage fails, every file goes back and, if Hyprland already
        # runs the new module, a reload brings the old one back.
        files = [p['module'], p['settings'], p['state'] / 'override']
        saved = {}
        try:
            saved = snapshot(*files, *([p['hyprland']] if args.action == 'install' else []))
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
