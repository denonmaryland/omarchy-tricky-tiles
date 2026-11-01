// Logic that lives inside TrickyTiles.qml, lifted out of the source and run in
// isolation: the share-code check used by Paste and the slider's wheel steps.
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
