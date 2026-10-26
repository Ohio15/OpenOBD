"""
livedata — two read-only workspaces backed by truck-mcp's drive-log store.

Live Data
  A session picker over truck-mcp's sessions/*.tmsession.db (aggregated across
  every candidate root, so the field kit's drives are visible) plus a tile grid
  of the latest value per channel, polled once a second off the SQLite file.
  Every tile renders through tmstore.display_state, so the facts the store keeps
  apart stay apart — and, crucially, a finished drive (archive) or a live drive
  whose logger stopped advancing (stale) never renders as a fresh live
  measurement. A read failure flips the whole grid to an explicit failed state
  rather than freezing on stale values that still claim to be live. Nothing here
  opens the serial port, so it never contends with a running logger for COM3.

  One combined view (HP Tuners-style): the compact tile grid (every channel,
  with provenance states — the raw store view) sits above Chart vs. Time
  (stripchart.py), a VCM-Scanner-style strip chart of the fresh numeric
  channels, so live numbers read against the traces while focusing on a
  driving condition. The Dashboard stays the visual gauge/controls surface;
  this tab is the raw view of what a drive capture is recording.

  Field selection and lane grouping live in ONE shared model (chanlayout):
  hiding a field removes it from the tiles AND the chart, adding it back
  restores both. The graph is composed by DRAGGING tiles onto the graphed-
  channels row table above the chart (drop on a row = join its lane, below
  the rows = new lane) — the table is the row-per-trace legend with live
  values, and once customized it shows exactly what plots. Menus (Fields…
  button, tile / chart-lane / table right-click) mirror every drag action.
  The layout persists across sessions; channels that cannot chart are
  labeled tile-only and refused as drops visibly, never silently dropped.

  Chrome is the Tuning / Diagnostics workspace's (v0.37.0): a breadcrumb
  whose chips carry the view state, provenance and Hold, one 36 px toolbar
  (picker, Rescan F5, Fields…, Window, Hold, tile filter Ctrl+F), danger
  banners for read / open failures. Hold freezes the display, never the
  reader cursors, and a frozen live value drops the live palette.

Active Tests
  A DISPLAY-ONLY reflection of vehicle-control state, polled on its own timer and
  stamped with the time of the last read so a stale verdict cannot masquerade as
  current. truck-mcp's registry of executable controls is empty by policy — a
  supported CPID executes on first contact with whatever bytes were guessed, so
  none may be scanned for — and this page states that as a decision, not a blank.
  It reads truck-mcp's control journal across all roots and surfaces the one
  thing that matters most: whether a control session ended without releasing an
  actuator. A journal that cannot be located or read yields UNKNOWN (a warning),
  never a green all-clear. There is deliberately NO arm/fire path in OpenOBD.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QDrag, QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFrame, QGridLayout, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QMenu, QPushButton, QScrollArea, QSizePolicy,
    QSplitter, QTableWidget, QTableWidgetItem, QToolButton, QVBoxLayout,
    QWidget,
)

from . import chanlayout, ctljournal, theme, tmstore
from .appsettings import app_settings
from .chanlayout import ChannelLayout
from .stripchart import ChartPane, channel_mime, chartable_names, fmt_value
from .tmstore import TmSessionReader, TmSessionError
from .tuningui import (
    GROUP_ORDER, Breadcrumb, ElidedLabel, channel_group, empty_state_label,
    make_toolbar,
)

# The states, and the ONLY colors each may take. Each means exactly one thing.
STATE_COLORS = theme.LIVEDATA_STATE_COLORS
STATE_CAPTION = {
    "fresh": "live", "stale": "not advancing", "archive": "archived drive",
    "error": "module error", "unavail": "no data", "notread": "not read",
    "badvalue": "bad value", "failed": "read failed",
}
POLL_MS = 1000
SESSION_RESCAN_MS = 5000
CONTROL_POLL_MS = 3000
# Character budget the session picker reserves (it elides beyond this when the
# window is narrow; the full label stays in its tooltip and its popup).
PICKER_MIN_CHARS = 28

#: QSettings keys (alongside chanlayout.SETTINGS_KEY = "livedata/layout").
GROUPS_KEY = "livedata/groups_collapsed"   # JSON list of collapsed groups
SPLIT_KEY = "livedata/split"               # tiles / chart splitter state
SPAN_KEY = "livedata/span"                 # chart window for live sessions

#: Tile width floor and the per-column pitch the reflow divides by.
TILE_MIN_W = 128
TILE_PITCH = 136

#: View state -> breadcrumb chip kind. Each state means exactly one thing.
VIEW_STATE_CHIP = {
    "live": "ok", "not advancing": "warning", "archived": "muted",
    "read failed": "danger",
}
HELD_CAPTION = "held"


class ValueTile(QFrame):
    """One channel's latest value, colored by display state. Built once per
    channel then updated in place — never rebuilt, so the grid stays stable.

    Name (with the unit right-aligned) over the big value, the state
    caption, then a muted min / max footer of the measured values seen since
    the session was bound."""

    def __init__(self, name: str, unit: str = ""):
        super().__init__()
        self.channel = name
        self._unit = unit
        self._press_pos = None
        self.state = "notread"         # last display_state (or failed)
        self.shown_state = "notread"   # the palette on screen (held -> stale)
        self.setFrameShape(QFrame.StyledPanel)
        self.setMinimumWidth(TILE_MIN_W)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.setCursor(Qt.OpenHandCursor)   # tiles drag onto the graph table
        self.setToolTip(f"{name}: drag onto the graphed-channels table to "
                        f"chart it; right-click for options")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 4, 8, 4)
        lay.setSpacing(0)
        top = QHBoxLayout()
        top.setSpacing(theme.SPACING["sm"])
        self._label = ElidedLabel(name)
        self._label.setObjectName("tileName")
        self._unit_lbl = QLabel(unit)
        self._unit_lbl.setObjectName("tileUnit")
        self._unit_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        top.addWidget(self._label, 1)
        top.addWidget(self._unit_lbl)
        self._value = QLabel("— not read")
        vf = QFont(); vf.setPointSize(13); vf.setBold(True)
        self._value.setFont(vf)
        self._state = QLabel("")
        self._state.setStyleSheet(
            theme.livedata_text_qss("faint-text", "font-size:9px;"))
        self._range = QLabel("min — · max —")
        self._range.setObjectName("tileRange")
        self._range.setToolTip("Lowest / highest measured (fresh) value "
                               "since this session was opened")
        lay.addLayout(top)
        lay.addWidget(self._value)
        lay.addWidget(self._state)
        lay.addWidget(self._range)
        self._apply("notread")

    def update_from(self, sample, channel_meta, *, archived: bool,
                    session_stale: bool):
        try:
            ds = tmstore.display_state(sample, channel_meta, archived=archived,
                                       session_stale=session_stale)
        except Exception as exc:   # contract violation — surface, never draw a lie
            self._value.setText("BAD")
            self._state.setText(str(exc)[:44])
            self.state = "badvalue"
            self._apply("badvalue")
            return
        # The unit lives on the name row; the value is the display text only.
        self._value.setText(ds.text)
        if ds.state == "carried" and ds.age is not None:
            self._state.setText(f"carried · {ds.age:.1f}s old")
        elif ds.state == "carried":
            self._state.setText("carried · age unknown")
        else:
            self._state.setText(STATE_CAPTION.get(ds.state, ds.state))
        self.state = ds.state
        self._apply(ds.state)

    def set_range(self, rng: Optional[tuple[float, float]]):
        if rng is None:
            self._range.setText("min — · max —")
        else:
            self._range.setText(f"min {fmt_value(rng[0])} · "
                                f"max {fmt_value(rng[1])}")

    def range_text(self) -> str:
        return self._range.text()

    def value_text(self) -> str:
        return self._value.text()

    def caption_text(self) -> str:
        return self._state.text()

    def set_held(self, on: bool):
        """Hold freezes the display: a value that was live is no longer being
        refreshed, so it may not keep the live palette. It takes the
        not-advancing palette and says why; the next tick after release
        restores the real state."""
        if on and self.state == "fresh":
            self._state.setText("held · not updating")
            self._apply("stale")

    def set_failed(self, message: str):
        self._value.setText("—")
        self._state.setText(message[:44])
        self.state = "failed"
        self._apply("failed")

    # -- drag source --------------------------------------------------------- #
    # A tile drags its channel name (stripchart.CHANNEL_MIME); the graphed-
    # channels table above the chart is the drop target that adds the field
    # to the graph. Whether the drop is legal (chartable) is the TARGET's
    # decision — a text field drags fine but is refused visibly.
    def mousePressEvent(self, event):   # noqa: N802
        if event.button() == Qt.LeftButton:
            self._press_pos = event.position().toPoint()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):   # noqa: N802
        self._press_pos = None
        super().mouseReleaseEvent(event)

    def mouseMoveEvent(self, event):   # noqa: N802
        if (self._press_pos is not None
                and event.buttons() & Qt.LeftButton
                and (event.position().toPoint() - self._press_pos
                     ).manhattanLength()
                >= QApplication.startDragDistance()):
            self._press_pos = None
            drag = QDrag(self)
            drag.setMimeData(channel_mime(self.channel))
            drag.setPixmap(self.grab())
            drag.exec(Qt.CopyAction)
        super().mouseMoveEvent(event)

    def _apply(self, state: str):
        self.shown_state = state
        frame_qss, value_qss = theme.livedata_tile_qss(state)
        self.setStyleSheet(frame_qss)
        self._value.setStyleSheet(value_qss)


class TileGroup(QWidget):
    """A collapsible group of tiles (Engine / Fuel / Spark / Trans / Sensors
    / Other) with its count in the header; tiles reflow in a grid whose
    column count the page sets from the available width."""

    collapsed_changed = Signal(str, bool)

    def __init__(self, name: str):
        super().__init__()
        self.name = name
        self.names: list[str] = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(theme.SPACING["sm"])
        self.header = QToolButton()
        self.header.setObjectName("tileGroupHeader")
        self.header.setCheckable(True)
        self.header.setChecked(True)
        self.header.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.header.setArrowType(Qt.DownArrow)
        self.header.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.header.setToolTip(f"Show / hide the {name} tiles")
        self.header.toggled.connect(self._on_toggled)
        lay.addWidget(self.header)
        self.body = QWidget()
        self.grid = QGridLayout(self.body)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(6)
        self._grid_cols = 0
        lay.addWidget(self.body)

    def is_collapsed(self) -> bool:
        return not self.header.isChecked()

    def set_collapsed(self, collapsed: bool):
        self.header.setChecked(not collapsed)

    def _on_toggled(self, expanded: bool):
        self.header.setArrowType(Qt.DownArrow if expanded else Qt.RightArrow)
        self.body.setVisible(expanded)
        self.collapsed_changed.emit(self.name, not expanded)

    def set_counts(self, shown: int, total: int):
        self.header.setText(f"{self.name}  ·  {shown}" if shown == total
                            else f"{self.name}  ·  {shown} of {total}")

    def place(self, tiles: list[ValueTile], cols: int):
        while self.grid.count():
            self.grid.takeAt(0)
        # Uniform columns even when the group has fewer tiles than columns,
        # so a lone tile does not stretch across the page.
        for c in range(max(cols, self._grid_cols)):
            on = c < cols
            self.grid.setColumnStretch(c, 1 if on else 0)
            self.grid.setColumnMinimumWidth(c, TILE_MIN_W if on else 0)
        self._grid_cols = cols
        for i, tile in enumerate(tiles):
            self.grid.addWidget(tile, i // cols, i % cols)


class LiveDataPage(QWidget):
    """Session picker + live tile grid + Chart vs. Time over truck-mcp's
    drive-log store, under the workspace chrome (breadcrumb + chips + one
    36 px toolbar) the Tuning and Diagnostics tabs share."""

    def __init__(self):
        super().__init__()
        self.setObjectName("liveDataPage")
        self.setStyleSheet(theme.livedata_qss())
        self._reader = None
        self._reader_path = None
        self._meta: dict = {}
        self._tiles: dict[str, ValueTile] = {}
        self._groups: dict[str, TileGroup] = {}
        self._channel_meta: dict[str, dict] = {}
        self._chartable: list[str] = []
        self._sessions: list[dict] = []
        self._archived = False
        self._fail_streak = 0
        self._active = False
        self._held = False
        self._view_state = ""
        self._as_of: Optional[str] = None
        self._cols = 0
        # Running min / max of measured samples per channel since bind, fed
        # by an incremental aggregate read (TmSessionReader.range_after).
        self._ranges: dict[str, tuple[float, float]] = {}
        self._range_cursor = 0

        # The ONE selection+grouping model both views derive from. Global, not
        # per-session: truck-mcp store channel names are stable across drives,
        # so a grouping tuned for troubleshooting carries to the next session.
        self._settings = app_settings()
        self._layout = ChannelLayout.from_json(
            self._settings.value(chanlayout.SETTINGS_KEY))
        self._collapsed = self._load_collapsed()

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.crumb = Breadcrumb(elastic_chips=True)
        self.crumb.set_path(["Live Data"])
        root.addWidget(self.crumb)

        # The chart pane is built first: its window-span combo lives in this
        # page's toolbar (one toolbar per page), the pane keeps owning it.
        self._chart = ChartPane(own_bar=False)
        root.addWidget(self._build_toolbar())

        body = QVBoxLayout()
        m = theme.SPACING["sm"]
        body.setContentsMargins(m, m, m, m)
        body.setSpacing(m)
        self._banner = QLabel()
        self._banner.setObjectName("liveBanner")
        self._banner.setWordWrap(True)
        self._banner.setTextFormat(Qt.PlainText)
        self._banner.setProperty("banner", "danger")
        self._banner.hide()
        body.addWidget(self._banner)

        # HP-Tuners-style combined view: the live tiles and the Chart vs. Time
        # strip chart share one page (no sub-tabs) so the numbers can be read
        # against the traces while focusing on a driving condition. Tiles sit
        # in collapsible groups with width-adaptive columns; the splitter
        # tunes the ratio and remembers it.
        self._grid_host = QWidget()
        self._grid_host.setObjectName("tileHost")
        self._groups_lay = QVBoxLayout(self._grid_host)
        self._groups_lay.setContentsMargins(2, 2, 2, 2)
        self._groups_lay.setSpacing(theme.SPACING["md"])
        self._groups_lay.addStretch(1)
        self._scroll = QScrollArea()
        self._scroll.setObjectName("tileScroll")
        self._scroll.setWidgetResizable(True)
        self._scroll.setWidget(self._grid_host)

        self._empty = empty_state_label("")
        self._empty.hide()

        tiles_host = QWidget()
        tiles_lay = QVBoxLayout(tiles_host)
        tiles_lay.setContentsMargins(0, 0, 0, 0)
        tiles_lay.addWidget(self._scroll, 1)
        tiles_lay.addWidget(self._empty, 1)

        # Right-click on a lane: move/hide its channels, or add a field back.
        self._chart.chart.setContextMenuPolicy(Qt.CustomContextMenu)
        self._chart.chart.customContextMenuRequested.connect(self._chart_menu)
        # Dropping a tile on the graphed-channels table adds that field to
        # the graph (into the row's lane, or a new lane below the rows).
        self._chart.table.channel_dropped.connect(self._on_channel_dropped)
        self._chart.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self._chart.table.customContextMenuRequested.connect(self._table_menu)
        self._split = QSplitter(Qt.Vertical)
        self._split.setObjectName("liveSplit")
        self._split.setChildrenCollapsible(False)
        self._split.addWidget(tiles_host)
        self._split.addWidget(self._chart)
        self._split.setStretchFactor(0, 1)
        self._split.setStretchFactor(1, 2)
        state = self._settings.value(SPLIT_KEY)
        if state is None or not self._split.restoreState(state):
            self._split.setSizes([250, 430])
        self._split.splitterMoved.connect(lambda *_a: self.save_state())
        body.addWidget(self._split, 1)
        root.addLayout(body, 1)

        # F5 rescans while this page is showing (enabled in showEvent, so it
        # can never fire from another tab and collide with the Dashboard's
        # F5 or Diagnostics' F5).
        self._f5 = QShortcut(QKeySequence(Qt.Key_F5), self)
        self._f5.setContext(Qt.WindowShortcut)
        self._f5.setEnabled(False)
        self._f5.activated.connect(self.rescan_sessions)

        self._poll = QTimer(self)
        self._poll.timeout.connect(self._tick)
        self._rescan = QTimer(self)
        self._rescan.timeout.connect(self._maybe_rescan)

        self.rescan_sessions()
        self._refresh_chrome()

    def _build_toolbar(self):
        # The toolbar must never set the page's minimum width: QTabWidget's
        # minimum is the max over its pages, so a wide bar here pins the
        # WHOLE window (it was 1522 px). The picker therefore sizes from a
        # fixed character budget instead of its longest session label (the
        # default AdjustToContentsOnFirstShow), grows into spare room, elides
        # when narrow and carries the full label as a tooltip; its popup is
        # widened to the full labels so nothing is hidden. Whatever does not
        # fit goes to the toolbar's overflow menu.
        tb = make_toolbar("liveToolbar")
        self.toolbar = tb
        tb.addWidget(QLabel("Session"))
        self._picker = QComboBox()
        self._picker.setSizeAdjustPolicy(
            QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self._picker.setMinimumContentsLength(PICKER_MIN_CHARS)
        self._picker.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._picker.view().setTextElideMode(Qt.ElideNone)
        self._picker.currentIndexChanged.connect(self._sync_picker_tooltip)
        self._picker.currentIndexChanged.connect(self._on_pick)
        tb.addWidget(self._picker)
        self.act_rescan = self._action(
            "refresh", "Rescan", "Rescan the truck-mcp session folders  (F5)",
            self.rescan_sessions)
        tb.addAction(self.act_rescan)
        self.act_fields = self._action(
            "fields", "Fields…",
            "Add or subtract fields — the tiles and the chart always show "
            "the same selection.", self._show_fields_menu)
        tb.addAction(self.act_fields)
        tb.addSeparator()
        tb.addWidget(QLabel("Window"))
        tb.addWidget(self._chart.span_combo)
        self._chart.span_combo.activated.connect(self._on_span_chosen)
        self.act_hold = self._action(
            "hold", "Hold",
            "Freeze the tiles, table and chart to read them; the session "
            "keeps recording and Release catches up. Archived drives do "
            "not need it.", lambda: None)
        self.act_hold.setCheckable(True)
        self.act_hold.toggled.connect(self._set_hold)
        tb.addAction(self.act_hold)
        tb.addSeparator()
        self._filter = QLineEdit()
        self._filter.setObjectName("liveFilter")
        self._filter.setPlaceholderText("Filter tiles  (Ctrl+F)")
        self._filter.setToolTip("Show only tiles whose name or group "
                                "contains this text (display only — the "
                                "field selection is unchanged). Esc clears.")
        self._filter.setClearButtonEnabled(True)
        self._filter.setMinimumWidth(110)
        self._filter.setMaximumWidth(200)
        self._filter.textChanged.connect(self._apply_filter)
        self._filter.installEventFilter(self)
        tb.addWidget(self._filter)
        # Long-standing names for the two buttons.
        self._refresh_btn = tb.widgetForAction(self.act_rescan)
        self._fields_btn = tb.widgetForAction(self.act_fields)
        return tb

    def _action(self, glyph: str, text: str, tip: str, slot) -> QAction:
        a = QAction(theme.glyph_icon(glyph), text, self)
        a.setToolTip(tip)
        a.triggered.connect(slot)
        return a

    # -- lifecycle: only run timers while the tab is actually visible -------- #
    def showEvent(self, event):   # noqa: N802
        super().showEvent(event)
        self._f5.setEnabled(True)
        if not self._active:
            self._active = True
            self.rescan_sessions()
            self._poll.start(POLL_MS)
            self._rescan.start(SESSION_RESCAN_MS)
        self._reflow_tiles()   # width is real once the page is shown

    def hideEvent(self, event):   # noqa: N802
        super().hideEvent(event)
        self._f5.setEnabled(False)
        self._active = False
        self._poll.stop()
        self._rescan.stop()

    def save_state(self):
        self._settings.setValue(SPLIT_KEY, self._split.saveState())

    def shutdown(self):
        self._active = False
        self._poll.stop()
        self._rescan.stop()
        self._close_reader()

    def closeEvent(self, event):   # noqa: N802
        self.shutdown()
        super().closeEvent(event)

    # -- keyboard ------------------------------------------------------------ #
    def focus_filter(self):
        """Ctrl+F (routed by the main window while this page is current)."""
        self._filter.setFocus(Qt.ShortcutFocusReason)
        self._filter.selectAll()

    def eventFilter(self, obj, event):  # noqa: N802
        if (obj is self._filter and event.type() == QEvent.KeyPress
                and event.key() == Qt.Key_Escape):
            self._filter.clear()
            return True
        return super().eventFilter(obj, event)

    # -- session list ------------------------------------------------------- #
    def rescan_sessions(self):
        dirs = tmstore.sessions_dirs()
        if not dirs:
            self._sessions = []
            self._picker.blockSignals(True)
            self._picker.clear()
            self._picker.blockSignals(False)
            self._fit_picker_popup()
            self._sync_picker_tooltip()
            self._set_empty(
                "truck-mcp data root not found.\n\nSet the TRUCK_MCP_DATA "
                "environment variable, or place truck-mcp at "
                "D:/Projects/truck-mcp (or install the field kit), then Rescan "
                "(F5). This panel reads drive-log sessions; it starts nothing "
                "itself.")
            self._refresh_chrome()
            return
        try:
            sessions = tmstore.list_sessions()
        except OSError as exc:
            self._show_banner(f"Could not scan sessions: {exc}")
            return

        # Only touch the picker if the set of sessions actually changed, so a
        # selected live drive isn't torn down and rebuilt every 5 seconds.
        new_paths = [s["path"] for s in sessions]
        old_paths = [s["path"] for s in self._sessions]
        self._sessions = sessions
        if new_paths == old_paths and self._picker.count():
            return

        prev = self._picker.currentData()
        self._picker.blockSignals(True)
        self._picker.clear()
        for s in sessions:
            tag = tmstore.session_tag(s)
            label = s.get("label") or s.get("name")
            started = (s.get("started_utc") or "")[:19].replace("T", " ")
            err = "  [unreadable]" if s.get("error") else ""
            text = (f"{tag}{label}  ·  {started}  ·  "
                    f"{s.get('channel_count', 0)} ch{err}")
            self._picker.addItem(text, s["path"])
            self._picker.setItemData(self._picker.count() - 1, text,
                                     Qt.ToolTipRole)
        idx = self._picker.findData(prev) if prev else 0
        self._picker.setCurrentIndex(max(0, idx))
        self._picker.blockSignals(False)
        self._fit_picker_popup()
        self._sync_picker_tooltip()
        if not sessions:
            self._close_reader()
            self._clear_grid()
            self._set_empty(
                "No drive-log sessions found.\n\nRun a truck-mcp "
                "session_drive_log against the truck and it will appear here.")
            self._refresh_chrome()
            return
        # Bind whatever is now selected (blockSignals suppressed the auto-bind).
        self._on_pick(self._picker.currentIndex())

    def _maybe_rescan(self):
        self.rescan_sessions()

    def _fit_picker_popup(self):
        """Widen the picker's dropdown to its longest label. The closed box is
        width-budgeted (and may elide), so the popup is where the full session
        label must always be readable."""
        view = self._picker.view()
        if not self._picker.count():
            view.setMinimumWidth(0)
            return
        view.setMinimumWidth(view.sizeHintForColumn(0)
                             + view.frameWidth() * 2
                             + view.verticalScrollBar().sizeHint().width())

    def _sync_picker_tooltip(self, *_):
        self._picker.setToolTip(self._picker.currentText())

    def _set_empty(self, text: str):
        self._empty.setText(text)
        self._empty.setVisible(True)
        self._scroll.setVisible(False)

    def _clear_empty(self):
        self._empty.setVisible(False)
        self._scroll.setVisible(True)

    def _show_banner(self, text: Optional[str]):
        if not text:
            self._banner.hide()
            return
        self._banner.setText(text)
        self._banner.show()

    def banner_text(self) -> Optional[str]:
        """The danger banner's text, or None while it is hidden."""
        return None if self._banner.isHidden() else self._banner.text()

    # -- chrome: breadcrumb path, chips and note ------------------------------ #
    def _refresh_chrome(self):
        if self._reader is None:
            self.crumb.set_path(["Live Data"])
            self.crumb.set_chips([])
            self.crumb.set_note("No session bound")
            self.act_hold.setEnabled(False)
            self.act_fields.setEnabled(False)
            return
        meta = self._meta
        label = meta.get("label") or Path(self._reader_path or "").name
        self.crumb.set_path(["Live Data", label])
        chips: list[tuple[str, str]] = []
        if self._view_state:
            chips.append((self._view_state,
                          VIEW_STATE_CHIP.get(self._view_state, "neutral")))
        if self._held:
            chips.append((HELD_CAPTION, "warning"))
        prov = tmstore.session_provenance(meta)
        if prov == tmstore.PROVENANCE_SIMULATED:
            chips.append(("SIMULATED", "danger"))
        elif prov == tmstore.PROVENANCE_UNKNOWN:
            chips.append(("PROVENANCE UNKNOWN", "warning"))
        n = len(self._channel_meta)
        chips.append((f"{n} channel{'s' if n != 1 else ''}", "neutral"))
        chips.append((f"{len(self._chart._channels)} graphed", "neutral"))
        self.crumb.set_chips(chips)
        veh = meta.get("vin") or "VIN unknown"
        src = meta.get("source") or "live"
        stamp = f"as of {self._as_of}" if self._as_of else "not read yet"
        self.crumb.set_note(f"{veh}  ·  source={src}  ·  {stamp}")
        self.act_hold.setEnabled(not self._archived)
        self.act_fields.setEnabled(bool(self._channel_meta))

    # -- selection ---------------------------------------------------------- #
    def _on_pick(self, _idx: int):
        path = self._picker.currentData()
        if path and path == self._reader_path and self._tiles:
            return   # same session already bound — don't rebuild the grid
        self._close_reader()
        self._clear_grid()
        self._fail_streak = 0
        self._show_banner(None)
        if not path:
            self._refresh_chrome()
            return
        try:
            self._reader = TmSessionReader(path)
            self._reader_path = path
            meta = self._reader.metadata()
            chans = self._reader.channels()
        except (TmSessionError, sqlite3.Error, OSError) as exc:
            self._close_reader()
            self._show_banner(f"Could not open session: {exc}")
            self._set_empty("No session bound: the selected session could "
                            "not be opened. Pick another, or Rescan (F5).")
            self._refresh_chrome()
            return
        self._meta = meta
        self._channel_meta = {c["name"]: c for c in chans}
        self._chartable = chartable_names(self._reader, self._channel_meta)
        self._archived = bool(meta.get("ended_utc"))
        self._clear_empty()
        # The channel set is fixed at bind from the session's channels. If a
        # LIVE drive discovers a new channel mid-session it won't get a tile
        # until the session is reselected — an acceptable completeness gap
        # (truck-mcp fixes the channel set from the sweep preset at session
        # start); it is never a wrong value, only a missing one. What actually
        # shows, in BOTH views, is the shared layout's decision.
        self._build_tiles(self._layout.tile_names(self._channel_meta))
        self._bind_chart()
        self._tick()

    def _build_tiles(self, names):
        by_group: dict[str, list[str]] = {}
        for name in names:
            by_group.setdefault(channel_group(name), []).append(name)
        insert_at = 0
        for gname in GROUP_ORDER:
            members = sorted(by_group.get(gname, ()))
            if not members:
                continue
            group = TileGroup(gname)
            group.names = members
            group.set_collapsed(gname in self._collapsed)
            group.collapsed_changed.connect(self._on_group_collapsed)
            for name in members:
                meta = self._channel_meta.get(name, {})
                tile = ValueTile(name, meta.get("unit") or "")
                tile.setContextMenuPolicy(Qt.CustomContextMenu)
                tile.customContextMenuRequested.connect(
                    lambda pos, t=tile: self._tile_menu(t, pos))
                self._tiles[name] = tile
            self._groups[gname] = group
            self._groups_lay.insertWidget(insert_at, group)
            insert_at += 1
        self._apply_filter(self._filter.text(), reflow=False)
        self._reflow_tiles(force=True)
        # The viewport may not have its real width yet (first build happens
        # before the page is laid out) — reflow once the event loop settles.
        QTimer.singleShot(0, self._reflow_tiles)

    def _grid_cols(self) -> int:
        """Columns that fit the current width — the grid reflows on resize so
        a normal window shows every tile without scrolling."""
        w = self._scroll.viewport().width() - 4
        if w <= 0:
            return 5
        return max(3, w // TILE_PITCH)

    def resizeEvent(self, event):   # noqa: N802
        super().resizeEvent(event)
        self._reflow_tiles()

    def _reflow_tiles(self, force: bool = False):
        if not self._tiles:
            return
        cols = self._grid_cols()
        if cols == self._cols and not force:
            return
        self._cols = cols
        for group in self._groups.values():
            group.place([self._tiles[n] for n in group.names
                         if not self._tiles[n].isHidden()], cols)

    # -- tile filter (display only: never touches the layout model) --------- #
    def _tile_matches(self, name: str, needle: str) -> bool:
        return (not needle or needle in name.lower()
                or needle in channel_group(name).lower())

    def _apply_filter(self, text: str = "", reflow: bool = True):
        needle = (text or "").strip().lower()
        for group in self._groups.values():
            shown = 0
            for name in group.names:
                ok = self._tile_matches(name, needle)
                self._tiles[name].setHidden(not ok)
                shown += ok
            group.set_counts(shown, len(group.names))
            group.setHidden(shown == 0)
        if reflow:
            self._reflow_tiles(force=True)

    def visible_tile_names(self) -> list[str]:
        """Tiles the filter leaves on screen (collapse aside)."""
        return sorted(n for n, t in self._tiles.items() if not t.isHidden())

    # -- group collapse (persisted) ------------------------------------------ #
    def _load_collapsed(self) -> set[str]:
        raw = self._settings.value(GROUPS_KEY)
        try:
            vals = json.loads(raw) if isinstance(raw, str) else []
        except ValueError:
            return set()
        return {v for v in vals if isinstance(v, str)} \
            if isinstance(vals, list) else set()

    def _on_group_collapsed(self, name: str, collapsed: bool):
        if collapsed:
            self._collapsed.add(name)
        else:
            self._collapsed.discard(name)
        if self._collapsed:
            self._settings.setValue(GROUPS_KEY,
                                    json.dumps(sorted(self._collapsed)))
        else:
            self._settings.remove(GROUPS_KEY)

    def _clear_tiles(self):
        for tile in self._tiles.values():
            tile.setParent(None)
            tile.deleteLater()
        self._tiles.clear()
        for group in self._groups.values():
            self._groups_lay.removeWidget(group)
            group.setParent(None)
            group.deleteLater()
        self._groups.clear()
        self._cols = 0

    def _clear_grid(self):
        self._clear_tiles()
        self._channel_meta.clear()
        self._chartable = []
        self._meta = {}
        self._ranges = {}
        self._range_cursor = 0
        self._view_state = ""
        self._as_of = None
        self._archived = False
        self._set_hold(False)

    def _close_reader(self):
        self._chart.unbind()   # before close — the pane must not query a closed DB
        if self._reader is not None:
            try:
                self._reader.close()
            except sqlite3.Error:
                pass
        self._reader = None
        self._reader_path = None

    # -- window span (persisted for live sessions) --------------------------- #
    def _on_span_chosen(self, idx: int):
        """A USER choice of window (activated, not programmatic changes)."""
        self._settings.setValue(SPAN_KEY, self._chart.span_combo.itemText(idx))

    # -- hold ------------------------------------------------------------------ #
    def is_held(self) -> bool:
        return self._held

    def _set_hold(self, on: bool):
        """Freeze tiles, table and chart. The chart's sample cursor and the
        min / max cursor stay where they were, so release reads everything
        recorded meanwhile. Archived drives do not advance: Hold is off."""
        on = bool(on) and self._reader is not None and not self._archived
        if self.act_hold.isChecked() != on:
            self.act_hold.blockSignals(True)
            self.act_hold.setChecked(on)
            self.act_hold.blockSignals(False)
        if on == self._held:
            return
        self._held = on
        if on:
            for tile in self._tiles.values():
                tile.set_held(True)
            self._chart.chart.set_caption(HELD_CAPTION)
            self._refresh_chrome()
        else:
            self._tick()
            self._refresh_chrome()

    # -- field selection + grouping (ONE model drives BOTH views) ----------- #
    def _bind_chart(self):
        """(Re)bind chart + graphed-channels table from the shared layout."""
        self._chart.bind(self._reader, self._channel_meta, self._archived,
                         self._layout.chart_lanes(self._chartable))
        self._chart.table.set_droppable(self._chartable)
        self._chart.chart.empty_text = (
            "No chartable channels in this session." if not self._chartable
            else "No fields on the graph — drag a tile onto the table above.")
        # A live session opens on the user's last chosen window; an archived
        # drive keeps the pane's whole-drive default.
        saved = self._settings.value(SPAN_KEY)
        if not self._archived and isinstance(saved, str):
            self._chart.set_span_text(saved)

    def _relayout(self):
        """Persist the layout and re-derive BOTH views from it. The tiles and
        the chart never diverge because neither decides anything itself."""
        self._save_layout()
        if self._reader is None:
            return
        self._clear_tiles()
        self._build_tiles(self._layout.tile_names(self._channel_meta))
        span = self._chart.span_text()   # keep the user's window across rebind
        self._bind_chart()
        self._chart.set_span_text(span)
        # A layout edit is the user's own action: render the new tiles now,
        # even while held (a held page would otherwise show "not read").
        self._tick(ignore_hold=True)

    def _save_layout(self):
        if self._layout.is_default():
            self._settings.remove(chanlayout.SETTINGS_KEY)
        else:
            self._settings.setValue(chanlayout.SETTINGS_KEY,
                                    self._layout.to_json())

    def _set_visible(self, name: str, visible: bool):
        if visible:
            self._layout.show(name)
        else:
            self._layout.hide(name)
        self._relayout()

    def _show_all_fields(self):
        self._layout.hidden.clear()
        self._relayout()

    def _reset_layout(self):
        self._layout.reset()
        self._relayout()

    def _move_to_lane(self, name: str, lane_index):
        self._layout.move_to_lane(name, lane_index, self._chartable)
        self._relayout()

    def _on_channel_dropped(self, name: str, lane_index):
        """A tile was dropped on the graphed-channels table: add the field to
        the graph — the table already refused anything unchartable."""
        if name not in self._channel_meta or name not in self._chartable:
            return
        self._layout.show(name)   # a graphed field is by definition visible
        self._move_to_lane(name, lane_index)

    def _remove_from_graph(self, name: str):
        """Take the field off the graph; its tile stays on the palette."""
        self._layout.remove_from_chart(name, self._chartable)
        self._relayout()

    def _add_to_graph(self, name: str):
        """Menu twin of the tile drop: unhide, and (once the layout is
        customized) put the field in a new lane so it actually plots."""
        self._layout.show(name)
        if (name in self._chartable and self._layout.lanes is not None
                and not any(name in lane for lane in self._layout.lanes)):
            self._layout.move_to_lane(name, None, self._chartable)
        self._relayout()

    def _tile_only_reason(self, name: str) -> str:
        """Why a channel cannot chart — shown, never silent."""
        kind = (self._channel_meta.get(name) or {}).get("kind") or "numeric"
        if kind != "numeric":
            return f"{kind} channel — tile only"
        return "no numeric data yet — tile only"

    def _show_fields_menu(self):
        """Add/subtract fields: every session channel, checked = shown in
        BOTH views. Channels that cannot chart say so on the entry."""
        if not self._channel_meta:
            return
        m = QMenu(self)
        chartable = set(self._chartable)
        for name in sorted(self._channel_meta):
            label = name if name in chartable \
                else f"{name}   ({self._tile_only_reason(name)})"
            act = m.addAction(label)
            act.setCheckable(True)
            act.setChecked(not self._layout.is_hidden(name))
            # On restores the field to BOTH views (tile + graph for chartable
            # fields); off hides it from both.
            act.toggled.connect(
                lambda on, n=name: self._add_to_graph(n) if on
                else self._set_visible(n, False))
        m.addSeparator()
        m.addAction("Show all fields", self._show_all_fields)
        m.addAction("Reset layout to defaults", self._reset_layout)
        anchor = self._fields_btn or self.toolbar
        m.exec(anchor.mapToGlobal(anchor.rect().bottomLeft()))

    def _channel_actions(self, m: QMenu, name: str):
        """Hide / graph-membership / move-to-lane actions for one channel,
        shared by the tile, chart-lane and table context menus."""
        m.addAction(f"Hide {name}",
                    lambda n=name: self._set_visible(n, False))
        if name not in self._chartable:
            note = m.addAction(self._tile_only_reason(name))
            note.setEnabled(False)
            return
        if self._layout.is_graphed(name, self._chartable):
            m.addAction("Remove from graph (keep tile)",
                        lambda n=name: self._remove_from_graph(n))
        else:
            m.addAction("Add to graph",
                        lambda n=name: self._add_to_graph(n))
        sub = m.addMenu("Move to lane")
        lanes = self._layout.chart_lanes(self._chartable)
        for i, lane in enumerate(lanes):
            others = [n for n in lane if n != name]
            label = (f"Lane {i + 1}:  " + ", ".join(others[:4])
                     + ("…" if len(others) > 4 else "")) if others \
                else f"Lane {i + 1}  (only {name})"
            act = sub.addAction(label)
            act.setEnabled(bool(others) or name not in lane)
            act.triggered.connect(
                lambda _=False, n=name, idx=i: self._move_to_lane(n, idx))
        sub.addSeparator()
        sub.addAction("New lane",
                      lambda _=False, n=name: self._move_to_lane(n, None))

    def _tile_menu(self, tile: ValueTile, pos):
        m = QMenu(self)
        self._channel_actions(m, tile.channel)
        m.exec(tile.mapToGlobal(pos))

    def _table_menu(self, pos):
        name = self._chart.table.channel_at(self._chart.table.indexAt(pos).row())
        if name is None:
            return
        m = QMenu(self)
        self._channel_actions(m, name)
        m.exec(self._chart.table.viewport().mapToGlobal(pos))

    def _chart_menu(self, pos):
        if self._reader is None:
            return
        chart = self._chart.chart
        m = QMenu(self)
        li = chart.lane_at(pos.y())
        if li is not None and li < len(chart.lanes):
            for name in chart.lanes[li]:
                self._channel_actions(m.addMenu(name), name)
            m.addSeparator()
        # Add back anything not on the graph: hidden fields, and (once the
        # layout is customized) visible chartable fields not graphed.
        chartable = set(self._chartable)
        candidates = [
            n for n in sorted(self._channel_meta)
            if self._layout.is_hidden(n)
            or (n in chartable
                and not self._layout.is_graphed(n, self._chartable))]
        if candidates:
            add = m.addMenu("Add field…")
            for n in candidates:
                label = n if n in chartable \
                    else f"{n}   ({self._tile_only_reason(n)})"
                add.addAction(label,
                              lambda _=False, n=n: self._add_to_graph(n))
        if m.isEmpty():
            return
        m.exec(chart.mapToGlobal(pos))

    # -- poll --------------------------------------------------------------- #
    def tile_range(self, name: str) -> Optional[tuple[float, float]]:
        return self._ranges.get(name)

    def _tick(self, ignore_hold: bool = False):
        if self._reader is None or not self._tiles:
            return
        if self._held and not ignore_hold:
            # Frozen: read nothing but whether the drive ended — an ended
            # drive cannot be held (it no longer advances), so it releases.
            try:
                ended = self._reader.ended()
            except (sqlite3.Error, TmSessionError, OSError):
                return
            if not ended:
                return
            self._archived = True
            self._set_hold(False)   # re-enters _tick unheld
            return
        try:
            latest = self._reader.latest(list(self._tiles))
            newest_ts = self._reader.latest_ts()
            # Re-check each tick: a drive that ENDS while still bound must stop
            # rendering as live immediately, not stay green until it goes stale.
            if not self._archived and self._reader.ended():
                self._archived = True
            ranges, range_cursor = self._reader.range_after(
                list(self._channel_meta), self._range_cursor)
        except (sqlite3.Error, TmSessionError, OSError) as exc:
            self._fail_streak += 1
            if self._fail_streak >= 2:
                # Not a momentary checkpoint blip — surface it. Never keep
                # showing stale values that still claim to be live.
                for tile in self._tiles.values():
                    tile.set_failed(f"read failed: {exc}")
                self._chart.set_failed(str(exc))
                self._view_state = "read failed"
                self._show_banner(
                    f"Read failed ({self._fail_streak}×): {exc}\nThe tiles "
                    f"and chart show the failure, not the last values: "
                    f"nothing below is live until a read succeeds.")
                self._refresh_chrome()
            return
        self._fail_streak = 0
        self._show_banner(None)
        for name, (lo, hi) in ranges.items():
            cur = self._ranges.get(name)
            self._ranges[name] = (lo, hi) if cur is None else (
                min(cur[0], lo), max(cur[1], hi))
        self._range_cursor = range_cursor

        # Is a LIVE session actually advancing? If its newest committed sample is
        # old in wall-clock, the logger stopped — nothing may render as live.
        session_stale = False
        if not self._archived:
            age = tmstore.sample_age_s({"ts_utc": newest_ts}) if newest_ts else None
            session_stale = age is not None and age > tmstore.STALE_SESSION_LIMIT_S

        for name, tile in self._tiles.items():
            tile.update_from(latest.get(name), self._channel_meta.get(name),
                             archived=self._archived, session_stale=session_stale)
            tile.set_range(self._ranges.get(name))
        self._chart.tick(archived=self._archived, session_stale=session_stale)
        self._chart.update_table(latest, self._channel_meta,
                                 archived=self._archived,
                                 session_stale=session_stale)
        self._as_of = datetime.now().strftime("%H:%M:%S")
        self._view_state = ("archived" if self._archived
                            else "not advancing" if session_stale else "live")
        if self._archived and self._held:
            self._set_hold(False)
        elif self._held:
            # A layout edit rendered fresh values while held: re-freeze them.
            for tile in self._tiles.values():
                tile.set_held(True)
            self._chart.chart.set_caption(HELD_CAPTION)
        self._refresh_chrome()


class ActiveTestsPage(QWidget):
    """DISPLAY-ONLY vehicle-control state. Commands nothing; enables no control.

    Reads truck-mcp's control journal across every root and reports one honest
    verdict: clean, outstanding (an actuator may be held), or unknown (the
    journal could not be located or trusted — a warning, never a green pass).
    """

    def __init__(self):
        super().__init__()
        self._active = False
        root = QVBoxLayout(self)

        banner = QLabel(
            "No executable vehicle controls are known for this truck.\n"
            "Enabling any active test requires a hardware CPID probe session "
            "(operator + truck + console) and a per-exposure adversarial review "
            "before the safety gate is flipped. Until then this page is "
            "display-only: it commands nothing and holds no lease.")
        banner.setWordWrap(True)
        banner.setStyleSheet(theme.livedata_banner_qss())
        root.addWidget(banner)

        row = QHBoxLayout()
        self._state_lbl = QLabel("")
        self._state_lbl.setWordWrap(True)
        self._state_lbl.setStyleSheet("font-size:13px; padding:6px;")
        row.addWidget(self._state_lbl, 1)
        col = QVBoxLayout()
        self._stamp = QLabel("")
        self._stamp.setStyleSheet(
            theme.livedata_text_qss("faint-text", "font-size:10px;"))
        self._refresh_btn = QPushButton("↻ Refresh")
        self._refresh_btn.clicked.connect(self.refresh)
        col.addWidget(self._refresh_btn)
        col.addWidget(self._stamp)
        row.addLayout(col, 0)
        root.addLayout(row)

        root.addWidget(QLabel("Control journal (read-only audit tail):"))
        self._table = QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(["when", "event", "module", "detail"])
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        root.addWidget(self._table, 1)

        self._poll = QTimer(self)
        self._poll.timeout.connect(self.refresh)
        self.refresh()

    def showEvent(self, event):   # noqa: N802
        super().showEvent(event)
        if not self._active:
            self._active = True
            self.refresh()
            self._poll.start(CONTROL_POLL_MS)

    def hideEvent(self, event):   # noqa: N802
        super().hideEvent(event)
        self._active = False
        self._poll.stop()

    def shutdown(self):
        self._active = False
        self._poll.stop()

    def closeEvent(self, event):   # noqa: N802
        self.shutdown()
        super().closeEvent(event)

    def refresh(self):
        try:
            state = ctljournal.control_state()
        except Exception as exc:   # the safety page must never fail to render
            self._state_lbl.setText(f"⚠  Could not read control journal: {exc}")
            self._warn_style()
            self._table.setRowCount(0)
            self._stamp.setText("read error")
            return

        if state.verdict == "outstanding":
            lines = "\n".join(
                f"  • module {o.get('module')} CPID {o.get('cpid')} — "
                f"release bytes {o.get('release', '?')}  [{_short(o.get('_journal'))}]"
                for o in state.outstanding)
            self._state_lbl.setText(
                "⚠  CONTROL LEFT ACTIVE. A control session ended without a "
                "recorded release; the truck may be holding an actuator:\n"
                + lines +
                "\n\nRun truck-mcp recovery (it replays the exact release bytes "
                "from the journal). OpenOBD will not send it.")
            self._danger_style()
        elif state.verdict == "unknown":
            self._state_lbl.setText("⚠  CONTROL STATE UNKNOWN. " + state.detail)
            self._warn_style()
        else:  # clean
            self._state_lbl.setText(
                "✓  No unreleased activation is recorded across "
                f"{len(state.reads)} journal(s), {state.total_entries} record(s). "
                "No control is commanded on this truck.")
            self._state_lbl.setStyleSheet(theme.livedata_text_qss(
                "clean-text", "font-size:13px; padding:6px;"))

        self._stamp.setText("as of " + datetime.now().strftime("%H:%M:%S"))
        self._fill_table(state)

    def _fill_table(self, state):
        recent = []
        for r in state.reads:
            recent.extend(r.entries)
        recent = list(reversed(recent))[:100]
        self._table.setRowCount(len(recent))
        for r, e in enumerate(recent):
            when = _fmt_epoch(e.get("at"))
            detail = ", ".join(
                f"{k}={v}" for k, v in e.items()
                if k not in ("at", "event", "module", "_journal"))
            for c, text in enumerate((when, str(e.get("event", "")),
                                      str(e.get("module", "")), detail)):
                item = QTableWidgetItem(text)
                ev = str(e.get("event", ""))
                if ev == "activate" or ev.startswith("fire_refused"):
                    item.setForeground(
                        theme.livedata_qcolor("journal-activate"))
                self._table.setItem(r, c, item)

    def _danger_style(self):
        self._state_lbl.setStyleSheet(theme.livedata_alert_qss("danger"))

    def _warn_style(self):
        self._state_lbl.setStyleSheet(theme.livedata_alert_qss("warn"))


def _short(path) -> str:
    if not path:
        return "?"
    try:
        return "…/" + "/".join(str(path).replace("\\", "/").split("/")[-2:])
    except Exception:
        return str(path)


def _fmt_epoch(at) -> str:
    try:
        return datetime.fromtimestamp(float(at)).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return str(at) if at is not None else ""
