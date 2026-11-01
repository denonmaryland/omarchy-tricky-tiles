import QtQuick
import qs.Ui
import qs.Commons

// Omarchy's stock popup card. Tricky Tiles reparents its content into `slot`.
KeyboardPanel {
  id: host
  property var tiles: null
  readonly property Item slot: slotItem
  function commitHover(owner) { return false }

  owner: tiles
  open: tiles ? tiles.opened : false
  focusTarget: tiles ? tiles.keyboardItem : null
  padding: Style.space(12)
  contentWidth: host.fittedContentWidth(Style.space(400))
  contentHeight: host.fittedContentHeight(tiles ? tiles.contentImplicitHeight : 0, Style.space(820))

  // No clip here: the content scrolls and clips itself, slightly outside the slot.
  Item { id: slotItem; anchors.fill: parent }
}
