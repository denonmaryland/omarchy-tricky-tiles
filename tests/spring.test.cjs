// The panel's spring maths (lib/spring.js), loaded the way QML does, checked
// against the closed-form solution and the Python helper's formula.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');

const source = fs.readFileSync(path.join(__dirname, '../lib/spring.js'), 'utf8').replace(/^\.pragma library\n/, '');
const S = vm.createContext({ Math });
vm.runInContext(source, S);

test('stiffness and damping match control.py', () => {
  const s = S.springOf(290, 15);
  assert.ok(Math.abs(s.k - Math.pow(2 * Math.PI / 0.29, 2)) < 1e-9);
  assert.ok(Math.abs(s.c / (2 * Math.sqrt(s.k)) - 0.85) < 1e-12);
});

test('critically damped spring never overshoots and matches 1-(1+wt)e^-wt', () => {
  const samples = S.curve(300, 0, 600, 60);
  const w = 2 * Math.PI / 0.3;
  samples.forEach((p, i) => {
    const t = i * 0.01;
    assert.ok(p <= 1 + 1e-9);
    assert.ok(Math.abs(p - (1 - (1 + w * t) * Math.exp(-w * t))) < 1e-6);
  });
});

test('overshoot follows exp(-pi*zeta/sqrt(1-zeta^2))', () => {
  for (const bounce of [15, 25, 40]) {
    const z = 1 - bounce / 100;
    const expected = Math.exp(-Math.PI * z / Math.sqrt(1 - z * z));
    const peak = Math.max(...S.curve(300, bounce, 1500, 3000)) - 1;
    assert.ok(Math.abs(peak - expected) < 2e-4, `bounce ${bounce}: ${peak} vs ${expected}`);
  }
});

test('stats for the default preset on a 1500 px switch', () => {
  const st = S.settle(290, 15, 1500);
  assert.ok(st.arrive > 120 && st.arrive < 180, JSON.stringify(st));
  assert.equal(st.overshoot, Math.round(1500 * Math.exp(-Math.PI * 0.85 / Math.sqrt(1 - 0.85 * 0.85))));
});

test('stock bezier and share code', () => {
  assert.ok(Math.abs(S.bezier(0.23, 1, 0.32, 1, 1) - 1) < 1e-6);
  assert.ok(S.bezier(0.23, 1, 0.32, 1, 0.1) > 0.35);   // easeOutQuint: about 39% at 10% of the time
  assert.equal(S.encode({ duration: 290, bounce: 15, travel: 30, focus_fade: true }), 'TT1-290-15-30-1');
});
