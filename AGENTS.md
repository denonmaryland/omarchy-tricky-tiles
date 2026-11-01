# Tricky Tiles: notes for contributors and agents

- This repository is the source of truth. A local install lives at
  `~/.config/omarchy/plugins/io.github.denonmaryland.tricky-tiles/`; deploy with
  `rsync -a --delete --exclude .git --exclude tests --exclude tools --exclude docs ./ <that dir>/`
  and then `omarchy restart shell` (QML hot-reload keeps stale components).
- Never edit `/usr/share/omarchy/`. The plugin only writes `~/.config/omarchy/tricky-tiles.json`,
  `~/.config/hypr/tricky-tiles.lua`, one marked line in `~/.config/hypr/hyprland.lua` (after
  consent, with a backup), and `$XDG_STATE_HOME/tricky-tiles/` (lock and power-saver flag).
- `hypr/module.lua` is the Lua body; `helpers/control.py` prepends the settings table. Motion
  changes apply live with `hyprctl eval`; gesture changes need a reload (gestures register once
  per Lua state, and the first registered gesture for a finger count/direction wins).
- Run all checks before a release: `python3 -m unittest discover -s tests`,
  `node --test tests/*.cjs`, `omarchy plugin validate .`.
- `tools/` holds the measurement harness (60 fps capture, spring fitting, a virtual touchpad).
  Test chords use Super+Ctrl+Shift; never script Ctrl+Alt+F-keys (they switch the VT).
- Keep motion rules from the userinterface.wiki skills: springs for gestures and interruptible
  moves, easing for state changes, user-initiated motion arriving well under 300 ms.
