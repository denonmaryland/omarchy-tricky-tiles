# Measurement tools

Developer tools used to tune and verify Tricky Tiles on a real compositor. They are
not loaded by the plugin. They open disposable foot windows on workspaces 98/99,
record the screen, and restore your workspace, focus and cursor afterwards.

Requirements: `foot`, `gpu-screen-recorder`, `ffmpeg`, ImageMagick, `ydotool` (for
key presses) and write access to `/dev/uinput` (for the virtual touchpad).

| Tool | What it does |
| --- | --- |
| `bench.py windows\|workspaces --label NAME` | Records window or workspace switches at 60 fps and tracks a window edge per frame |
| `fitall.py` | Fits each measured move to the spring model: start delay and RMS error |
| `springsim.py` | Simulates hyprutils' closed-form spring at 60 Hz for a full-width move |
| `vtouchpad.py swipe DX DY` | A virtual multi-touch touchpad; libinput treats it like a real one |
| `swipebench.py LABEL --mods none` | Records drag, flick, short and long swipes made with the virtual touchpad |
| `wstrack.py VIDEO` | Per-frame vertical runs for workspace switches |
| `sheet.sh VIDEO START COUNT OUT` | Numbered contact sheet of frames |
| `loopprobe.py` | Hammers Hyprland's IPC socket around switches to see whether its main loop blocks |

Safety: test key chords use Super+Ctrl+Shift. Never script Ctrl+Alt+F-keys: they switch
the virtual console. If that happens, `ydotool key 29:1 56:1 59:1 59:0 56:0 29:0` returns
to console 1.
