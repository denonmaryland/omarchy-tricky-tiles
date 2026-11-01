-- Runs a rendered module against a fake `hl` API and prints every call as JSON-ish lines.
-- arg[2] "twice" loads it again (a live re-apply). Any other arg[2] is a scenario script
-- that fills `world` (windows, monitor, config), drives TrickyTiles and calls fire() to
-- run pending timers; note() adds its own line to the output.
local calls = {}
local function ser(v)
  if type(v) == "table" then
    local keys = {}
    for k in pairs(v) do keys[#keys + 1] = k end
    table.sort(keys, function(a, b) return tostring(a) < tostring(b) end)
    local parts = {}
    for _, k in ipairs(keys) do parts[#parts + 1] = tostring(k) .. "=" .. ser(v[k]) end
    return "{" .. table.concat(parts, ",") .. "}"
  elseif type(v) == "function" then return "fn"
  else return tostring(v) end
end
local function rec(name) return function(...) local a = { ... } calls[#calls + 1] = name .. " " .. ser(a[1]) .. (a[2] and (" " .. ser(a[2])) or "") end end
function note(text) calls[#calls + 1] = text end

world = { config = { ["animations.enabled"] = true, ["scrolling.follow_focus"] = true }, windows = {}, timers = {} }
function fire()
  local due = world.timers
  world.timers = {}
  for _, fn in ipairs(due) do fn() end
end

hl = { curve = rec("curve"), animation = rec("animation"),
       dsp = { layout = function(m) return "layout:" .. m end, focus = function(t) return t end } }
-- Like Hyprland 0.56.2, hl.gesture returns nothing and never raises, even when it refuses the
-- swipe (it only logs "<file>:<line>: hl.gesture: ..." for the caller). The line ends with that
-- caller, so tests can build the same message.
function hl.gesture(spec)
  local info = debug.getinfo(2, "Sl")
  rec("gesture")(spec)
  calls[#calls] = calls[#calls] .. " at " .. (info.source:gsub("^@", "")) .. ":" .. info.currentline
end
function hl.config(t)
  rec("config")(t)
  if t.animations and t.animations.enabled ~= nil then world.config["animations.enabled"] = t.animations.enabled end
  if t.scrolling and t.scrolling.follow_focus ~= nil then world.config["scrolling.follow_focus"] = t.scrolling.follow_focus end
end
function hl.dispatch(d)
  rec("dispatch")(d)
  if world.on_dispatch then world.on_dispatch(d) end
end
function hl.timer(fn, opts)
  rec("timer")(fn, opts)
  world.timers[#world.timers + 1] = fn
  return {}
end
function hl.get_config(key) return world.config[key] end
function hl.get_active_window() return world.active end
function hl.get_active_monitor() return world.monitor end
function hl.get_active_workspace() return world.workspace end
function hl.get_workspace_windows() return world.windows end
function hl.get_window(selector)
  for _, w in ipairs(world.windows) do
    if "address:" .. w.address == selector then return w end
  end
end

-- The calls made before an error are printed too; the error goes to stderr with exit code 1.
local ok, err = pcall(function()
  dofile(arg[1])
  if arg[2] == "twice" then dofile(arg[1]) elseif arg[2] then dofile(arg[2]) end
end)
for _, c in ipairs(calls) do print(c) end
if not ok then io.stderr:write(tostring(err), "\n"); os.exit(1) end
