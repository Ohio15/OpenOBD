"""
diagui — the Diagnostics workspace: module map + codes & readiness.

Troubleshooting lives here, segregated from the tuning workflow:
  Module Map        — the vehicle network drawn as tool -> DLC -> bus rails ->
                      modules, with the comms pipeline verdict from
                      vehnet.localize() painted onto nodes and segments so a
                      failure shows WHERE it happened. Hover a module for its
                      address; click it for the module card.
  Codes & Readiness — stored/pending/permanent DTCs grouped by kind with the
                      reporting module, clear codes (confirmed — it also
                      resets readiness), readiness monitor tiles.

Both sub-tabs carry the Tuning workspace's chrome (tuningui.Breadcrumb +
the 36 px toolbar, theme.diag_qss): scan-tool cues from GM GDS2 / Tech2
(module list with status), HP Tuners VCM Scanner and Autel / Snap-on
(DTC rows with code, description, source module and status; readiness grid;
MIL banner).

"Unknown is never clean": a kind that was not examined, a module whose reply
was incomplete, and a MIL that could not be read each get their own visible
row / chip / banner — never an empty list or a clean-looking state.

All hardware IO runs through GtJob on a worker thread: it borrows the
dashboard's live GT connection (polling paused via run_exclusive) or opens a
temporary one, so the GUI never blocks on serial traffic. One job runs at a
time; every job-starting action is disabled until it finishes.
"""
from __future__ import annotations

import threading
import time
from typing import Optional

from PySide6.QtCore import (
    QEvent, QModelIndex, QObject, QPointF, QRectF, QSize,
    QSortFilterProxyModel, Qt, Signal,
)
from PySide6.QtGui import (
    QAction, QColor, QFont, QKeySequence, QPainter, QPainterPath, QPen,
    QPixmap, QShortcut, QStandardItem, QStandardItemModel,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QFrame, QGridLayout, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMessageBox, QScrollArea, QSplitter,
    QTabWidget, QToolTip, QTreeView, QVBoxLayout, QWidget,
)

from . import theme, vehnet
from .gt import DTC_DESCRIPTIONS
from .tuningui import (
    CHIP_KIND_ROLE, Breadcrumb, ChipDelegate, CollapsiblePanel,
    empty_state_label, make_toolbar, repolish, set_chip,
)
from .vehnet import HS, SW, ScanResult, SegStatus, Status, Verdict


STATUS_COLORS = {
    Status.OK: theme.diag_qcolor("status-ok"),
    Status.SILENT: theme.diag_qcolor("status-silent"),
    Status.UNREACHABLE: theme.diag_qcolor("status-unreachable"),
    # grey family: no finding either way. NOT_EXAMINED is a cooler grey so a
    # refused header reads differently from "never scanned" at a glance.
    Status.NOT_EXAMINED: theme.diag_qcolor("status-not-examined"),
    Status.UNKNOWN: theme.diag_qcolor("status-unknown"),
}
SEG_COLORS = {
    SegStatus.OK: theme.diag_qcolor("status-ok"),
    SegStatus.FAILED: theme.diag_qcolor("status-silent"),
    SegStatus.UNREACHABLE: theme.diag_qcolor("status-unreachable"),
    SegStatus.UNKNOWN: theme.diag_qcolor("status-unknown"),
}
#: Module status -> chip kind (module card, legend order).
STATUS_CHIP = {
    Status.OK: "ok",
    Status.SILENT: "danger",
    Status.UNREACHABLE: "warning",
    Status.NOT_EXAMINED: "muted",
    Status.UNKNOWN: "muted",
}
#: The legend row under the Module Map header, in this order.
LEGEND = ((Status.OK, "responding"),
          (Status.SILENT, "no response"),
          (Status.UNREACHABLE, "unreachable via this path"),
          (Status.NOT_EXAMINED, "not examined"),
          (Status.UNKNOWN, "not scanned"))
#: vehnet failure points -> what the chip says.
SEGMENT_LABELS = {
    "pc_gt": "USB link (PC to GT)",
    "pc_gt:binary_mode": "GT in binary mode",
    "gt_dlc": "DLC power",
    "dlc_hs": HS,
    "dlc_sw": SW,
}
#: DTC kinds in display order: (label, read_dtcs key, chip kind).
DTC_KINDS = (("Stored", "stored", "danger"),
             ("Pending", "pending", "warning"),
             ("Permanent", "permanent", "muted"))

#: QSettings keys (all under "diag/").
MAP_SPLIT_KEY = "diag/map_split"
CODES_SPLIT_KEY = "diag/codes_split"
NOTES_OPEN_KEY = "diag/scan_notes_open"

#: Item roles on the DTC model.
ROW_KIND_ROLE = Qt.UserRole + 41     # group | code | not_examined |
                                     # incomplete | summary
DTC_KIND_ROLE = Qt.UserRole + 42     # Stored | Pending | Permanent | ""

NO_DESCRIPTION = "No local description"
BINARY_MODE_ADVICE = ("OBDX GT is stuck in binary (J2534) mode — unplug it "
                      "from USB and the OBD port for 10 s, then rescan.")
MAP_EMPTY = ("Not scanned yet. Scan Modules (F5) checks the USB link, DLC "
             "power, the bus and every module, using the live GT link or "
             "opening its own.")
MAP_PICK = "Click a module on the map for its status, bus and address."
READY_EMPTY = "Readiness not read yet. Read Codes (F5) reads it with the codes."


def failure_label(point: Optional[str]) -> str:
    """vehnet failure point -> human label ('module:ebcm' -> 'EBCM (ABS)')."""
    if not point:
        return ""
    if point.startswith("module:"):
        m = vehnet._module(point.split(":", 1)[1])
        return m.name if m else point
    return SEGMENT_LABELS.get(point, point)


def module_name(resp_id: str) -> str:
    """A responder CAN id -> its module's name (vehnet.MODULES resp_id, or
    the GMLAN-routed report ids); an unidentified id stays the raw id."""
    rid = (resp_id or "").upper()
    for m in vehnet.MODULES:
        if m.resp_id and m.resp_id.upper() == rid:
            return m.name
    for key, ids in vehnet.GMLAN_ROUTED.items():
        if rid in (i.upper() for i in ids):
            m = vehnet._module(key)
            if m:
                return m.name
    return resp_id


class GtJob(QObject):
    """Run fn(gt) off the GUI thread against whatever GT link exists."""
    finished = Signal(object, object)   # result, error string or None

    BUSY_ERROR = "another GT job is still running"

    def __init__(self, dashboard, parent=None):
        super().__init__(parent)
        self.dashboard = dashboard
        # One job at a time is enforced HERE, not only by the caller: two
        # workers would interleave AT commands on the one serial link.
        self._busy = threading.Lock()

    def run(self, fn):
        if not self._busy.acquire(blocking=False):
            self.finished.emit(None, self.BUSY_ERROR)
            return

        def work():
            try:
                result, error = self._call(fn), None
            except Exception as e:
                result, error = None, str(e)
            except BaseException:
                self._busy.release()
                raise
            # release BEFORE emitting: the queued result handler may start
            # the next job and must not find the lock still held
            self._busy.release()
            try:
                self.finished.emit(result, error)
            except RuntimeError:   # receiver already destroyed at shutdown
                pass
        try:
            threading.Thread(target=work, name="gt-diag", daemon=True).start()
        except BaseException:
            self._busy.release()
            raise

    def _call(self, fn):
        from .transport import GtDataSource
        src = self.dashboard.source
        if isinstance(src, GtDataSource) and src._thread is not None:
            return src.run_exclusive(fn)
        from . import gt as _gt
        g = _gt.ObdxGt()
        g.open()
        try:
            return fn(g)
        finally:
            g.close()


def clear_and_reread(gt) -> dict:
    """The Clear Codes job. Each step is captured on its own: once the ECM
    has accepted a clear, a failed re-read must not hide that outcome nor
    leave the pre-clear rows standing as current. A step that raised comes
    back as {"error": why}. If the clear itself raised, nothing after it is
    attempted (whether the ECM acted is unknown) and the error propagates."""
    out = {"clear": gt.clear_dtcs()}
    for key, step in (("dtcs", gt.read_dtcs), ("ready", gt.readiness)):
        try:
            out[key] = step()
        except Exception as e:   # noqa: BLE001 — reported per step
            out[key] = {"error": str(e) or type(e).__name__}
    return out


# --------------------------------------------------------------------------- #
# Module map
# --------------------------------------------------------------------------- #
class ModuleMapView(QWidget):
    """Paints the network: PC -> GT -> DLC, then the HS and SW bus rails with
    their modules. Node/segment colors come from the scan verdict; the first
    failed segment gets a warning marker. Hover a node for its address,
    click it for the module card. The status legend is the page's legend
    row, not painted here."""

    module_selected = Signal(str)

    def __init__(self):
        super().__init__()
        self.verdict: Optional[Verdict] = None
        self.selected: Optional[str] = None
        self.setMinimumHeight(300)
        self.setMouseTracking(True)

    def set_verdict(self, v: Optional[Verdict]):
        self.verdict = v
        self.update()

    def module_status(self, key) -> Status:
        return self._mod(key)

    # -- hit testing / tooltips ---------------------------------------------
    # Geometry is a pure function of the widget size, shared by painting and
    # hit-testing, so a click or tooltip right after a resize never tests
    # against the previous paint's layout.
    NODE_H = 40.0

    @staticmethod
    def _chain_layout(w: float, h: float) -> tuple[QRectF, QRectF, QRectF]:
        """PC, GT and DLC node rects."""
        nh = ModuleMapView.NODE_H
        chain_y = h * 0.5 - nh / 2
        nw = min(110.0, w * 0.13)
        pc_r = QRectF(12, chain_y, nw, nh)
        gt_r = QRectF(pc_r.right() + 46, chain_y, nw, nh)
        dlc_r = QRectF(gt_r.right() + 46, chain_y, nw, nh)
        return pc_r, gt_r, dlc_r

    @staticmethod
    def _rails(w: float, h: float) -> tuple[float, float, float, float]:
        """rail_x0, rail_x1, hs_y, sw_y."""
        dlc_r = ModuleMapView._chain_layout(w, h)[2]
        return dlc_r.right() + 40, w - 16, h * 0.22, h * 0.78

    @staticmethod
    def _layout_nodes(w: float, h: float) -> dict[str, QRectF]:
        """Module key -> node rect for a w x h canvas (modules hang off
        their bus rail: HS below it, SW above it)."""
        nh = ModuleMapView.NODE_H
        rail_x0, rail_x1, hs_y, sw_y = ModuleMapView._rails(w, h)
        span = rail_x1 - rail_x0
        out: dict[str, QRectF] = {}
        for bus, rail_y, drop in ((HS, hs_y, +1), (SW, sw_y, -1)):
            mods = [m for m in vehnet.MODULES if m.bus == bus]
            mw = min(120.0, span / max(1, len(mods)) - 10)
            for i, m in enumerate(mods):
                cx = rail_x0 + span * (i + 0.5) / len(mods)
                ny = rail_y + drop * 26
                out[m.key] = QRectF(cx - mw / 2,
                                    ny if drop > 0 else ny - nh, mw, nh)
        return out

    def node_rects(self) -> dict[str, QRectF]:
        return self._layout_nodes(self.width(), self.height())

    def node_at(self, pos) -> Optional[str]:
        for key, rect in self.node_rects().items():
            if rect.contains(QPointF(pos)):
                return key
        return None

    def node_center(self, key):
        rect = self.node_rects().get(key)
        return rect.center() if rect is not None else None

    def tooltip_for(self, key) -> str:
        m = vehnet._module(key)
        if not m:
            return ""
        addr = (f"request {m.req_id} / response {m.resp_id}" if m.req_id
                else "none on HS-CAN (single-wire bus only)")
        return "\n".join([m.name,
                          f"Status: {self._mod(key).value}",
                          f"Bus: {m.bus}",
                          f"Diagnostic address: {addr}",
                          f"Role: {m.role}"])

    def event(self, e):  # noqa: D401 (Qt override)
        if e.type() == QEvent.ToolTip:
            key = self.node_at(e.pos())
            if key:
                QToolTip.showText(e.globalPos(), self.tooltip_for(key), self)
            else:
                QToolTip.hideText()
                e.ignore()
            return True
        return super().event(e)

    def mouseMoveEvent(self, event):  # noqa: N802
        over = self.node_at(event.position()) is not None
        self.setCursor(Qt.PointingHandCursor if over else Qt.ArrowCursor)
        super().mouseMoveEvent(event)

    # -- painting ------------------------------------------------------------
    def _seg(self, name) -> SegStatus:
        return self.verdict.segments.get(name, SegStatus.UNKNOWN) \
            if self.verdict else SegStatus.UNKNOWN

    def _mod(self, key) -> Status:
        # Before a scan every module is UNKNOWN — never a finding. A real
        # verdict (vehnet.localize) is what marks SW modules unreachable.
        if self.verdict is None:
            return Status.UNKNOWN
        return self.verdict.modules.get(key, Status.UNKNOWN)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        p.fillRect(0, 0, w, h, theme.diag_qcolor("map-canvas"))

        f = QFont(); f.setPointSizeF(8.5); p.setFont(f)

        def draw_node(rect, title, sub, color, selected=False):
            p.setPen(QPen(color.lighter(130) if selected else color,
                          3.2 if selected else 2.2))
            fill = QColor(color)
            fill.setAlpha(theme.DIAG_NODE_FILL_ALPHA * (2 if selected else 1))
            p.setBrush(fill)
            p.drawRoundedRect(rect, 6, 6)
            p.setPen(theme.diag_qcolor("node-title"))
            p.drawText(rect.adjusted(4, 3, -4, -rect.height() / 2),
                       Qt.AlignCenter, title)
            p.setPen(theme.diag_qcolor("node-sub"))
            p.drawText(rect.adjusted(4, rect.height() / 2 - 3, -4, -3),
                       Qt.AlignCenter, sub)

        def warning_mark(mx, my):
            # vector warning triangle (no glyph font needed offscreen)
            tri = QPainterPath(QPointF(mx, my - 28))
            tri.lineTo(mx - 10, my - 10)
            tri.lineTo(mx + 10, my - 10)
            tri.closeSubpath()
            mark = theme.diag_qcolor("segment-failed-mark")
            p.setPen(QPen(mark, 2.0))
            p.setBrush(Qt.NoBrush)
            p.drawPath(tri)
            p.drawLine(QPointF(mx, my - 23), QPointF(mx, my - 17))
            p.setBrush(mark)
            p.drawEllipse(QPointF(mx, my - 13.5), 1.2, 1.2)

        def seg_line(x1, y1, x2, y2, status, label=""):
            p.setPen(QPen(SEG_COLORS[status], 3 if status != SegStatus.UNKNOWN
                          else 1.6, Qt.SolidLine
                          if status != SegStatus.UNREACHABLE else Qt.DashLine))
            p.drawLine(QPointF(x1, y1), QPointF(x2, y2))
            if status == SegStatus.FAILED:
                warning_mark((x1 + x2) / 2, (y1 + y2) / 2)
            if label:
                p.setPen(theme.diag_qcolor("segment-label"))
                p.drawText(QRectF((x1 + x2) / 2 - 55, min(y1, y2) - 34,
                                  110, 14), Qt.AlignCenter, label)

        # chain: PC -> GT -> DLC
        pc_r, gt_r, dlc_r = self._chain_layout(w, h)
        gt_ok = self._seg("pc_gt")
        seg_line(pc_r.right(), pc_r.center().y(), gt_r.left(),
                 gt_r.center().y(), gt_ok, "USB")
        seg_line(gt_r.right(), gt_r.center().y(), dlc_r.left(),
                 dlc_r.center().y(), self._seg("gt_dlc"), "OBD cable")
        draw_node(pc_r, "PC", "OpenOBD", theme.diag_qcolor("node-tool"))
        draw_node(gt_r, "OBDX Pro GT", "interface",
                  SEG_COLORS[gt_ok] if gt_ok != SegStatus.UNKNOWN
                  else theme.diag_qcolor("node-tool"))
        draw_node(dlc_r, "DLC", "OBD-II port",
                  SEG_COLORS[self._seg("gt_dlc")]
                  if self._seg("gt_dlc") != SegStatus.UNKNOWN
                  else theme.diag_qcolor("node-tool"))

        # bus rails
        rail_x0, rail_x1, hs_y, sw_y = self._rails(w, h)
        seg_line(dlc_r.right(), dlc_r.center().y(), rail_x0, hs_y,
                 self._seg("dlc_hs"))
        seg_line(dlc_r.right(), dlc_r.center().y(), rail_x0, sw_y,
                 self._seg("dlc_sw"))
        seg_line(rail_x0, hs_y, rail_x1, hs_y, self._seg("dlc_hs"), HS)
        seg_line(rail_x0, sw_y, rail_x1, sw_y, self._seg("dlc_sw"))
        # SW modules hang ABOVE their rail, so its label goes below the line
        p.setPen(theme.diag_qcolor("segment-label"))
        p.drawText(QRectF((rail_x0 + rail_x1) / 2 - 70, sw_y + 8, 140, 14),
                   Qt.AlignCenter, SW)

        # modules hang off their rail
        rects = self._layout_nodes(w, h)
        for m in vehnet.MODULES:
            rect = rects.get(m.key)
            if rect is None:
                continue
            drop_down = m.bus == HS          # HS hangs below, SW above
            rail_y = hs_y if drop_down else sw_y
            cx = rect.center().x()
            st = self._mod(m.key)
            p.setPen(QPen(STATUS_COLORS[st], 1.6))
            p.drawLine(QPointF(cx, rail_y), QPointF(cx, rect.top()
                       if drop_down else rect.bottom()))
            sub = m.req_id and f"{m.req_id}/{m.resp_id}" or st.value
            draw_node(rect, m.name, sub, STATUS_COLORS[st],
                      selected=self.selected == m.key)
        p.end()

    def mousePressEvent(self, event):
        key = self.node_at(event.position())
        if key:
            self.selected = key
            self.update()
            self.module_selected.emit(key)


def status_swatch(status: Status, size: int = 10) -> QPixmap:
    """A small rounded square in a module status colour (legend row)."""
    px = QPixmap(size * 2, size * 2)
    px.fill(Qt.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(STATUS_COLORS[status])
    p.drawRoundedRect(QRectF(0, 0, size * 2, size * 2), 4, 4)
    p.end()
    px.setDevicePixelRatio(2.0)
    return px


class LegendRow(QWidget):
    """The Module Map's status legend as a row of swatch + label pairs.
    It never sets the page's minimum width: on a narrow window it clips."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("legendRow")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(theme.SPACING["xs"], 0, 0, 0)
        lay.setSpacing(theme.SPACING["lg"])
        self._labels: list[str] = []
        for st, text in LEGEND:
            pair = QHBoxLayout()
            pair.setSpacing(theme.SPACING["sm"])
            sw = QLabel()
            sw.setPixmap(status_swatch(st))
            pair.addWidget(sw)
            lab = QLabel(text)
            lab.setObjectName("legendText")
            pair.addWidget(lab)
            lay.addLayout(pair)
            self._labels.append(text)
        lay.addStretch(1)
        self.setMinimumSize(1, lay.sizeHint().height())

    def labels(self) -> list[str]:
        return list(self._labels)


class ModuleCard(QWidget):
    """The right-hand module card: title + status chip, Bus / Role /
    Diagnostic address rows, and a collapsible "Scan notes" list."""

    notes_toggled = Signal(bool)

    def __init__(self, notes_open: bool = True,
                 parent: Optional[QWidget] = None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setObjectName("cardScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        outer.addWidget(scroll)
        body = QWidget()
        body.setObjectName("cardBody")
        scroll.setWidget(body)
        lay = QVBoxLayout(body)
        lay.setContentsMargins(theme.SPACING["sm"], 0, theme.SPACING["sm"], 0)
        lay.setSpacing(theme.SPACING["md"])

        self.empty = empty_state_label(MAP_EMPTY)
        lay.addWidget(self.empty)

        self.details = QFrame()
        self.details.setObjectName("overlayPanel")
        dl = QVBoxLayout(self.details)
        m = theme.SPACING["md"]
        dl.setContentsMargins(m, m, m, m)
        dl.setSpacing(theme.SPACING["md"])
        head = QHBoxLayout()
        head.setSpacing(theme.SPACING["sm"])
        self.title = QLabel()
        self.title.setObjectName("cardTitle")
        self.title.setWordWrap(True)
        head.addWidget(self.title, 1)
        self.status_chip = QLabel()
        self.status_chip.setProperty("chip", "muted")
        head.addWidget(self.status_chip, 0, Qt.AlignTop)
        dl.addLayout(head)
        grid = QGridLayout()
        grid.setHorizontalSpacing(theme.SPACING["md"])
        grid.setVerticalSpacing(theme.SPACING["sm"])
        self._values: dict[str, QLabel] = {}
        for row, key in enumerate(("Bus", "Role", "Diagnostic address")):
            k = QLabel(key)
            k.setObjectName("cardKey")
            grid.addWidget(k, row, 0, Qt.AlignTop)
            v = QLabel()
            v.setObjectName("cardValue")
            v.setWordWrap(True)
            v.setTextInteractionFlags(Qt.TextSelectableByMouse)
            grid.addWidget(v, row, 1)
            self._values[key] = v
        grid.setColumnStretch(1, 1)
        dl.addLayout(grid)
        lay.addWidget(self.details)

        self.notes = QLabel()
        self.notes.setObjectName("cardNotes")
        self.notes.setWordWrap(True)
        self.notes.setTextFormat(Qt.PlainText)   # device replies are data
        self.notes.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.notes_panel = CollapsiblePanel("Scan notes", self.notes,
                                            expanded=notes_open,
                                            body_width=None,
                                            align=Qt.AlignLeft)
        self.notes_panel.toggled.connect(self.notes_toggled)
        lay.addWidget(self.notes_panel)
        lay.addStretch(1)

        self.details.hide()
        self.notes_panel.hide()
        self._notes: list[str] = []

    def show_hint(self, text: str) -> None:
        self.empty.setText(text)
        self.empty.show()
        self.details.hide()

    def show_module(self, key: str, status: Status) -> None:
        m = vehnet._module(key)
        if not m:
            return
        self.title.setText(m.name)
        self.status_chip.setText(status.value)
        set_chip(self.status_chip, STATUS_CHIP[status])
        self._values["Bus"].setText(m.bus)
        self._values["Role"].setText(m.role)
        self._values["Diagnostic address"].setText(
            f"request {m.req_id} / response {m.resp_id}" if m.req_id else
            "No HS-CAN diagnostic address — reachable only via the "
            "single-wire bus.")
        self.empty.hide()
        self.details.show()

    def set_notes(self, notes: list[str]) -> None:
        self._notes = list(notes)
        self.notes.setText("\n".join(f"• {n}" for n in notes)
                           if notes else "No notes from this scan.")
        self.notes_panel.show()

    def notes_list(self) -> list[str]:
        return list(self._notes)

    def text(self) -> str:
        """What the card shows, as plain text (title, status, rows)."""
        if self.details.isHidden():
            return self.empty.text()
        rows = [f"{k}: {v.text()}" for k, v in self._values.items()]
        return "\n".join([f"{self.title.text()}   [{self.status_chip.text()}]"]
                         + rows)


# --------------------------------------------------------------------------- #
# Codes
# --------------------------------------------------------------------------- #
class DtcFilter(QSortFilterProxyModel):
    """Case-insensitive search across every column; a matching group shows
    all its rows, a matching row keeps its group."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setRecursiveFilteringEnabled(True)
        self.setAutoAcceptChildRows(True)
        self.setFilterCaseSensitivity(Qt.CaseInsensitive)
        self.setFilterKeyColumn(-1)


class MonitorTile(QFrame):
    """One readiness monitor: name and a Ready / Not ready chip."""

    def __init__(self, name: str, complete: bool,
                 parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("monitorTile")
        self.setProperty("state", "ready" if complete else "notready")
        lay = QHBoxLayout(self)
        m = theme.SPACING["md"]
        lay.setContentsMargins(m, theme.SPACING["sm"], m, theme.SPACING["sm"])
        self.name = QLabel(name)
        self.name.setObjectName("monitorName")
        lay.addWidget(self.name, 1)
        self.chip = QLabel("Ready" if complete else "Not ready")
        self.chip.setProperty("chip", "ok" if complete else "warning")
        lay.addWidget(self.chip)
        self.complete = complete


class DiagnosticsPage(QWidget):
    """Top-level Diagnostics workspace: Module Map + Codes & Readiness."""

    def __init__(self, dashboard, settings=None):
        super().__init__()
        self.setObjectName("diagPage")
        self.setStyleSheet(theme.diag_qss())
        if settings is None:
            from .appsettings import app_settings
            settings = app_settings()
        self.settings = settings
        self.job = GtJob(dashboard, self)
        self.job.finished.connect(self._on_job_done)
        self._pending = None   # which handler consumes the next result
        self._scan: Optional[ScanResult] = None
        self._verdict: Optional[Verdict] = None
        self._scan_error: Optional[str] = None
        self._scanned_at: Optional[str] = None
        self._dtcs: Optional[dict] = None
        self._ready: Optional[dict] = None
        self._read_at: Optional[str] = None
        self._splits: list[tuple[QSplitter, str]] = []

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.addTab(self._build_map_tab(), "Module Map")
        self.tabs.addTab(self._build_codes_tab(), "Codes && Readiness")
        lay.addWidget(self.tabs)

        # F5 runs the current sub-tab's primary action. A window shortcut
        # owned by this page: Qt only fires it while the page is visible, so
        # it never collides with the Dashboard's F5 (start replay).
        self._f5 = QShortcut(QKeySequence(Qt.Key_F5), self)
        self._f5.setContext(Qt.WindowShortcut)
        self._f5.activated.connect(self.run_primary)

        self._refresh_map_chrome()
        self._refresh_codes_chrome()

    # -- shared helpers -------------------------------------------------------
    @staticmethod
    def _banner() -> QLabel:
        lab = QLabel()
        lab.setWordWrap(True)
        lab.setTextFormat(Qt.PlainText)
        lab.setProperty("banner", "danger")
        lab.hide()
        return lab

    @staticmethod
    def _show_banner(lab: QLabel, text: Optional[str], kind: str = "danger"):
        if not text:
            lab.hide()
            return
        lab.setText(text)
        if lab.property("banner") != kind:
            lab.setProperty("banner", kind)
            repolish(lab)
        lab.show()

    def _action(self, glyph: str, text: str, tip: str, slot) -> QAction:
        a = QAction(theme.glyph_icon(glyph), text, self)
        a.setToolTip(tip)
        a.triggered.connect(slot)
        return a

    @staticmethod
    def _mark_primary(tb, action: QAction) -> None:
        btn = tb.widgetForAction(action)
        if btn is not None:
            btn.setProperty("primary", True)

    def _restore_split(self, split: QSplitter, key: str, default: list[int]):
        state = self.settings.value(key)
        if state is None or not split.restoreState(state):
            split.setSizes(default)
        split.splitterMoved.connect(
            lambda *_a, s=split, k=key: self.settings.setValue(k, s.saveState()))
        self._splits.append((split, key))

    def save_state(self) -> None:
        """Persist the splitter positions (also saved on every drag)."""
        for split, key in self._splits:
            self.settings.setValue(key, split.saveState())

    def busy(self) -> Optional[str]:
        """The running job: 'scan' | 'read' | 'clear', or None."""
        return self._pending

    def _set_busy(self, op: Optional[str]) -> None:
        self._pending = op
        idle = op is None
        # One GT job at a time: _pending names the handler of THE result.
        for a in (self.act_scan, self.act_read, self.act_clear):
            a.setEnabled(idle)
        self._refresh_map_chrome()
        self._refresh_codes_chrome()

    def run_primary(self) -> None:
        """F5: the current sub-tab's primary action."""
        if self._pending is not None:
            return
        if self.tabs.currentIndex() == 0:
            self.scan_modules()
        else:
            self.read_codes()

    def has_search(self) -> bool:
        """True while the sub-tab with a search field is showing."""
        return self.tabs.currentIndex() == 1

    def focus_search(self) -> None:
        self.tabs.setCurrentIndex(1)
        self.dtc_search.setFocus(Qt.ShortcutFocusReason)
        self.dtc_search.selectAll()

    def eventFilter(self, obj, event):  # noqa: N802
        if (obj is self.dtc_search and event.type() == QEvent.KeyPress
                and event.key() == Qt.Key_Escape):
            self.dtc_search.clear()
            return True
        return super().eventFilter(obj, event)

    # -- module map tab -------------------------------------------------------
    def _build_map_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.map_crumb = Breadcrumb(elastic_chips=True)
        self.map_crumb.set_path(["Diagnostics", "Module Map"])
        lay.addWidget(self.map_crumb)
        tb = make_toolbar("diagToolbar")
        self.map_toolbar = tb
        self.act_scan = self._action(
            "scan", "Scan Modules",
            "Scan the comms pipeline: USB link, DLC power, HS-GMLAN and "
            "every module  (F5)", self.scan_modules)
        tb.addAction(self.act_scan)
        self._mark_primary(tb, self.act_scan)
        tb.addSeparator()
        self.act_copy_report = self._action(
            "copy", "Copy report",
            "Copy the scan verdict, every module's status and the scan "
            "notes as text", self.copy_report)
        tb.addAction(self.act_copy_report)
        lay.addWidget(tb)

        body = QVBoxLayout()
        m = theme.SPACING["sm"]
        body.setContentsMargins(m, m, m, m)
        body.setSpacing(m)
        self.map_banner = self._banner()
        body.addWidget(self.map_banner)
        self.legend = LegendRow()
        body.addWidget(self.legend)

        split = QSplitter(Qt.Horizontal)
        split.setObjectName("diagMapSplit")
        split.setChildrenCollapsible(False)
        self.map_view = ModuleMapView()
        self.map_view.module_selected.connect(self._show_module)
        split.addWidget(self.map_view)
        notes_open = str(self.settings.value(NOTES_OPEN_KEY, "true")).lower()
        self.card = ModuleCard(notes_open == "true")
        self.card.notes_toggled.connect(
            lambda on: self.settings.setValue(NOTES_OPEN_KEY,
                                              "true" if on else "false"))
        split.addWidget(self.card)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 0)
        self.map_split = split
        self._restore_split(split, MAP_SPLIT_KEY, [900, 300])
        body.addWidget(split, 1)
        lay.addLayout(body, 1)
        return w

    def _map_chips(self) -> list[tuple[str, str]]:
        v, scan = self._verdict, self._scan
        if v is None or scan is None:
            chips = [("not scanned", "muted")]
        else:
            if scan.dlc_volts is not None:
                chips = [(f"DLC {scan.dlc_volts:.1f} V",
                          "danger" if v.segments.get("gt_dlc")
                          == SegStatus.FAILED else "neutral")]
            else:
                chips = [("DLC not read", "muted")]
            ok = sum(1 for s in v.modules.values() if s == Status.OK)
            chips.append((f"{ok} responding", "ok" if ok else "neutral"))
            unexamined = sum(1 for s in v.modules.values()
                             if s == Status.NOT_EXAMINED)
            if unexamined:
                chips.append((f"{unexamined} not examined", "muted"))
            if v.failure_point:
                chips.append((f"fault at: {failure_label(v.failure_point)}",
                              "danger"))
        if self._pending == "scan":
            chips.append(("Scanning…", "muted"))
        return chips

    def _refresh_map_chrome(self) -> None:
        self.map_crumb.set_chips(self._map_chips())
        if self._scanned_at is None:
            self.map_crumb.set_note(
                "Not scanned — Scan Modules (F5) uses the live GT link or "
                "opens its own. Hover a module for its address, click it "
                "for details.")
        else:
            self.map_crumb.set_note(
                f"Scanned at {self._scanned_at} — hover a module for its "
                "address, click it for details.")
        self.act_copy_report.setEnabled(self._verdict is not None)

    def scan_modules(self):
        if self._pending is not None:
            return
        self._set_busy("scan")
        self.job.run(vehnet.scan_pipeline)

    def _apply_scan(self, result, error):
        self._scan_error = error
        if error is not None:
            # the port never opened — that IS the pipeline verdict
            result = ScanResult(port_open=False, interface_alive=False,
                                dlc_volts=None, hs_responders=set(),
                                pinged={})
        v = vehnet.localize(result)
        self._scan, self._verdict = result, v
        self._scanned_at = time.strftime("%H:%M:%S")
        self.map_view.set_verdict(v)
        if error is not None:
            self._show_banner(self.map_banner,
                              f"Scan failed at the tool link: {error}")
        elif v.failure_point == "pc_gt:binary_mode":
            self._show_banner(self.map_banner, BINARY_MODE_ADVICE)
        else:
            self._show_banner(self.map_banner, None)
        self.card.set_notes(v.notes)
        if self.map_view.selected:
            self._show_module(self.map_view.selected)
        else:
            self.card.show_hint(MAP_PICK)
        if self._pending == "scan":
            self._set_busy(None)
        else:
            self._refresh_map_chrome()

    def _show_module(self, key):
        if not vehnet._module(key):
            return
        self.card.show_module(key, self.map_view.module_status(key))

    def scan_report(self) -> str:
        """The Copy report text: verdict, every module, the notes."""
        v, scan = self._verdict, self._scan
        if v is None or scan is None:
            return ""
        lines = [f"OpenOBD module scan at {self._scanned_at}"]
        if self._scan_error:
            lines.append(f"Scan failed at the tool link: {self._scan_error}")
        lines.append("DLC: " + (f"{scan.dlc_volts:.1f} V"
                                if scan.dlc_volts is not None else "not read"))
        lines.append("Fault at: " + (failure_label(v.failure_point)
                                     if v.failure_point else "none found"))
        lines.append("")
        lines.append("module\tbus\trequest/response\tstatus")
        for m in vehnet.MODULES:
            addr = f"{m.req_id}/{m.resp_id}" if m.req_id else "-"
            st = v.modules.get(m.key, Status.UNKNOWN)
            lines.append(f"{m.name}\t{m.bus}\t{addr}\t{st.value}")
        if v.notes:
            lines.append("")
            lines.append("Notes:")
            lines.extend(f"- {n}" for n in v.notes)
        return "\n".join(lines)

    def copy_report(self) -> None:
        text = self.scan_report()
        if text:
            QApplication.clipboard().setText(text)

    # -- codes tab ------------------------------------------------------------
    def _build_codes_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.codes_crumb = Breadcrumb(elastic_chips=True)
        self.codes_crumb.set_path(["Diagnostics", "Codes & Readiness"])
        lay.addWidget(self.codes_crumb)
        tb = make_toolbar("diagToolbar")
        self.codes_toolbar = tb
        self.act_read = self._action(
            "read", "Read Codes",
            "Read stored, pending and permanent codes and the readiness "
            "monitors  (F5)", self.read_codes)
        tb.addAction(self.act_read)
        self._mark_primary(tb, self.act_read)
        self.act_clear = self._action(
            "clear", "Clear Codes…",
            "Clear every stored and pending code (asks first; also resets "
            "every readiness monitor)", self.clear_codes)
        # QAction.iconText() drops a trailing ellipsis; the button must keep
        # it — "…" is what says this action asks before it acts.
        self.act_clear.setIconText(self.act_clear.text())
        tb.addAction(self.act_clear)
        tb.addSeparator()
        self.act_copy_codes = self._action(
            "copy", "Copy codes",
            "Copy the visible rows as tab-separated text: kind, code, "
            "description, module", self.copy_codes)
        tb.addAction(self.act_copy_codes)
        tb.addSeparator()
        self.dtc_search = QLineEdit()
        self.dtc_search.setObjectName("dtcSearch")
        self.dtc_search.setPlaceholderText("Search codes (Ctrl+F)")
        self.dtc_search.setClearButtonEnabled(True)
        self.dtc_search.setFixedWidth(220)
        self.dtc_search.installEventFilter(self)
        self.dtc_search.textChanged.connect(self.set_filter)
        tb.addWidget(self.dtc_search)
        lay.addWidget(tb)

        body = QVBoxLayout()
        m = theme.SPACING["sm"]
        body.setContentsMargins(m, m, m, m)
        body.setSpacing(m)
        self.codes_banner = self._banner()
        body.addWidget(self.codes_banner)
        self.mil_banner = self._banner()
        body.addWidget(self.mil_banner)

        split = QSplitter(Qt.Horizontal)
        split.setObjectName("diagCodesSplit")
        split.setChildrenCollapsible(False)

        self.dtc_model = QStandardItemModel(0, 4, self)
        self.dtc_model.setHorizontalHeaderLabels(
            ["Code", "Description", "Module", "Status"])
        self.dtc_proxy = DtcFilter(self)
        self.dtc_proxy.setSourceModel(self.dtc_model)
        self.dtc_tree = QTreeView()
        self.dtc_tree.setObjectName("dtcTree")
        self.dtc_tree.setModel(self.dtc_proxy)
        self.dtc_tree.setUniformRowHeights(True)
        self.dtc_tree.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.dtc_tree.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.dtc_tree.setIconSize(QSize(theme.ICON_SIZE, theme.ICON_SIZE))
        self.dtc_tree.setItemDelegateForColumn(3, ChipDelegate(self.dtc_tree))
        hdr = self.dtc_tree.header()
        hdr.setStretchLastSection(False)
        hdr.setSectionResizeMode(1, QHeaderView.Stretch)
        hdr.resizeSection(0, 150)
        hdr.resizeSection(2, 140)
        hdr.resizeSection(3, 120)
        split.addWidget(self.dtc_tree)

        ready = QWidget()
        rl = QVBoxLayout(ready)
        rl.setContentsMargins(theme.SPACING["sm"], 0, 0, 0)
        rl.setSpacing(theme.SPACING["sm"])
        head = QHBoxLayout()
        title = QLabel("Readiness monitors")
        title.setObjectName("sectionTitle")
        head.addWidget(title, 1)
        self.ready_chip = QLabel()
        self.ready_chip.setProperty("chip", "muted")
        head.addWidget(self.ready_chip)
        rl.addLayout(head)
        self.ready_empty = empty_state_label(READY_EMPTY)
        rl.addWidget(self.ready_empty)
        scroll = QScrollArea()
        scroll.setObjectName("readinessScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        grid_host = QWidget()
        grid_host.setObjectName("readinessGrid")
        self.ready_grid = QGridLayout(grid_host)
        self.ready_grid.setContentsMargins(0, 0, 0, 0)
        self.ready_grid.setSpacing(theme.SPACING["sm"])
        self.ready_grid.setAlignment(Qt.AlignTop)
        scroll.setWidget(grid_host)
        self.ready_scroll = scroll
        rl.addWidget(scroll, 1)
        self.monitor_tiles: list[MonitorTile] = []
        split.addWidget(ready)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 0)
        self.codes_split = split
        self._restore_split(split, CODES_SPLIT_KEY, [880, 400])
        body.addWidget(split, 1)
        lay.addLayout(body, 1)
        return w

    def _codes_chips(self) -> list[tuple[str, str]]:
        dtcs, ready = self._dtcs, self._ready
        if dtcs is None:
            chips = [("not read", "muted")]
        else:
            if not ready or ready.get("error"):
                chips = [("MIL not examined", "muted")]
            elif ready.get("mil"):
                chips = [("MIL ON", "danger")]
            else:
                chips = [("MIL off", "ok")]
            incomplete = dtcs.get("incomplete") or {}
            for kind, key, chip in DTC_KINDS:
                codes = dtcs.get(key)
                partial = bool(incomplete.get(key))
                if codes is None:
                    chips.append((f"{kind} "
                                  + ("incomplete" if partial
                                     else "not examined"), "muted"))
                    continue
                n = len(codes)
                text = f"{kind} {n}" + (" + incomplete" if partial else "")
                # zero is only "ok" when every module answered completely
                chips.append((text, chip if n else
                              ("muted" if partial else "ok")))
        if self._pending in ("read", "clear"):
            chips.append(("Reading…" if self._pending == "read"
                          else "Clearing…", "muted"))
        return chips

    def _refresh_codes_chrome(self) -> None:
        self.codes_crumb.set_chips(self._codes_chips())
        if self._read_at is None:
            self.codes_crumb.set_note(
                "Read Codes (F5) reads stored, pending and permanent codes "
                "and the readiness monitors. Clear Codes also resets every "
                "readiness monitor.")
        else:
            self.codes_crumb.set_note(
                f"Codes read at {self._read_at} — Clear Codes also resets "
                "every readiness monitor.")
        self.act_copy_codes.setEnabled(self._dtcs is not None)

    def read_codes(self):
        if self._pending is not None:
            return
        self._set_busy("read")
        self.job.run(lambda gt: {"dtcs": gt.read_dtcs(),
                                 "ready": gt.readiness()})

    def clear_codes(self):
        if self._pending is not None:
            return
        ans = QMessageBox.warning(
            self, "Clear trouble codes",
            "Clear ALL stored and pending DTCs?\n\nThis also resets every "
            "readiness monitor — the truck will need a full drive cycle "
            "before it can pass an emissions check.",
            QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel)
        if ans != QMessageBox.Yes:
            return
        self._set_busy("clear")
        self.job.run(clear_and_reread)

    # model rows -------------------------------------------------------------
    @staticmethod
    def _row(code: str, desc: str, module: str, status: str, chip: str,
             row_kind: str, dtc_kind: str) -> list[QStandardItem]:
        items = [QStandardItem(t) for t in (code, desc, module, status)]
        for it in items:
            it.setEditable(False)
            it.setData(row_kind, ROW_KIND_ROLE)
            it.setData(dtc_kind, DTC_KIND_ROLE)
        items[3].setData(chip, CHIP_KIND_ROLE)
        return items

    def _uncertain_row(self, kind: str, text: str, row_kind: str,
                       module: str = "") -> list[QStandardItem]:
        """A not-examined / incomplete row: muted italic, its own glyph, the
        why in Description — never mistakable for a code or a clean row."""
        items = self._row("?", text, module or "—",
                          "not examined" if row_kind == "not_examined"
                          else "incomplete", "muted", row_kind, kind)
        items[0].setIcon(theme.glyph_icon(row_kind, "text-muted"))
        muted = theme.diag_qcolor("not-examined-text")
        for it in items:
            f = it.font()
            f.setItalic(True)
            it.setFont(f)
            it.setForeground(muted)
        return items

    def populate_codes(self, dtcs: dict, ready: dict):
        self._dtcs = dtcs or {}
        self._ready = ready or {}
        self._read_at = time.strftime("%H:%M:%S")
        dtcs, ready = self._dtcs, self._ready
        self.dtc_model.removeRows(0, self.dtc_model.rowCount())
        root = self.dtc_model.invisibleRootItem()
        unexamined = []
        n_codes = 0
        incomplete = dtcs.get("incomplete") or {}
        by_module = dtcs.get("by_module") or {}
        expand: list[QStandardItem] = []
        bold = QFont()
        bold.setBold(True)

        for kind, key, chip in DTC_KINDS:
            codes = dtcs.get(key)
            partial = bool(incomplete.get(key))
            if codes is None:
                count = "incomplete" if partial else "not examined"
            else:
                count = f"{len(codes)}" + (" + incomplete" if partial else "")
            group = self._row(f"{kind} ({count})", "", "", "", "",
                              "group", kind)
            group[0].setFont(bold)
            root.appendRow(group)
            g = group[0]
            uncertain = False
            # a module whose reply could not be read completely: its codes
            # are UNKNOWN — never let a short list pass as the whole story
            for cid, why in sorted(incomplete.get(key, {}).items()):
                unexamined.append(kind)
                uncertain = True
                g.appendRow(self._uncertain_row(
                    kind, f"Incomplete from {cid} — {why}", "incomplete",
                    module_name(cid)))
            if codes is None:
                # not examined is NOT "no codes" — say so on its own row
                if not partial:
                    unexamined.append(kind)
                    uncertain = True
                    g.appendRow(self._uncertain_row(
                        kind, "Not examined — " + (
                            dtcs.get("error") or "no module answered"),
                        "not_examined"))
            else:
                n_codes += len(codes)
                mods = by_module.get(key) or {}
                for code in codes:
                    names = [module_name(cid) for cid, cs in sorted(
                        mods.items()) if code in (cs or [])]
                    desc = DTC_DESCRIPTIONS.get(code)
                    row = self._row(code, desc or NO_DESCRIPTION,
                                    ", ".join(names) or "—", kind, chip,
                                    "code", kind)
                    if kind == "Stored":
                        row[0].setForeground(theme.diag_qcolor("dtc-stored"))
                    if not desc:
                        row[1].setForeground(theme.qcolor("text-muted"))
                    if not names:
                        row[2].setForeground(theme.qcolor("text-muted"))
                    g.appendRow(row)
            if kind == "Stored" or uncertain:
                expand.append(g)

        if n_codes == 0 and len(set(unexamined)) < 3:
            # only the kinds that were actually examined can be called clean
            clean = not unexamined
            root.insertRow(0, self._row(
                "—", "No trouble codes" if clean else
                "No codes in the examined kinds", "",
                "clear" if clean else "partial",
                "ok" if clean else "muted", "summary", ""))
        self._expand_groups = expand
        self._apply_tree_layout()
        self._populate_readiness(ready)
        self._update_mil(ready)
        self._show_banner(self.codes_banner, None)
        self._refresh_codes_chrome()

    def _apply_tree_layout(self) -> None:
        """Group rows span the tree; the default groups are open (all of
        them while a search is active)."""
        proxy = self.dtc_proxy
        searching = bool(self.dtc_search.text().strip())
        for r in range(proxy.rowCount()):
            idx = proxy.index(r, 0)
            if idx.data(ROW_KIND_ROLE) == "group":
                self.dtc_tree.setFirstColumnSpanned(r, idx.parent(), True)
        if searching:
            self.dtc_tree.expandAll()
            return
        self.dtc_tree.collapseAll()
        for g in getattr(self, "_expand_groups", []):
            src = self.dtc_model.indexFromItem(g)
            self.dtc_tree.setExpanded(proxy.mapFromSource(src), True)

    def set_filter(self, text: str) -> None:
        self.dtc_proxy.setFilterFixedString((text or "").strip())
        self._apply_tree_layout()

    def visible_rows(self) -> list[dict]:
        """The rows the tree shows under the current filter (groups
        excluded): {kind, code, description, module, status, row}."""
        out = []
        proxy = self.dtc_proxy

        def take(parent):
            for r in range(proxy.rowCount(parent)):
                idx = proxy.index(r, 0, parent)
                rk = idx.data(ROW_KIND_ROLE)
                if rk == "group":
                    take(idx)
                    continue
                cells = [proxy.index(r, c, parent).data() or ""
                         for c in range(4)]
                out.append({"kind": idx.data(DTC_KIND_ROLE) or "",
                            "code": cells[0], "description": cells[1],
                            "module": cells[2], "status": cells[3],
                            "row": rk})

        take(QModelIndex())
        return out

    def codes_tsv(self) -> str:
        lines = ["kind\tcode\tdescription\tmodule"]
        for r in self.visible_rows():
            lines.append("\t".join(r[k].replace("\t", " ") for k in
                                   ("kind", "code", "description", "module")))
        return "\n".join(lines)

    def copy_codes(self) -> None:
        if self._dtcs is None:
            return
        QApplication.clipboard().setText(self.codes_tsv())

    def _populate_readiness(self, ready: dict) -> None:
        for t in self.monitor_tiles:
            self.ready_grid.removeWidget(t)
            t.deleteLater()
        self.monitor_tiles = []
        monitors = list(ready.get("monitors") or [])
        cols = 2
        for i, (name, complete) in enumerate(monitors):
            tile = MonitorTile(name, bool(complete))
            self.ready_grid.addWidget(tile, i // cols, i % cols)
            self.monitor_tiles.append(tile)
        if not ready or ready.get("error"):
            self.ready_empty.setText(
                "Readiness not examined — "
                + (ready.get("error") or "no readiness reply"))
            self._ready_summary = ("readiness not examined", "muted")
        elif not monitors:
            self.ready_empty.setText(
                "The ECM reported no supported readiness monitors.")
            self._ready_summary = ("no monitors reported", "muted")
        else:
            done = sum(1 for _n, c in monitors if c)
            self._ready_summary = (f"{done}/{len(monitors)} ready",
                                   "ok" if done == len(monitors)
                                   else "warning")
        self.ready_empty.setVisible(not monitors)
        self.ready_scroll.setVisible(bool(monitors))
        self.ready_chip.setText(self._ready_summary[0])
        set_chip(self.ready_chip, self._ready_summary[1])

    def readiness_summary(self) -> tuple[str, str]:
        return (self.ready_chip.text(), str(self.ready_chip.property("chip")))

    def _update_mil(self, ready: dict) -> None:
        if not ready or ready.get("error"):
            self._show_banner(
                self.mil_banner, "MIL not examined — "
                + (ready.get("error") or "no readiness reply"), "muted")
        elif ready.get("mil"):
            self._show_banner(
                self.mil_banner,
                f"MIL is ON — {ready.get('dtc_count', 0)} code(s)", "danger")
        else:
            self._show_banner(self.mil_banner, None)

    # -- shared result dispatch ----------------------------------------------
    def _on_job_done(self, result, error):
        pending = self._pending
        try:
            if pending == "scan":
                # restore busy FIRST: a handler exception must never leave
                # every action disabled with F5 dead
                self._set_busy(None)
                self._apply_scan(result, error)
            else:
                self._apply_codes(pending, result, error)
        except Exception as e:   # noqa: BLE001 — shown, never swallowed
            if self._pending is not None:
                self._set_busy(None)
            banner = (self.map_banner if pending == "scan"
                      else self.codes_banner)
            self._show_banner(
                banner, f"Couldn't show the {pending or 'job'} result: "
                f"{type(e).__name__}: {e}")
        finally:
            if self._pending is not None:
                self._set_busy(None)

    def _apply_codes(self, pending, result, error):
        if error is not None:
            self._set_busy(None)
            self._show_banner(
                self.codes_banner, f"Couldn't reach the truck: {error}"
                + (" — the rows below are from the previous read at "
                   f"{self._read_at}." if self._read_at else ""))
            return
        result = result or {}
        dtcs, ready = result.get("dtcs") or {}, result.get("ready") or {}
        # A step that failed comes back as {"error": why}: populate_codes
        # renders that as not-examined rows / banners — never old rows.
        failed = [name for name, part in (("codes", dtcs),
                                          ("readiness", ready))
                  if isinstance(part, dict) and part.get("error")
                  and not part.get("examined")]
        # Populate BEFORE any dialog, still busy, so the table is current
        # while a modal is up and no Read can be started underneath it.
        self.populate_codes(dtcs, ready)
        clr = result.get("clear") if pending == "clear" else None
        if clr is not None and failed:
            why = "; ".join(f"{n} re-read failed: "
                            + str((dtcs if n == "codes" else ready)["error"])
                            for n in failed)
            outcome = (f"Clear confirmed by "
                       f"{', '.join(clr.get('acked_by') or []) or 'the ECM'}"
                       if clr.get("cleared") else
                       "Clear NOT sent" if not clr.get("sent") else
                       "Clear sent, not confirmed")
            self._show_banner(self.codes_banner, f"{outcome}; {why}")
        self._set_busy(None)
        if clr is None:
            return
        if not clr.get("sent"):
            QMessageBox.warning(
                self, "Clear codes",
                "The clear was NOT sent — nothing was changed.\n\n"
                f"{clr.get('error') or 'addressing not confirmed'}")
        elif not clr.get("cleared"):
            QMessageBox.warning(
                self, "Clear codes",
                "The clear was sent but the ECM did not confirm it.\n\n"
                f"{clr.get('error')}\nAcknowledged by: "
                f"{', '.join(clr.get('acked_by', [])) or 'nobody'}")
