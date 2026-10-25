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

import sqlite3
from datetime import datetime

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QDrag, QFont
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFrame, QGridLayout, QHBoxLayout, QHeaderView,
    QLabel, QMenu, QPushButton, QScrollArea, QSizePolicy, QSplitter,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from . import chanlayout, ctljournal, tmstore
from .appsettings import app_settings
from .chanlayout import ChannelLayout
from .stripchart import ChartPane, channel_mime, chartable_names
from .tmstore import TmSessionReader, TmSessionError

# The states, and the ONLY colors each may take. Each means exactly one thing.
STATE_COLORS = {
    "fresh":    ("#2E3B31", "#5FD08A", "#DEE0E4"),   # measured now — live
    "stale":    ("#3A3629", "#D6A93A", "#D6C9A0"),   # live view, but not advancing
    "archive":  ("#2B3138", "#5A7EC3", "#AEBBD0"),   # finished drive (not live)
    "carried":  ("#3A3629", "#D6A93A", "#D6C9A0"),   # measured earlier, carried
    "error":    ("#3B2B2B", "#CD3C32", "#E7A9A4"),   # module returned an error
    "unavail":  ("#2B3138", "#5A7EC3", "#9FB0C8"),   # answered with nothing
    "notread":  ("#25272B", "#5F646E", "#7B8088"),   # nobody ever asked
    "badvalue": ("#3B2B33", "#C0568F", "#E3A9C6"),   # tool-side format fault
    "failed":   ("#3B2B2B", "#CD3C32", "#E7A9A4"),   # the read itself failed
}
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


class ValueTile(QFrame):
    """One channel's latest value, colored by display state. Built once per
    channel then updated in place — never rebuilt, so the grid stays stable."""

    def __init__(self, name: str, unit: str = ""):
        super().__init__()
        self.channel = name
        self._unit = unit
        self._press_pos = None
        self.setFrameShape(QFrame.StyledPanel)
        self.setMinimumWidth(128)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.setCursor(Qt.OpenHandCursor)   # tiles drag onto the graph table
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 4, 8, 4)
        lay.setSpacing(0)
        self._label = QLabel(name)
        self._label.setStyleSheet("color:#9DA3AD; font-size:10px;")
        self._value = QLabel("— not read")
        vf = QFont(); vf.setPointSize(13); vf.setBold(True)
        self._value.setFont(vf)
        self._state = QLabel("")
        self._state.setStyleSheet("color:#7B8088; font-size:9px;")
        lay.addWidget(self._label)
        lay.addWidget(self._value)
        lay.addWidget(self._state)
        self._apply("notread")

    def update_from(self, sample, channel_meta, *, archived: bool,
                    session_stale: bool):
        try:
            ds = tmstore.display_state(sample, channel_meta, archived=archived,
                                       session_stale=session_stale)
        except Exception as exc:   # contract violation — surface, never draw a lie
            self._value.setText("BAD")
            self._state.setText(str(exc)[:44])
            self._apply("badvalue")
            return
        unit = ds.unit or (self._unit if ds.state in ("fresh", "stale", "archive",
                                                       "carried") else "")
        show_unit = unit and ds.state in ("fresh", "stale", "archive", "carried")
        self._value.setText(ds.text + (f" {unit}" if show_unit else ""))
        if ds.state == "carried" and ds.age is not None:
            self._state.setText(f"carried · {ds.age:.1f}s old")
        elif ds.state == "carried":
            self._state.setText("carried · age unknown")
        else:
            self._state.setText(STATE_CAPTION.get(ds.state, ds.state))
        self._apply(ds.state)

    def set_failed(self, message: str):
        self._value.setText("—")
        self._state.setText(message[:44])
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
        bg, accent, text = STATE_COLORS.get(state, STATE_COLORS["notread"])
        self.setStyleSheet(
            f"ValueTile {{ background:{bg}; border:1px solid {accent}; "
            f"border-radius:6px; }}")
        self._value.setStyleSheet(f"color:{text};")


class LiveDataPage(QWidget):
    """Session picker + live tile grid over truck-mcp's drive-log store."""

    def __init__(self):
        super().__init__()
        self._reader = None
        self._reader_path = None
        self._tiles: dict[str, ValueTile] = {}
        self._channel_meta: dict[str, dict] = {}
        self._chartable: list[str] = []
        self._sessions: list[dict] = []
        self._archived = False
        self._fail_streak = 0
        self._active = False
        self._status_base = ""

        # The ONE selection+grouping model both views derive from. Global, not
        # per-session: truck-mcp store channel names are stable across drives,
        # so a grouping tuned for troubleshooting carries to the next session.
        self._settings = app_settings()
        self._layout = ChannelLayout.from_json(
            self._settings.value(chanlayout.SETTINGS_KEY))

        root = QVBoxLayout(self)

        # The bar must never set the page's minimum width: QTabWidget's minimum
        # is the max over its pages, so a wide bar here pins the WHOLE window
        # (it was 1522 px). The picker therefore sizes from a fixed character
        # budget instead of its longest session label (the default
        # AdjustToContentsOnFirstShow), grows into spare room, elides when
        # narrow and carries the full label as a tooltip; its popup is widened
        # to the full labels so nothing is hidden. The status line wraps rather
        # than forcing its whole sentence onto one row.
        bar = QHBoxLayout()
        bar.addWidget(QLabel("Session:"))
        self._picker = QComboBox()
        self._picker.setSizeAdjustPolicy(
            QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self._picker.setMinimumContentsLength(PICKER_MIN_CHARS)
        self._picker.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._picker.view().setTextElideMode(Qt.ElideNone)
        self._picker.currentIndexChanged.connect(self._sync_picker_tooltip)
        self._picker.currentIndexChanged.connect(self._on_pick)
        bar.addWidget(self._picker, 1)
        self._refresh_btn = QPushButton("↻ Rescan")
        self._refresh_btn.clicked.connect(self.rescan_sessions)
        bar.addWidget(self._refresh_btn)
        self._fields_btn = QPushButton("Fields…")
        self._fields_btn.setToolTip(
            "Add or subtract fields — the tiles and the chart always show "
            "the same selection.")
        self._fields_btn.clicked.connect(self._show_fields_menu)
        bar.addWidget(self._fields_btn)
        self._status = QLabel("")
        self._status.setStyleSheet("color:#9DA3AD;")
        self._status.setWordWrap(True)
        self._status.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._status.setSizePolicy(QSizePolicy.Expanding,
                                   QSizePolicy.Preferred)
        bar.addWidget(self._status, 1)
        root.addLayout(bar)

        # HP-Tuners-style combined view: the live tiles and the Chart vs. Time
        # strip chart share one page (no sub-tabs) so the numbers can be read
        # against the traces while focusing on a driving condition. Tiles are
        # compact with width-adaptive columns so the grid fits without
        # scrolling at normal window sizes; the splitter tunes the ratio.
        self._grid_host = QWidget()
        self._grid = QGridLayout(self._grid_host)
        self._grid.setContentsMargins(2, 2, 2, 2)
        self._grid.setSpacing(6)
        self._cols = 5
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setWidget(self._grid_host)

        self._empty = QLabel()
        self._empty.setAlignment(Qt.AlignCenter)
        self._empty.setWordWrap(True)
        self._empty.setStyleSheet("color:#7B8088; font-size:13px; padding:24px;")

        tiles_host = QWidget()
        tiles_lay = QVBoxLayout(tiles_host)
        tiles_lay.setContentsMargins(0, 0, 0, 0)
        tiles_lay.addWidget(self._scroll, 1)
        tiles_lay.addWidget(self._empty)

        self._chart = ChartPane()
        # Right-click on a lane: move/hide its channels, or add a field back.
        self._chart.chart.setContextMenuPolicy(Qt.CustomContextMenu)
        self._chart.chart.customContextMenuRequested.connect(self._chart_menu)
        # Dropping a tile on the graphed-channels table adds that field to
        # the graph (into the row's lane, or a new lane below the rows).
        self._chart.table.channel_dropped.connect(self._on_channel_dropped)
        self._chart.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self._chart.table.customContextMenuRequested.connect(self._table_menu)
        self._split = QSplitter(Qt.Vertical)
        self._split.addWidget(tiles_host)
        self._split.addWidget(self._chart)
        self._split.setStretchFactor(0, 1)
        self._split.setStretchFactor(1, 2)
        self._split.setSizes([250, 430])
        root.addWidget(self._split, 1)

        self._poll = QTimer(self)
        self._poll.timeout.connect(self._tick)
        self._rescan = QTimer(self)
        self._rescan.timeout.connect(self._maybe_rescan)

        self.rescan_sessions()

    # -- lifecycle: only run timers while the tab is actually visible -------- #
    def showEvent(self, event):   # noqa: N802
        super().showEvent(event)
        if not self._active:
            self._active = True
            self.rescan_sessions()
            self._poll.start(POLL_MS)
            self._rescan.start(SESSION_RESCAN_MS)
        self._reflow_tiles()   # width is real once the page is shown

    def hideEvent(self, event):   # noqa: N802
        super().hideEvent(event)
        self._active = False
        self._poll.stop()
        self._rescan.stop()

    def shutdown(self):
        self._active = False
        self._poll.stop()
        self._rescan.stop()
        self._close_reader()

    def closeEvent(self, event):   # noqa: N802
        self.shutdown()
        super().closeEvent(event)

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
                "D:/Projects/truck-mcp (or install the field kit), then Rescan. "
                "This panel reads drive-log sessions; it starts nothing itself.")
            return
        try:
            sessions = tmstore.list_sessions()
        except OSError as exc:
            self._set_empty(f"Could not scan sessions:\n{exc}")
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
            self._set_empty(
                "No drive-log sessions found.\n\nRun a truck-mcp "
                "session_drive_log against the truck and it will appear here.")
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
        self._grid_host.setVisible(False)

    def _clear_empty(self):
        self._empty.setVisible(False)
        self._grid_host.setVisible(True)

    # -- selection ---------------------------------------------------------- #
    def _on_pick(self, _idx: int):
        path = self._picker.currentData()
        if path and path == self._reader_path and self._tiles:
            return   # same session already bound — don't rebuild the grid
        self._close_reader()
        self._clear_grid()
        self._fail_streak = 0
        if not path:
            return
        try:
            self._reader = TmSessionReader(path)
            self._reader_path = path
            meta = self._reader.metadata()
            chans = self._reader.channels()
        except (TmSessionError, sqlite3.Error, OSError) as exc:
            self._set_empty(f"Could not open session:\n{exc}")
            return
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
        veh = meta.get("vin") or meta.get("label") or "session"
        src = meta.get("source") or "live"
        self._status_base = f"{veh}  ·  source={src}"
        if tmstore.session_simulated(meta):
            self._status_base = ("SIMULATED demo data, not the truck  ·  "
                                 + self._status_base)
        self._status.setText(self._status_base)
        self._tick()

    def _build_tiles(self, names):
        self._cols = self._grid_cols()
        for i, name in enumerate(names):
            meta = self._channel_meta.get(name, {})
            tile = ValueTile(name, meta.get("unit") or "")
            tile.setContextMenuPolicy(Qt.CustomContextMenu)
            tile.customContextMenuRequested.connect(
                lambda pos, t=tile: self._tile_menu(t, pos))
            self._tiles[name] = tile
            self._grid.addWidget(tile, i // self._cols, i % self._cols)
        self._pin_rows_top(len(names))
        # The viewport may not have its real width yet (first build happens
        # before the page is laid out) — reflow once the event loop settles.
        QTimer.singleShot(0, self._reflow_tiles)

    def _pin_rows_top(self, n_tiles: int):
        """Keep tile rows packed at the top: zero the row stretches and put all
        the stretch on one empty row below the last tile row."""
        last = (max(0, n_tiles - 1)) // self._cols
        for r in range(self._grid.rowCount() + 1):
            self._grid.setRowStretch(r, 0)
        self._grid.setRowStretch(last + 1, 1)

    def _grid_cols(self) -> int:
        """Columns that fit the current width — the grid reflows on resize so
        a normal window shows every tile without scrolling."""
        w = self._scroll.viewport().width()
        if w <= 0:
            return 5
        return max(3, w // 136)

    def resizeEvent(self, event):   # noqa: N802
        super().resizeEvent(event)
        self._reflow_tiles()

    def _reflow_tiles(self):
        if not self._tiles:
            return
        cols = self._grid_cols()
        if cols == self._cols:
            return
        self._cols = cols
        names = sorted(self._tiles)
        while self._grid.count():
            self._grid.takeAt(0)
        for i, name in enumerate(names):
            self._grid.addWidget(self._tiles[name], i // cols, i % cols)
        self._pin_rows_top(len(names))

    def _clear_tiles(self):
        for tile in self._tiles.values():
            tile.setParent(None)
            tile.deleteLater()
        self._tiles.clear()

    def _clear_grid(self):
        self._clear_tiles()
        self._channel_meta.clear()
        self._chartable = []

    def _close_reader(self):
        self._chart.unbind()   # before close — the pane must not query a closed DB
        if self._reader is not None:
            try:
                self._reader.close()
            except sqlite3.Error:
                pass
        self._reader = None
        self._reader_path = None

    # -- field selection + grouping (ONE model drives BOTH views) ----------- #
    def _bind_chart(self):
        """(Re)bind chart + graphed-channels table from the shared layout."""
        self._chart.bind(self._reader, self._channel_meta, self._archived,
                         self._layout.chart_lanes(self._chartable))
        self._chart.table.set_droppable(self._chartable)
        self._chart.chart.empty_text = (
            "No chartable channels in this session." if not self._chartable
            else "No fields on the graph — drag a tile onto the table above.")

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
        self._tick()

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
        m.exec(self._fields_btn.mapToGlobal(
            self._fields_btn.rect().bottomLeft()))

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
    def _tick(self):
        if self._reader is None or not self._tiles:
            return
        try:
            latest = self._reader.latest(list(self._tiles))
            newest_ts = self._reader.latest_ts()
            # Re-check each tick: a drive that ENDS while still bound must stop
            # rendering as live immediately, not stay green until it goes stale.
            if not self._archived and self._reader.ended():
                self._archived = True
        except (sqlite3.Error, TmSessionError, OSError) as exc:
            self._fail_streak += 1
            if self._fail_streak >= 2:
                # Not a momentary checkpoint blip — surface it. Never keep
                # showing stale values that still claim to be live.
                for tile in self._tiles.values():
                    tile.set_failed(f"read failed: {exc}")
                self._chart.set_failed(str(exc))
                self._status.setText(f"{self._status_base}  ·  ⚠ read failed "
                                     f"({self._fail_streak}×)")
            return
        self._fail_streak = 0

        # Is a LIVE session actually advancing? If its newest committed sample is
        # old in wall-clock, the logger stopped — nothing may render as live.
        session_stale = False
        if not self._archived:
            age = tmstore.sample_age_s({"ts_utc": newest_ts}) if newest_ts else None
            session_stale = age is not None and age > tmstore.STALE_SESSION_LIMIT_S

        for name, tile in self._tiles.items():
            tile.update_from(latest.get(name), self._channel_meta.get(name),
                             archived=self._archived, session_stale=session_stale)
        self._chart.tick(archived=self._archived, session_stale=session_stale)
        self._chart.update_table(latest, self._channel_meta,
                                 archived=self._archived,
                                 session_stale=session_stale)

        stamp = datetime.now().strftime("%H:%M:%S")
        note = ("archived" if self._archived
                else "not advancing" if session_stale else "live")
        # Rebuild from the stored clean base, never from the current label (which
        # may carry a stale warning prefix after a recovered read failure).
        self._status.setText(f"{self._status_base}  ·  as of {stamp} ({note})")


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
        banner.setStyleSheet(
            "background:#2B3138; border:1px solid #5A7EC3; border-radius:6px; "
            "color:#C7D2E4; padding:12px; font-size:12px;")
        root.addWidget(banner)

        row = QHBoxLayout()
        self._state_lbl = QLabel("")
        self._state_lbl.setWordWrap(True)
        self._state_lbl.setStyleSheet("font-size:13px; padding:6px;")
        row.addWidget(self._state_lbl, 1)
        col = QVBoxLayout()
        self._stamp = QLabel("")
        self._stamp.setStyleSheet("color:#7B8088; font-size:10px;")
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
            self._state_lbl.setStyleSheet(
                "color:#8FD0A6; font-size:13px; padding:6px;")

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
                    item.setForeground(QColor("#D6A93A"))
                self._table.setItem(r, c, item)

    def _danger_style(self):
        self._state_lbl.setStyleSheet(
            "color:#E7A9A4; background:#3B2B2B; border:1px solid #CD3C32; "
            "border-radius:6px; font-size:13px; padding:10px;")

    def _warn_style(self):
        self._state_lbl.setStyleSheet(
            "color:#E5C77A; background:#3A3629; border:1px solid #D6A93A; "
            "border-radius:6px; font-size:13px; padding:10px;")


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
