"""Unit tests for helpers/control.py and the Lua module it renders (no Hyprland needed)."""
import contextlib
import io
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'helpers'))
import control  # noqa: E402


BOOTSTRAP = 'dofile((os.getenv("OMARCHY_PATH") or "/usr/share/omarchy") .. "/default/hypr/bootstrap.lua")\n'
USER_CONFIG = '-- user config\n' + BOOTSTRAP + 'require("hypr.input")\n'
# A stand-in for /usr/share/omarchy, so tests never read the installed one.
OMARCHY = Path(tempfile.mkdtemp())


def setUpModule():
    (OMARCHY / 'default/hypr').mkdir(parents=True)
    (OMARCHY / 'default/hypr/bootstrap.lua').write_text('-- sets package.path\n')
    # Safety: every path the helper resolves must be in the temp folder, and no test may reach the
    # real hyprctl; tests that need Hyprland mock control.hypr with FakeHyprland.
    for patcher in (mock.patch.dict(os.environ, {'OMARCHY_PATH': str(OMARCHY)}),
                    mock.patch.object(control, 'paths', guarded_paths(control.paths)),
                    mock.patch.object(control, 'hypr', no_hyprctl)):
        patcher.start()
        unittest.addModuleCleanup(patcher.stop)


class Unsafe(BaseException):
    """A test reached the real system. BaseException, so no `except Exception` can swallow it."""


TMP = Path(os.path.realpath(tempfile.gettempdir()))


def guarded_paths(real):
    def paths(home=None):
        p = real(home)
        for key, value in p.items():
            if not Path(os.path.realpath(value)).is_relative_to(TMP):
                raise Unsafe(f'control.paths() resolved {key} to {value}, outside {TMP}')
        return p
    return paths


def no_hyprctl(*args):
    raise Unsafe(f'a test ran the real hyprctl {" ".join(args)}; mock control.hypr')


STUB = ROOT / 'tests/hl_stub.lua'
AXIS = dict(left='horizontal', right='horizontal', horizontal='horizontal', up='vertical', down='vertical',
            vertical='vertical', swipe='swipe')
GESTURE_LINE = re.compile(r'^gesture \{(.*)\} at (.*:\d+)$')
LOADED_LINE = re.compile(r'^curve ttLoaded \{points=\{1=\{1=(\S+?),2=(\S+?)\},2=\{1=(\S+?),2=(\S+?)\}\}')


def refusal(where, previous, new):
    """The exact error Hyprland 0.56.2 logs for a refused gesture (LuaBindingsConfigRules.cpp:962 with
    the message from TrackpadGestures.cpp:85, after getSourceInfo's "<file>:<line>")."""
    return (f'{where}: hl.gesture: Gesture will be overshadowed by a previous gesture. '
            f'Previous {previous.upper()} shadows new {new.upper()}')


class FakeHyprland:
    """Answers hyprctl like Hyprland 0.56.2 (ConfigManager.cpp, TrackpadGestures.cpp).

    reload: when hyprland.lua does not parse (`parses` off) nothing changes and only its syntax error
    (`errors`) is listed. Otherwise animations, gestures and the error list reset and the config runs:
    the user's own errors (`errors`), the user's `before` swipes, the module (while the loader line is
    there, unless `runs` is off: an earlier error stops Lua before it), the user's `after` swipes.
    The module really runs, against tests/hl_stub.lua. A swipe that an earlier one with the same
    fingers and modifiers covers is refused: hl.gesture neither raises nor returns anything, it adds
    a config error naming the calling file and line. Curves outlive reloads.
    eval: empties the error list, then runs the module in the running Lua state, where its
    gestures are already registered; errors come back as "error: ..." lines (`eval_runs` off: the
    eval answers "ok" without running it)."""

    def __init__(self, p=None, errors='', runs=True, parses=True, before=(), after=(), eval_runs=True):
        self.p, self.errors, self.runs, self.parses = p, errors, runs, parses
        self.before, self.after, self.eval_runs = list(before), list(after), eval_runs
        self.calls, self.leaves, self.curves, self.gestures, self.listed = [], [], {}, [], []
        self.lua_has_module = False

    def __call__(self, *args):
        self.calls.append(args)
        if args[0] == 'reload':
            self.reload()
        elif args[0] == 'eval' and 'dofile' in args[1]:
            self.listed = []
            if self.eval_runs and self.p['module'].exists():
                self.run(register=not self.lua_has_module)
                self.lua_has_module = True
            return '\n'.join('error: ' + e for e in self.listed) or 'ok'
        elif args[0] == 'eval':
            return 'ok'
        elif args[0] == 'animations':
            curves = [dict(name=n, X0=round(a, 2), Y0=round(b, 2), X1=round(c, 2), Y1=round(d, 2))
                      for n, (a, b, c, d) in self.curves.items()]
            return json.dumps([self.leaves, curves])
        elif args[0] == 'configerrors':
            return '\n'.join(self.listed)
        elif args[0] == 'version':
            return 'Hyprland 0.56.2'
        return ''

    def reload(self):
        if not self.parses:
            self.listed = [line for line in self.errors.split('\n') if line.strip()]
            return
        self.leaves, self.gestures, self.lua_has_module = [], [], False
        self.listed = [line for line in self.errors.split('\n') if line.strip()]
        for swipe in self.before:
            self.add(*swipe)
        if not self.runs:
            return
        if control.installed(self.p) and self.p['module'].exists():
            self.run(register=True)
            self.lua_has_module = True
        for swipe in self.after:
            self.add(*swipe)

    def add(self, fingers, direction, where, mods=''):
        axis = AXIS[direction]
        for g in self.gestures:
            if (g['fingers'], g['mods']) != (fingers, mods):
                continue
            if g['direction'] in (axis, direction) or (axis in ('horizontal', 'vertical') and g['direction'] == 'swipe'):
                self.listed.append(refusal(where, g['direction'], direction))
                return
        self.gestures.append(dict(fingers=fingers, direction=direction, mods=mods, where=where))

    def run(self, register):
        r = subprocess.run(['lua', str(STUB), str(self.p['module'])], capture_output=True, text=True)
        for line in r.stdout.splitlines():
            if line.startswith('animation '):
                leaf = re.search(r'leaf=(\w+)', line).group(1)
                curve = re.search(r'(?:spring|bezier)=(\w+)', line)
                self.leaves = [l for l in self.leaves if l['name'] != leaf]
                if curve:
                    self.leaves.append(dict(name=leaf, bezier=('spring:' if 'spring=' in line else '') + curve.group(1)))
            m = LOADED_LINE.match(line)
            if m:
                self.curves['ttLoaded'] = [float(x) for x in m.groups()]
            m = GESTURE_LINE.match(line)
            if m and register:
                body, where = m.groups()
                self.add(int(re.search(r'fingers=(\d+)', body).group(1)), re.search(r'direction=(\w+)', body).group(1), where)
        if r.returncode:
            self.listed.append(r.stderr.strip())


def home_with(hyprland=USER_CONFIG, files=None):
    tmp = Path(tempfile.mkdtemp())
    (tmp / '.config/hypr').mkdir(parents=True)
    (tmp / '.config/omarchy').mkdir(parents=True)
    if hyprland is not None:
        (tmp / '.config/hypr/hyprland.lua').write_text(hyprland)
    for name, text in (files or {}).items():
        (tmp / '.config/hypr' / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp / '.config/hypr' / name).write_text(text)
    return tmp


class SpringMaths(unittest.TestCase):
    def test_damping_ratio_is_one_minus_bounce(self):
        for duration, bounce in [(290, 15), (330, 0), (360, 25), (150, 40)]:
            v = dict(duration=duration, bounce=bounce)
            k, c = control.spring(v)
            self.assertAlmostEqual(c / (2 * math.sqrt(k)), 1 - bounce / 100, places=9)
            self.assertAlmostEqual(math.sqrt(k), 2 * math.pi / (duration / 1000), places=6)

    def test_workspace_styles(self):
        self.assertEqual(control.workspace_style(0, 'vertical'), 'fade')
        self.assertEqual(control.workspace_style(100, 'vertical'), 'slidevert')
        self.assertEqual(control.workspace_style(30, 'vertical'), 'slidefadevert 30%')
        self.assertEqual(control.workspace_style(30, 'horizontal'), 'slidefade 30%')
        self.assertEqual(control.workspace_style(100, 'horizontal'), 'slide')


class Values(unittest.TestCase):
    def test_presets_match_and_custom(self):
        v = control.fresh()
        self.assertEqual(control.matching_preset(v), 'spring')
        v['duration'] = 300
        self.assertEqual(control.matching_preset(v), 'custom')

    def test_clamp(self):
        self.assertEqual(control.clamp('duration', 9999), 500)
        self.assertEqual(control.clamp('bounce', -3), 0)
        self.assertIs(control.clamp('swipes', 'true'), True)
        with self.assertRaises(ValueError):
            control.clamp('tracking', 'teleport')
        with self.assertRaises(ValueError):
            control.clamp('nonsense', 1)

    def test_layout_defaults(self):
        scrolling = home_with(files={'looknfeel.lua': 'hl.config({ general = { layout = "scrolling" } })\n'})
        dwindle = home_with()
        a, b = control.fresh(control.paths(scrolling)), control.fresh(control.paths(dwindle))
        self.assertEqual((a['swipe_horizontal'], a['workspace_axis']), ('windows', 'vertical'))
        self.assertEqual((b['swipe_horizontal'], b['workspace_axis']), ('workspaces', 'horizontal'))

    def test_normalize_horizontal_workspaces(self):
        v = control.fresh()
        v.update(swipes=True, swipe_horizontal='workspaces', swipe_vertical='workspaces', workspace_axis='vertical')
        control.normalize(v)
        self.assertEqual((v['swipe_vertical'], v['workspace_axis']), ('off', 'horizontal'))

    def test_read_ignores_garbage(self):
        home = home_with()
        p = control.paths(home)
        p['settings'].write_text(json.dumps({'duration': 'fast', 'bounce': 12, 'tracking': 'warp', 'unknown': 1}))
        v = control.read(p)
        self.assertEqual(v['duration'], 290)
        self.assertEqual(v['bounce'], 12)
        self.assertEqual(v['tracking'], 'follow')
        self.assertEqual(v['preset'], 'custom')

    def test_power_override(self):
        home = home_with()
        p = control.paths(home)
        p['state'].mkdir(parents=True)
        (p['state'] / 'override').write_text('snappy\n')
        v = control.fresh()
        self.assertEqual(control.effective(v, p)['duration'], 290)   # power_saver off: ignored
        v['power_saver'] = 'snappy'
        self.assertEqual(control.effective(v, p)['duration'], control.PRESETS['snappy']['duration'])


class Codes(unittest.TestCase):
    def test_round_trip(self):
        v = control.fresh()
        self.assertEqual(control.encode(v), 'TT1-290-15-30-1')
        self.assertEqual(control.decode('TT1-290-15-30-1'), dict(duration=290, bounce=15, travel=30, focus_fade=True))

    def test_rejects_and_clamps(self):
        for bad in ('', 'TT2-290-15-30-1', 'TT1-290-15-30', 'rm -rf /', 'TT1-29a-15-30-1'):
            with self.assertRaises(ValueError):
                control.decode(bad)
        self.assertEqual(control.decode('TT1-999-99-100-0')['duration'], 500)


class Module(unittest.TestCase):
    def render(self, **over):
        v = control.fresh()
        v.update(over)
        control.normalize(v)
        tmp = Path(tempfile.mkdtemp()) / 'tricky-tiles.lua'
        tmp.write_text(control.render(v))
        return tmp

    def run_lua(self, path, *extra):
        r = subprocess.run(['lua', str(ROOT / 'tests/hl_stub.lua'), str(path), *extra], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.splitlines()

    def test_syntax(self):
        r = subprocess.run(['luac', '-p', str(self.render())], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_switching_scope(self):
        calls = self.run_lua(self.render())
        leaves = [c for c in calls if c.startswith('animation')]
        self.assertTrue(any('leaf=windowsMove' in c and 'spring=ttSpring' in c for c in leaves))
        self.assertTrue(any('leaf=workspaces' in c and 'style=slidefade 30%' in c for c in leaves))
        self.assertTrue(any('leaf=fadeSwitch' in c and 'enabled=true' in c for c in leaves))
        self.assertFalse(any('leaf=windowsIn' in c for c in leaves))
        self.assertFalse(any(c.startswith('gesture') for c in calls))

    def test_everything_scope(self):
        calls = self.run_lua(self.render(scope='everything'))
        for leaf in ('windowsIn', 'windowsOut', 'layersIn', 'layersOut', 'border'):
            self.assertTrue(any(f'leaf={leaf}' in c for c in calls), leaf)

    def test_off_registers_nothing(self):
        calls = self.run_lua(self.render(enabled=False))
        self.assertFalse(any(c.startswith(('animation', 'gesture')) for c in calls))
        self.assertEqual([c.split(' ')[1] for c in calls if c.startswith('curve')], ['ttLoaded'])   # the proof only

    def test_each_render_proves_itself_with_new_points(self):
        v = control.fresh()
        a, b = control.render(v), control.render(v)
        self.assertNotEqual(control.module_marker(a), control.module_marker(b))
        self.assertEqual(control.render(v, marker=control.module_marker(a)), a)
        marker = control.module_marker(a)
        self.assertTrue(all(-1 <= x <= 2 and abs(round(x * 100) - x * 100) < 1e-9 for x in marker))
        path = Path(tempfile.mkdtemp()) / 'tricky-tiles.lua'
        path.write_text(a)
        curve, = [c for c in self.run_lua(path) if c.startswith('curve ttLoaded')]
        self.assertEqual(curve, 'curve ttLoaded {points={1={1=%r,2=%r},2={1=%r,2=%r}},type=bezier}' % tuple(marker))
        self.assertTrue(self.run_lua(path)[-1].startswith('curve ttLoaded'))   # last: the module ran to the end

    def test_follow_gestures(self):
        calls = self.run_lua(self.render(swipes=True, swipe_horizontal='windows', swipe_vertical='workspaces', workspace_axis='vertical'))
        gestures = [c for c in calls if c.startswith('gesture')]
        self.assertEqual(len(gestures), 2)
        self.assertTrue(any('action=scroll_move' in g and 'direction=horizontal' in g and 'scale=1.4' in g for g in gestures))
        self.assertTrue(any('action=workspace' in g and 'direction=vertical' in g for g in gestures))
        self.assertTrue(any('workspace_swipe_distance=700' in c for c in calls if c.startswith('config')))

    def test_step_gestures(self):
        calls = self.run_lua(self.render(swipes=True, tracking='step', swipe_horizontal='windows', swipe_vertical='workspaces'))
        gestures = [c for c in calls if c.startswith('gesture')]
        self.assertEqual(sorted(g.split('direction=')[1].split(',')[0] for g in gestures), ['down', 'left', 'right', 'up'])

    def test_live_reapply_does_not_duplicate_gestures(self):
        calls = self.run_lua(self.render(swipes=True, swipe_horizontal='windows', swipe_vertical='workspaces'), 'twice')
        self.assertEqual(len([c for c in calls if c.startswith('gesture')]), 2)
        self.assertEqual(len([c for c in calls if 'leaf=windowsMove' in c]), 2)


class Install(unittest.TestCase):
    def setUp(self):
        self.hl = FakeHyprland()
        self.calls = self.hl.calls
        patcher = mock.patch.object(control, 'hypr', side_effect=self.hl)
        patcher.start()
        self.addCleanup(patcher.stop)
        real = control.paths
        paths = mock.patch.object(control, 'paths', side_effect=lambda home=None: setattr(self.hl, 'p', real(home)) or self.hl.p)
        paths.start()
        self.addCleanup(paths.stop)

    def test_install_adds_one_line_and_backup(self):
        home = home_with()
        p = control.paths(home)
        control.install(control.fresh(), p)
        text = p['hyprland'].read_text()
        self.assertEqual(text.count(control.MARK), 1)
        self.assertTrue(text.startswith(USER_CONFIG))
        self.assertTrue(p['module'].exists())
        self.assertEqual(len(list(p['hypr_dir'].glob('hyprland.lua.bak.*-tricky-tiles'))), 1)
        control.install(control.fresh(), p)   # idempotent
        self.assertEqual(p['hyprland'].read_text().count(control.MARK), 1)
        self.assertIn(('reload', 'config-only'), self.calls)

    def test_uninstall_restores_file(self):
        original = USER_CONFIG
        home = home_with(original)
        p = control.paths(home)
        control.install(control.fresh(), p)
        control.uninstall(p)
        self.assertEqual(p['hyprland'].read_text().rstrip('\n'), original.rstrip('\n'))
        self.assertFalse(p['module'].exists())

    def test_install_without_hyprland_lua(self):
        home = home_with(hyprland=None)
        with self.assertRaises(RuntimeError):
            control.install(control.fresh(), control.paths(home))

    def test_apply_live_vs_reload(self):
        home = home_with()
        p = control.paths(home)
        v = control.fresh()
        control.install(v, p)
        self.calls.clear()
        v['duration'] = 250
        self.assertEqual(control.apply_module(v, p), 'live')
        self.assertEqual([c[0] for c in self.calls if c[0] in ('eval', 'reload')], ['eval'])
        v['swipes'] = True
        self.assertEqual(control.apply_module(v, p), 'reloaded')
        v['enabled'] = False
        self.assertEqual(control.apply_module(v, p), 'reloaded')
        self.assertEqual(control.apply_module(v, p), 'unchanged')

    def test_failed_install_leaves_files_as_they_were(self):
        original = USER_CONFIG
        home = home_with(original)
        p = control.paths(home)

        def failing(*args):
            self.calls.append(args)
            if args[0] == 'reload':
                raise RuntimeError('reload failed')
            return ''
        with mock.patch.object(control, 'hypr', side_effect=failing):
            with self.assertRaises(RuntimeError):
                control.install(control.fresh(), p)
        self.assertEqual(p['hyprland'].read_text(), original)
        self.assertFalse(p['module'].exists())

    def test_symlinked_config_stays_a_link(self):
        home = home_with(hyprland=None)
        real = home / 'dotfiles/hyprland.lua'
        real.parent.mkdir()
        real.write_text('-- user config\n')
        real.chmod(0o600)
        p = control.paths(home)
        p['hyprland'].symlink_to(real)
        control.install(control.fresh(), p)
        self.assertTrue(p['hyprland'].is_symlink())
        self.assertIn(control.MARK, real.read_text())
        backup, = p['hypr_dir'].glob('hyprland.lua.bak.*')
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        control.uninstall(p)
        self.assertTrue(p['hyprland'].is_symlink())
        self.assertNotIn(control.MARK, real.read_text())

    def test_rollback_reports_the_first_error(self):
        home = home_with()
        p = control.paths(home)
        v = control.fresh()
        control.install(v, p)
        before = p['module'].read_text()

        def failing(*args):
            raise RuntimeError('first: ' + args[0])
        v['duration'] = 400
        with mock.patch.object(control, 'hypr', side_effect=failing):
            with self.assertRaisesRegex(RuntimeError, 'first: eval'):
                control.apply_module(v, p)
        self.assertEqual(p['module'].read_text(), before)

    def test_leaving_everything_reloads(self):
        home = home_with()
        p = control.paths(home)
        v = control.fresh()
        control.install(v, p)
        v['scope'] = 'everything'
        self.assertEqual(control.apply_module(v, p), 'live')
        v['scope'] = 'switching'
        self.assertEqual(control.apply_module(v, p), 'reloaded')

    def test_failed_change_keeps_saved_settings(self):
        home = home_with()
        p = control.paths(home)
        control.install(control.fresh(), p)
        control.save(control.fresh(), p)
        out = io.StringIO()
        with mock.patch.object(control, 'paths', return_value=p), \
                mock.patch.object(control, 'hypr', side_effect=RuntimeError('no compositor')), \
                contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit):
                control.main(['set', 'duration', '400'])
        self.assertEqual(control.read(p)['duration'], 290)
        self.assertEqual(json.loads(out.getvalue())['values']['duration'], 290)

    def test_not_installed_touches_nothing(self):
        home = home_with()
        p = control.paths(home)
        self.assertEqual(control.apply_module(control.fresh(), p), 'not-installed')
        self.assertFalse(p['module'].exists())
        self.assertEqual(self.calls, [])


class Conflicts(unittest.TestCase):
    def test_finds_active_swipes_only(self):
        home = home_with(files={'input.lua': '\n'.join([
            'hl.gesture({ fingers = 3, direction = "left", action = function() end })',
            '-- hl.gesture({ fingers = 3, direction = "horizontal", action = "workspace" })',
            'hl.gesture({ fingers = 4, direction = "up", action = "workspace" })',
            'local x = 1 -- hl.gesture({ fingers = 3, direction = "up" })',
        ])})
        p = control.paths(home)
        v = control.fresh()
        found = control.conflicts(v, p)
        self.assertEqual([(f['file'], f['line']) for f in found], [('.config/hypr/input.lua', 1)])
        v['fingers'] = '4'
        self.assertEqual([f['line'] for f in control.conflicts(v, p)], [3])

    def test_finds_multiline_and_ignores_block_comments(self):
        home = home_with(files={'input.lua': '\n'.join([
            'hl.gesture({',
            '  fingers = 3,',
            '  direction = "horizontal",',
            '  action = function() hl.dispatch(hl.dsp.focus({ direction = "l" })) end,',
            '})',
            '--[[ hl.gesture({ fingers = 3, direction = "up", action = "workspace" }) ]]',
            'hl.gesture({ fingers = 3, action = "close", direction = "down" })',
        ])})
        found = control.conflicts(control.fresh(), control.paths(home))
        self.assertEqual([f['line'] for f in found], [1, 7])
        self.assertEqual(found[0]['text'], 'hl.gesture({')

    def test_skips_own_module_and_backups(self):
        line = 'hl.gesture({ fingers = 3, direction = "horizontal", action = "scroll_move" })\n'
        home = home_with(files={'tricky-tiles.lua': line, 'input.lua.bak.1': line})
        self.assertEqual(control.conflicts(control.fresh(), control.paths(home)), [])


class LoadedFiles(unittest.TestCase):
    """Only files Hyprland really runs count: hyprland.lua and what it loads, found like Hyprland
    and Omarchy's bootstrap find them."""

    GESTURE = 'hl.gesture({ fingers = 3, direction = "horizontal", action = "workspace" })\n'

    def test_an_unused_file_does_not_count(self):
        home = home_with(files={'input.lua': '-- no swipes\n', 'profiles/laptop.lua': self.GESTURE,
                                'old-input.lua': self.GESTURE})
        self.assertEqual(control.conflicts(control.fresh(), control.paths(home)), [])

    def test_follows_require_and_dofile(self):
        omarchy_file = OMARCHY / 'default/hypr/tt_swipes.lua'
        omarchy_file.write_text(self.GESTURE)
        self.addCleanup(omarchy_file.unlink)
        hyprland = (USER_CONFIG + 'require("default.hypr.tt_swipes")\n'
                    'require "hypr.extra"\n'
                    'dofile(os.getenv("HOME") .. "/.config/hypr/by-path.lua")\n'
                    'local name = "hypr.dynamic"; require(name)\n'
                    '-- require("hypr.commented")\n')
        home = home_with(hyprland, files={
            'input.lua': 'require("nested")\n',   # the config folder's nested/init.lua
            'nested/init.lua': self.GESTURE,
            'extra.lua': self.GESTURE,
            'by-path.lua': self.GESTURE,
            'dynamic.lua': self.GESTURE,
            'commented.lua': self.GESTURE})
        p = control.paths(home)
        found = control.conflicts(control.fresh(), p)
        self.assertEqual([f['file'] for f in found], [
            '.config/hypr/nested/init.lua', str(omarchy_file), '.config/hypr/extra.lua', '.config/hypr/by-path.lua'])
        sources = control.config_sources(p)[0]
        self.assertIn(OMARCHY / 'default/hypr/bootstrap.lua', sources)
        self.assertEqual(control.config_sources(p)[1], None)

    def test_explicit_paths_and_wildcards(self):
        # Hyprland reads a require() starting with / ./ ../ or ~/ as a path, from hyprland.lua's folder.
        home = home_with('require("./extras/*.lua")\nrequire("~/.config/hypr/own")\n', files={
            'extras/a.lua': self.GESTURE, 'extras/b.lua': self.GESTURE, 'own.lua': self.GESTURE})
        found = control.conflicts(control.fresh(), control.paths(home))
        self.assertEqual([f['file'] for f in found],
                         ['.config/hypr/extras/a.lua', '.config/hypr/extras/b.lua', '.config/hypr/own.lua'])

    def test_without_the_bootstrap_hyprland_looks_in_its_folder(self):
        # Hyprland's own package.path is the config folder, so "hypr.input" is hypr/hypr/input.lua.
        home = home_with('require("input")\nrequire("hypr.input")\n', files={'input.lua': self.GESTURE})
        self.assertEqual([f['line'] for f in control.conflicts(control.fresh(), control.paths(home))], [1])

    def test_unreadable_hyprland_lua_falls_back_and_says_so(self):
        home = home_with(None, files={'profile.lua': self.GESTURE})
        p = control.paths(home)
        out = io.StringIO()
        with mock.patch.object(control, 'hypr', return_value=''), contextlib.redirect_stdout(out):
            control.report(True, control.fresh(), p)
        r = json.loads(out.getvalue())
        self.assertEqual([c['file'] for c in r['conflicts']], ['.config/hypr/profile.lua'])
        self.assertIn('every .lua file', r['sources_error'])
        home = home_with()
        out = io.StringIO()
        with mock.patch.object(control, 'hypr', return_value=''), contextlib.redirect_stdout(out):
            control.report(True, control.fresh(), control.paths(home))
        self.assertIsNone(json.loads(out.getvalue())['sources_error'])

    def test_path_expressions(self):
        env = {'HOME': '/h', 'OMARCHY_PATH': '/o', 'EMPTY': ''}
        cases = {
            '"/a.lua"': '/a.lua',
            "'/a.lua'": '/a.lua',
            '(os.getenv("OMARCHY_PATH") or "/usr/share/omarchy") .. "/b.lua"': '/o/b.lua',
            '(os.getenv("UNSET") or "/usr/share/omarchy") .. "/b.lua"': '/usr/share/omarchy/b.lua',
            '(os.getenv("EMPTY") or "/x") .. "/b.lua"': '/b.lua',   # Lua: "" is true
            'os.getenv("HOME") .. "/c.lua"': '/h/c.lua',
            'os.getenv("UNSET") .. "/c.lua"': None,
            'p': None,
            'base .. "/c.lua"': None,
        }
        for expr, value in cases.items():
            with self.subTest(expr):
                got = control.path_expr(expr, env)
                self.assertEqual(got if isinstance(got, str) else None, value)


class Transactions(unittest.TestCase):
    """A change is all or nothing: files, saved settings and what Hyprland runs agree."""

    def setUp(self):
        self.home = home_with()
        self.p = control.paths(self.home)
        self.hl = FakeHyprland(self.p)
        self.calls = self.hl.calls
        with mock.patch.object(control, 'hypr', side_effect=self.hypr):
            control.install(control.fresh(), self.p)
        control.save(control.fresh(), self.p)
        self.calls.clear()

    def hypr(self, *args):
        self.hl.p = self.p
        return self.hl(*args)

    def main(self, *argv, hypr=None, **patches):
        out = io.StringIO()
        with mock.patch.object(control, 'paths', return_value=self.p), \
                mock.patch.object(control, 'hypr', side_effect=hypr or self.hypr), \
                contextlib.redirect_stdout(out), contextlib.ExitStack() as stack:
            for name, value in patches.items():
                stack.enter_context(mock.patch.object(control, name, value))
            try:
                control.main(list(argv))
                code = 0
            except SystemExit as e:
                code = e.code
        return code, json.loads(out.getvalue())

    def test_failed_save_restores_module_and_runtime(self):
        module = self.p['module'].read_bytes()
        settings = self.p['settings'].read_bytes()
        code, out = self.main('set', 'duration', '400', save=mock.Mock(side_effect=OSError('disk full')))
        self.assertEqual(code, 1)
        self.assertIn('disk full', out['error'])
        self.assertEqual(out['values']['duration'], 290)
        self.assertEqual(self.p['module'].read_bytes(), module)
        self.assertEqual(self.p['settings'].read_bytes(), settings)
        # The new values went live with eval; a reload brings the old module back.
        kinds = [c[0] for c in self.calls if c[0] in ('eval', 'reload')]
        self.assertEqual(kinds, ['eval', 'reload'])

    def test_failed_override_keeps_the_old_flag(self):
        values = control.fresh()
        values['power_saver'] = 'snappy'
        control.save(values, self.p)

        def failing(*args):
            self.calls.append(args)
            raise RuntimeError('no compositor')
        code, out = self.main('override', 'snappy', hypr=failing)
        self.assertEqual(code, 1)
        self.assertFalse((self.p['state'] / 'override').exists())
        self.assertIsNone(out['override'])

    def test_failed_install_save_removes_the_loader_again(self):
        home = home_with('-- mine\n')
        p = control.paths(home)
        self.p = p
        code, out = self.main('install', save=mock.Mock(side_effect=OSError('read-only')))
        self.assertEqual(code, 1)
        self.assertEqual(p['hyprland'].read_bytes(), b'-- mine\n')
        self.assertFalse(p['module'].exists())
        self.assertFalse(out['installed'])


class Remove(unittest.TestCase):
    def setUp(self):
        self.original = USER_CONFIG.encode()
        self.home = home_with(self.original.decode())
        self.p = control.paths(self.home)
        self.hl = FakeHyprland(self.p)
        with mock.patch.object(control, 'hypr', side_effect=self.hl):
            control.install(control.fresh(), self.p)
        self.p['state'].mkdir(parents=True, exist_ok=True)
        (self.p['state'] / 'override').write_text('snappy\n')
        self.installed = self.p['hyprland'].read_bytes()
        self.module = self.p['module'].read_bytes()

    def assert_untouched(self):
        self.assertEqual(self.p['hyprland'].read_bytes(), self.installed)
        self.assertEqual(self.p['module'].read_bytes(), self.module)
        self.assertEqual((self.p['state'] / 'override').read_text(), 'snappy\n')

    def test_without_compositor_touches_nothing(self):
        with mock.patch.object(control, 'hypr', side_effect=RuntimeError('no compositor')):
            with self.assertRaisesRegex(RuntimeError, 'no compositor'):
                control.uninstall(self.p)
        self.assert_untouched()

    def test_failed_reload_puts_files_back(self):
        calls = []

        def failing(*args):
            calls.append(args)
            if args[0] == 'reload' and len([c for c in calls if c[0] == 'reload']) == 1:
                raise RuntimeError('reload failed')
            return ''
        with mock.patch.object(control, 'hypr', side_effect=failing):
            with self.assertRaisesRegex(RuntimeError, 'reload failed'):
                control.uninstall(self.p)
        self.assert_untouched()
        self.assertEqual(calls[-1], ('reload', 'config-only'))   # the old module runs again
        self.assertTrue(control.installed(self.p))   # so the panel still offers Remove


class BatchedReload(unittest.TestCase):
    """Reload and configerrors go in one hyprctl request: the shell's own eval/dispatch traffic
    empties the error list between two separate calls."""

    def run_with(self, stdout, code=0):
        done = subprocess.CompletedProcess(['hyprctl'], code, stdout=stdout, stderr='')
        with mock.patch.object(control.subprocess, 'run', return_value=done) as run, \
                mock.patch.object(control, 'hypr', control._real_hypr):   # subprocess is faked
            out = control.reload_and_errors()
        self.assertEqual(run.call_args[0][0], ['hyprctl', '--batch', 'reload config-only; configerrors'])
        return out

    def test_errors_from_the_same_request(self):
        out = self.run_with("ok\n\n\n/home/u/.config/hypr/input.lua:98: unknown config key 'x'\n")
        self.assertEqual(out, "/home/u/.config/hypr/input.lua:98: unknown config key 'x'")

    def test_clean_reload(self):
        self.assertEqual(self.run_with('ok\n\n\n'), '')

    def test_refused_reload_raises(self):
        with self.assertRaises(RuntimeError):
            self.run_with('error: could not reload\n')


class LineEndings(unittest.TestCase):
    CASES = {
        'lf': b'-- user\nrequire("a")\n',
        'crlf': b'-- user\r\nrequire("a")\r\n',
        'no final newline': b'-- user\nrequire("a")',
        'trailing blank lines': b'-- user\nrequire("a")\n\n\n',
        'crlf without final newline': b'-- user\r\nrequire("a")',
        'cr only': b'-- config\r',
        'cr lines': b'-- user\rrequire("a")\r',
        'empty': b'',
    }

    def test_install_remove_round_trips_bytes(self):
        for name, original in self.CASES.items():
            home = home_with(None)
            p = control.paths(home)
            with self.subTest(name), mock.patch.object(control, 'hypr', side_effect=FakeHyprland(p)):
                p['hyprland'].write_bytes(original)
                control.install(control.fresh(), p)
                installed = p['hyprland'].read_bytes()
                self.assertTrue(installed.startswith(original))
                self.assertEqual(installed.count(control.MARK.encode()), 1)
                if b'\r\n' in original:
                    self.assertTrue(installed.endswith(control.MARK.encode() + b'\r\n'))
                    self.assertNotIn(b'\n', installed.replace(b'\r\n', b''))
                elif b'\r' in original:
                    self.assertTrue(installed.endswith(control.MARK.encode() + b'\r'))
                    self.assertNotIn(b'\n', installed)
                backup, = p['hypr_dir'].glob('hyprland.lua.bak.*-tricky-tiles')
                self.assertEqual(backup.read_bytes(), original)
                control.install(control.fresh(), p)   # idempotent
                self.assertEqual(p['hyprland'].read_bytes(), installed)
                control.uninstall(p)
                self.assertEqual(p['hyprland'].read_bytes(), original)

    def test_lines_added_after_ours_stay(self):
        home = home_with('-- user\n')
        p = control.paths(home)
        with mock.patch.object(control, 'hypr', side_effect=FakeHyprland(p)):
            control.install(control.fresh(), p)
            with p['hyprland'].open('a') as f:
                f.write('require("later")\n')
            control.uninstall(p)
        self.assertEqual(p['hyprland'].read_text(), '-- user\nrequire("later")\n')

    def test_removes_an_older_loader_line(self):
        old = ('-- user\n\ndo local p = os.getenv("HOME") .. "/.config/hypr/tricky-tiles.lua"; local f = io.open(p); '
               'if f then f:close(); dofile(p) end end ' + control.MARK + '\n')
        home = home_with(old)
        p = control.paths(home)
        with mock.patch.object(control, 'hypr', side_effect=FakeHyprland(p)):
            control.uninstall(p)
        self.assertEqual(p['hyprland'].read_text(), '-- user\n')


class ModuleLoaded(unittest.TestCase):
    """A reload must prove it ran the module just written, and Remove that none of it runs."""

    SYNTAX = '/home/u/.config/hypr/hyprland.lua:3: unexpected symbol near \'}\''

    def installed(self, **over):
        home = home_with()
        p = control.paths(home)
        v = control.fresh()
        v.update(over)
        hl = FakeHyprland(p)
        with mock.patch.object(control, 'hypr', side_effect=hl):
            control.install(v, p)
        return p, v, hl

    def test_install_fails_when_an_earlier_error_stops_the_module(self):
        original = 'error("x")\n'
        home = home_with(original)
        p = control.paths(home)
        # The error was already there, so configerrors does not change.
        with mock.patch.object(control, 'hypr', side_effect=FakeHyprland(p, errors='hyprland.lua:1: x', runs=False)):
            with self.assertRaisesRegex(RuntimeError, 'did not load(.|\\n)*hyprland.lua:1: x'):
                control.install(control.fresh(), p)
        self.assertEqual(p['hyprland'].read_text(), original)
        self.assertFalse(p['module'].exists())

    def test_reload_without_the_module_rolls_back(self):
        p, v, hl = self.installed()
        before = p['module'].read_text()
        v['swipes'] = True
        hl.runs = False
        with mock.patch.object(control, 'hypr', side_effect=hl):
            with self.assertRaisesRegex(RuntimeError, 'did not load'):
                control.apply_module(v, p)
        self.assertEqual(p['module'].read_text(), before)

    def test_old_module_left_running_by_a_refused_reload_fails(self):
        # hyprland.lua stopped parsing (an error the user already had), so the reload keeps the old
        # config running, ttSpring leaf and all; only the new curve points tell.
        p, v, hl = self.installed()
        before = p['module'].read_text()
        hl.parses, hl.errors = False, self.SYNTAX
        v['swipes'] = True
        with mock.patch.object(control, 'hypr', side_effect=hl):
            with self.assertRaisesRegex(RuntimeError, 'did not load'):
                control.apply_module(v, p)
        self.assertEqual(p['module'].read_text(), before)
        self.assertIn(('animations', '-j'), hl.calls)   # read back, never changed with eval
        self.assertFalse([c for c in hl.calls if c[0] == 'eval'])

    def test_remove_fails_while_the_old_module_still_runs(self):
        p, _, hl = self.installed()
        installed, module = p['hyprland'].read_bytes(), p['module'].read_bytes()
        hl.parses = False   # a refused reload with no error text at all: the proof tells
        with mock.patch.object(control, 'hypr', side_effect=hl):
            with self.assertRaisesRegex(RuntimeError, 'did not reload'):
                control.uninstall(p)
        self.assertEqual((p['hyprland'].read_bytes(), p['module'].read_bytes()), (installed, module))

    def test_remove_with_motion_off_fails_on_a_refused_reload(self):
        p, _, hl = self.installed(enabled=False)
        installed = p['hyprland'].read_bytes()
        hl.parses, hl.errors = False, self.SYNTAX
        with mock.patch.object(control, 'hypr', side_effect=hl):
            with self.assertRaisesRegex(RuntimeError, 'did not reload(.|\\n)*unexpected symbol'):
                control.uninstall(p)
        self.assertEqual(p['hyprland'].read_bytes(), installed)
        self.assertTrue(p['module'].exists())

    def test_remove_with_motion_off_and_swipes_on_fails_on_a_refused_reload(self):
        # Codex: Remove succeeded although Hyprland refused the reload, when motion was off (no
        # animation left to look at) and the syntax error's path had a space in it.
        p, _, hl = self.installed(enabled=False, swipes=True)
        installed, module = p['hyprland'].read_bytes(), p['module'].read_bytes()
        self.assertEqual([g['direction'] for g in hl.gestures], ['horizontal'])
        hl.parses, hl.errors = False, "/home/my user/.config/hypr/hyprland.lua:3: unexpected symbol near '}'"
        hl('reload', 'config-only')   # Hyprland reloaded when the broken file was saved: the error is listed
        with mock.patch.object(control, 'hypr', side_effect=hl):
            with self.assertRaisesRegex(RuntimeError, 'did not reload'):
                control.uninstall(p)
        self.assertEqual((p['hyprland'].read_bytes(), p['module'].read_bytes()), (installed, module))
        self.assertTrue(control.installed(p))
        self.assertEqual([g['direction'] for g in hl.gestures], ['horizontal'])   # still running, still offered

    def test_remove_proves_nothing_of_ours_runs_before_taking_the_line_out(self):
        p, _, hl = self.installed(swipes=True, scope='everything')
        self.assertTrue(hl.leaves and hl.gestures)
        hl.calls.clear()
        seen = {}

        def watch(*args):
            out = hl(*args)
            if args[0] == 'reload':   # phase 1: the loader is still there, the module is the stand-in
                seen.update(loader=control.installed(p), module=p['module'].read_text(),
                            leaves=list(hl.leaves), gestures=list(hl.gestures))
            return out
        with mock.patch.object(control, 'hypr', side_effect=watch):
            control.uninstall(p)
        self.assertTrue(seen['loader'])
        self.assertEqual((seen['leaves'], seen['gestures']), ([], []))
        self.assertTrue(control.same_marker(hl.curves['ttLoaded'], control.module_marker(seen['module'])))
        self.assertEqual([c[0] for c in hl.calls].count('reload'), 1)   # nothing after the files go
        self.assertFalse(control.installed(p))
        self.assertFalse(p['module'].exists())

    def test_remove_puts_everything_back_when_taking_the_line_out_fails(self):
        p, _, hl = self.installed(swipes=True)
        installed, module = p['hyprland'].read_bytes(), p['module'].read_bytes()
        real = control.atomic

        def atomic(path, data, mode=None):
            if Path(path) == p['hyprland']:
                raise OSError('read-only file system')
            return real(path, data, mode)
        with mock.patch.object(control, 'hypr', side_effect=hl), mock.patch.object(control, 'atomic', atomic):
            with self.assertRaisesRegex(OSError, 'read-only'):
                control.uninstall(p)
        self.assertEqual((p['hyprland'].read_bytes(), p['module'].read_bytes()), (installed, module))
        self.assertEqual(hl.calls[-1], ('reload', 'config-only'))   # the real module runs again
        self.assertEqual([g['direction'] for g in hl.gestures], ['horizontal'])

    def test_remove_succeeds_once_the_module_is_gone(self):
        p, _, hl = self.installed()
        with mock.patch.object(control, 'hypr', side_effect=hl):
            control.uninstall(p)
        self.assertFalse(control.installed(p))
        self.assertEqual(hl.leaves, [])

    def test_an_unreadable_answer_is_not_proof(self):
        # No answer must never pass for success: install keeps nothing, Remove deletes nothing.
        home = home_with()
        p = control.paths(home)
        before = p['hyprland'].read_bytes()
        with mock.patch.object(control, 'hypr', return_value='not json'):
            with self.assertRaises(RuntimeError):
                control.install(control.fresh(), p)
        self.assertEqual(p['hyprland'].read_bytes(), before)
        self.assertFalse(p['module'].exists())
        with mock.patch.object(control, 'hypr', side_effect=FakeHyprland(p)):
            control.install(control.fresh(), p)
        installed = p['hyprland'].read_bytes()
        with mock.patch.object(control, 'hypr', return_value='not json'):
            with self.assertRaises(RuntimeError):
                control.uninstall(p)
        self.assertEqual(p['hyprland'].read_bytes(), installed)
        self.assertTrue(p['module'].exists())

    def test_motion_off_is_checked_too(self):
        # The proof curve does not depend on motion, so a module stopped early is caught either way.
        home = home_with()
        p = control.paths(home)
        v = control.fresh()
        v['enabled'] = False
        with mock.patch.object(control, 'hypr', side_effect=FakeHyprland(p, runs=False)):
            with self.assertRaisesRegex(RuntimeError, 'did not load'):
                control.install(v, p)
        self.assertFalse(control.installed(p))
        with mock.patch.object(control, 'hypr', side_effect=FakeHyprland(p)):
            control.install(v, p)
        self.assertTrue(control.installed(p))


class ConfigFolder(unittest.TestCase):
    def env(self, home, xdg):
        return mock.patch.dict(os.environ, {'HOME': str(home), 'XDG_CONFIG_HOME': str(xdg), 'XDG_STATE_HOME': ''})

    def test_follows_xdg_config_home(self):
        home = home_with()
        xdg = home / 'xdg'
        (xdg / 'hypr').mkdir(parents=True)
        (xdg / 'hypr/hyprland.lua').write_text('-- xdg\n')
        with self.env(home, xdg):
            p = control.paths()
        self.assertEqual(p['hyprland'], xdg / 'hypr/hyprland.lua')
        self.assertEqual(p['module'], xdg / 'hypr/tricky-tiles.lua')
        self.assertEqual(p['settings'], xdg / 'omarchy/tricky-tiles.json')

    def test_falls_back_like_hyprland(self):
        home = home_with()
        with self.env(home, home / 'xdg'):
            self.assertEqual(control.paths()['hyprland'], home / '.config/hypr/hyprland.lua')
        with self.env(home, 'relative/path'):   # Hyprland ignores a relative XDG_CONFIG_HOME
            self.assertEqual(control.paths()['hyprland'], home / '.config/hypr/hyprland.lua')

    def test_loader_finds_the_module_next_to_hyprland_lua(self):
        # A folder name no os.getenv("HOME") path would reach, with characters Lua must escape.
        folder = Path(tempfile.mkdtemp()) / 'my "hypr" \\ conf'
        folder.mkdir()
        (folder / 'tricky-tiles.lua').write_text('print("loaded")\n')
        (folder / 'hyprland.lua').write_text('-- user\n\n' + control.LOADER + '\n')
        r = subprocess.run(['lua', str(folder / 'hyprland.lua')], capture_output=True, text=True,
                           env={**os.environ, 'HOME': '/nonexistent'})
        self.assertEqual((r.returncode, r.stdout), (0, 'loaded\n'), r.stderr)

    def test_conflicts_outside_home_are_listed(self):
        home = home_with()
        xdg = Path(tempfile.mkdtemp())
        (xdg / 'hypr').mkdir()
        (xdg / 'hypr/hyprland.lua').write_text('hl.gesture({ fingers = 3, direction = "left", action = "close" })\n')
        with self.env(home, xdg):
            p = control.paths()
        found = control.conflicts(control.fresh(), p)
        self.assertEqual(found[0]['file'], str(xdg / 'hypr/hyprland.lua'))


class LuaStrings(unittest.TestCase):
    def lua_reads(self, literal):
        r = subprocess.run(['lua', '-e', f'io.write({literal})'], capture_output=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def test_any_text_round_trips(self):
        for text in ['plain', 'a "quote"', 'back\\slash', 'new\nline\r\t', 'é and 漢', '\x00\x01\x7f', ']]--[[']:
            with self.subTest(text):
                self.assertEqual(self.lua_reads(control.lua_str(text)), text.encode())
                self.assertEqual(self.lua_reads(control.lua_value(text)), text.encode())

    def test_live_apply_with_an_awkward_home(self):
        home = Path(tempfile.mkdtemp()) / 'o\'neil "x" \\ é'
        home.mkdir()
        for sub in ('.config/hypr', '.config/omarchy'):
            (home / sub).mkdir(parents=True)
        (home / '.config/hypr/hyprland.lua').write_text('-- user\n')
        p = control.paths(home)
        hl = FakeHyprland(p)
        calls = hl.calls
        with mock.patch.object(control, 'hypr', side_effect=hl):
            control.install(control.fresh(), p)
            v = control.fresh()
            v['duration'] = 250
            self.assertEqual(control.apply_module(v, p), 'live')
        code = [c[1] for c in calls if c[0] == 'eval'][-1]
        r = subprocess.run(['lua', '-e', 'function dofile(p) io.write(p) end ' + code], capture_output=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, os.fsencode(str(p['module'])))


class SettingsRules(unittest.TestCase):
    def test_axis_is_free_while_swipes_are_off(self):
        v = control.fresh()   # dwindle defaults: horizontal swipes would switch workspaces
        v.update(swipes=False, workspace_axis='vertical', swipe_vertical='workspaces')
        control.normalize(v)
        self.assertEqual((v['workspace_axis'], v['swipe_vertical']), ('vertical', 'workspaces'))
        v['swipes'] = True
        control.normalize(v)
        self.assertEqual((v['workspace_axis'], v['swipe_vertical']), ('horizontal', 'off'))

    def test_axis_set_from_the_panel_sticks(self):
        home = home_with()
        p = control.paths(home)
        out = io.StringIO()
        with mock.patch.object(control, 'paths', return_value=p), \
                mock.patch.object(control, 'hypr', side_effect=FakeHyprland(p)), \
                contextlib.redirect_stdout(out):
            control.main(['set', 'workspace_axis', 'vertical'])
        self.assertEqual(json.loads(out.getvalue())['values']['workspace_axis'], 'vertical')
        self.assertEqual(control.read(p)['workspace_axis'], 'vertical')

    def test_monitor_width_on_a_rotated_panel(self):
        mons = [dict(name='DP-1', width=2560, height=1440, scale=1.0, transform=1, refreshRate=60.0, focused=True)]
        with mock.patch.object(control, 'hypr', return_value=json.dumps(mons)):
            self.assertEqual(control.monitor_info()['width'], 1440)
        mons[0].update(transform=2, scale=2.0)
        with mock.patch.object(control, 'hypr', return_value=json.dumps(mons)):
            self.assertEqual(control.monitor_info()['width'], 1280)


class Hints(unittest.TestCase):
    """The user's own swipes, read from their files: hints for the panel only (best effort)."""

    def found(self, *lines, fingers='3', eol='\n'):
        home = home_with(files={'input.lua': eol.join(lines)})
        v = control.fresh()
        v['fingers'] = fingers
        return control.conflicts(v, control.paths(home))

    def test_directions_a_swipe_takes(self):
        found = self.found('hl.gesture({ fingers = 3, direction = "left", action = "close" })',
                           'hl.gesture({ fingers = 3, direction = "Vert", action = "workspace" })',
                           'hl.gesture({ fingers = 3, direction = "swipe", action = "move" })')
        self.assertEqual([(f['direction'], f['directions']) for f in found],
                         [('left', ['left']), ('vertical', ['up', 'down']), ('swipe', ['left', 'right', 'up', 'down'])])

    def test_what_never_overlaps(self):
        found = self.found(
            'hl.gesture({ fingers = 3, direction = "horizontal", mods = "SUPER", action = "workspace" })',
            'hl.gesture({ fingers = 3, direction = "pinchin", action = "close" })',
            'hl.gesture({ fingers = 3, direction = "up", action = "unset" })',
            'hl.gesture({ fingers = 3, action = function() hl.dispatch(hl.dsp.focus({ direction = "l" })) end, direction = "down" })')
        self.assertEqual([f['line'] for f in found], [4])
        self.assertEqual(found[0]['directions'], ['down'])

    def test_bare_cr_and_crlf_line_ends(self):
        # Codex: a comment in a file with bare CR line ends hid every line after it.
        swipe = 'hl.gesture({ fingers = 3, direction = "left", action = "close" })'
        for eol in ('\r', '\r\n', '\n'):
            with self.subTest(repr(eol)):
                found = self.found('-- my swipes', swipe, '--[[ a', 'block ]]', swipe.replace('left', 'up'), eol=eol)
                self.assertEqual([(f['line'], f['direction'], f['text']) for f in found],
                                 [(2, 'left', swipe), (5, 'up', swipe.replace('left', 'up'))])
        self.assertEqual(control.strip_comments('a -- x\rb -- y\r\nc'), 'a \nb \nc')

    def test_loadfile_only_compiles(self):
        # Codex: loadfile() was followed as if it ran the file.
        gesture = 'hl.gesture({ fingers = 3, direction = "horizontal", action = "workspace" })\n'
        home = home_with(USER_CONFIG + 'local chunk = loadfile(os.getenv("HOME") .. "/.config/hypr/compiled.lua")\n',
                         files={'compiled.lua': gesture})
        p = control.paths(home)
        self.assertEqual(control.conflicts(control.fresh(), p), [])
        self.assertNotIn(p['hypr_dir'] / 'compiled.lua', control.config_sources(p)[0])

    def test_report_always_lists_conflicts_per_finger_count(self):
        home = home_with(files={'input.lua': '\n'.join([
            'hl.gesture({ fingers = 3, direction = "horizontal", action = "workspace" })',
            'hl.gesture({ fingers = 4, direction = "up", action = "workspace" })'])})
        p = control.paths(home)
        out = io.StringIO()
        with mock.patch.object(control, 'hypr', return_value=''), contextlib.redirect_stdout(out):
            control.report(True, control.fresh(), p)   # swipes are off
        r = json.loads(out.getvalue())
        self.assertEqual([c['line'] for c in r['conflicts']], [1])
        self.assertEqual({k: [c['line'] for c in v] for k, v in r['conflicts_by_fingers'].items()}, {'3': [1], '4': [2]})
        self.assertEqual((r['skipped'], r['config_errors']), ([], []))

    def test_hints_never_decide_what_the_module_registers(self):
        # Codex: a load the static reading cannot judge (here one that never runs) made the module
        # give up a swipe. Now only Hyprland's answer counts.
        home = home_with(USER_CONFIG + 'if false then require("hypr.never") end\n', files={
            'never.lua': 'hl.gesture({ fingers = 3, direction = "horizontal", action = "workspace" })\n'})
        p = control.paths(home)
        hl = FakeHyprland(p)
        out = io.StringIO()
        with mock.patch.object(control, 'paths', return_value=p), mock.patch.object(control, 'hypr', side_effect=hl), \
                contextlib.redirect_stdout(out):
            control.main(['install'])
            control.main(['set', 'swipes', 'true'])
        r = json.loads(out.getvalue().splitlines()[-1])
        self.assertTrue(r['ok'])
        self.assertEqual([c['line'] for c in r['conflicts']], [1])   # still shown as a hint
        self.assertEqual(r['skipped'], [])
        self.assertEqual(control.module_skip(p['module'].read_text()), [])
        self.assertEqual([g['direction'] for g in hl.gestures], ['horizontal'])

    def test_module_registers_only_free_directions(self):
        v = control.fresh()
        v.update(swipes=True, tracking='step', swipe_horizontal='windows', swipe_vertical='workspaces')
        control.normalize(v)
        path = Path(tempfile.mkdtemp()) / 'tricky-tiles.lua'
        path.write_text(control.render(v, ['left', 'up']))
        r = subprocess.run(['lua', str(STUB), str(path)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        dirs = sorted(c.split('direction=')[1].split(',')[0] for c in r.stdout.splitlines() if c.startswith('gesture'))
        self.assertEqual(dirs, ['down', 'right'])
        self.assertEqual(control.module_skip(path.read_text()), ['left', 'up'])

    def test_skip_change_needs_a_reload(self):
        v = control.fresh()
        v['swipes'] = True
        self.assertNotEqual(control.gesture_part(control.render(v)), control.gesture_part(control.render(v, ['horizontal'])))


class Refusals(unittest.TestCase):
    """Hyprland 0.56.2 refuses an overlapping swipe with a config error only; the helper reads the
    errors after a reload, leaves those directions to the user's config and reloads once more."""

    def setUp(self):
        self.home = home_with()
        self.p = control.paths(self.home)
        self.input = f'{self.p["hypr_dir"]}/input.lua'
        self.hl = FakeHyprland(self.p)
        self.main('install')
        self.hl.calls.clear()

    def main(self, *argv):
        out = io.StringIO()
        with mock.patch.object(control, 'paths', return_value=self.p), \
                mock.patch.object(control, 'hypr', side_effect=self.hl), contextlib.redirect_stdout(out):
            try:
                control.main(list(argv))
            except SystemExit:
                pass
        return json.loads(out.getvalue())

    def reloads(self):
        return [c[0] for c in self.hl.calls].count('reload')

    def ours(self):
        return [g['direction'] for g in self.hl.gestures if str(self.p['module']) in g['where']]

    def test_the_stub_and_the_fake_never_raise_on_a_refusal(self):
        self.hl.before = [(3, 'horizontal', self.input + ':1')]
        with mock.patch.object(control, 'hypr', side_effect=self.hl):
            v = control.fresh()
            v['swipes'] = True
            path = self.p['module']
            path.write_text(control.render(v))
            self.hl('reload', 'config-only')
        line, = self.hl.listed
        self.assertTrue(control.REFUSAL_RE.match(line))
        self.assertTrue(line.startswith(f'{path}:'))   # Hyprland names the module's own line
        self.assertEqual(control.refusals([line], self.p),
                         [dict(line=line, previous='horizontal', new='horizontal', ours=True)])

    def test_refused_directions_are_left_to_the_user_and_the_error_goes(self):
        self.hl.before = [(3, 'horizontal', self.input + ':1')]
        r = self.main('set', 'swipes', 'true')
        self.assertTrue(r['ok'], r.get('error'))
        self.assertEqual(r['skipped'], ['horizontal'])
        self.assertEqual(self.reloads(), 2)
        self.assertEqual(self.hl.listed, [])   # the second reload left no error
        self.assertEqual(self.ours(), [])
        self.assertEqual(control.module_skip(self.p['module'].read_text()), ['horizontal'])
        lua = subprocess.run(['lua', str(STUB), str(self.p['module'])], capture_output=True, text=True)
        self.assertFalse([c for c in lua.stdout.splitlines() if 'workspace_swipe_distance' in c])

    def test_only_the_refused_side_is_skipped(self):
        self.hl.before = [(3, 'left', self.input + ':1')]
        self.main('set', 'tracking', 'step')
        r = self.main('set', 'swipes', 'true')
        self.assertEqual(r['skipped'], ['left'])
        self.assertEqual(self.ours(), ['right'])

    def test_an_earlier_left_leaves_our_horizontal_alone(self):
        self.hl.before = [(3, 'left', self.input + ':1')]
        r = self.main('set', 'swipes', 'true')
        self.assertEqual((r['skipped'], self.reloads(), self.ours()), ([], 1, ['horizontal']))

    def test_swipes_found_through_a_custom_package_path(self):
        # Codex: the static reading ignores a package.path the user sets; Hyprland does not.
        self.p['hyprland'].write_text(USER_CONFIG + 'package.path = os.getenv("HOME") .. "/mine/?.lua;" .. package.path\n'
                                      'require("swipes")\n' + control.LOADER + '\n')
        (self.home / 'mine').mkdir()
        (self.home / 'mine/swipes.lua').write_text('hl.gesture({ fingers = 3, direction = "horizontal", action = "workspace" })\n')
        self.hl.before = [(3, 'horizontal', f'{self.home}/mine/swipes.lua:1')]
        r = self.main('set', 'swipes', 'true')
        self.assertTrue(r['ok'], r.get('error'))
        self.assertEqual(r['conflicts'], [])   # invisible to the hints
        self.assertEqual(r['skipped'], ['horizontal'])
        self.assertEqual(self.hl.listed, [])

    def test_a_later_swipe_of_the_users_gets_its_direction(self):
        # Their "up" loads after our line; ours would make Hyprland refuse it, so ours steps aside.
        self.hl.after = [(3, 'up', f'{self.p["hyprland"]}:9')]
        self.main('set', 'tracking', 'step')
        self.main('set', 'swipe_horizontal', 'windows')
        r = self.main('set', 'swipe_vertical', 'workspaces')   # swipes still off
        r = self.main('set', 'swipes', 'true')
        self.assertTrue(r['ok'], r.get('error'))
        self.assertEqual(r['skipped'], ['up'])
        self.assertEqual(self.hl.listed, [])
        self.assertEqual(self.ours(), ['left', 'right', 'down'])

    def test_a_blame_that_does_not_clear_is_taken_back(self):
        # The user's own 4-finger swipes clash ("Previous HORIZONTAL shadows new LEFT"); the message
        # has no finger count, so ours (3 fingers, horizontal) is suspected first, then cleared.
        self.hl.before = [(4, 'horizontal', self.input + ':1')]
        self.hl.after = [(4, 'left', self.input + ':9')]
        r = self.main('set', 'swipes', 'true')
        self.assertTrue(r['ok'], r.get('error'))
        self.assertEqual(r['skipped'], [])
        self.assertEqual(self.ours(), ['horizontal'])
        self.assertEqual(self.reloads(), 3)
        self.assertEqual(r['config_errors'], [refusal(self.input + ':9', 'horizontal', 'left')])

    def test_an_unrelated_refusal_does_not_clear_ours(self):
        # Two refusals read the same ("Previous HORIZONTAL shadows new LEFT"): the user's own
        # 4-finger clash, which stays, and their later 3-finger left, which ours blocks. Clearing
        # the first must not clear the second: ours steps aside and their swipe registers.
        self.hl.before = [(4, 'horizontal', self.input + ':1')]
        self.hl.after = [(4, 'left', self.input + ':9'), (3, 'left', self.input + ':10')]
        r = self.main('set', 'swipes', 'true')
        self.assertTrue(r['ok'], r.get('error'))
        self.assertEqual(r['skipped'], ['horizontal'])
        self.assertIn(dict(fingers=3, direction='left', mods='', where=self.input + ':10'), self.hl.gestures)
        self.assertEqual(r['config_errors'], [refusal(self.input + ':9', 'horizontal', 'left')])

    def test_the_report_keeps_what_the_reload_saw(self):
        # The shell's eval traffic can empty the error list between the reload and the report.
        fake = self.hl
        fake.errors = self.input + ':3: unknown config key \'x\''

        def emptied_after_reload(*args):
            out = fake(*args)
            if args[0] == 'configerrors' and fake.calls[-2][0] == 'reload':
                fake.listed = []   # someone ran hyprctl eval right after
            return out
        self.hl = emptied_after_reload
        try:
            r = self.main('apply')
        finally:
            self.hl = fake
        self.assertTrue(r['ok'], r.get('error'))
        self.assertEqual(r['config_errors'], [self.input + ":3: unknown config key 'x'"])

    def test_a_freed_direction_is_taken_at_the_next_swipe_change_or_apply(self):
        self.hl.before = [(3, 'horizontal', self.input + ':1')]
        self.main('set', 'swipes', 'true')
        self.hl.before = []   # the user comments their swipe out
        self.hl.calls.clear()
        r = self.main('set', 'enabled', 'false')   # a reload, swipes unchanged: no refusal to flash
        self.assertEqual((r['skipped'], self.reloads(), self.ours()), (['horizontal'], 1, []))
        r = self.main('set', 'sensitivity', '120')
        self.assertEqual((r['skipped'], self.ours()), ([], ['horizontal']))
        self.hl.before = [(3, 'horizontal', self.input + ':1')]
        self.assertEqual(self.main('apply')['skipped'], ['horizontal'])
        self.hl.before = []
        self.assertEqual(self.main('apply')['skipped'], [])
        self.assertEqual(self.ours(), ['horizontal'])

    def test_state_says_what_was_skipped_without_reloading(self):
        self.hl.before = [(3, 'horizontal', self.input + ':1')]
        self.main('set', 'swipes', 'true')
        self.main('set', 'duration', '250')   # live: eval empties the error list
        self.assertEqual(self.hl.listed, [])
        self.hl.calls.clear()
        r = self.main('state')
        self.assertEqual(r['skipped'], ['horizontal'])
        self.assertFalse([c for c in self.hl.calls if c[0] in ('reload', 'eval')])
        # A module rendered by anything else is not the load that was recorded.
        v = control.read(self.p)
        self.p['module'].write_text(control.render(control.effective(v, self.p), ['horizontal']))
        self.assertEqual(self.main('state')['skipped'], [])

    def test_refusal_messages(self):
        spaced = '/tmp/my home/.config/hypr/tricky-tiles.lua'
        p = dict(self.p, module=Path(spaced))
        for line, previous, new, ours in [
                (refusal(spaced + ':142', 'horizontal', 'horizontal'), 'horizontal', 'horizontal', True),
                (refusal('/tmp/my home/.config/hypr/input.lua:3', 'swipe', 'left'), 'swipe', 'left', False),
                (refusal('/tmp/other/tricky-tiles.lua:3', 'up', 'up'), 'up', 'up', False)]:
            with self.subTest(line):
                self.assertEqual(control.refusals([line], p), [dict(line=line, previous=previous, new=new, ours=ours)])
        self.assertEqual(control.refusals(['input.lua:3: hl.gesture: invalid direction "sideways"'], p), [])


class ConfigErrors(unittest.TestCase):
    """An error blocks a change only when it is ours; the user's own are reported, never blamed."""

    def setUp(self):
        self.home = home_with()
        self.p = control.paths(self.home)
        self.hl = FakeHyprland(self.p)

    def main(self, *argv):
        out = io.StringIO()
        with mock.patch.object(control, 'paths', return_value=self.p), \
                mock.patch.object(control, 'hypr', side_effect=self.hl), contextlib.redirect_stdout(out):
            try:
                control.main(list(argv))
            except SystemExit:
                pass
        return json.loads(out.getvalue())

    def test_the_users_errors_never_block_and_are_reported(self):
        mine = f'{self.p["hypr_dir"]}/input.lua:3: attempt to call a nil value (global \'oops\')'
        self.hl.errors = mine
        r = self.main('install')
        self.assertTrue(r['ok'], r.get('error'))
        self.assertEqual(r['config_errors'], [mine])
        self.assertTrue(self.main('set', 'swipes', 'true')['ok'])

    def test_an_error_the_user_adds_after_a_live_change_is_not_blamed(self):
        # Codex/52c6de9: `hyprctl eval` empties the error list, and remembered errors only counted
        # for an unchanged config, so a reload after editing input.lua blamed Tricky Tiles.
        self.assertTrue(self.main('install')['ok'])
        self.assertEqual(self.main('set', 'duration', '250')['applied'], 'live')
        (self.p['hypr_dir'] / 'input.lua').write_text('oops()\n')
        self.hl.errors = f'{self.p["hypr_dir"]}/input.lua:1: attempt to call a nil value (global \'oops\')'
        r = self.main('set', 'swipes', 'true')
        self.assertTrue(r['ok'], r.get('error'))
        self.assertEqual(r['applied'], 'reloaded')

    def test_an_error_in_our_module_blocks(self):
        self.assertTrue(self.main('install')['ok'])
        before = self.p['module'].read_bytes()
        for line in (f'{self.p["module"]}:57: hl.animation: unknown style "x"',
                     '...' + str(self.p['module'])[-45:] + ':12: attempt to index a nil value'):
            with self.subTest(line):
                self.hl.errors = line
                r = self.main('set', 'swipes', 'true')
                self.assertFalse(r['ok'])
                self.assertIn('error in Tricky Tiles', r['error'])
                self.assertEqual(self.p['module'].read_bytes(), before)
                self.assertFalse(control.read(self.p)['swipes'])

    def test_an_error_at_our_loader_line_blocks_but_not_elsewhere(self):
        number = len(USER_CONFIG.splitlines()) + 2   # after the blank line with_loader() adds
        self.hl.errors = f'{self.p["hyprland"]}:{number}: attempt to call a nil value (global \'dofile\')'
        r = self.main('install')
        self.assertFalse(r['ok'])
        self.assertFalse(control.installed(self.p))
        self.hl.errors = f'{self.p["hyprland"]}:2: attempt to call a nil value'
        r = self.main('install')
        self.assertTrue(r['ok'], r.get('error'))
        self.assertEqual(control.loader_line(self.p), number)
        self.assertEqual(r['config_errors'], [self.hl.errors])

    def test_a_reload_that_did_not_run_us_blocks(self):
        self.hl.errors, self.hl.runs = f'{self.p["hypr_dir"]}/input.lua:1: boom', False
        r = self.main('install')
        self.assertFalse(r['ok'])
        self.assertIn('did not load', r['error'])
        self.assertIn('input.lua:1: boom', r['error'])

    def test_the_old_error_memory_is_deleted(self):
        self.p['state'].mkdir(parents=True)
        (self.p['state'] / 'known-errors').write_text('{"config": "x", "errors": ""}\n')
        self.main('state')
        self.assertFalse((self.p['state'] / 'known-errors').exists())

    def test_mentions(self):
        module = '/tmp/a b/.config/hypr/tricky-tiles.lua'
        yes = [module + ':3: x', 'stack: ' + module + ':3: in main chunk', '\t' + module + ':3:',
               '...' + module[-30:] + ':3: in function', f'[string "x"]:1: {module}:3: y']
        no = ['/tmp/a b/.config/hypr/old-tricky-tiles.lua:3: x', '/tmp/other/tricky-tiles.lua:3: x',
              'tricky-tiles.lua:3: x', module + ': no line']
        for line in yes:
            self.assertTrue(control.mentions(line, module), line)
        for line in no:
            self.assertFalse(control.mentions(line, module), line)
        self.assertTrue(control.mentions('/h/hyprland.lua:7: x', '/h/hyprland.lua', 7))
        self.assertFalse(control.mentions('/h/hyprland.lua:8: x', '/h/hyprland.lua', 7))


class LoadProof(unittest.TestCase):
    """Every change renders a new ttLoaded proof and checks Hyprland runs it."""

    def setUp(self):
        self.home = home_with()
        self.p = control.paths(self.home)
        self.hl = FakeHyprland(self.p)
        self.assertTrue(self.main('install')['ok'])
        self.hl.calls.clear()

    def main(self, *argv):
        out = io.StringIO()
        with mock.patch.object(control, 'paths', return_value=self.p), \
                mock.patch.object(control, 'hypr', side_effect=self.hl), contextlib.redirect_stdout(out):
            try:
                control.main(list(argv))
            except SystemExit:
                pass
        return json.loads(out.getvalue())

    def marker(self):
        return control.module_marker(self.p['module'].read_text())

    def stop_running(self):
        """The user reloads with an error before our line: Hyprland no longer runs the module."""
        self.hl.runs = False
        self.hl('reload', 'config-only')
        self.hl.runs = True
        self.hl.calls.clear()

    def test_a_live_change_must_prove_it_ran(self):
        before = self.p['module'].read_bytes()
        self.hl.eval_runs = False
        r = self.main('set', 'duration', '250')
        self.assertFalse(r['ok'])
        self.assertIn('did not run', r['error'])
        self.assertEqual(self.p['module'].read_bytes(), before)
        self.assertEqual(control.read(self.p)['duration'], 290)

    def test_a_live_change_hyprland_reports_an_error_for_fails(self):
        before = self.p['module'].read_bytes()
        hl = self.hl
        self.hl = lambda *a: f'error: {self.p["module"]}:30: boom' if a[0] == 'eval' else hl(*a)
        r = self.main('set', 'duration', '250')
        self.assertFalse(r['ok'])
        self.assertIn('boom', r['error'])
        self.assertEqual(self.p['module'].read_bytes(), before)
        kinds = [c[0] for c in hl.calls if c[0] in ('eval', 'reload')]
        self.assertEqual(kinds[-1], 'reload')   # the old module runs again

    def test_a_live_change_proves_itself(self):
        old = self.marker()
        r = self.main('set', 'duration', '250')
        self.assertEqual(r['applied'], 'live')
        self.assertFalse(control.same_marker(old, self.marker()))
        self.assertTrue(control.same_marker(self.hl.curves['ttLoaded'], self.marker()))

    def test_apply_reloads_even_when_nothing_changed(self):
        # Codex: Apply answered "unchanged" although Hyprland had stopped running the module.
        self.stop_running()
        old = self.marker()
        r = self.main('apply')
        self.assertTrue(r['ok'], r.get('error'))
        self.assertEqual(r['applied'], 'reloaded')
        self.assertIn(('reload', 'config-only'), self.hl.calls)
        self.assertFalse(control.same_marker(old, self.marker()))
        self.assertTrue(control.same_marker(self.hl.curves['ttLoaded'], self.marker()))
        self.hl.calls.clear()
        self.assertEqual(self.main('apply')['applied'], 'reloaded')   # always, live or not
        self.assertIn(('reload', 'config-only'), self.hl.calls)

    def test_apply_fails_while_the_module_cannot_run(self):
        self.hl.runs = False
        r = self.main('apply')
        self.assertFalse(r['ok'])
        self.assertIn('did not load', r['error'])

    def test_an_identical_change_is_unchanged_only_while_it_runs(self):
        r = self.main('set', 'duration', '290')
        self.assertEqual(r['applied'], 'unchanged')
        self.assertFalse([c for c in self.hl.calls if c[0] in ('reload', 'eval')])
        self.stop_running()
        r = self.main('set', 'duration', '290')
        self.assertEqual(r['applied'], 'reloaded')
        self.assertTrue(control.same_marker(self.hl.curves['ttLoaded'], self.marker()))


class Safety(unittest.TestCase):
    def test_paths_outside_the_temp_folder_fail_loudly(self):
        with self.assertRaises(Unsafe):
            control.paths(Path('/nonexistent-home'))

    def test_hyprctl_is_never_real(self):
        with self.assertRaises(Unsafe):
            control.hypr('version')


class Carousel(unittest.TestCase):
    """TT.step's wrap-around, run against the stub with a scrolling strip of three windows."""

    WORLD = '''
local ws = { id = %(ws)s, tiled_layout = "scrolling" }
local mon = { x = 0, width = %(width)s, height = %(height)s, scale = 1, transform = %(transform)s }
local function win(address, x) return { address = address, at = { x = x }, size = { x = 600 }, mapped = true,
                                          workspace = ws, monitor = mon } end
world.windows = { win("a", %(a)s), win("b", %(b)s), win("c", %(c)s) }
world.active = world.windows[%(active)s]
world.monitor = mon
world.workspace = { id = 1 }   -- the monitor's regular workspace
'''

    def run_step(self, script, direction='r', ws=1, width=1920, height=1080, transform=0, xs=(-1800, -1200, 0),
                 active=3):
        v = control.fresh()
        v.update(swipes=True, tracking='step', swipe_horizontal='windows', wrap=True)
        tmp = Path(tempfile.mkdtemp())
        (tmp / 'm.lua').write_text(control.render(v))
        world = self.WORLD % dict(ws=ws, width=width, height=height, transform=transform,
                                  a=xs[0], b=xs[1], c=xs[2], active=active)
        (tmp / 's.lua').write_text(world + script.replace('DIR', direction))
        r = subprocess.run(['lua', str(ROOT / 'tests/hl_stub.lua'), str(tmp / 'm.lua'), str(tmp / 's.lua')],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = r.stdout.splitlines()
        return lines[lines.index('start') + 1:]

    STEP = '''
note("start")
TrickyTiles.step("DIR")
note("fired")
fire()
note("animations=" .. tostring(world.config["animations.enabled"]) .. " follow=" .. tostring(world.config["scrolling.follow_focus"]))
'''

    def test_wrap_warps_then_slides_the_far_window_in(self):
        out = self.run_step(self.STEP)
        fired = out.index('fired')
        self.assertIn('config {animations={enabled=false},scrolling={follow_focus=false}}', out[:fired])
        self.assertEqual(len([c for c in out[:fired] if 'layout:swapcol r' in c]), 2)
        self.assertIn('dispatch {window=address:a}', out[fired:])
        self.assertEqual(out[-1], 'animations=true follow=true')

    def test_wrap_on_a_special_workspace(self):
        out = self.run_step(self.STEP, ws=-98)
        self.assertIn('dispatch {window=address:a}', out[out.index('fired'):])
        self.assertEqual(out[-1], 'animations=true follow=true')

    def test_no_final_focus_after_leaving_the_workspace(self):
        out = self.run_step(self.STEP.replace('fire()', 'world.active = { address = "z", workspace = { id = 7 } }; fire()'))
        self.assertNotIn('dispatch {window=address:a}', out[out.index('fired'):])
        self.assertEqual(out[-1], 'animations=true follow=true')

    def test_rotated_monitor_uses_its_logical_width(self):
        # 1920x1080 turned a quarter is 1080 wide: the far window at x=1500 is off-screen.
        xs = (0, 600, 1500)
        flat = self.run_step(self.STEP, direction='l', xs=xs, active=1)
        self.assertFalse([c for c in flat if 'swapcol' in c])   # visible: focus moves across
        turned = self.run_step(self.STEP, direction='l', xs=xs, active=1, transform=1)
        self.assertEqual(len([c for c in turned if 'layout:swapcol l' in c]), 2)

    def test_settings_come_back_when_a_dispatch_fails(self):
        out = self.run_step('''
world.on_dispatch = function(d) if d == "layout:swapcol r" then error("dispatch failed") end end
note("start")
local ok, err = pcall(TrickyTiles.step, "r")
note("ok=" .. tostring(ok) .. " pending=" .. tostring(TrickyTiles.pending))
note("animations=" .. tostring(world.config["animations.enabled"]) .. " follow=" .. tostring(world.config["scrolling.follow_focus"]))
''')
        self.assertEqual(out[-2:], ['ok=false pending=nil', 'animations=true follow=true'])

if __name__ == '__main__':
    unittest.main()
