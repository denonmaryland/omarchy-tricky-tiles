-- Runs a rendered module against a fake `hl` API and prints every call as JSON-ish lines.
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
hl = { curve = rec("curve"), animation = rec("animation"), gesture = rec("gesture"), config = rec("config"),
       dispatch = rec("dispatch"), timer = rec("timer"),
       dsp = { layout = function(m) return "layout:" .. m end, focus = function(t) return t end } }
dofile(arg[1])
if arg[2] == "twice" then dofile(arg[1]) end
for _, c in ipairs(calls) do print(c) end
