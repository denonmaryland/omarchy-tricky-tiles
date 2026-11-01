// Logic that lives inside TrickyTiles.qml, lifted out of the source and run in
// isolation: the share-code check used by Paste, the slider's wheel steps and the
// keyboard rows; plus the panel bookkeeping in lib/spring.js (queued values,
// swipe conflicts).
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');

const qml = fs.readFileSync(path.join(__dirname, '../TrickyTiles.qml'), 'utf8');
const control = fs.readFileSync(path.join(__dirname, '../helpers/control.py'), 'utf8');

test('Paste accepts exactly the codes control.py accepts', () => {
  const ours = new RegExp(qml.match(/\/(\^TT1-[^/]+\$)\/\.test\(pasted\)/)[1]);
  const theirs = new RegExp(control.match(/CODE_RE = re\.compile\(r'([^']+)'\)/)[1].replace(/\((?!\?)/g, '(?:'));
  for (const code of ['TT1-290-15-30-1', 'TT1-150-0-0-0', 'TT1-500-40-100-1', 'TT1-99-9-9-0', 'TT1-1000-15-30-1',
                      'TT1-290-150-30-1', 'TT1-290-15-1000-1', 'TT1-290-15-30-2', 'TT1-29a-15-30-1', 'TT2-290-15-30-1',
                      'TT1-290-15-30', '', 'rm -rf /'])
    assert.equal(ours.test(code), theirs.test(code), code);
});

test('the pasted text is not shadowed by its own declaration', () => {
  assert.doesNotMatch(qml, /const text\s*=\s*\(?\s*text\b/);
});

test('wheel deltas add up to 120 per step and ignore leftovers from the other way', () => {
  const body = qml.match(/function wheelSteps\(delta\) \{([\s\S]*?)\n    \}/)[1];
  const sl = { wheelRest: 0 };
  const wheelSteps = vm.runInNewContext(`(function(delta) {${body}})`, { sl, Math });
  const run = (deltas) => deltas.reduce((sum, d) => sum + wheelSteps(d), 0);

  assert.equal(run([30, 30, 30]), 0);           // trackpad: nothing until a full notch
  assert.equal(run([30]), 1);
  assert.equal(run([120]), 1);                  // mouse notch
  assert.equal(run([480]), 1);                  // oversized notch stays one step
  assert.equal(run([-120, -120]), -2);
  sl.wheelRest = 0;
  assert.equal(run([100, -30]), 0);             // reversing drops the forward remainder
  assert.equal(sl.wheelRest, -30);
  assert.equal(run([-90]), -1);
});

const spring = fs.readFileSync(path.join(__dirname, '../lib/spring.js'), 'utf8').replace(/^\.pragma library\n/, '');
const S = vm.createContext({ Math });
vm.runInContext(spring, S);
const plain = (x) => JSON.parse(JSON.stringify(x));

test('queued changes stay on screen when an older reply lands', () => {
  const saved = { duration: 290, bounce: 15, preset: 'spring', enabled: true, swipes: false };
  // Duration 400 is in flight; then bounce 30 and swipes on are queued.
  const queue = [
    { key: 'set:bounce', patch: { bounce: 30, enabled: true, preset: 'custom' } },
    { key: 'override', patch: null },
    { key: 'set:swipes', patch: { swipes: true } },
  ];
  const reply = { ...saved, duration: 400, preset: 'custom' };
  assert.deepEqual(plain(S.withPending(reply, queue)),
                   { duration: 400, bounce: 30, preset: 'custom', enabled: true, swipes: true });
  assert.deepEqual(plain(S.withPending(reply, [])), reply);
  assert.equal(reply.bounce, 15);               // the reply itself is left alone
  // A later queued preset wins over an earlier queued slider value.
  const later = [{ patch: { duration: 350 } }, { patch: { preset: 'glide', duration: 420 } }];
  assert.equal(S.withPending(saved, later).duration, 420);
});

test('swipe conflicts follow the selected finger count and tolerate older helpers', () => {
  const line = { file: '.config/hypr/input.lua', line: 12, text: 'hl.gesture({fingers = 3, direction = "horizontal"})' };
  let info = S.swipeConflicts([line], { 3: [line], 4: [] }, '3');
  assert.deepEqual(plain(info), { list: [line], other: '4', otherFree: true });
  // Switched to 4 before the reply: the by-fingers map answers for 4 straight away.
  info = S.swipeConflicts([line], { 3: [line], 4: [] }, '4');
  assert.deepEqual(plain(info), { list: [], other: '3', otherFree: false });
  // Both taken: nothing to offer.
  assert.equal(S.swipeConflicts([line], { 3: [line], 4: [line] }, '3').otherFree, false);
  // Older helper: only `conflicts`, and nothing known about the other count.
  assert.deepEqual(plain(S.swipeConflicts([line], null, '3')), { list: [line], other: '4', otherFree: false });
  assert.deepEqual(plain(S.swipeConflicts(undefined, undefined, 3)), { list: [], other: '4', otherFree: false });
});

test('skipped directions read as a list', () => {
  assert.equal(S.listText([]), '');
  assert.equal(S.listText(undefined), '');
  assert.equal(S.listText(['left']), 'left');
  assert.equal(S.listText(['left', 'right']), 'left and right');
  assert.equal(S.listText(['left', 'right', { direction: 'up' }]), 'left, right and up');
});

test('keyboard rows match what each tab shows, top to bottom', () => {
  const body = qml.match(/readonly property var rows: \{([\s\S]*?)\n  \}/)[1];
  const rows = (ctx) => plain(vm.runInNewContext(`(function() {${body}})()`, {
    loaded: true, installed: true, tab: 0, skipped: [], ...ctx,
    values: { enabled: true, swipes: false, swipe_horizontal: 'windows', tracking: 'follow', ...ctx.values },
    swipeInfo: { list: [], other: '4', otherFree: false, ...ctx.swipeInfo },
    hasConflicts: !!(ctx.swipeInfo && ctx.swipeInfo.list && ctx.swipeInfo.list.length),
  }));
  assert.deepEqual(rows({}), ['enabled', 'tabs', 'compare', 'try', 'preview_windows', 'presets', 'duration', 'bounce', 'travel', 'focus_fade']);
  assert.deepEqual(rows({ values: { enabled: false } }), ['enabled', 'tabs', 'compare', 'presets', 'duration', 'bounce', 'travel', 'focus_fade']);
  assert.deepEqual(rows({ tab: 1 }), ['enabled', 'tabs', 'swipes']);
  // Swipes off, but the config already uses three fingers and four are free.
  assert.deepEqual(rows({ tab: 1, swipeInfo: { list: [{}], otherFree: true } }), ['enabled', 'tabs', 'swipes', 'fingers', 'use_fingers']);
  assert.deepEqual(rows({ tab: 1, values: { swipes: true }, swipeInfo: { list: [{}] } }),
                   ['enabled', 'tabs', 'swipes', 'tracking', 'swipe_horizontal', 'swipe_vertical', 'sensitivity', 'fingers']);
  assert.deepEqual(rows({ tab: 1, installed: false }), ['enabled', 'allow', 'tabs']);
  assert.deepEqual(rows({ tab: 2 }), ['enabled', 'tabs', 'scope', 'workspace_axis', 'power_saver', 'share', 'uninstall']);
});

test('a failed reply never resyncs power saver (no retry loop)', () => {
  const qml = require('fs').readFileSync(require('path').join(__dirname, '..', 'TrickyTiles.qml'), 'utf8')
  const receive = qml.slice(qml.indexOf('function receive('), qml.indexOf('function send('))
  assert.match(receive, /if \(result\.ok\) syncPower\(\)/)
  assert.doesNotMatch(receive.replace(/if \(result\.ok\) syncPower\(\)/, ''), /syncPower\(\)/)
})

test('the panel watches the settings file where the helper writes it', () => {
  const qml = require('fs').readFileSync(require('path').join(__dirname, '..', 'TrickyTiles.qml'), 'utf8')
  assert.match(qml, /XDG_CONFIG_HOME[^\n]*\/omarchy\/tricky-tiles\.json/)
})
