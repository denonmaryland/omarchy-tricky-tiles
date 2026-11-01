# Tricky Tiles

Spring physics for switching windows and workspaces on Omarchy. Moves start from rest,
keep their momentum when you interrupt them, and settle, instead of jumping most of the
way in the first frame. Pick a preset, tune it with three sliders, and let three-finger
swipes follow your fingers.

![Three-finger swipes: up to the next workspace, through its windows and around, back down](docs/demo-workspaces.webp)

## What it does

![Opening Tricky Tiles from the bar, dragging Bounce, picking presets and stepping through the tabs](docs/demo-panel.webp)

- **Presets, drawn as their curves**: Spring (default), Glide (no overshoot), Snappy,
  Lively (a visible bounce that settles) and Reduced (least motion).
- **Sliders**: duration, bounce, and workspace slide (full slide, a short lift with a fade,
  or a plain crossfade). A focus fade eases the arriving window up to its focused opacity.
- **A live preview** that runs the exact spring Hyprland uses, with stills of your own
  windows. Hold *Compare* to see stock Omarchy, or press *Try it on my desktop* for one real
  window step and workspace hop.
- **Trackpad swipes**: *Follow fingers* (content tracks your fingers, then the spring finishes
  from where you let go) or *One per swipe* (one window or workspace per swipe, with optional
  wrap-around on the window strip). Three or four fingers, adjustable finger travel.
- **Everything mode** (optional): opening, closing, menus and fades share the same duration.
- **Power saver**: switch to Snappy or Reduced while the power-saver profile is on.
- **Share codes** such as `TT1-290-15-30-1` to copy a motion to another machine.
- The numbers for your screen: when a full-width switch arrives, when it is at rest, and how
  far it overshoots, at your monitor's width and refresh rate.
- **Works from the keyboard**: Up/Down move between rows, Left/Right change the focused value,
  Enter or Space activates it, and `1`/`2`/`3` (or `[`/`]`) switch tabs.

![One window per swipe with wrap-around: past the last window, the first comes round again](docs/demo-carousel.webp)

![The panel: Motion, Swipes and More](docs/panel-tabs.png)

## Install

```bash
omarchy plugin add https://github.com/denonmaryland/omarchy-tricky-tiles --enable
```

Click the ◫ icon in the bar and press **Allow** (see below). Requirements: Omarchy with the
Lua Hyprland config and **Hyprland 0.56 or newer** (native spring curves), Python 3, and
`wl-clipboard` for share codes (already part of Omarchy).

## What it changes on your system

Nothing, until you press **Allow** in the panel. Then:

1. `tricky-tiles.lua` is written next to your `hyprland.lua` (the generated module), in
   `$XDG_CONFIG_HOME/hypr/` or `~/.config/hypr/`, wherever Hyprland reads its config.
2. One marked line is appended to `hyprland.lua`, in the file's own line-ending style; an
   exact timestamped backup `hyprland.lua.bak.<time>-tricky-tiles` is kept next to it:

   ```lua
   do local d = debug.getinfo(1, "S").source:match("^@(.*/)") or os.getenv("HOME") .. "/.config/hypr/"; local p = d .. "tricky-tiles.lua"; local f = io.open(p); if f then f:close(); dofile(p) end end -- Tricky Tiles (remove via its panel)
   ```

3. Settings are kept in `~/.config/omarchy/tricky-tiles.json`; a lock file, the power-saver
   flag and `last-load.json` (which swipes Hyprland turned down at the last load) live in
   `~/.local/state/tricky-tiles/`.

No other byte of your config is edited; Remove takes out exactly what was added. It uses no `sudo`, services, downloads or binaries.
Motion changes apply instantly with `hyprctl eval`; swipe changes reload Hyprland. Every change
is checked: each version of the module sets a fresh marker curve, and Tricky Tiles reads it back
from `hyprctl animations` to confirm Hyprland runs that version. Errors that are not about Tricky
Tiles (its module or its line in `hyprland.lua`) never block a change.

**Swipes you already define are left alone.** Hyprland refuses a swipe that an earlier one with
the same finger count already covers, and reports it as a config error. Tricky Tiles reads those
errors after the reload, leaves the refused directions to you, and reloads once more so no error
remains (Hyprland's error bar may show for a moment). The same goes for a swipe of yours that
loads after Tricky Tiles and would be refused because of it. Directions you free later are taken
at your next swipe change. The Swipes tab also lists the swipes it finds in your files, with file
and line, and offers the other finger count when it is free; that list is a best-effort reading of
your files, while what Tricky Tiles registers comes only from Hyprland's answer.

**Dotfile managers are safe.** If `hyprland.lua` is a symlink (stow, chezmoi), Tricky Tiles
writes through it, so the link stays a link. If Hyprland rejects a change, or the module does not
load, the previous files come back and your saved settings are left as they were.

The frosted popup is used only alongside the separate `denon.bar` plugin; everyone else gets
Omarchy's stock popup.

## Remove

1. Open the panel, **More → Remove**. Hyprland first reloads with an empty stand-in module, to
   confirm that nothing of Tricky Tiles runs any more; then the line comes out of `hyprland.lua`
   and the module is deleted. Your previous motion and swipes return. If that reload fails,
   nothing is removed.
2. `omarchy plugin remove io.github.denonmaryland.tricky-tiles`
3. Optionally delete `~/.config/omarchy/tricky-tiles.json` and `~/.local/state/tricky-tiles/`.

If you remove the plugin first, the module keeps working (it lives in `~/.config/hypr`). Delete
the marked line and `~/.config/hypr/tricky-tiles.lua` by hand, then `hyprctl reload`.

## How it works

Hyprland 0.56 ships native, closed-form spring curves that carry their velocity when an
animation is retargeted. Tricky Tiles exposes them as duration and bounce, like SwiftUI:

```
stiffness = (2π / duration)²        mass = 1
damping   = 4π (1 − bounce) / duration     →  damping ratio = 1 − bounce
```

The springs drive `windowsMove` (window strip steps, swipes, keyboard focus, width toggles),
`workspaces` and `specialWorkspace`. The focus fade (`fadeSwitch`) is a state change, so it
eases instead. *Follow fingers* uses Hyprland's own trackers (`scroll_move` for the window
strip of the scrolling layout, `workspace` for workspaces). Lua cannot move the strip
smoothly frame by frame, so this is the only way to get true 1:1 tracking. *One per swipe*
runs a small Lua function when the fingers lift.

Design rules come from the motion checks on [userinterface.wiki](https://www.userinterface.wiki)
([skills](https://github.com/raphaelsalaja/skills)): springs for gestures and interruptible
moves (`spring-for-gestures`, `spring-for-interruptible`), easing for state changes, small
overshoot (`physics-subtle-deformation`), one focal point (the arriving window), and moves
that arrive well under 300 ms.

## Measured, not eyeballed

Every preset was recorded at 60 fps on a real compositor, with an edge tracked frame by frame
and fitted to the spring model (`tools/`). The recorded moves match the spring the panel shows
to within about 1% RMS (0.1–0.4% on clean recordings).

![Before and after: a full-width window switch at 4× slow motion](docs/before-after-windows.gif)

![Workspace switch: full slide vs. lift and fade](docs/before-after-workspaces.gif)

- With a front-loaded ease-out curve, the first moving frame of a full-width switch on a
  60 Hz panel covered 50–70% of the distance (up to about 1,300 px in one frame). With the
  Spring preset it covered 26–51%, and when the first frame is on time the move starts
  1% → 8% → 18% → 30%.
- Two quick switches in a row glide as one move instead of hitching.
- Swipes were tested with a virtual multi-touch touchpad (drag, flick, short nudge, long drag;
  vertical workspaces; one-per-swipe with wrap-around).

Known open question: in most screen recordings the first frame of a switch that changes focus
appears 40–60 ms after the move starts, with any curve. Hyprland's main loop is not blocked
during that time, and blur, shadows, the cursor and the input method are ruled out. It may be
the recorder. Springs make the effect much smaller either way.

## Development

```bash
python3 -m unittest discover -s tests   # helper, rendered Lua module (with a stub hl API), install/remove
node --test tests/*.cjs                 # the panel's spring maths
omarchy plugin validate .
```

`tools/` holds the measurement harness. See `tools/README.md`.

## License

MIT
