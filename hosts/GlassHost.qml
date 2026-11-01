import QtQuick
import qs.Commons
import "../../denon.bar/panels" as Kit

// The frosted panel host from the denon.bar plugin (hover to preview, click to
// keep). Only loaded when that plugin is installed; same API as StockHost.
Kit.PopupPanel {
  id: host
  property var tiles: null
  readonly property Item slot: slotItem
  function commitHover(owner) { return Kit.PanelMotion.commitHover(owner) }

  owner: tiles
  open: tiles ? tiles.opened : false
  focusTarget: tiles ? tiles.keyboardItem : null
  contentWidth: host.fittedContentWidth(Style.space(400))
  contentHeight: host.fittedContentHeight(tiles ? tiles.contentImplicitHeight : 0, Style.space(820))

  Item { id: slotItem; anchors.fill: parent }
}
