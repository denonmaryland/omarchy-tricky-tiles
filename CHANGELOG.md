# Changelog

## 1.0.0 (unreleased, 2026-10-28)

First public version.

- Native Hyprland springs for window moves, workspace switches and the special workspace,
  set as duration and bounce (damping ratio = 1 − bounce).
- Five presets drawn as their own curves: Spring, Glide, Snappy, Lively, Reduced. Sliders for
  duration, bounce and workspace slide (full slide, lift and fade, or a plain crossfade), and a
  focus fade that eases the arriving window up to its focused opacity.
- Live preview that runs the same closed-form spring as Hyprland, using stills of your own
  windows, with hold-to-compare against stock Omarchy and "Try it on my desktop".
- Trackpad swipes that follow your fingers (Hyprland's own trackers, finished by the spring) or
  move one window/workspace per swipe, with optional wrap-around; 3 or 4 fingers; finger travel.
  Lists any swipes already defined in your config, since those take precedence.
- Optional "Everything" scope: opening, closing, menus and fades share the same duration.
- Power saver: swap to Snappy or Reduced while the power-saver profile is on.
- Share codes (`TT1-290-15-30-1`) to copy and paste a motion.
- Nothing touches Hyprland until you press Allow; Remove takes the line and module out again.
- Works in Omarchy's stock popup, or the frosted host from the denon.bar plugin when present.
- Keyboard: every control on every tab is reachable (arrows, Enter/Space, `1`/`2`/`3` for tabs),
  with visible focus rings.

Fixed before release (code review, 2026-10-31): Paste threw instead of importing; a failed
install or change left files or saved settings out of step with Hyprland; leaving "Everything"
kept its animations until the next reload; symlinked `hyprland.lua` was replaced by a copy;
multi-line swipes in your config went undetected; a trackpad scroll over a slider jumped it to
the end; changes sent in the same moment could be dropped; the Fingers control hung below the Swipes card.
