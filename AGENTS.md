# Tricky Tiles: notes for contributors and agents

- This repository is the source of truth. A local install lives at
  `~/.config/omarchy/plugins/io.github.denonmaryland.tricky-tiles/`; deploy with
  `rsync -a --delete --exclude .git --exclude tests --exclude tools --exclude docs ./ <that dir>/`
  and then `omarchy restart shell` (QML hot-reload keeps stale components).
- Never edit `/usr/share/omarchy/`. The plugin only writes `~/.config/omarchy/tricky-tiles.json`,
  `~/.config/hypr/tricky-tiles.lua`, one marked line in `~/.config/hypr/hyprland.lua` (after
  consent, with a backup), and `$XDG_STATE_HOME/tricky-tiles/` (lock, power-saver flag,
  `last-load.json`).
- `hypr/module.lua` is the Lua body; `helpers/control.py` prepends the settings table. Motion
  changes apply live with `hyprctl eval`; gesture changes need a reload (gestures register once
  per Lua state). Ask Hyprland, don't infer from the user's files: every render carries a fresh
  `ttLoaded` curve that is read back after each reload/eval; Hyprland 0.56.2's `hl.gesture`
  refuses an overlapping swipe with a config error only (no raise, no return value), so the
  helper reads `hyprctl configerrors`, puts refused directions in `gestures.skip` and reloads
  once more. Only errors naming the module or its loader line block a change. The static scan
  of the user's files is a best-effort hint for the panel and decides nothing.
- Run all checks before a release: `python3 -m unittest discover -s tests`,
  `node --test tests/*.cjs`, `omarchy plugin validate .`.
- `tools/` holds the measurement harness (60 fps capture, spring fitting, a virtual touchpad).
  Test chords use Super+Ctrl+Shift; never script Ctrl+Alt+F-keys (they switch the VT).
- Keep motion rules from the userinterface.wiki skills: springs for gestures and interruptible
  moves, easing for state changes, user-initiated motion arriving well under 300 ms.
- Sandboxing the helper: a temp `HOME` is not enough. The helper honours an absolute
  `XDG_CONFIG_HOME`/`XDG_STATE_HOME`, which Omarchy sessions set to the real folders, so
  always run it as `env -u XDG_CONFIG_HOME -u XDG_STATE_HOME HOME=$tmp ...` (or point both at
  the temp tree). A review run that skipped this once edited the live config. The unit tests
  fail loudly if `control.paths()` leaves the temp folder or anything reaches the real `hyprctl`.
