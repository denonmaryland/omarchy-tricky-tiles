"""Unit tests for helpers/control.py and the Lua module it renders (no Hyprland needed)."""
import contextlib
import io
import json
import math
import os
from pathlib import Path
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
    patcher = mock.patch.dict(os.environ, {'OMARCHY_PATH': str(OMARCHY)})
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


class FakeHyprland:
    """Answers hyprctl like Hyprland 0.56: a reload resets every animation and runs hyprland.lua,
    which runs the module while it has the loader line; curves outlive reloads. With `parses`
    off hyprland.lua has a syntax error, so a reload changes nothing; with `runs` off an error
    earlier in the config stops Lua before the loader line."""

    def __init__(self, p=None, errors='', runs=True, parses=True):
        self.p, self.errors, self.runs, self.parses = p, errors, runs, parses
        self.calls, self.leaves, self.curves = [], [], {}

    def __call__(self, *args):
        self.calls.append(args)
        module = self.p and self.p['module']
        if args[0] == 'reload' and self.parses:
            self.leaves = []
            if self.runs and control.installed(self.p) and module.exists():
                self.run(module.read_text())
        elif args[0] == 'eval' and 'dofile' in args[1] and module.exists():
            self.run(module.read_text())
        elif args[0] == 'animations':
            curves = [dict(name=n, X0=round(a, 2), Y0=round(b, 2), X1=round(c, 2), Y1=round(d, 2))
                      for n, (a, b, c, d) in self.curves.items()]
            return json.dumps([self.leaves, curves])
        elif args[0] == 'configerrors':
            return self.errors
        return ''

    def run(self, text):
        if control.module_enabled(text):
            self.leaves = [dict(name='windowsMove', bezier='spring:ttSpring')]
        marker = control.module_marker(text)
        if marker:   # a module rendered before the proof curve has none
            self.curves['ttLoaded'] = marker


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
        self.assertTrue(self.calls[0][0] == 'eval')
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
        sources = [s['path'] for s in control.config_sources(p)[0]]
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
        self.assertEqual(self.calls[0][0], 'eval')
        self.assertIn(('reload', 'config-only'), self.calls[1:])

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


class KnownErrors(unittest.TestCase):
    """`hyprctl eval` empties the error list, so a reload brings old errors back."""

    def fake(self, errors_after):
        state = {'reloaded': False}

        def hypr(*args):
            if args[0] == 'reload':
                state['reloaded'] = True
            if args[0] == 'configerrors':
                return errors_after if state['reloaded'] else ''
            return ''
        return hypr

    def test_old_error_is_not_blamed_on_us(self):
        p = control.paths(home_with())
        always = lambda *args: 'input.lua: old typo' if args[0] == 'configerrors' else ''
        with mock.patch.object(control, 'hypr', side_effect=always):
            control.reload_checked(p=p)   # a reload before any live change remembers it
        with mock.patch.object(control, 'hypr', side_effect=self.fake('input.lua: old typo')):
            control.reload_checked(p=p)   # after a live eval cleared the list
        with mock.patch.object(control, 'hypr', side_effect=self.fake('input.lua: old typo\nnew: ours')):
            with self.assertRaises(RuntimeError):
                control.reload_checked(p=p)

    def test_memory_is_dropped_when_the_config_changes(self):
        # Remembered, then fixed and reloaded elsewhere, then back in another form: ours to report.
        home = home_with(files={'input.lua': 'oops(\n'})
        p = control.paths(home)
        always = lambda *args: 'input.lua: old typo' if args[0] == 'configerrors' else ''
        with mock.patch.object(control, 'hypr', side_effect=always):
            control.reload_checked(p=p)
        (p['hypr_dir'] / 'input.lua').write_text('-- fixed\noops(\n')
        with mock.patch.object(control, 'hypr', side_effect=self.fake('input.lua: old typo')):
            with self.assertRaises(RuntimeError):
                control.reload_checked(p=p)

    def test_our_line_and_module_do_not_change_the_config(self):
        home = home_with(files={'input.lua': '-- mine\n'})
        p = control.paths(home)
        before = control.config_fingerprint(p)
        with mock.patch.object(control, 'hypr', side_effect=FakeHyprland(p)):
            control.install(control.fresh(), p)
        self.assertEqual(control.config_fingerprint(p), before)
        (p['hypr_dir'] / 'input.lua').write_text('-- mine, edited\n')
        self.assertNotEqual(control.config_fingerprint(p), before)

    def test_an_older_memory_file_is_ignored(self):
        p = control.paths(home_with())
        p['state'].mkdir(parents=True)
        (p['state'] / 'known-errors').write_text('input.lua: old typo\n')   # before configs were tracked
        with mock.patch.object(control, 'hypr', side_effect=self.fake('input.lua: old typo')):
            with self.assertRaises(RuntimeError):
                control.reload_checked(p=p)

    def test_without_memory_a_returning_error_fails(self):
        with mock.patch.object(control, 'hypr', side_effect=self.fake('input.lua: old typo')):
            with self.assertRaises(RuntimeError):
                control.reload_checked()


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
        with mock.patch.object(control, 'hypr', return_value=''):
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
            with self.assertRaisesRegex(RuntimeError, 'did not load.*hyprland.lua:1: x'):
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
        hl.parses = False   # an error that cannot be read as a refused reload: the leaves tell
        with mock.patch.object(control, 'hypr', side_effect=hl):
            with self.assertRaisesRegex(RuntimeError, 'kept running Tricky Tiles'):
                control.uninstall(p)
        self.assertEqual((p['hyprland'].read_bytes(), p['module'].read_bytes()), (installed, module))

    def test_remove_with_motion_off_fails_on_a_refused_reload(self):
        p, _, hl = self.installed(enabled=False)
        installed = p['hyprland'].read_bytes()
        hl.parses, hl.errors = False, self.SYNTAX
        with mock.patch.object(control, 'hypr', side_effect=hl):
            with self.assertRaisesRegex(RuntimeError, 'did not reload.*unexpected symbol'):
                control.uninstall(p)
        self.assertEqual(p['hyprland'].read_bytes(), installed)
        self.assertTrue(p['module'].exists())

    def test_remove_succeeds_once_the_module_is_gone(self):
        p, _, hl = self.installed()
        with mock.patch.object(control, 'hypr', side_effect=hl):
            control.uninstall(p)
        self.assertFalse(control.installed(p))
        self.assertEqual(hl.leaves, [])

    def test_unreadable_answer_does_not_block(self):
        home = home_with()
        p = control.paths(home)
        with mock.patch.object(control, 'hypr', return_value='not json'):
            control.install(control.fresh(), p)
            control.uninstall(p)
        self.assertFalse(control.installed(p))

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
        code = calls[-1][1]
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


class SwipeOverlap(unittest.TestCase):
    """Hyprland 0.56 refuses an overlapping swipe, so ours step aside (TrackpadGestures.cpp)."""

    def found(self, *lines, fingers='3'):
        home = home_with(files={'input.lua': '\n'.join(lines)})
        v = control.fresh()
        v['fingers'] = fingers
        return control.conflicts(v, control.paths(home)), home

    def skipped(self, found, **over):
        v = control.fresh()
        v.update(swipes=True, **over)
        return control.skipped(v, found)

    def test_directions_that_overlap(self):
        found, _ = self.found('hl.gesture({ fingers = 3, direction = "left", action = "close" })')
        self.assertEqual(found[0]['directions'], ['left'])
        self.assertEqual((found[0]['direction'], found[0]['after']), ('left', False))
        # Hyprland takes our later "horizontal" next to their "left": left swipes stay theirs.
        self.assertEqual(self.skipped(found, tracking='follow', swipe_horizontal='windows'), [])
        self.assertEqual(self.skipped(found, tracking='step', swipe_horizontal='windows'), ['left'])
        self.assertEqual(self.skipped(found, tracking='step', swipe_horizontal='off', swipe_vertical='workspaces'), [])
        found, _ = self.found('hl.gesture({ fingers = 3, direction = "swipe", action = "move" })')
        self.assertEqual(self.skipped(found, tracking='step', swipe_horizontal='windows', swipe_vertical='workspaces'),
                         ['left', 'right', 'up', 'down'])
        found, _ = self.found('hl.gesture({ fingers = 3, direction = "Vert", action = "workspace" })')
        self.assertEqual(self.skipped(found, tracking='follow', swipe_horizontal='windows', swipe_vertical='workspaces'),
                         ['vertical'])

    def test_an_earlier_axis_takes_both_sides(self):
        found, _ = self.found('hl.gesture({ fingers = 3, direction = "horizontal", action = "workspace" })')
        self.assertEqual(self.skipped(found, tracking='step', swipe_horizontal='windows', swipe_vertical='workspaces'),
                         ['left', 'right'])
        self.assertEqual(self.skipped(found, tracking='follow', swipe_horizontal='windows', swipe_vertical='workspaces'),
                         ['horizontal'])
        found, _ = self.found('hl.gesture({ fingers = 3, direction = "r", action = "close" })')
        self.assertEqual(self.skipped(found, tracking='step', swipe_horizontal='windows'), ['right'])
        self.assertEqual(self.skipped(found, tracking='follow', swipe_horizontal='windows'), [])

    def test_swipes_after_our_line_are_the_ones_refused(self):
        # A swipe loading after ours is refused when ours has its direction or is its axis, so
        # ours steps aside for it; a later "horizontal" never minds an earlier "left".
        lines = ['hl.gesture({ fingers = 3, direction = "horizontal", action = "workspace" })',
                 'hl.gesture({ fingers = 3, direction = "up", action = "close" })']
        home = home_with(USER_CONFIG + control.LOADER + '\n' + '\n'.join(lines) + '\n')
        found = control.conflicts(control.fresh(), control.paths(home))
        self.assertEqual([(f['direction'], f['after']) for f in found], [('horizontal', True), ('up', True)])
        self.assertEqual(self.skipped(found, tracking='step', swipe_horizontal='windows', swipe_vertical='workspaces'),
                         ['up'])
        self.assertEqual(self.skipped(found, tracking='follow', swipe_horizontal='windows', swipe_vertical='workspaces'),
                         ['horizontal', 'vertical'])

    def test_what_never_overlaps(self):
        found, _ = self.found(
            'hl.gesture({ fingers = 3, direction = "horizontal", mods = "SUPER", action = "workspace" })',
            'hl.gesture({ fingers = 3, direction = "pinchin", action = "close" })',
            'hl.gesture({ fingers = 3, direction = "up", action = "unset" })',
            'hl.gesture({ fingers = 3, action = function() hl.dispatch(hl.dsp.focus({ direction = "l" })) end, direction = "down" })')
        self.assertEqual([f['line'] for f in found], [4])
        self.assertEqual(found[0]['directions'], ['down'])

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
        self.assertEqual(r['skipped'], [])

    def test_enabling_swipes_skips_taken_directions(self):
        home = home_with(files={'input.lua': 'hl.gesture({ fingers = 3, direction = "horizontal", action = "workspace" })\n'})
        p = control.paths(home)
        out = io.StringIO()
        with mock.patch.object(control, 'paths', return_value=p), \
                mock.patch.object(control, 'hypr', side_effect=FakeHyprland(p)), \
                contextlib.redirect_stdout(out):
            control.main(['install'])
            control.main(['set', 'swipes', 'true'])
        r = json.loads(out.getvalue().splitlines()[-1])
        self.assertTrue(r['ok'])
        self.assertEqual(r['skipped'], ['horizontal'])   # every direction asked for is taken
        self.assertIn('skip = { horizontal = true }', p['module'].read_text())
        lua = subprocess.run(['lua', str(ROOT / 'tests/hl_stub.lua'), str(p['module'])], capture_output=True, text=True)
        self.assertEqual(lua.returncode, 0, lua.stderr)
        self.assertFalse([c for c in lua.stdout.splitlines() if c.startswith('gesture')])
        self.assertFalse([c for c in lua.stdout.splitlines() if 'workspace_swipe_distance' in c])

    def test_module_registers_only_free_directions(self):
        v = control.fresh()
        v.update(swipes=True, tracking='step', swipe_horizontal='windows', swipe_vertical='workspaces')
        control.normalize(v)
        path = Path(tempfile.mkdtemp()) / 'tricky-tiles.lua'
        path.write_text(control.render(v, ['left', 'up']))
        r = subprocess.run(['lua', str(ROOT / 'tests/hl_stub.lua'), str(path)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        dirs = sorted(c.split('direction=')[1].split(',')[0] for c in r.stdout.splitlines() if c.startswith('gesture'))
        self.assertEqual(dirs, ['down', 'right'])

    def test_skip_change_needs_a_reload(self):
        v = control.fresh()
        v['swipes'] = True
        self.assertNotEqual(control.gesture_part(control.render(v)), control.gesture_part(control.render(v, ['horizontal'])))


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
