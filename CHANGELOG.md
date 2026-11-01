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

Fixed after an independent review (Codex, 2026-10-31): swipes you already define are skipped
instead of making Hyprland refuse the config, and the panel shows them with a one-click switch
to the free finger count; every change, Remove included, is one transaction that puts files
and the running desktop back if any step fails, and install checks the module really loaded;
`XDG_CONFIG_HOME` is honoured; install and Remove keep `hyprland.lua` byte for byte (CRLF,
trailing blank lines); the carousel wraps correctly on special workspaces and rotated
monitors; "Up and down" no longer snaps back while swipes are off; paths with quotes no longer
break live changes; the panel scrolls on short screens; Compare, Try it and the windows toggle
are reachable by keyboard; controls no longer snap back while changes queue; Remove asks twice;
demo media are WebP (5 MB instead of 13 MB).

Second review pass: a reload now has to prove this module ran (fresh points on a `ttLoaded`
curve), and Remove has to prove it stopped, so a config that fails to parse can no longer fake
success; remembered config errors are tied to the exact config they came from; only files your
config actually loads count as conflicts, using Hyprland's directional overlap rule; bare-CR
line endings round-trip; power saver never retries a failed change in a loop; the panel
watches the settings file under `XDG_CONFIG_HOME`.

Third review pass: Tricky Tiles no longer guesses from your Lua files. It reloads and asks
Hyprland, reading the errors in the same request as the reload (Omarchy's shell traffic empties
Hyprland's error list moments later), skips exactly the swipe directions Hyprland refused, and
takes them back once you free them. Only errors that point at Tricky Tiles block a change;
others are reported. Every change, Apply included, proves it ran with fresh `ttLoaded` points,
and Remove first reloads a do-nothing stand-in and proves that before deleting anything.

Release polish: nothing in the panel changes before its settings load; the preview never plays
while the panel is closed; "Workspaces travel" is locked, with a reason, whenever swipes move
workspaces; the panel mentions errors elsewhere in your Hyprland config. Checked on Omarchy's
stock bar (added with `omarchy plugin enable`) and stock popup, in Tokyo Night and Catppuccin
Latte. New preview.png from the stock popup.
