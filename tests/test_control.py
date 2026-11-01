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


def home_with(hyprland='-- user config\nrequire("hypr.input")\n', files=None):
    tmp = Path(tempfile.mkdtemp())
    (tmp / '.config/hypr').mkdir(parents=True)
    (tmp / '.config/omarchy').mkdir(parents=True)
    if hyprland is not None:
        (tmp / '.config/hypr/hyprland.lua').write_text(hyprland)
    for name, text in (files or {}).items():
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
        self.assertFalse(any(c.startswith(('animation', 'curve', 'gesture')) for c in calls))

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
        self.calls = []
        patcher = mock.patch.object(control, 'hypr', side_effect=self.fake_hypr)
        patcher.start()
        self.addCleanup(patcher.stop)

    def fake_hypr(self, *args):
        self.calls.append(args)
        return ''

    def test_install_adds_one_line_and_backup(self):
        home = home_with()
        p = control.paths(home)
        control.install(control.fresh(), p)
        text = p['hyprland'].read_text()
        self.assertEqual(text.count(control.MARK), 1)
        self.assertTrue(text.startswith('-- user config\nrequire("hypr.input")\n'))
        self.assertTrue(p['module'].exists())
        self.assertEqual(len(list(p['hypr_dir'].glob('hyprland.lua.bak.*-tricky-tiles'))), 1)
        control.install(control.fresh(), p)   # idempotent
        self.assertEqual(p['hyprland'].read_text().count(control.MARK), 1)
        self.assertIn(('reload', 'config-only'), self.calls)

    def test_uninstall_restores_file(self):
        original = '-- user config\nrequire("hypr.input")\n'
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
        original = '-- user config\nrequire("hypr.input")\n'
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


if __name__ == '__main__':
    unittest.main()
