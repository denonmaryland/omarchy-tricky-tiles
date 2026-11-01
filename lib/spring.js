.pragma library
// Spring maths shared by the panel preview and the tests. Mirrors hyprutils'
// advanceSpring (closed form, mass 1), so the preview moves exactly like
// Hyprland. Duration/bounce map to stiffness/damping as in helpers/control.py.

function springOf(duration, bounce) {
  var d = Math.max(0.05, duration / 1000)
  return { k: Math.pow(2 * Math.PI / d, 2), c: 4 * Math.PI * (1 - bounce / 100) / d }
}

// Advance displacement-from-target x (target 0) and velocity v by dt seconds.
function advance(s, x, v, dt) {
  var w0 = Math.sqrt(s.k), g = s.c / 2
  if (g < w0 - 1e-4) {
    var wd = Math.sqrt(w0 * w0 - g * g), e = Math.exp(-g * dt), cs = Math.cos(wd * dt), sn = Math.sin(wd * dt)
    return [e * (x * cs + (v + g * x) / wd * sn), e * (v * cs - (g * v + w0 * w0 * x) / wd * sn)]
  }
  if (g <= w0 + 1e-4) {
    var e2 = Math.exp(-g * dt), b = v + g * x
    return [e2 * (x + b * dt), e2 * (v - g * b * dt)]
  }
  var r = Math.sqrt(g * g - w0 * w0), r1 = -g + r, r2 = -g - r
  var A = (v - r2 * x) / (r1 - r2), B = x - A, e1 = Math.exp(r1 * dt), e3 = Math.exp(r2 * dt)
  return [A * e1 + B * e3, A * r1 * e1 + B * r2 * e3]
}

// Progress samples (0 -> 1) of a move from rest, over `ms` milliseconds.
function curve(duration, bounce, ms, n) {
  var s = springOf(duration, bounce), st = [-1, 0], out = [0], dt = ms / 1000 / n
  for (var i = 0; i < n; i++) { st = advance(s, st[0], st[1], dt); out.push(1 + st[0]) }
  return out
}

// For a move of `distance` px: when it reaches 90%, when it is within 2 px of
// rest, and how far past the target it swings (px).
function settle(duration, bounce, distance) {
  var s = springOf(duration, bounce), st = [-1, 0], t90 = 0, rest = 0, peak = 0
  for (var i = 1; i <= 240; i++) {
    st = advance(s, st[0], st[1], 1 / 240)
    if (!t90 && st[0] >= -0.1) t90 = i
    if (Math.abs(st[0]) * distance > 2) rest = i
    peak = Math.max(peak, st[0])
  }
  return { arrive: Math.round(t90 * 1000 / 240), rest: Math.round((rest + 1) * 1000 / 240),
           overshoot: Math.round(peak * distance) }
}

// Cubic bezier easing (CSS-style control points), for the stock comparison.
function bezier(p1x, p1y, p2x, p2y, t) {
  function at(s, a, b) { return 3 * (1 - s) * (1 - s) * s * a + 3 * (1 - s) * s * s * b + s * s * s }
  var lo = 0, hi = 1
  for (var i = 0; i < 30; i++) { var mid = (lo + hi) / 2; if (at(mid, p1x, p2x) < t) lo = mid; else hi = mid }
  return at((lo + hi) / 2, p1y, p2y)
}

// Share codes: TT1-<duration>-<bounce>-<travel>-<fade>.
function encode(v) {
  return "TT1-" + Math.round(v.duration) + "-" + Math.round(v.bounce) + "-" + Math.round(v.travel) + "-" + (v.focus_fade ? 1 : 0)
}

// ---------- Panel bookkeeping (plain data in and out, so the tests can run it) ----------

// Each queued helper command carries the values it promises (`patch`). Until the
// queue drains, a helper reply is older than what the panel shows; laying the
// queued promises back over it stops a slider or segment snapping back.
function withPending(values, queue) {
  var v = {}
  for (var k in values) v[k] = values[k]
  for (var i = 0; i < queue.length; i++) {
    var patch = queue[i].patch
    if (patch) for (var p in patch) v[p] = patch[p]
  }
  return v
}

// Swipes the user's config already defines for `fingers`, and whether the other
// finger count is known to be free. Older helpers only send `conflicts`.
function swipeConflicts(conflicts, byFingers, fingers) {
  var here = String(fingers) === "4" ? "4" : "3", other = here === "4" ? "3" : "4"
  var list = byFingers && byFingers[here] ? byFingers[here] : conflicts || []
  var free = !!(byFingers && byFingers[other] && byFingers[other].length === 0)
  return { list: list, other: other, otherFree: free }
}

// "left", "left and right", "left, right and up" (strings, or {direction}).
function listText(items) {
  var words = []
  for (var i = 0; i < (items || []).length; i++) {
    var it = items[i]
    words.push(typeof it === "string" ? it : it && it.direction ? String(it.direction) : String(it))
  }
  if (words.length < 2) return words.join("")
  return words.slice(0, -1).join(", ") + " and " + words[words.length - 1]
}
