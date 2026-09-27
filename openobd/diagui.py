"""
diagui — the Diagnostics workspace: module map + codes & readiness.

Troubleshooting lives here, segregated from the tuning workflow:
  Module Map        — the vehicle network drawn as tool -> DLC -> bus rails ->
                      modules, with the comms pipeline verdict from
                      vehnet.localize() painted onto nodes and segments so a
                      failure shows WHERE it happened.
  Codes & Readiness — read stored/pending/permanent DTCs, clear codes
                      (confirmed — it also resets readiness), monitor table.

All hardware IO runs through GtJob on a worker thread: it borrows the
dashboard's live GT connection (polling paused via run_exclusive) or opens a
temporary one, so the GUI never blocks on serial traffic.
"""
from __future__ import annotations

import threading
from typing import Optional

from PySide6.QtCore import Qt, QObject, QRectF, QPointF, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTabWidget, QPushButton, QLabel,
    QTableWidget, QTableWidgetItem, QTextEdit, QHeaderView, QMessageBox,
    QSplitter,
)

from . import vehnet
from .gt import DTC_DESCRIPTIONS
from .vehnet import HS, SW, ScanResult, SegStatus, Status, Verdict


STATUS_COLORS = {
    Status.OK: QColor(60, 170, 95),
    Status.SILENT: QColor(205, 60, 50),
    Status.UNREACHABLE: QColor(205, 150, 45),
    # grey family: no finding either way. NOT_EXAMINED is a cooler grey so a
    # refused header reads differently from "never scanned" at a glance.
    Status.NOT_EXAMINED: QColor(110, 118, 140),
    Status.UNKNOWN: QColor(95, 100, 110),
}
SEG_COLORS = {
    SegStatus.OK: QColor(60, 170, 95),
    SegStatus.FAILED: QColor(205, 60, 50),
    SegStatus.UNREACHABLE: QColor(205, 150, 45),
    SegStatus.UNKNOWN: QColor(95, 100, 110),
}


class GtJob(QObject):
    """Run fn(gt) off the GUI thread against whatever GT link exists."""
    finished = Signal(object, object)   # result, error string or None

    def __init__(self, dashboard, parent=None):
        super().__init__(parent)
        self.dashboard = dashboard

    def run(self, fn):
        def work():
            try:
                from .transport import GtDataSource
                src = self.dashboard.source
                if isinstance(src, GtDataSource) and src._thread is not None:
                    result = src.run_exclusive(fn)
                else:
                    from . import gt as _gt
                    g = _gt.ObdxGt()
                    g.open()
                    try:
                        result = fn(g)
                    finally:
                        g.close()
            except Exception as e:
                try:
                    self.finished.emit(None, str(e))
                except RuntimeError:
                    pass
                return
            try:
                self.finished.emit(result, None)
            except RuntimeError:
                pass
        threading.Thread(target=work, name="gt-diag", daemon=True).start()


class ModuleMapView(QWidget):
    """Paints the network: PC -> GT -> DLC, then the HS and SW bus rails with
    their modules. Node/segment colors come from the scan verdict; the first
    failed segment gets a ⚠ marker. Click a node for details."""

    module_selected = Signal(str)

    def __init__(self):
        super().__init__()
        self.verdict: Optional[Verdict] = None
        self.selected: Optional[str] = None
        self._node_rects: dict[str, QRectF] = {}
        self.setMinimumHeight(320)

    def set_verdict(self, v: Optional[Verdict]):
        self.verdict = v
        self.update()

    # -- painting ------------------------------------------------------------
    def _seg(self, name) -> SegStatus:
        return self.verdict.segments.get(name, SegStatus.UNKNOWN) \
            if self.verdict else SegStatus.UNKNOWN

    def _mod(self, key) -> Status:
        if self.verdict:
            return self.verdict.modules.get(key, Status.UNKNOWN)
        return Status.UNREACHABLE if (vehnet._module(key)
                                      and vehnet._module(key).bus == SW) \
            else Status.UNKNOWN

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        p.fillRect(0, 0, w, h, QColor(24, 26, 30))
        self._node_rects.clear()

        f = QFont(); f.setPointSizeF(8.5); p.setFont(f)
        node_h = 40
        chain_y = h * 0.5 - node_h / 2

        def draw_node(rect, title, sub, color, key=None, selected=False):
            p.setPen(QPen(color.lighter(130) if selected else color, 2.2))
            p.setBrush(QColor(color.red(), color.green(), color.blue(), 46))
            p.drawRoundedRect(rect, 6, 6)
            p.setPen(QColor(228, 230, 234))
            p.drawText(rect.adjusted(4, 3, -4, -rect.height() / 2),
                       Qt.AlignCenter, title)
            p.setPen(QColor(165, 170, 178))
            p.drawText(rect.adjusted(4, rect.height() / 2 - 3, -4, -3),
                       Qt.AlignCenter, sub)
            if key:
                self._node_rects[key] = QRectF(rect)

        def seg_line(x1, y1, x2, y2, status, label=""):
            p.setPen(QPen(SEG_COLORS[status], 3 if status != SegStatus.UNKNOWN
                          else 1.6, Qt.SolidLine
                          if status != SegStatus.UNREACHABLE else Qt.DashLine))
            p.drawLine(QPointF(x1, y1), QPointF(x2, y2))
            if status == SegStatus.FAILED:
                mx, my = (x1 + x2) / 2, (y1 + y2) / 2
                p.setPen(QPen(QColor(235, 80, 70), 2.5))
                fb = QFont(); fb.setPointSizeF(13); fb.setBold(True)
                p.setFont(fb)
                p.drawText(QRectF(mx - 30, my - 26, 60, 20),
                           Qt.AlignCenter, "⚠")
                p.setFont(f)
            if label:
                p.setPen(QColor(140, 146, 156))
                p.drawText(QRectF((x1 + x2) / 2 - 55, min(y1, y2) - 34,
                                  110, 14), Qt.AlignCenter, label)

        # chain: PC -> GT -> DLC
        nw = min(110.0, w * 0.13)
        pc_r = QRectF(12, chain_y, nw, node_h)
        gt_ok = self._seg("pc_gt")
        gt_r = QRectF(pc_r.right() + 46, chain_y, nw, node_h)
        dlc_r = QRectF(gt_r.right() + 46, chain_y, nw, node_h)
        seg_line(pc_r.right(), pc_r.center().y(), gt_r.left(),
                 gt_r.center().y(), gt_ok, "USB")
        seg_line(gt_r.right(), gt_r.center().y(), dlc_r.left(),
                 dlc_r.center().y(), self._seg("gt_dlc"), "OBD cable")
        draw_node(pc_r, "PC", "OpenOBD", QColor(90, 130, 200))
        draw_node(gt_r, "OBDX Pro GT", "interface",
                  SEG_COLORS[gt_ok] if gt_ok != SegStatus.UNKNOWN
                  else QColor(90, 130, 200))
        draw_node(dlc_r, "DLC", "OBD-II port",
                  SEG_COLORS[self._seg("gt_dlc")]
                  if self._seg("gt_dlc") != SegStatus.UNKNOWN
                  else QColor(90, 130, 200))

        # bus rails
        rail_x0 = dlc_r.right() + 40
        rail_x1 = w - 16
        hs_y = h * 0.22
        sw_y = h * 0.78
        seg_line(dlc_r.right(), dlc_r.center().y(), rail_x0, hs_y,
                 self._seg("dlc_hs"))
        seg_line(dlc_r.right(), dlc_r.center().y(), rail_x0, sw_y,
                 self._seg("dlc_sw"))
        seg_line(rail_x0, hs_y, rail_x1, hs_y, self._seg("dlc_hs"), HS)
        seg_line(rail_x0, sw_y, rail_x1, sw_y, self._seg("dlc_sw"))
        # SW modules hang ABOVE their rail, so its label goes below the line
        p.setPen(QColor(140, 146, 156))
        p.drawText(QRectF((rail_x0 + rail_x1) / 2 - 70, sw_y + 8, 140, 14),
                   Qt.AlignCenter, SW)

        # modules hang off their rail
        for bus, rail_y, drop in ((HS, hs_y, +1), (SW, sw_y, -1)):
            mods = [m for m in vehnet.MODULES if m.bus == bus]
            span = rail_x1 - rail_x0
            mw = min(120.0, span / max(1, len(mods)) - 10)
            for i, m in enumerate(mods):
                cx = rail_x0 + span * (i + 0.5) / len(mods)
                ny = rail_y + drop * 26
                rect = QRectF(cx - mw / 2, ny if drop > 0 else ny - node_h,
                              mw, node_h)
                st = self._mod(m.key)
                p.setPen(QPen(STATUS_COLORS[st], 1.6))
                p.drawLine(QPointF(cx, rail_y), QPointF(cx, rect.top()
                           if drop > 0 else rect.bottom()))
                sub = m.req_id and f"{m.req_id}/{m.resp_id}" or st.value
                draw_node(rect, m.name, sub, STATUS_COLORS[st], key=m.key,
                          selected=self.selected == m.key)

        # legend — wraps upward onto a second row when the view is narrow
        lx = 14
        ly = h - 20
        for st, txt in ((Status.OK, "responding"),
                        (Status.SILENT, "no response"),
                        (Status.UNREACHABLE, "unreachable via this path"),
                        (Status.NOT_EXAMINED, "not examined"),
                        (Status.UNKNOWN, "not scanned")):
            tw = p.fontMetrics().horizontalAdvance(txt) + 18
            if lx > 14 and lx + 14 + tw > w - 8:
                lx, ly = 14, ly - 18
            p.setBrush(STATUS_COLORS[st]); p.setPen(Qt.NoPen)
            p.drawRect(QRectF(lx, ly, 10, 10))
            p.setPen(QColor(170, 175, 183))
            p.drawText(QRectF(lx + 14, ly - 3, tw, 16),
                       Qt.AlignLeft | Qt.AlignVCenter, txt)
            lx += tw + 26
        p.end()

    def mousePressEvent(self, event):
        pos = event.position()
        for key, rect in self._node_rects.items():
            if rect.contains(pos):
                self.selected = key
                self.update()
                self.module_selected.emit(key)
                return


class DiagnosticsPage(QWidget):
    """Top-level Diagnostics workspace: Module Map + Codes & Readiness."""

    def __init__(self, dashboard):
        super().__init__()
        self.job = GtJob(dashboard, self)
        self.job.finished.connect(self._on_job_done)
        self._pending = None   # which handler consumes the next result

        lay = QVBoxLayout(self)
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_map_tab(), "Module Map")
        self.tabs.addTab(self._build_codes_tab(), "Codes && Readiness")
        lay.addWidget(self.tabs)

    # -- module map tab -------------------------------------------------------
    def _build_map_tab(self) -> QWidget:
        w = QWidget(); lay = QVBoxLayout(w)
        bar = QHBoxLayout()
        self.scan_btn = QPushButton("🔍 Scan Modules")
        self.scan_btn.clicked.connect(self.scan_modules)
        bar.addWidget(self.scan_btn)
        self.map_status = QLabel("Not scanned. Connect the GT (or just hit "
                                 "Scan — it opens its own link).")
        bar.addWidget(self.map_status, 1)
        lay.addLayout(bar)

        split = QSplitter(Qt.Vertical)
        self.map_view = ModuleMapView()
        self.map_view.module_selected.connect(self._show_module)
        split.addWidget(self.map_view)
        self.map_details = QTextEdit(); self.map_details.setReadOnly(True)
        self.map_details.setPlaceholderText(
            "Scan results and per-module detail appear here. Click a module.")
        split.addWidget(self.map_details)
        split.setSizes([380, 130])
        lay.addWidget(split, 1)
        return w

    def scan_modules(self):
        self.scan_btn.setEnabled(False)
        self.map_status.setText("Scanning the comms pipeline…")
        self._pending = "scan"

        self.job.run(vehnet.scan_pipeline)

    def _apply_scan(self, result, error):
        self.scan_btn.setEnabled(True)
        if error is not None:
            # the port never opened — that IS the pipeline verdict
            result = ScanResult(port_open=False, interface_alive=False,
                                dlc_volts=None, hs_responders=set(),
                                pinged={})
            self.map_status.setText(f"Scan failed at the tool link: {error}")
        v = vehnet.localize(result)
        self.map_view.set_verdict(v)
        if error is None and v.failure_point == "pc_gt:binary_mode":
            self.map_status.setText(
                "⚠ OBDX GT is stuck in binary (J2534) mode — unplug it from "
                "USB and the OBD port for 10 s, then rescan.")
        elif error is None:
            volts = (f"{result.dlc_volts:.1f} V"
                     if result.dlc_volts is not None else "voltage unreadable")
            ok = sum(1 for s in v.modules.values() if s == Status.OK)
            unexamined = sum(1 for s in v.modules.values()
                             if s == Status.NOT_EXAMINED)
            self.map_status.setText(
                f"Scan complete — DLC {volts}, {ok} module(s) responding."
                + (f"  {unexamined} not examined." if unexamined else "")
                + (f"  ⚠ fault at: {v.failure_point}" if v.failure_point
                   else ""))
        self.map_details.setPlainText("\n".join(f"• {n}" for n in v.notes))

    def _show_module(self, key):
        m = vehnet._module(key)
        if not m:
            return
        st = self.map_view._mod(key)
        lines = [f"{m.name}   [{st.value}]",
                 f"Bus: {m.bus}",
                 f"Role: {m.role}"]
        if m.req_id:
            lines.append(f"Diagnostic address: request {m.req_id} / "
                         f"response {m.resp_id}")
        else:
            lines.append("No HS-CAN diagnostic address — reachable only via "
                         "the single-wire bus.")
        self.map_details.setPlainText("\n".join(lines))

    # -- codes tab ------------------------------------------------------------
    def _build_codes_tab(self) -> QWidget:
        w = QWidget(); lay = QVBoxLayout(w)
        bar = QHBoxLayout()
        self.read_btn = QPushButton("📖 Read Codes")
        self.read_btn.clicked.connect(self.read_codes)
        bar.addWidget(self.read_btn)
        self.clear_btn = QPushButton("🧹 Clear Codes…")
        self.clear_btn.clicked.connect(self.clear_codes)
        bar.addWidget(self.clear_btn)
        self.mil_label = QLabel("MIL: —")
        f = self.mil_label.font(); f.setBold(True); self.mil_label.setFont(f)
        bar.addWidget(self.mil_label)
        bar.addStretch(1)
        lay.addLayout(bar)

        split = QSplitter(Qt.Vertical)
        self.dtc_table = QTableWidget(0, 3)
        self.dtc_table.setHorizontalHeaderLabels(["Type", "Code", "Description"])
        self.dtc_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.Stretch)
        self.dtc_table.setEditTriggers(QTableWidget.NoEditTriggers)
        split.addWidget(self.dtc_table)

        self.ready_table = QTableWidget(0, 2)
        self.ready_table.setHorizontalHeaderLabels(["Monitor", "Status"])
        self.ready_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.Stretch)
        self.ready_table.setEditTriggers(QTableWidget.NoEditTriggers)
        split.addWidget(self.ready_table)
        split.setSizes([300, 200])
        lay.addWidget(split, 1)
        return w

    def read_codes(self):
        self.read_btn.setEnabled(False)
        self._pending = "read"
        self.job.run(lambda gt: {"dtcs": gt.read_dtcs(),
                                 "ready": gt.readiness()})

    def clear_codes(self):
        ans = QMessageBox.warning(
            self, "Clear trouble codes",
            "Clear ALL stored and pending DTCs?\n\nThis also resets every "
            "readiness monitor — the truck will need a full drive cycle "
            "before it can pass an emissions check.",
            QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel)
        if ans != QMessageBox.Yes:
            return
        self.clear_btn.setEnabled(False)
        self._pending = "clear"
        self.job.run(lambda gt: {"cleared": gt.clear_dtcs(),
                                 "dtcs": gt.read_dtcs(),
                                 "ready": gt.readiness()})

    def populate_codes(self, dtcs: dict, ready: dict):
        self.dtc_table.setRowCount(0)
        for kind, codes in (("Stored", dtcs.get("stored", [])),
                            ("Pending", dtcs.get("pending", [])),
                            ("Permanent", dtcs.get("permanent", []))):
            for code in codes:
                r = self.dtc_table.rowCount()
                self.dtc_table.insertRow(r)
                self.dtc_table.setItem(r, 0, QTableWidgetItem(kind))
                ci = QTableWidgetItem(code)
                if kind == "Stored":
                    ci.setForeground(QColor(235, 110, 100))
                self.dtc_table.setItem(r, 1, ci)
                self.dtc_table.setItem(
                    r, 2, QTableWidgetItem(DTC_DESCRIPTIONS.get(code, "")))
        if self.dtc_table.rowCount() == 0:
            self.dtc_table.insertRow(0)
            self.dtc_table.setItem(0, 1, QTableWidgetItem("—"))
            self.dtc_table.setItem(0, 2, QTableWidgetItem("No trouble codes"))

        self.ready_table.setRowCount(0)
        for name, complete in ready.get("monitors", []):
            r = self.ready_table.rowCount()
            self.ready_table.insertRow(r)
            self.ready_table.setItem(r, 0, QTableWidgetItem(name))
            si = QTableWidgetItem("Ready" if complete else "Not ready")
            si.setForeground(QColor(96, 190, 120) if complete
                             else QColor(225, 165, 60))
            self.ready_table.setItem(r, 1, si)
        if ready:
            mil = "ON" if ready.get("mil") else "off"
            self.mil_label.setText(
                f"MIL: {mil}   ·   {ready.get('dtc_count', 0)} code(s)")
            self.mil_label.setStyleSheet(
                "color:#e06c60;" if ready.get("mil") else "color:#60be78;")

    # -- shared result dispatch ----------------------------------------------
    def _on_job_done(self, result, error):
        pending, self._pending = self._pending, None
        if pending == "scan":
            self._apply_scan(result, error)
            return
        self.read_btn.setEnabled(True)
        self.clear_btn.setEnabled(True)
        if error is not None:
            QMessageBox.warning(self, "GT link",
                                f"Couldn't reach the truck: {error}")
            return
        if pending == "clear" and not result.get("cleared"):
            QMessageBox.warning(self, "Clear codes",
                                "The ECU did not acknowledge the clear "
                                "request (no 44 response).")
        self.populate_codes(result.get("dtcs", {}), result.get("ready", {}))
