import QtQuick
import QtQuick.Controls as QQC
import Quickshell
import Quickshell.Io
import Quickshell.Widgets
import Quickshell.Hyprland
import Quickshell.Wayland
import Quickshell.Services.UPower
import qs.Ui
import qs.Commons
import qs.Commons as Commons
import "lib/spring.js" as Spring

// Tricky Tiles: how windows and workspaces travel when you switch with the
// keyboard or three-finger swipes. Five presets drawn as their own spring
// curves, a live preview (with your own windows) that runs the same closed-form
// spring Hyprland uses, sliders, trackpad swipes that follow your fingers, and
// a power-saver mode. Settings: ~/.config/omarchy/tricky-tiles.json, applied by
// helpers/control.py through ~/.config/hypr/tricky-tiles.lua.
Panel {
  id: root
  moduleName: "io.github.denonmaryland.tricky-tiles"
  ipcTarget: "io.github.denonmaryland.tricky-tiles"
  manageIpc: false
  implicitWidth: icon.implicitWidth
  implicitHeight: icon.implicitHeight

  readonly property string pluginDir: decodeURIComponent(Qt.resolvedUrl(".").toString().replace(/^(file|qs):\/\//, "")).replace(/\/$/, "")
  readonly property string helper: pluginDir + "/helpers/control.py"
  readonly property color fg: bar ? bar.foreground : Commons.Color.foreground
  readonly property color accent: Commons.Color.accent
  readonly property string family: bar ? bar.fontFamily : Style.font.family
  readonly property string mono: "Adwaita Mono"
  readonly property string wallpaper: "file://" + Quickshell.env("HOME") + "/.local/state/omarchy/current/background"

  property var values: ({enabled: true, preset: "spring", duration: 290, bounce: 15, travel: 30, focus_fade: true,
                         scope: "switching", workspace_axis: "vertical", swipes: false, swipe_horizontal: "windows",
                         swipe_vertical: "workspaces", tracking: "follow", wrap: false, sensitivity: 100,
                         power_saver: "off", fingers: "3", preview_windows: true})
  property var presets: ({})
  property var conflicts: []
  property var conflictsByFingers: null   // {"3": [...], "4": [...]}, from newer helpers
  property var skipped: []                // directions the module left to the user's config
  property var monitor: null
  property string code: ""
  property string overrideName: ""
  property string layoutName: ""
  property var draft: ({})
  property var pending: []
  property bool loaded: false
  property bool installed: true
  property bool comparing: false
  property bool compareOnce: false   // a keyboard compare: one playback, then back to yours
  property bool confirmRemove: false // Remove asks twice
  property bool confirmByKey: false
  property real scrolledAt: 0        // last time the body scrolled (ms), for the slider wheel
  property var wheelOwner: null      // slider a touchpad scroll began over
  property int openCount: 0
  property int tab: 0                // 0 motion · 1 swipes · 2 more
  property string cursor: "presets"  // keyboard row, one of `rows`
  property int shareIndex: 0         // share row: 0 copy · 1 paste
  property string message: ""
  property string notice: ""

  readonly property var presetOrder: ["spring", "glide", "snappy", "lively", "reduced"]
  readonly property var controls: [
    {key: "duration", label: "Duration", min: 150, max: 500, step: 10},
    {key: "bounce", label: "Bounce", min: 0, max: 40, step: 1},
    {key: "travel", label: "Workspace slide", min: 0, max: 100, step: 5}
  ]
  readonly property var swipeControl: ({key: "sensitivity", label: "Finger travel", min: 50, max: 200, step: 10})
  // Segmented choices, shared by the controls and the keyboard.
  readonly property var choices: ({
    tab: [{id: 0, label: "Motion"}, {id: 1, label: "Swipes"}, {id: 2, label: "More"}],
    tracking: [{id: "follow", label: "Follow fingers"}, {id: "step", label: "One per swipe"}],
    swipe_horizontal: [{id: "windows", label: "Windows"}, {id: "workspaces", label: "Workspaces"}, {id: "off", label: "Off"}],
    swipe_vertical: [{id: "workspaces", label: "Workspaces"}, {id: "off", label: "Off"}],
    fingers: [{id: "3", label: "3"}, {id: "4", label: "4"}],
    scope: [{id: "switching", label: "Switching only"}, {id: "everything", label: "Everything"}],
    workspace_axis: [{id: "vertical", label: "Up and down"}, {id: "horizontal", label: "Side to side"}],
    power_saver: [{id: "off", label: "No change"}, {id: "snappy", label: "Snappy"}, {id: "reduced", label: "Reduced"}]
  })
  readonly property var switches: ["enabled", "focus_fade", "swipes", "wrap"]
  // Keyboard rows of the current tab, top to bottom, matching what is visible.
  readonly property var rows: {
    const r = ["enabled"]
    if (loaded && !installed) r.push("allow")
    r.push("tabs")
    if (tab === 0) {
      r.push("compare")
      if (installed && values.enabled) r.push("try", "preview_windows")
      return r.concat(["presets", "duration", "bounce", "travel", "focus_fade"])
    }
    if (tab === 1) {
      if (!installed) return r
      r.push("swipes")
      if (values.swipes) {
        r.push("tracking", "swipe_horizontal")
        if (values.swipe_horizontal !== "workspaces") r.push("swipe_vertical")
        if (values.tracking === "follow") r.push("sensitivity")
        if (values.tracking === "step" && values.swipe_horizontal === "windows") r.push("wrap")
      }
      // Fingers stays reachable while swipes are off when the config already uses them.
      if (values.swipes || hasConflicts) r.push("fingers")
      if (hasConflicts && swipeInfo.otherFree) r.push("use_fingers")
      return r
    }
    r.push("scope")
    if (!(values.swipes && values.swipe_horizontal === "workspaces")) r.push("workspace_axis")
    r.push("power_saver", "share")
    if (installed) r.push("uninstall")
    return r
  }
  onRowsChanged: if (rows.indexOf(cursor) < 0) cursor = "tabs"
  // A Process reports running only once it has started, so track writes ourselves:
  // otherwise two commands sent in the same moment overwrite each other.
  property bool writing: false
  readonly property bool busy: writing || writeProc.running || readProc.running
  readonly property var effective: Object.assign({}, values, draft)
  readonly property real distance: monitor && monitor.width ? monitor.width - Style.space(32) : 1500
  readonly property real refresh: monitor && monitor.refresh ? monitor.refresh : 60
  readonly property var stats: Spring.settle(effective.duration, effective.bounce, distance)
  readonly property bool powerSaving: PowerProfiles.profile === PowerProfile.PowerSaver
  readonly property var swipeInfo: Spring.swipeConflicts(conflicts, conflictsByFingers, values.fingers)
  readonly property bool hasConflicts: swipeInfo.list.length > 0 || (values.swipes && skipped.length > 0)

  function alphaOf(c, a) { return Qt.rgba(c.r, c.g, c.b, a) }
  function fmt(key, v) {
    if (key === "duration") return Math.round(v) + " ms"
    if (key === "travel") return v <= 0 ? "Fade" : v >= 100 ? "Full" : Math.round(v) + "%"
    if (key === "sensitivity") return Math.round(v) + "%"
    return Math.round(v) + "%"
  }
  function frames(ms) { return Math.max(1, Math.round(ms * refresh / 1000)) }

  // ---------- Settings through helpers/control.py ----------
  function refresh_() { if (!busy && pending.length === 0) readProc.running = true }
  function receive(text) {
    try {
      const result = JSON.parse(text)
      if (result.presets) presets = result.presets
      // A reply while more changes are queued predates them: keep showing the queued values.
      values = Spring.withPending(result.values, pending)
      conflicts = result.conflicts || []
      conflictsByFingers = result.conflicts_by_fingers || null
      // Helpers without `skipped` only imply it: nothing skipped after an apply or with swipes off.
      if (Array.isArray(result.skipped)) skipped = result.skipped
      else if (result.applied || !values.swipes) skipped = []
      monitor = result.monitor || null
      code = result.code || ""
      overrideName = result.override || ""
      layoutName = result.layout || ""
      installed = result.installed !== false
      loaded = true
      message = result.ok ? "" : result.error
      // Never resync from a failed reply: a failing override would retry forever. The next
      // good reply or power-profile change tries again.
      if (result.ok) syncPower()
    } catch (e) { message = "Could not read Tricky Tiles settings." }
  }
  // Commands run in order; a newer change to the same key replaces a queued one.
  // `patch` is what the command will save, re-applied over replies until it has run.
  function send(args, patch) {
    if (!loaded) return
    const key = args[0] === "set" ? "set:" + args[1] : args[0]
    const q = pending.filter(function(item) { return item.key !== key })
    q.push({key: key, args: args, patch: patch || null})
    pending = q
    drain()
  }
  function drain() {
    if (busy || pending.length === 0) return
    const next = pending[0]
    pending = pending.slice(1)
    writing = true
    writeProc.command = ["/usr/bin/python3", helper].concat(next.args)
    writeProc.running = true
  }
  function commit(key, value) {
    const d = Object.assign({}, draft); delete d[key]; draft = d
    const v = Object.assign({}, values)
    const patch = {}
    patch[key] = value
    if (key === "duration" || key === "bounce" || key === "travel" || key === "focus_fade") { v[key] = value; patch.enabled = true; patch.preset = matching(v) }
    values = Object.assign(v, patch)
    send(["set", key, String(value)], patch)
    if (key !== "power_saver" && key !== "preview_windows") preview.play()
  }
  function pickPreset(id) {
    const p = presets[id]
    if (!p) return
    draft = {}
    const patch = {enabled: true, preset: id, duration: p.duration, bounce: p.bounce, travel: p.travel, focus_fade: p.focus_fade}
    values = Object.assign({}, values, patch)
    send(["preset", id], patch)
    preview.play()
  }
  function matching(v) {
    for (var i = 0; i < presetOrder.length; i++) {
      const p = presets[presetOrder[i]]
      if (p && p.duration === v.duration && p.bounce === v.bounce && p.travel === v.travel && p.focus_fade === v.focus_fade) return presetOrder[i]
    }
    return "custom"
  }
  function presetIndex() { return Math.max(0, presetOrder.indexOf(values.preset)) }
  // Power saver: swap in a lighter preset while the power-saver profile is on.
  function syncPower() {
    if (!loaded || !installed) return
    const want = values.power_saver !== "off" && powerSaving ? values.power_saver : ""
    if (want !== overrideName) { overrideName = want; send(["override", want || "none"]) }
  }
  onPowerSavingChanged: syncPower()

  // ---------- Keyboard: Up/Down pick a row, Left/Right change it, Enter/Space act ----------
  function moveCursor(dy) {
    const i = Math.max(0, rows.indexOf(cursor))
    cursor = rows[Math.max(0, Math.min(rows.length - 1, i + dy))]
    revealCursor()
  }
  function showTab(index) { tab = Math.max(0, Math.min(2, index)) }
  onTabChanged: { confirmRemove = false; scroller.contentItem.contentY = 0 }

  // ---------- Scrolling: short screens cap the card, so the body scrolls ----------
  // Rows are tagged objectName "row:<cursor>". Only keyboard moves reveal the row;
  // hovering (which also moves the cursor) never scrolls under the pointer.
  function findRow(item, name) {
    if (!item || !item.visible) return null
    if (item.objectName === name) return item
    for (var i = 0; i < item.children.length; i++) {
      const hit = findRow(item.children[i], name)
      if (hit) return hit
    }
    return null
  }
  // Positioners lay out on the next polish; settle them now so positions are current.
  function layoutAll(item) {
    for (var i = 0; i < item.children.length; i++) layoutAll(item.children[i])
    if (typeof item.forceLayout === "function") item.forceLayout()
  }
  function revealCursor() {
    const flick = scroller.contentItem
    const maxY = Math.max(0, flick.contentHeight - flick.height)
    if (maxY <= 1) return
    layoutAll(content)
    const item = findRow(content, "row:" + cursor)
    if (!item) return
    var top = item.mapToItem(content, 0, 0).y
    const bottom = top + item.height
    // Keep a control's caption (the Label just above it) in view too.
    const sibs = item.parent ? item.parent.children : []
    var at = 0
    while (at < sibs.length && sibs[at] !== item) at++
    for (var i = at - 1; i >= 0; i--) {
      if (!sibs[i].visible) continue
      if (sibs[i].objectName === "label") top = sibs[i].mapToItem(content, 0, 0).y
      break
    }
    const pad = Style.space(8)
    var y = flick.contentY
    if (top - pad < y) y = top - pad
    else if (bottom + pad > y + flick.height) y = bottom + pad - flick.height
    flick.contentY = Math.max(0, Math.min(maxY, y))
    scrolledAt = 0   // not a wheel scroll: leave the slider wheel alone
  }
  function sliderSpec(key) {
    if (key === swipeControl.key) return swipeControl
    for (var i = 0; i < controls.length; i++) if (controls[i].key === key) return controls[i]
    return null
  }
  // Move a segmented choice by dx; `wrap` cycles past the ends (Enter/Space).
  function stepChoice(key, dx, wrap) {
    const list = choices[key]
    var i = 0
    for (var k = 0; k < list.length; k++) if (String(list[k].id) === String(values[key])) i = k
    const j = wrap ? (i + dx + list.length) % list.length : Math.max(0, Math.min(list.length - 1, i + dx))
    if (j !== i) commit(key, list[j].id)
  }
  function adjust(dx) {
    if (cursor === "tabs") { showTab(tab + dx); return }
    if (cursor === "share") { shareIndex = dx > 0 ? 1 : 0; return }
    if (!loaded) return
    const c = sliderSpec(cursor)
    if (cursor === "preview_windows") commit("preview_windows", !values.preview_windows)
    else if (cursor === "presets") pickPreset(presetOrder[Math.max(0, Math.min(presetOrder.length - 1, presetIndex() + dx))])
    else if (c) commit(c.key, Math.max(c.min, Math.min(c.max, Number(values[c.key]) + dx * c.step)))
    else if (switches.indexOf(cursor) >= 0) { if (values[cursor] !== (dx > 0)) commit(cursor, dx > 0) }
    else if (choices[cursor]) stepChoice(cursor, dx, false)
  }
  function activate() {
    if (!loaded) return
    if (switches.indexOf(cursor) >= 0) commit(cursor, !values[cursor])
    else if (choices[cursor]) stepChoice(cursor, 1, true)
    else if (cursor === "allow") allowInstall()
    else if (cursor === "share") { if (shareIndex === 0) copyCode(); else pasteCode() }
    else if (cursor === "uninstall") removeModule(true)
    else if (cursor === "compare") compareStock()
    else if (cursor === "try") send(["demo"])
    else if (cursor === "preview_windows") commit("preview_windows", !values.preview_windows)
    else if (cursor === "use_fingers") useOtherFingers()
    else if (cursor === "presets" && !(values.enabled && values.preset === presetOrder[presetIndex()])) pickPreset(presetOrder[presetIndex()])
    else if (tab === 0) preview.play()
  }
  function textKey(t) {
    if (t >= "1" && t <= "3") showTab(Number(t) - 1)
    else if (t === "[") showTab(tab - 1)
    else if (t === "]") showTab(tab + 1)
  }
  function allowInstall() { notice = ""; send(["install"]) }
  // Keys can't hold, so Enter plays stock Omarchy once and the preview returns to yours.
  function compareStock() { comparing = true; preview.play(); compareOnce = true }
  function useOtherFingers() {
    const other = swipeInfo.other
    commit("fingers", other)
    // The button (and, with swipes off, the selector) goes once the new count is free.
    cursor = rows.indexOf("fingers") >= 0 ? "fingers" : "swipes"
  }
  // First press arms it (for 3 s or until the tab changes); the second removes.
  function removeModule(byKey) {
    if (!confirmRemove) { confirmRemove = true; confirmByKey = byKey; removeTimer.restart(); return }
    confirmRemove = false; removeTimer.stop()
    send(["uninstall"])
  }
  function copyCode() { clipProc.command = ["wl-copy", code]; clipProc.running = true; notice = "Copied " + code }
  function pasteCode() { pasteProc.running = true }

  Component.onCompleted: refresh_()
  onOpenedChanged: {
    if (opened) { openCount++; draft = {}; notice = ""; scroller.contentItem.contentY = 0; refresh_(); Hyprland.refreshToplevels(); preview.play() }
    else { preview.stop(); comparing = false; compareOnce = false; confirmRemove = false }
  }
  Timer { id: removeTimer; interval: 3000; onTriggered: root.confirmRemove = false }

  IpcHandler {
    target: "io.github.denonmaryland.tricky-tiles"
    function open(): void { root.open(); root.refresh_() }
    function close(): void { root.close() }
    function state(): string { return JSON.stringify({values: root.values, loaded: root.loaded, installed: root.installed, message: root.message, busy: root.busy, stats: root.stats, tab: root.tab, cursor: root.cursor, rows: root.rows, override: root.overrideName, conflicts: root.conflicts, skipped: root.skipped}) }
    function preset(id: string): void { root.pickPreset(id) }
    function set(key: string, value: string): void {
      const bools = ["enabled", "focus_fade", "swipes", "wrap", "preview_windows"]
      const texts = ["scope", "workspace_axis", "swipe_horizontal", "swipe_vertical", "tracking", "power_saver", "fingers"]
      root.commit(key, bools.indexOf(key) >= 0 ? value === "true" : texts.indexOf(key) >= 0 ? value : Number(value))
    }
    function showTab(index: int): void { root.showTab(index) }
    function replay(): void { root.preview.play() }
    function compare(on: bool): void { root.comparing = on; root.preview.play() }
    // For testing the stock look on a desktop that has the glass host installed.
    function useHost(kind: string): string {
      root.close()
      root.useGlassHost = kind === "glass"
      hostLoader.setSource(root.useGlassHost ? "hosts/GlassHost.qml" : "hosts/StockHost.qml", { tiles: root, anchorItem: icon, bar: root.bar })
      return root.useGlassHost ? "glass" : "stock"
    }
  }
  Process {
    id: readProc
    command: ["/usr/bin/python3", root.helper, "state"]
    stdout: StdioCollector { onStreamFinished: root.receive(text) }
    onExited: Qt.callLater(root.drain)
  }
  Process {
    id: writeProc
    stdout: StdioCollector { onStreamFinished: root.receive(text) }
    onExited: { root.writing = false; Qt.callLater(root.drain) }
  }
  // Pick up changes made outside the panel (the CLI, another session).
  FileView {
    // The helper's rule: an absolute $XDG_CONFIG_HOME, else ~/.config.
    path: ((Quickshell.env("XDG_CONFIG_HOME") || "").startsWith("/") ? Quickshell.env("XDG_CONFIG_HOME") : Quickshell.env("HOME") + "/.config") + "/omarchy/tricky-tiles.json"
    watchChanges: true
    printErrors: false
    onFileChanged: if (!root.busy && root.pending.length === 0) root.refresh_()
  }
  Process { id: clipProc }
  Process {
    id: pasteProc
    command: ["wl-paste", "--no-newline", "--type", "text"]   // text only, never an image
    stdout: StdioCollector {
      onStreamFinished: {
        // Same pattern as CODE_RE in helpers/control.py.
        const pasted = (text || "").trim()
        if (/^TT1-\d{2,3}-\d{1,2}-\d{1,3}-[01]$/.test(pasted)) { root.send(["import", pasted]); root.notice = "Applied " + pasted; root.preview.play() }
        else root.notice = "The clipboard has no Tricky Tiles code"
      }
    }
  }

  BarIconButton {
    id: icon
    anchors.fill: parent
    bar: root.bar
    text: "◫"
    tooltipText: !root.values.enabled ? "Tricky Tiles · off"
      : "Tricky Tiles · " + (root.overrideName ? "power saver" : root.presets[root.values.preset] ? root.presets[root.values.preset].label : "Custom")
    onPressed: if (!(root.popup && root.popup.commitHover(root))) root.toggle()
  }

  // ---------------------------------------------------------------- card
  // The popup host is Omarchy's stock KeyboardPanel, or the frosted host from
  // the denon.bar plugin when that is installed. The content below is
  // reparented into whichever loads (hosts/*.qml).
  property bool hostChosen: false
  property bool useGlassHost: false
  readonly property var popup: hostLoader.item
  readonly property Item keyboardItem: keyboard
  readonly property real contentImplicitHeight: content.implicitHeight
  readonly property bool overflows: content.implicitHeight > scroller.availableHeight + 1
  readonly property alias preview: previewItem
  FileView {
    path: root.pluginDir + "/../denon.bar/panels/PopupPanel.qml"
    printErrors: false
    onLoaded: { root.useGlassHost = true; root.hostChosen = true }
    onLoadFailed: root.hostChosen = true
  }
  onHostChosenChanged: hostLoader.setSource(useGlassHost ? "hosts/GlassHost.qml" : "hosts/StockHost.qml",
                                            { tiles: root, anchorItem: icon, bar: root.bar })
  Loader { id: hostLoader }
  Item { id: parking; visible: false; width: Style.space(400); height: Style.space(700) }

  PanelKeyCatcher {
    id: keyboard
    parent: root.popup ? root.popup.slot : parking
    anchors.fill: parent
    onCloseRequested: root.close()
    onTabRequested: function(direction) { root.switchPanel(direction) }
    onMoveRequested: function(dx, dy) {
      if (dy) root.moveCursor(dy)
      else if (dx) root.adjust(dx)
    }
    onActivateRequested: root.activate()
    // 1 2 3 or [ ] switch tabs; Tab itself moves to the next bar panel, as in Omarchy's own panels.
    onTextKey: function(t) { root.textKey(t) }

    // Scrolls only when the card is capped (short screens), like Omarchy's own panels.
    // It reaches a little into the card's padding (and pads back by as much) so focus
    // rings drawn just outside a control are not clipped.
    QQC.ScrollView {
      id: scroller
      anchors.fill: parent
      anchors.margins: -Style.space(6)
      padding: Style.space(6)
      clip: true
      QQC.ScrollBar.horizontal.policy: QQC.ScrollBar.AlwaysOff
      QQC.ScrollBar.vertical.policy: root.overflows ? QQC.ScrollBar.AsNeeded : QQC.ScrollBar.AlwaysOff
      Binding { target: scroller.contentItem; property: "interactive"; value: root.overflows }
      Connections { target: scroller.contentItem; function onContentYChanged() { root.scrolledAt = Date.now() } }

      Column {
        id: content
        width: scroller.availableWidth
        spacing: Style.space(14)

        // ---------- Header: title · caption · switch ----------
        Item {
          objectName: "row:enabled"
          width: parent.width
          height: Math.max(titleCol.implicitHeight, mainSwitch.height)
          Column {
            id: titleCol
            anchors.verticalCenter: parent.verticalCenter
            width: parent.width - mainSwitch.width - Style.space(12)
            spacing: Style.space(1)
            Text { text: "Tricky Tiles"; color: root.fg; font.family: root.family; font.pixelSize: Style.font.title + 3; font.weight: Font.Bold }
            Text {
              width: parent.width
              elide: Text.ElideRight
              text: !root.values.enabled ? "Off · your config's own motion"
                : root.overrideName ? "Power saver · using " + (root.presets[root.overrideName] ? root.presets[root.overrideName].label : root.overrideName)
                : root.presets[root.values.preset] ? root.presets[root.values.preset].caption : "Custom · your own spring"
              color: root.alphaOf(root.fg, 0.55); font.family: root.family; font.pixelSize: Style.font.caption + 1
            }
          }
          Switch {
            id: mainSwitch
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            checked: root.values.enabled
            focusRing: root.cursor === "enabled"
            onToggled: root.commit("enabled", !root.values.enabled)
            onHoveredIn: root.cursor = "enabled"
          }
        }

        // ---------- Consent: nothing touches Hyprland until the user allows it ----------
        Rectangle {
          objectName: "row:allow"
          visible: root.loaded && !root.installed
          width: parent.width
          height: consentCol.implicitHeight + Style.space(24)
          radius: Style.space(12)
          color: root.alphaOf(root.accent, 0.1)
          border.width: 1; border.color: root.alphaOf(root.accent, 0.35)
          Column {
            id: consentCol
            x: Style.space(12); y: Style.space(12)
            width: parent.width - Style.space(24)
            spacing: Style.space(10)
            Text {
              width: parent.width; wrapMode: Text.WordWrap
              text: "To change your motion, Tricky Tiles adds one marked line to ~/.config/hypr/hyprland.lua and writes ~/.config/hypr/tricky-tiles.lua. A backup of hyprland.lua is kept, and Remove in the More tab takes both out again."
              color: root.fg; font.family: root.family; font.pixelSize: Style.font.caption + 1
            }
            Pill { text: "Allow"; primary: true; focusRing: root.cursor === "allow"; onClicked: root.allowInstall() }
          }
        }

        // ---------- Tabs ----------
        Segmented {
          objectName: "row:tabs"
          width: parent.width
          model: root.choices.tab
          current: root.tab
          focusRing: root.cursor === "tabs"
          onPicked: function(id) { root.tab = id }
        }

        // ---------- Live preview ----------
        ClippingRectangle {
          id: stage
          objectName: "row:compare"   // the keyboard compare plays here, so reveal all of it
          visible: root.tab === 0
          width: parent.width
          height: Style.space(140)
          radius: Style.space(12)
          color: root.alphaOf(root.fg, 0.06)
          Image {
            anchors.fill: parent
            source: root.wallpaper + "?" + root.openCount
            sourceSize.width: 480
            fillMode: Image.PreserveAspectCrop
            asynchronous: true
          }
          Rectangle { anchors.fill: parent; color: Qt.rgba(0, 0, 0, 0.18) }
          MotionPreview {
            id: previewItem
            anchors.fill: parent
            duration: root.effective.duration
            bounce: root.values.enabled ? root.effective.bounce : 0
            travel: root.values.enabled ? root.effective.travel : 100
            focusFade: root.values.enabled && root.effective.focus_fade
            stock: root.comparing || !root.values.enabled
            vertical: root.values.workspace_axis !== "horizontal"
            onPlayed: if (root.compareOnce) { root.compareOnce = false; root.comparing = false }
          }
          Text {
            anchors.left: parent.left; anchors.bottom: parent.bottom
            anchors.margins: Style.space(9)
            text: root.comparing ? "STOCK OMARCHY" : previewItem.caption
            color: Qt.rgba(1, 1, 1, 0.75); font.family: root.family; font.pixelSize: Style.font.caption - 1; font.letterSpacing: 0.6
            style: Text.Raised; styleColor: Qt.rgba(0, 0, 0, 0.35)
          }
          MouseArea {
            id: stageMouse
            anchors.fill: parent
            hoverEnabled: true; cursorShape: Qt.PointingHandCursor
            onClicked: previewItem.play()
          }
          // Hold to compare with stock Omarchy (windows ease out, workspaces cut).
          // Enter plays the comparison once. The ring is white: it sits on the wallpaper.
          Rectangle {
            anchors.right: parent.right; anchors.top: parent.top
            anchors.margins: Style.space(8)
            width: compareText.implicitWidth + Style.space(16); height: Style.space(22); radius: height / 2
            border.width: root.cursor === "compare" ? 2 : 0; border.color: Qt.rgba(1, 1, 1, 0.85)
            color: root.comparing ? Qt.rgba(1, 1, 1, 0.9) : Qt.rgba(0, 0, 0, compareMouse.containsMouse ? 0.5 : 0.35)
            Behavior on color { ColorAnimation { duration: 120; easing.type: Easing.OutCubic } }
            Text {
              id: compareText
              anchors.centerIn: parent
              text: "Hold to compare"
              color: root.comparing ? "black" : Qt.rgba(1, 1, 1, 0.85); font.family: root.family; font.pixelSize: Style.font.caption - 1
            }
            MouseArea {
              id: compareMouse
              anchors.fill: parent; anchors.margins: -Style.space(4)
              hoverEnabled: true; cursorShape: Qt.PointingHandCursor
              onEntered: root.cursor = "compare"
              onPressed: { root.compareOnce = false; root.comparing = true; previewItem.play() }
              onReleased: { root.comparing = false; previewItem.play() }
              onCanceled: root.comparing = false
            }
          }
          Text {
            anchors.right: parent.right; anchors.bottom: parent.bottom
            anchors.margins: Style.space(9)
            text: previewItem.running ? "" : "Replay ↻"
            color: Qt.rgba(1, 1, 1, stageMouse.containsMouse ? 0.95 : 0.6); font.family: root.family; font.pixelSize: Style.font.caption - 1
            style: Text.Raised; styleColor: Qt.rgba(0, 0, 0, 0.35)
          }
        }
        Item {
          objectName: "row:try"
          visible: root.tab === 0 && root.installed && root.values.enabled
          width: parent.width
          height: tryText.implicitHeight
          Text {
            id: tryText
            text: "Try it on my desktop  →"
            color: root.alphaOf(root.fg, tryMouse.containsMouse || root.cursor === "try" ? 0.95 : 0.65)
            font.family: root.family; font.pixelSize: Style.font.caption + 1; font.weight: Font.DemiBold
            FocusRing { visible: root.cursor === "try" }
            MouseArea {
              id: tryMouse
              anchors.fill: parent; anchors.margins: -Style.space(5)
              hoverEnabled: true; cursorShape: Qt.PointingHandCursor
              onClicked: root.send(["demo"])
              onEntered: root.cursor = "try"
            }
          }
          Text {
            objectName: "row:preview_windows"
            anchors.right: parent.right
            text: root.values.preview_windows ? "Your windows" : "Sample windows"
            color: root.alphaOf(root.fg, winMouse.containsMouse || root.cursor === "preview_windows" ? 0.9 : 0.45)
            font.family: root.family; font.pixelSize: Style.font.caption
            FocusRing { visible: root.cursor === "preview_windows" }
            MouseArea {
              id: winMouse
              anchors.fill: parent; anchors.margins: -Style.space(5)
              hoverEnabled: true; cursorShape: Qt.PointingHandCursor
              onClicked: root.commit("preview_windows", !root.values.preview_windows)
              onEntered: root.cursor = "preview_windows"
            }
          }
        }

        // ---------- Presets: each drawn as its own spring ----------
        Row {
          objectName: "row:presets"
          visible: root.tab === 0
          width: parent.width
          spacing: Style.space(8)
          opacity: root.values.enabled ? 1 : 0.5
          Repeater {
            model: root.presetOrder
            Column {
              id: card
              required property string modelData
              required property int index
              readonly property var spec: root.presets[modelData] || ({duration: 300, bounce: 0, label: modelData})
              readonly property bool selected: root.values.enabled && root.values.preset === modelData
              readonly property bool hovered: cardMouse.containsMouse
              readonly property bool keyFocus: root.cursor === "presets" && index === root.presetIndex()
              width: (parent.width - Style.space(32)) / 5
              spacing: Style.space(6)
              Rectangle {
                width: parent.width; height: Style.space(46)
                radius: Style.space(10)
                color: card.selected ? root.alphaOf(root.accent, 0.12) : root.alphaOf(root.fg, card.hovered ? 0.07 : 0.04)
                border.width: card.selected ? 2 : 1
                border.color: card.selected ? root.accent : root.alphaOf(root.fg, card.hovered ? 0.32 : 0.1)
                Behavior on color { ColorAnimation { duration: 150; easing.type: Easing.OutCubic } }
                scale: cardMouse.pressed ? 0.98 : 1
                Behavior on scale { NumberAnimation { duration: 120; easing.type: Easing.OutCubic } }
                // Keyboard focus: a ring outside the card, so it reads apart from the accent border of the selected one.
                Rectangle {
                  anchors.fill: parent; anchors.margins: -Style.space(3)
                  radius: parent.radius + Style.space(3)
                  color: "transparent"
                  border.width: 2; border.color: root.alphaOf(root.fg, 0.7)
                  visible: card.keyFocus
                }
                CurveGlyph {
                  anchors.fill: parent
                  anchors.margins: Style.space(7)
                  samples: Spring.curve(card.spec.duration, card.spec.bounce, 450, 48)
                  stroke: card.selected ? root.accent : root.alphaOf(root.fg, 0.7)
                  guide: root.alphaOf(root.fg, 0.16)
                }
                MouseArea {
                  id: cardMouse
                  anchors.fill: parent
                  hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                  enabled: root.loaded
                  onClicked: root.pickPreset(card.modelData)
                  onEntered: root.cursor = "presets"
                }
              }
              Text {
                width: parent.width
                horizontalAlignment: Text.AlignHCenter
                text: card.spec.label
                color: card.selected ? root.fg : root.alphaOf(root.fg, 0.62)
                font.family: root.family; font.pixelSize: Style.font.caption + 1
                font.weight: card.selected ? Font.Bold : Font.Normal
              }
            }
          }
        }

        // ---------- Sliders ----------
        Column {
          visible: root.tab === 0
          width: parent.width
          spacing: Style.space(12)
          opacity: root.values.enabled ? 1 : 0.45
          Repeater {
            model: root.controls
            Slider {
              required property var modelData
              width: content.width
              spec: modelData
            }
          }
        }

        SwitchRow {
          objectName: "row:focus_fade"
          visible: root.tab === 0
          width: parent.width
          opacity: root.values.enabled ? 1 : 0.45
          title: "Focus fade"
          caption: "The arriving window fades up as it slides in"
          checked: root.values.focus_fade
          focusRing: root.cursor === "focus_fade"
          onToggled: root.commit("focus_fade", !root.values.focus_fade)
          onHoveredIn: root.cursor = "focus_fade"
        }

        // ---------- Numbers for a full-width switch on this screen ----------
        Rectangle {
          visible: root.tab === 0 && root.values.enabled
          width: parent.width
          height: statsCol.implicitHeight + Style.space(18)
          radius: Style.space(12)
          color: root.alphaOf(root.fg, 0.05)
          border.width: 1; border.color: root.alphaOf(root.fg, 0.06)
          Column {
            id: statsCol
            anchors.centerIn: parent
            width: parent.width - Style.space(20)
            spacing: Style.space(6)
            Row {
              width: parent.width
              Repeater {
                model: [
                  {label: "Arrives", value: root.stats.arrive + " ms"},
                  {label: "At rest", value: root.stats.rest + " ms"},
                  {label: "Overshoot", value: root.stats.overshoot > 0 ? root.stats.overshoot + " px" : "None"}
                ]
                Column {
                  required property var modelData
                  width: statsCol.width / 3
                  spacing: Style.space(2)
                  Text {
                    anchors.horizontalCenter: parent.horizontalCenter
                    text: modelData.value; color: root.fg
                    font.family: root.mono; font.pixelSize: Style.font.body + 1; font.weight: Font.DemiBold
                  }
                  Text {
                    anchors.horizontalCenter: parent.horizontalCenter
                    text: modelData.label; color: root.alphaOf(root.fg, 0.5)
                    font.family: root.family; font.pixelSize: Style.font.caption - 1
                  }
                }
              }
            }
            Text {
              anchors.horizontalCenter: parent.horizontalCenter
              text: "A " + Math.round(root.distance) + " px switch at " + Math.round(root.refresh) + " Hz · arrives in " + root.frames(root.stats.arrive) + " frames"
              color: root.alphaOf(root.fg, 0.4); font.family: root.family; font.pixelSize: Style.font.caption - 2
            }
          }
        }

        // ================================================================ Swipes
        SwitchRow {
          objectName: "row:swipes"
          visible: root.tab === 1
          width: parent.width
          title: "Trackpad swipes"
          caption: root.values.swipes ? (root.values.tracking === "follow" ? "Content follows your fingers, then springs into place" : "One window or workspace per swipe")
                                      : "Let Tricky Tiles handle " + root.values.fingers + "-finger swipes"
          checked: root.values.swipes
          enabled: root.installed
          focusRing: root.cursor === "swipes"
          onToggled: root.commit("swipes", !root.values.swipes)
        }
        Column {
          visible: root.tab === 1 && root.values.swipes
          width: parent.width
          spacing: Style.space(12)
          Label { text: "Tracking" }
          Segmented {
            objectName: "row:tracking"
            width: parent.width
            model: root.choices.tracking
            current: root.values.tracking
            focusRing: root.cursor === "tracking"
            onPicked: function(id) { root.commit("tracking", id) }
          }
          Label { text: "Swipe left and right to move between" }
          Segmented {
            objectName: "row:swipe_horizontal"
            width: parent.width
            model: root.choices.swipe_horizontal
            current: root.values.swipe_horizontal
            focusRing: root.cursor === "swipe_horizontal"
            onPicked: function(id) { root.commit("swipe_horizontal", id) }
          }
          Label { visible: root.values.swipe_horizontal !== "workspaces"; text: "Swipe up and down to move between" }
          Segmented {
            objectName: "row:swipe_vertical"
            visible: root.values.swipe_horizontal !== "workspaces"
            width: parent.width
            model: root.choices.swipe_vertical
            current: root.values.swipe_vertical
            focusRing: root.cursor === "swipe_vertical"
            onPicked: function(id) { root.commit("swipe_vertical", id) }
          }
          Slider {
            visible: root.values.tracking === "follow"
            width: parent.width
            spec: root.swipeControl
          }
          SwitchRow {
            objectName: "row:wrap"
            visible: root.values.tracking === "step" && root.values.swipe_horizontal === "windows"
            width: parent.width
            title: "Wrap around"
            caption: "Past the last window, the first one comes round again"
            checked: root.values.wrap
            focusRing: root.cursor === "wrap"
            onToggled: root.commit("wrap", !root.values.wrap)
          }
        }
        // Outside the column above: with swipes off it still shows when the config
        // already uses this finger count, so the user can pick the free one.
        Item {
          objectName: "row:fingers"
          visible: root.tab === 1 && root.installed && (root.values.swipes || root.hasConflicts)
          width: parent.width
          height: Math.max(fingerText.implicitHeight, fingerPick.height)
          Text { id: fingerText; text: "Fingers"; color: root.fg; font.family: root.family; font.pixelSize: Style.font.body; anchors.verticalCenter: parent.verticalCenter }
          Segmented {
            id: fingerPick
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            width: Style.space(120)
            model: root.choices.fingers
            current: root.values.fingers
            focusRing: root.cursor === "fingers"
            onPicked: function(id) { root.commit("fingers", id) }
          }
        }
        Text {
          visible: root.tab === 1 && root.values.swipes && root.values.tracking === "follow" && root.values.swipe_horizontal === "windows" && root.layoutName !== "scrolling"
          width: parent.width; wrapMode: Text.WordWrap
          text: "Window swipes follow your fingers on scrolling workspaces (Super+L toggles a workspace's layout)."
          color: root.alphaOf(root.fg, 0.5); font.family: root.family; font.pixelSize: Style.font.caption
        }
        // Swipes the user's config already defines for this finger count. Hyprland keeps
        // the first gesture per direction, so the module leaves those directions alone.
        Rectangle {
          visible: root.tab === 1 && root.installed && root.hasConflicts
          width: parent.width
          height: conflictCol.implicitHeight + Style.space(20)
          radius: Style.space(10)
          color: root.alphaOf(Commons.Color.urgent, root.values.swipes ? 0.1 : 0.06)
          border.width: 1; border.color: root.alphaOf(Commons.Color.urgent, root.values.swipes ? 0.35 : 0.22)
          Column {
            id: conflictCol
            x: Style.space(10); y: Style.space(10)
            width: parent.width - Style.space(20)
            spacing: Style.space(6)
            Text {
              width: parent.width; wrapMode: Text.WordWrap
              text: "Your config already has " + root.values.fingers + "-finger swipes"
              color: root.fg; font.family: root.family; font.pixelSize: Style.font.caption + 1; font.weight: Font.DemiBold
            }
            Repeater {
              model: root.swipeInfo.list
              Column {
                required property var modelData
                width: conflictCol.width
                Text {
                  width: parent.width; elide: Text.ElideRight
                  text: "~/" + modelData.file + ":" + modelData.line
                  color: root.alphaOf(root.fg, 0.8); font.family: root.mono; font.pixelSize: Style.font.caption - 1
                }
                Text {
                  visible: text !== ""
                  width: parent.width; elide: Text.ElideRight
                  text: modelData.text || ""
                  color: root.alphaOf(root.fg, 0.5); font.family: root.mono; font.pixelSize: Style.font.caption - 2
                }
              }
            }
            Text {
              width: parent.width; wrapMode: Text.WordWrap
              text: !root.values.swipes ? "Turn swipes on and Tricky Tiles leaves the directions these lines use to your config, and handles the rest."
                : root.skipped.length > 0 ? "Left to your config: " + Spring.listText(root.skipped) + " swipes. Tricky Tiles handles the rest; comment out those lines to hand them over."
                : "Tricky Tiles leaves the directions these lines use to your config. Comment them out to hand them over."
              color: root.fg; font.family: root.family; font.pixelSize: Style.font.caption
            }
            Item {
              visible: root.swipeInfo.otherFree
              width: parent.width
              height: Math.max(freeText.implicitHeight, useFingers.height)
              Text {
                id: freeText
                anchors.verticalCenter: parent.verticalCenter
                width: parent.width - useFingers.width - Style.space(10); wrapMode: Text.WordWrap
                text: root.swipeInfo.other + "-finger swipes are free."
                color: root.alphaOf(root.fg, 0.7); font.family: root.family; font.pixelSize: Style.font.caption
              }
              Pill {
                id: useFingers
                objectName: "row:use_fingers"
                anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter
                text: "Use " + root.swipeInfo.other + " fingers"
                focusRing: root.cursor === "use_fingers"
                onClicked: root.useOtherFingers()
              }
            }
          }
        }
        Text {
          visible: root.tab === 1 && !root.installed
          width: parent.width; wrapMode: Text.WordWrap
          text: "Allow Tricky Tiles above first; swipes are registered by its Hyprland module."
          color: root.alphaOf(root.fg, 0.55); font.family: root.family; font.pixelSize: Style.font.caption
        }

        // ================================================================ More
        Column {
          visible: root.tab === 2
          width: parent.width
          spacing: Style.space(12)
          Label { text: "Animate" }
          Segmented {
            objectName: "row:scope"
            width: parent.width
            model: root.choices.scope
            current: root.values.scope
            focusRing: root.cursor === "scope"
            onPicked: function(id) { root.commit("scope", id) }
          }
          Text {
            width: parent.width; wrapMode: Text.WordWrap
            text: root.values.scope === "everything" ? "Opening, closing, menus and fades use the same duration, easing in and out." : "Only window moves, workspace switches and the focus fade."
            color: root.alphaOf(root.fg, 0.5); font.family: root.family; font.pixelSize: Style.font.caption
          }
          Label { text: "Workspaces travel" }
          Segmented {
            objectName: "row:workspace_axis"
            width: parent.width
            enabled: !(root.values.swipes && root.values.swipe_horizontal === "workspaces")
            model: root.choices.workspace_axis
            current: root.values.workspace_axis
            focusRing: root.cursor === "workspace_axis"
            onPicked: function(id) { root.commit("workspace_axis", id) }
          }
          Label { text: "On power saver, switch to" }
          Segmented {
            objectName: "row:power_saver"
            width: parent.width
            model: root.choices.power_saver
            current: root.values.power_saver
            focusRing: root.cursor === "power_saver"
            onPicked: function(id) { root.commit("power_saver", id) }
          }
          Label { text: "Share your motion" }
          Item {
            objectName: "row:share"
            width: parent.width
            height: Style.space(30)
            Text {
              anchors.verticalCenter: parent.verticalCenter
              text: root.code
              color: root.fg; font.family: root.mono; font.pixelSize: Style.font.body
            }
            Row {
              anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter
              spacing: Style.space(6)
              Pill { text: "Copy"; focusRing: root.cursor === "share" && root.shareIndex === 0; onClicked: root.copyCode() }
              Pill { text: "Paste"; focusRing: root.cursor === "share" && root.shareIndex === 1; onClicked: root.pasteCode() }
            }
          }
          // Remove asks twice: the first press arms it, the second (within 3 s) removes.
          Item {
            objectName: "row:uninstall"
            visible: root.installed
            width: parent.width
            height: Math.max(Style.space(30), removeText.implicitHeight)
            Text {
              id: removeText
              anchors.verticalCenter: parent.verticalCenter
              width: parent.width - removeBtn.width - Style.space(10); wrapMode: Text.WordWrap
              text: "Take the line out of hyprland.lua and delete the module"
              color: root.alphaOf(root.fg, 0.55); font.family: root.family; font.pixelSize: Style.font.caption
            }
            Pill {
              id: removeBtn
              anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter
              text: !root.confirmRemove ? "Remove" : root.confirmByKey ? "Press again to remove" : "Click again to remove"
              danger: true
              focusRing: root.cursor === "uninstall"
              onClicked: root.removeModule(false)
            }
          }
        }

        Text {
          visible: root.message !== "" || root.notice !== ""
          width: parent.width
          wrapMode: Text.WordWrap
          text: root.message !== "" ? root.message : root.notice
          color: root.message !== "" ? Commons.Color.urgent : root.alphaOf(root.fg, 0.6)
          font.family: root.family; font.pixelSize: Style.font.caption
        }
      }
    }
  }

  // ------------------------------------------------------------ components

  component Label: Text {
    objectName: "label"   // revealCursor keeps a control's label in view
    color: root.alphaOf(root.fg, 0.6); font.family: root.family; font.pixelSize: Style.font.caption
    font.weight: Font.DemiBold; font.letterSpacing: 0.3
  }

  // A recessed track with equal-width segments; the current one is raised.
  component Segmented: Rectangle {
    id: seg
    property var model: []
    property var current: null
    property bool focusRing: false
    signal picked(var id)
    readonly property bool lightTheme: Commons.Color.background.hslLightness > 0.5
    height: Style.space(30)
    radius: Style.space(9)
    color: lightTheme ? root.alphaOf(root.fg, 0.06) : Qt.rgba(0, 0, 0, 0.24)
    border.width: focusRing ? 2 : 1; border.color: root.alphaOf(root.fg, focusRing ? 0.7 : 0.08)
    opacity: enabled ? 1 : 0.45
    Row {
      anchors.fill: parent; anchors.margins: Style.space(3)
      spacing: Style.space(2)
      Repeater {
        model: seg.model
        Rectangle {
          required property var modelData
          readonly property bool on: String(seg.current) === String(modelData.id)
          width: (parent.width - Style.space(2) * (seg.model.length - 1)) / seg.model.length
          height: parent.height
          radius: Style.space(7)
          color: on ? root.alphaOf(root.fg, seg.lightTheme ? 0.95 : 0.16) : segMouse.containsMouse ? root.alphaOf(root.fg, 0.06) : "transparent"
          Behavior on color { ColorAnimation { duration: 140; easing.type: Easing.OutCubic } }
          Text {
            anchors.centerIn: parent
            text: modelData.label
            color: parent.on && seg.lightTheme ? Commons.Color.background : root.alphaOf(root.fg, parent.on ? 1 : 0.65)
            font.family: root.family; font.pixelSize: Style.font.caption + 1; font.weight: parent.on ? Font.DemiBold : Font.Normal
          }
          MouseArea {
            id: segMouse
            anchors.fill: parent
            hoverEnabled: true; cursorShape: Qt.PointingHandCursor
            enabled: seg.enabled && root.loaded
            onClicked: seg.picked(parent.modelData.id)
          }
        }
      }
    }
  }

  // Keyboard focus around a text link, drawn like the preset cards' ring.
  component FocusRing: Rectangle {
    anchors.fill: parent
    anchors.leftMargin: -Style.space(5); anchors.rightMargin: -Style.space(5)
    anchors.topMargin: -Style.space(3); anchors.bottomMargin: -Style.space(3)
    radius: height / 2
    color: "transparent"
    border.width: 2; border.color: root.alphaOf(root.fg, 0.7)
  }

  component Pill: Rectangle {
    id: pill
    property string text: ""
    property bool primary: false
    property bool danger: false
    property bool focusRing: false
    signal clicked()
    width: pillText.implicitWidth + Style.space(22); height: Style.space(28); radius: height / 2
    border.width: focusRing ? 2 : 0; border.color: root.alphaOf(root.fg, 0.7)
    color: primary ? root.accent : danger ? root.alphaOf(Commons.Color.urgent, pillMouse.containsMouse ? 0.25 : 0.14)
                   : root.alphaOf(root.fg, pillMouse.containsMouse ? 0.14 : 0.08)
    scale: pillMouse.pressed ? 0.98 : 1
    Behavior on scale { NumberAnimation { duration: 120; easing.type: Easing.OutCubic } }
    Text {
      id: pillText
      anchors.centerIn: parent
      text: pill.text
      color: pill.primary ? Commons.Color.background : root.fg
      font.family: root.family; font.pixelSize: Style.font.caption + 1; font.weight: Font.DemiBold
    }
    MouseArea { id: pillMouse; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; enabled: root.loaded; onClicked: pill.clicked() }
  }

  component Switch: Rectangle {
    id: sw
    property bool checked: false
    property bool focusRing: false
    signal toggled()
    signal hoveredIn()
    width: Style.space(38); height: Style.space(22); radius: height / 2
    color: checked ? root.accent : root.alphaOf(root.fg, 0.16)
    border.width: focusRing ? 2 : 0
    border.color: root.alphaOf(root.fg, 0.7)
    opacity: enabled ? 1 : 0.45
    Behavior on color { ColorAnimation { duration: 160; easing.type: Easing.OutCubic } }
    Rectangle {
      width: parent.height - Style.space(4); height: width; radius: width / 2
      y: Style.space(2)
      x: sw.checked ? parent.width - width - Style.space(2) : Style.space(2)
      color: "white"
      Behavior on x { NumberAnimation { duration: 160; easing.type: Easing.OutCubic } }
    }
    MouseArea {
      anchors.fill: parent; anchors.margins: -Style.space(5)
      hoverEnabled: true; cursorShape: Qt.PointingHandCursor
      enabled: sw.enabled && root.loaded
      onClicked: sw.toggled()
      onEntered: sw.hoveredIn()
    }
  }

  component SwitchRow: Item {
    id: row
    property string title: ""
    property string caption: ""
    property bool checked: false
    property bool focusRing: false
    signal toggled()
    signal hoveredIn()
    height: Math.max(rowText.implicitHeight, rowSwitch.height)
    Column {
      id: rowText
      anchors.verticalCenter: parent.verticalCenter
      width: parent.width - rowSwitch.width - Style.space(12)
      spacing: Style.space(2)
      Text { text: row.title; color: root.fg; font.family: root.family; font.pixelSize: Style.font.body; font.weight: row.focusRing ? Font.DemiBold : Font.Normal }
      Text { width: parent.width; wrapMode: Text.WordWrap; text: row.caption; color: root.alphaOf(root.fg, 0.5); font.family: root.family; font.pixelSize: Style.font.caption }
    }
    Switch {
      id: rowSwitch
      anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter
      width: Style.space(32); height: Style.space(18)
      checked: row.checked
      enabled: row.enabled
      focusRing: row.focusRing
      onToggled: row.toggled()
      onHoveredIn: row.hoveredIn()
    }
  }

  // A preset's spring as position over 450 ms, with a faint line at rest.
  // Overshoot is drawn 6x taller so a 1% settle is visible at icon size.
  component CurveGlyph: Canvas {
    id: glyph
    property var samples: []
    property color stroke: "white"
    property color guide: "gray"
    onSamplesChanged: requestPaint()
    onStrokeChanged: requestPaint()
    onWidthChanged: requestPaint()
    onPaint: {
      var ctx = getContext("2d")
      ctx.reset()
      var top = height * 0.18, bottom = height
      function yAt(p) { return bottom - (bottom - top) * (p <= 1 ? p : 1 + (p - 1) * 6) }
      ctx.strokeStyle = glyph.guide
      ctx.lineWidth = 1
      ctx.beginPath(); ctx.moveTo(0, Math.round(yAt(1)) + 0.5); ctx.lineTo(width, Math.round(yAt(1)) + 0.5); ctx.stroke()
      if (samples.length < 2) return
      ctx.strokeStyle = glyph.stroke
      ctx.lineWidth = 1.6
      ctx.lineCap = "round"; ctx.lineJoin = "round"
      ctx.beginPath()
      for (var i = 0; i < samples.length; i++) {
        var x = width * i / (samples.length - 1), y = yAt(samples[i])
        if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y)
      }
      ctx.stroke()
    }
  }

  // Miniature desktop: workspace A is a strip of three full-width windows (your
  // own, when "Your windows" is on), B holds two half-width ones. The camera
  // steps along A, hops to B and back, integrating the spring per real frame.
  // Stock mode replays Omarchy's defaults: windows ease out, workspaces cut.
  component MotionPreview: Item {
    id: mp
    property real duration: 290
    property real bounce: 15
    property real travel: 30
    property bool focusFade: true
    property bool stock: false
    property bool vertical: true
    readonly property bool running: frames.running || script.running
    property string caption: "LIVE · your settings"
    signal played()   // the scripted run reached its end
    property real camX: 0
    property real camV: 0
    property real targetX: 0
    property real ws: 0
    property real wsV: 0
    property real targetWs: 0
    property int focusIndex: 0
    property int step: 0
    property real stockT: 1
    property real stockFromX: 0
    readonly property real gap: Style.space(6)
    readonly property real winW: width - Style.space(28)
    readonly property real winH: height - Style.space(40)
    readonly property real pitch: winW + gap
    readonly property var steps: [{x: 1}, {x: 2}, {ws: 1}, {ws: 0}, {x: 1}, {x: 0}]
    // Your real windows on the focused workspace.
    readonly property var mine: {
      if (!root.values.preview_windows || !root.opened) return []
      var mon = Hyprland.focusedMonitor
      var wsId = mon && mon.activeWorkspace ? mon.activeWorkspace.id : -1
      var list = []
      var all = Hyprland.toplevels.values
      for (var i = 0; i < all.length; i++) {
        var t = all[i]
        var o = t.lastIpcObject || {}
        if (t.workspace && t.workspace.id === wsId && t.wayland && !o.floating)
          list.push({t: t, x: o.at ? o.at[0] : 0, recent: o.focusHistoryID === undefined ? 1e9 : o.focusHistoryID})
      }
      list.sort(function(a, b) { return a.x - b.x })
      // Start from the window you were last in (the panel holds focus while it
      // is open) and continue to the right, like Super+Right.
      var start = 0
      for (var j = 0; j < list.length; j++) if (list[j].recent < list[start].recent) start = j
      var out = []
      for (var k = 0; k < Math.min(3, list.length); k++) out.push(list[(start + k) % list.length].t.wayland)
      return out
    }

    function play() { stop(); step = 0; caption = "LIVE · your settings"; script.interval = 220; script.start() }
    function stop() { script.stop(); frames.stop(); camX = 0; camV = 0; targetX = 0; ws = 0; wsV = 0; targetWs = 0; focusIndex = 0 }
    function next() {
      if (step >= steps.length) { caption = "LIVE · your settings"; played(); return }
      var s = steps[step++]
      if (s.x !== undefined) { targetX = s.x; focusIndex = s.x; caption = "Window " + (s.x + 1) + " of 3" }
      else {
        targetWs = s.ws; caption = s.ws ? "Next workspace" : "Back"
        if (stock) { ws = s.ws; wsV = 0 }   // stock Omarchy: workspaces switch without animation
      }
      stockT = 0; stockFromX = camX
      frames.start()
      script.interval = s.ws !== undefined ? 620 : 520
      script.start()
    }

    Timer { id: script; repeat: false; onTriggered: mp.next() }
    FrameAnimation {
      id: frames
      onTriggered: {
        var dt = Math.min(0.05, frameTime)
        if (mp.stock) {
          mp.stockT += dt / 0.379
          mp.camX = mp.stockFromX + (mp.targetX - mp.stockFromX) * Spring.bezier(0.23, 1, 0.32, 1, Math.min(1, mp.stockT))
          if (mp.stockT >= 1) { mp.camX = mp.targetX; stop() }
          return
        }
        var s = Spring.springOf(mp.duration, mp.bounce)
        var nx = Spring.advance(s, mp.camX - mp.targetX, mp.camV, dt)
        mp.camX = mp.targetX + nx[0]; mp.camV = nx[1]
        var nw = Spring.advance(s, mp.ws - mp.targetWs, mp.wsV, dt)
        mp.ws = mp.targetWs + nw[0]; mp.wsV = nw[1]
        if (Math.abs(nx[0]) < 0.0005 && Math.abs(nx[1]) < 0.001 && Math.abs(nw[0]) < 0.0005 && Math.abs(nw[1]) < 0.001) {
          mp.camX = mp.targetX; mp.ws = mp.targetWs; mp.camV = 0; mp.wsV = 0; stop()
        }
      }
    }

    // Workspace layers follow Hyprland's slidefade: both move by `travel` of
    // the screen, the leaving one fades out and the arriving one fades in.
    readonly property real t: stock ? 1 : travel / 100
    readonly property real fadeOn: !stock && travel < 100 ? 1 : 0
    readonly property real wsP: Math.max(-0.2, Math.min(1.2, ws))
    readonly property real span: vertical ? height : width

    Item {
      width: parent.width; height: parent.height
      x: mp.vertical ? 0 : -mp.wsP * mp.t * mp.span
      y: mp.vertical ? -mp.wsP * mp.t * mp.span : 0
      opacity: 1 - mp.fadeOn * Math.max(0, Math.min(1, mp.wsP))
      Repeater {
        model: 3
        MiniWin {
          required property int index
          x: Style.space(14) + (index - mp.camX) * mp.pitch
          y: Style.space(14)
          width: mp.winW; height: mp.winH
          focused: mp.targetWs === 0 && mp.focusIndex === index
          fade: mp.focusFade && !mp.stock
          fadeMs: Math.max(120, mp.duration * 0.8)
          lines: index
          source: index < mp.mine.length ? mp.mine[index] : null
        }
      }
    }
    Item {
      width: parent.width; height: parent.height
      x: mp.vertical ? 0 : (1 - mp.wsP) * mp.t * mp.span
      y: mp.vertical ? (1 - mp.wsP) * mp.t * mp.span : 0
      opacity: mp.fadeOn ? Math.max(0, Math.min(1, mp.wsP)) : 1
      visible: mp.wsP > 0.001 || mp.targetWs > 0
      Repeater {
        model: 2
        MiniWin {
          required property int index
          x: Style.space(14) + index * ((mp.winW - mp.gap) / 2 + mp.gap)
          y: Style.space(14)
          width: (mp.winW - mp.gap) / 2; height: mp.winH
          focused: mp.targetWs === 1 && index === 0
          fade: mp.focusFade && !mp.stock
          fadeMs: Math.max(120, mp.duration * 0.8)
          lines: index + 3
        }
      }
    }
  }

  // A theme-coloured window (or a still of a real one): focused at 95% opacity,
  // the rest at 62%.
  component MiniWin: ClippingRectangle {
    id: win
    property bool focused: false
    property bool fade: true
    property real fadeMs: 230
    property int lines: 0
    property var source: null
    radius: Style.space(7)
    color: Commons.Color.background
    border.width: 1
    border.color: focused ? root.accent : root.alphaOf(root.fg, 0.14)
    opacity: focused ? 0.95 : 0.62
    Behavior on opacity { enabled: win.fade; NumberAnimation { duration: win.fadeMs; easing.type: Easing.OutQuart } }
    Column {
      visible: !shot.hasContent
      x: Style.space(10); y: Style.space(12)
      spacing: Style.space(5)
      Rectangle { width: win.width * (0.22 + 0.06 * (win.lines % 3)); height: Style.space(5); radius: 2.5; color: root.alphaOf(root.fg, 0.32) }
      Rectangle { width: win.width * (0.55 - 0.05 * (win.lines % 2)); height: Style.space(5); radius: 2.5; color: root.alphaOf(root.fg, 0.16) }
      Rectangle { width: win.width * 0.4; height: Style.space(5); radius: 2.5; color: root.alphaOf(root.fg, 0.16) }
    }
    // Cover the card like a window seen through it: scaled to fill, top-aligned.
    ScreencopyView {
      id: shot
      readonly property real aspect: sourceSize.width > 0 && sourceSize.height > 0 ? sourceSize.width / sourceSize.height : 1.7
      width: Math.max(win.width, win.height * aspect)
      height: width / aspect
      x: (win.width - width) / 2
      captureSource: win.source
      live: false
      visible: win.source !== null && hasContent
    }
  }

  // Slim slider row: label · value · 6px track; previews live, applies on release
  // (or once the wheel has been still for a moment).
  component Slider: Column {
    id: sl
    property var spec: ({})
    property bool dragging: false
    property real live: Number(root.values[spec.key] || 0)
    property real wheelRest: 0
    readonly property real range: Math.max(0.0001, spec.max - spec.min)
    readonly property real progress: Math.max(0, Math.min(1, (live - spec.min) / range))
    readonly property bool focused: root.cursor === spec.key
    objectName: "row:" + spec.key
    spacing: Style.space(7)
    enabled: root.loaded
    Connections { target: root; function onValuesChanged() { if (!sl.dragging && !settle.running) sl.live = Number(root.values[sl.spec.key] || 0) } }
    Timer { id: settle; interval: 250; onTriggered: root.commit(sl.spec.key, sl.live) }
    // Whole steps from a wheel delta. Touchpads send many small deltas, so they add up to 120
    // per step (one mouse notch, clamped for wheels that report more); reversing starts afresh.
    function wheelSteps(delta) {
      delta = Math.max(-120, Math.min(120, delta))
      const total = (sl.wheelRest * delta < 0 ? 0 : sl.wheelRest) + delta
      const steps = total < 0 ? Math.ceil(total / 120) : Math.floor(total / 120)
      sl.wheelRest = total - steps * 120
      return steps
    }

    Item {
      width: parent.width
      height: Math.max(nameText.implicitHeight, valueText.implicitHeight)
      Text {
        id: nameText
        text: sl.spec.label
        color: root.fg; font.family: root.family; font.pixelSize: Style.font.body
        font.weight: sl.focused ? Font.DemiBold : Font.Normal
      }
      Text {
        id: valueText
        anchors.right: parent.right
        text: root.fmt(sl.spec.key, sl.live)
        color: root.alphaOf(root.fg, 0.6)
        font.family: root.mono; font.pixelSize: Style.font.body
      }
    }
    Item {
      width: parent.width
      height: Style.space(12)
      Rectangle {
        id: track
        anchors.verticalCenter: parent.verticalCenter
        width: parent.width; height: Style.space(6); radius: height / 2
        color: root.alphaOf(root.fg, mouse.containsMouse || sl.dragging || sl.focused ? 0.16 : 0.1)
        Rectangle {
          width: Math.max(track.height, track.width * sl.progress); height: parent.height; radius: parent.radius
          color: root.alphaOf(root.fg, 0.78)
          Behavior on width { enabled: !sl.dragging; NumberAnimation { duration: 140; easing.type: Easing.OutCubic } }
        }
      }
      MouseArea {
        id: mouse
        anchors.fill: parent
        anchors.topMargin: -Style.space(6); anchors.bottomMargin: -Style.space(6)
        hoverEnabled: true
        cursorShape: Qt.PointingHandCursor
        preventStealing: true   // a slightly diagonal drag stays a drag, not a body scroll
        function valueAt(x) {
          var f = Math.max(0, Math.min(1, x / track.width))
          var v = Math.round((sl.spec.min + f * sl.range) / sl.spec.step) * sl.spec.step
          return Math.max(sl.spec.min, Math.min(sl.spec.max, v))
        }
        function preview(v) { sl.live = v; const d = Object.assign({}, root.draft); d[sl.spec.key] = v; root.draft = d }
        onEntered: root.cursor = sl.spec.key
        onExited: if (root.wheelOwner === sl) root.wheelOwner = null
        onPressed: function(m) { sl.dragging = true; root.cursor = sl.spec.key; preview(valueAt(m.x)) }
        onPositionChanged: function(m) { if (sl.dragging) preview(valueAt(m.x)) }
        onReleased: { sl.dragging = false; root.commit(sl.spec.key, sl.live) }
        // An interrupted drag (grab stolen, panel closing) keeps the saved value.
        onCanceled: {
          sl.dragging = false
          const d = Object.assign({}, root.draft); delete d[sl.spec.key]; root.draft = d
          sl.live = Number(root.values[sl.spec.key] || 0)
        }
        // Show each step at once; one commit after the scrolling settles.
        // A touchpad scroll belongs to what it began over, so scrolling the body past a
        // slider keeps scrolling. Mouse wheels have no phases: a body scroll in the last
        // 300 ms wins instead. Unaccepted events scroll the body.
        onWheel: function(w) {
          if (w.phase === Qt.ScrollBegin) root.wheelOwner = sl
          const mine = w.phase === Qt.NoScrollPhase ? Date.now() - root.scrolledAt > 300 : root.wheelOwner === sl
          if (!mine || w.angleDelta.y === 0 || sl.dragging) { w.accepted = false; return }
          const steps = sl.wheelSteps(w.angleDelta.y)
          if (steps === 0) return
          const v = Math.max(sl.spec.min, Math.min(sl.spec.max, sl.live + steps * sl.spec.step))
          if (v !== sl.live) { preview(v); settle.restart() }
        }
      }
    }
  }
}
