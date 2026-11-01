-- Tricky Tiles module body. helpers/control.py prepends `local S = { ... }` (the
-- current settings) and writes the result to tricky-tiles.lua next to the user's
-- hyprland.lua, which loads it through one marked line. Hyprland re-runs it on every
-- reload (a fresh Lua state), and the panel re-runs it with `hyprctl eval` to
-- apply motion changes live; gestures are only registered on a fresh load. S.loaded is a
-- fresh set of curve points per render (see the end of this file).

TrickyTiles = TrickyTiles or {}
local TT = TrickyTiles
TT.S = S

local function anim(leaf, spec)
  spec.leaf = leaf
  hl.animation(spec)
end

-- Motion ---------------------------------------------------------------------
-- Window moves (carousel steps, swipes, keyboard focus, width toggles) and
-- workspace switches share one native spring: they start from rest and keep
-- their momentum when interrupted. Opacity is a state change, so it eases.
function TT.apply(s)
  if not s.enabled then return end
  hl.curve("ttSpring", { type = "spring", mass = 1, stiffness = s.stiffness, dampening = s.damping })
  hl.curve("ttFade", { type = "bezier", points = { { 0.25, 1 }, { 0.5, 1 } } })
  anim("windowsMove", { enabled = true, speed = s.speed, spring = "ttSpring" })
  anim("workspaces", { enabled = true, speed = s.speed, spring = "ttSpring", style = s.ws_style })
  anim("specialWorkspace", { enabled = true, speed = s.speed, spring = "ttSpring", style = s.special_style })
  if s.focus_fade then
    anim("fadeSwitch", { enabled = true, speed = s.fade_speed, bezier = "ttFade" })
  else
    anim("fadeSwitch", { enabled = false })
  end
  if s.scope == "everything" then
    -- One personality for the whole desktop: entrances ease out, exits ease in,
    -- all scaled from the same duration.
    hl.curve("ttEnter", { type = "bezier", points = { { 0.16, 1 }, { 0.3, 1 } } })
    hl.curve("ttExit", { type = "bezier", points = { { 0.7, 0 }, { 0.84, 0 } } })
    anim("windowsIn", { enabled = true, speed = s.enter_speed, bezier = "ttEnter", style = "popin 92%" })
    anim("windowsOut", { enabled = true, speed = s.exit_speed, bezier = "ttExit", style = "popin 95%" })
    anim("fadeIn", { enabled = true, speed = s.enter_speed, bezier = "ttEnter" })
    anim("fadeOut", { enabled = true, speed = s.exit_speed, bezier = "ttExit" })
    anim("layersIn", { enabled = true, speed = s.enter_speed, bezier = "ttEnter", style = "fade" })
    anim("layersOut", { enabled = true, speed = s.exit_speed, bezier = "ttExit", style = "fade" })
    anim("fadeLayersIn", { enabled = true, speed = s.enter_speed, bezier = "ttEnter" })
    anim("fadeLayersOut", { enabled = true, speed = s.exit_speed, bezier = "ttExit" })
    anim("border", { enabled = true, speed = s.fade_speed, bezier = "ttFade" })
  end
end

-- One window per swipe ---------------------------------------------------------
-- Used by "One per swipe" tracking. On a scrolling workspace it moves one column;
-- with wrap on, the strip is circular: at an edge the off-screen far column jumps
-- to the other end while animations pause for about two frames, then slides in.
local function layout(message) hl.dispatch(hl.dsp.layout(message)) end
local function focus_window(address) hl.dispatch(hl.dsp.focus({ window = "address:" .. address })) end
local WARP_MS = 34

function TT.step(direction)
  if direction ~= "l" and direction ~= "r" then return end
  if TT.pending then return end
  local active = hl.get_active_window()
  local ws = active and active.workspace
  if not ws or ws.tiled_layout ~= "scrolling" or active.floating then
    hl.dispatch(hl.dsp.focus({ direction = direction }))
    return
  end
  if not TT.S.gestures.wrap then layout("focus " .. direction); return end
  local windows = {}
  for _, w in ipairs(hl.get_workspace_windows(ws.id)) do
    if w.mapped and not w.hidden and not w.floating then
      if w.group then layout("focus " .. direction); return end
      table.insert(windows, w)
    end
  end
  table.sort(windows, function(a, b) return a.at.x < b.at.x end)
  if #windows < 2 then return end
  for i = 2, #windows do
    -- Stacked columns keep native behaviour.
    if math.abs(windows[i].at.x - windows[i - 1].at.x) < 2 then layout("focus " .. direction); return end
  end
  local edge = direction == "l" and windows[1] or windows[#windows]
  if active.address ~= edge.address then layout("focus " .. direction); return end

  local far = direction == "l" and windows[#windows] or windows[1]
  local mon = active.monitor or hl.get_active_monitor()
  -- width and height are the mode's; transforms 1, 3, 5 and 7 turn the panel a quarter.
  local span = (mon.transform or 0) % 2 == 1 and mon.height or mon.width
  local left, right = mon.x, mon.x + span / mon.scale
  if far.at.x + far.size.x > left and far.at.x < right then
    focus_window(far.address)   -- whole strip visible: just move focus across
    return
  end
  local address, workspace, original_x = active.address, ws.id, active.at.x
  local animations = hl.get_config("animations.enabled")
  local follow = hl.get_config("scrolling.follow_focus")
  local function restore()
    hl.config({ animations = { enabled = animations }, scrolling = { follow_focus = follow } })
  end
  local ok, err = pcall(function()
    hl.config({ animations = { enabled = false }, scrolling = { follow_focus = false } })
    focus_window(far.address)
    for _ = 1, #windows - 1 do layout("swapcol " .. direction) end
    focus_window(address)
    local shifted = hl.get_window("address:" .. address)
    if shifted and shifted.at.x ~= original_x then layout("move " .. tostring(original_x - shifted.at.x)) end
    hl.config({ scrolling = { follow_focus = follow } })
  end)
  if not ok then restore(); error(err) end
  TT.pending = hl.timer(function()
    TT.pending = nil
    restore()
    -- Still on the strip's workspace (special ones included, which the monitor's active
    -- workspace never is): let the far window slide in.
    local current = hl.get_active_window()
    if current and current.workspace and current.workspace.id == workspace then focus_window(far.address) end
  end, { timeout = WARP_MS, type = "oneshot" })
end

local function workspace_step(delta)
  hl.dispatch(hl.dsp.focus({ workspace = delta > 0 and "r+1" or "r-1" }))
end

-- Swipes ----------------------------------------------------------------------
-- "Follow fingers" uses Hyprland's own trackers (scroll_move for the window
-- strip, workspace for workspaces): content moves with the fingers and the
-- spring above finishes the move from where they lift. "One per swipe" moves
-- exactly one window or workspace when the fingers lift. Hyprland refuses a swipe
-- that an earlier one with the same fingers already covers (a config error), so the
-- helper lists in g.skip the directions that would clash with the user's own swipes
-- and those are left to them (the panel lists the lines).
function TT.register(g)
  if not g.enabled then return end
  local f = g.fingers
  local skip = g.skip or {}
  local function gesture(spec)
    if not skip[spec.direction] then hl.gesture(spec) end
  end
  if g.tracking == "follow" then
    -- Leave the user's own swipe tuning alone when all of ours are skipped.
    local any = (g.horizontal ~= "off" and not skip.horizontal) or (g.vertical == "workspaces" and not skip.vertical)
    if any then hl.config({ gestures = {
      workspace_swipe_distance = g.ws_distance, workspace_swipe_cancel_ratio = 0.3,
      workspace_swipe_min_speed_to_force = 20, workspace_swipe_use_r = true,
      workspace_swipe_direction_lock = true, scrolling = { move_snap_to_grid = true },
    } }) end
    if g.horizontal == "windows" then
      gesture({ fingers = f, direction = "horizontal", action = "scroll_move", scale = g.scale })
    elseif g.horizontal == "workspaces" then
      gesture({ fingers = f, direction = "horizontal", action = "workspace" })
    end
    if g.vertical == "workspaces" then
      gesture({ fingers = f, direction = "vertical", action = "workspace" })
    end
  else
    -- Natural direction: swipe left to reveal what is on the right.
    if g.horizontal == "windows" then
      gesture({ fingers = f, direction = "left", action = function() TT.step("r") end })
      gesture({ fingers = f, direction = "right", action = function() TT.step("l") end })
    elseif g.horizontal == "workspaces" then
      gesture({ fingers = f, direction = "left", action = function() workspace_step(1) end })
      gesture({ fingers = f, direction = "right", action = function() workspace_step(-1) end })
    end
    if g.vertical == "workspaces" then
      gesture({ fingers = f, direction = "up", action = function() workspace_step(1) end })
      gesture({ fingers = f, direction = "down", action = function() workspace_step(-1) end })
    end
  end
end

-- "Try it on my desktop": one real window step and one workspace hop, then back.
function TT.demo()
  local active = hl.get_active_window()
  local ws = hl.get_active_workspace()
  if not ws then return end
  local home = ws.id
  local delay = 0
  local function later(ms, fn) delay = delay + ms; hl.timer(fn, { timeout = delay, type = "oneshot" }) end
  if active and not active.floating and ws.tiled_layout == "scrolling" and #hl.get_workspace_windows(ws.id) > 1 then
    later(10, function() TT.step("r") end)
    later(700, function() focus_window(active.address) end)
  end
  later(delay > 0 and 750 or 10, function() workspace_step(1) end)
  later(800, function()
    hl.dispatch(hl.dsp.focus({ workspace = tostring(home) }))
    if active then focus_window(active.address) end
  end)
end

TT.apply(S)
if not TT.registered then
  TT.registered = true
  TT.register(S.gestures)
end

-- Proof for the helper that this render ran to the end in Hyprland's current config, motion
-- on or off: curves outlive a reload, so each render gives this unused one new points.
if S.loaded then
  hl.curve("ttLoaded", { type = "bezier", points = { { S.loaded[1], S.loaded[2] }, { S.loaded[3], S.loaded[4] } } })
end
