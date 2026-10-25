"""
OpenOBD GUI — HP-Tuners-style calibration viewer/editor + log overlay +
live/replay dashboard, for truck-mcp.

Tabs
----
  Editor      : tree of tables (by category) -> editable heatmapped grid with
                stock diff + optional log overlay (operation-count / mean-value).
  Scalars     : editable list of single-value parameters with stock diff.
  Log         : load a VCM Scanner CSV / plain CSV, see the regime report, and
                push a table overlay from any two channels.
  Dashboard   : gauges fed by a DataSource — CSV log replay, the OBDX Pro GT
                live link, or a truck-mcp drive-log session (tmsource): live
                sessions follow the store at ~1 Hz with tmstore's freshness
                states on every gauge; archived drives replay through the
                transport bar. Store access is read-only, never the serial port.
  Live Data   : session picker + a combined raw view off truck-mcp's drive-log
                store (sessions/*.tmsession.db), never the serial port: compact
                live tiles above a Chart vs. Time strip chart, read together.
  Active Tests: display-only vehicle-control state from truck-mcp's control
                journal — empty executable registry, outstanding-activation
                warnings. Commands nothing.

Settings → Preferences houses the dashboard-visual selector (HP Tuners
classic dial/bar cluster, or the modern dual-dial cluster); the choice is
persisted via QSettings and applies without rebinding the data source.

Run:  python -m openobd.app  [optional path to a .cal.json]
"""
from __future__ import annotations

import csv
import os
import sys
import threading
from typing import Optional

from PySide6.QtCore import Qt, QTimer, QEvent, Signal
from PySide6.QtGui import (
    QAction, QColor, QIcon, QKeySequence, QPalette, QShortcut, QFont,
    QUndoCommand, QUndoStack,
)
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QTreeWidget, QTreeWidgetItem, QTableView, QTabWidget, QSplitter, QLabel,
    QComboBox, QCheckBox, QPushButton, QFileDialog, QTextEdit, QTableWidget,
    QTableWidgetItem, QHeaderView, QMessageBox, QGroupBox, QLineEdit,
    QSlider, QListWidget, QListWidgetItem, QMenu, QDialog, QDialogButtonBox,
    QFormLayout, QSizePolicy, QToolTip,
)

import numpy as np
import pyqtgraph as pg

from . import __version__, editops
from .appsettings import app_settings
from .calspec import Calibration
from .diagui import DiagnosticsPage
from .livedata import ActiveTestsPage, LiveDataPage
from .logbin import (
    Log, Overlay, parse_csv, analyze_log, bin_log_to_table, detect_shift_points,
    CANONICAL, time_axis, attach_long, calibrate_pedal_from_log,
)
from . import pollplan
from .pedal import PedalCal

pg.setConfigOption("background", (28, 30, 34))
pg.setConfigOption("foreground", (200, 203, 208))
pg.setConfigOptions(antialias=True)
from .model import CalTableModel, HeatmapDelegate
from .transport import DataSource, LogReplaySource, GtDataSource, Sample
from . import tmsource, tmstore
from .recorder import (
    DriveRecorder, default_log_dirs, meta_path, read_meta, sidecar_path,
)
from PySide6.QtCore import QStandardPaths

# Gauge widgets, cluster layouts and the dashboard style registry live in
# gauges.py; the names are re-exported here for existing importers.
from .gauges import (  # noqa: E402
    DASHBOARD_STYLES, GaugeBase, build_classic_cluster, build_modern_cluster,
)


# Column labels for recorded datalogs. Chosen so logbin.map_channel maps them
# back to the same canonical key when the CSV is reloaded in Log Analysis.
RECORD_LABELS = {
    "rpm": "Engine Speed (RPM)",
    "vss": "Vehicle Speed (mph)",
    "map": "Manifold Absolute Pressure (kPa)",
    "maf": "Mass Air Flow (g/s)",
    "load": "Engine Load (%)",
    "tps": "Throttle Position (%)",
    "app": "Accelerator Pedal (%)",
    "cmd_throttle": "Commanded Throttle (%)",
    "spark": "Spark Advance (deg)",
    "ect": "Engine Coolant Temp (F)",
    "iat": "Intake Air Temp (F)",
    "stft": "Short Term Fuel Trim (%)",
    "ltft": "Long Term Fuel Trim (%)",
    "eq_ratio": "Commanded Equivalence Ratio",
    "fuel_press": "Fuel Pressure (psi)",
    "fuel_level": "Fuel Level (%)",
    "voltage": "Module Voltage (V)",
    "baro": "Barometric Pressure (kPa)",
    "ambient": "Ambient Air Temp (F)",
    # PID 52 is an ECM ESTIMATE (no flex-fuel sensor): labelled as such. Old
    # recordings' "Ethanol (%)" still reloads as ethanol (logbin.CANONICAL).
    "ethanol": "Ethanol, ECM estimate (%)",
    # DERIVED from APP D with the learned pedal calibration (openobd.pedal);
    # the raw "app" column is always recorded beside it.
    "pedal": "Pedal, normalized (%)",
    # Every key the GT can produce needs a column, or it is silently dropped
    # from the log (tft and run_time were). run_time deliberately avoids the
    # word "time": map_channel would read "Run Time" as the time axis.
    # tft has no verified GT source since v0.32.1 (gt.REJECTED_DIDS), so the
    # recorder writes no such column; the label stays for when a verified
    # source (gt.py follow-up: TCM DID 1940) is added to gt.DID_TABLE.
    "tft": "Trans Fluid Temp (F)",
    "run_time": "Engine Run Duration (s)",
    # v0.33.0 channels (batched polling made room for them).
    "abs_load": "Absolute Load (%)",
    "rel_tps": "Relative Throttle Position (%)",
    "app_e": "Accelerator Pedal E (%)",
    "app_f": "Accelerator Pedal F (%)",
    "stft_b2": "STFT Bank 2 (%)",
    "ltft_b2": "LTFT Bank 2 (%)",
    "fuel_system": "Fuel System Status",
    "o2_b1s1": "O2 Voltage B1S1 (V)",
    "o2_b1s2": "O2 Voltage B1S2 (V)",
    "o2_b2s1": "O2 Voltage B2S1 (V)",
    "o2_b2s2": "O2 Voltage B2S2 (V)",
    "cat_b1s1": "Catalyst Temp B1S1 (F)",
    "cat_b2s1": "Catalyst Temp B2S1 (F)",
}
# Column order. A recording only gets the keys in here that its source can
# produce (Dashboard.toggle_record filters by recordable_channels()), so
# "tft" holds its place for a future verified source but yields no column
# on the GT today (tests/test_gt_did_table.py checks that).
RECORD_ORDER = ["rpm", "vss", "map", "maf", "load", "abs_load", "tps",
                "rel_tps", "app", "pedal", "app_e", "app_f", "cmd_throttle",
                "spark",
                "ect", "iat", "tft", "stft", "ltft", "stft_b2", "ltft_b2",
                "fuel_system", "o2_b1s1", "o2_b1s2", "o2_b2s1", "o2_b2s2",
                "eq_ratio", "fuel_press", "fuel_level", "voltage", "baro",
                "ambient", "ethanol", "cat_b1s1", "cat_b2s1", "run_time"]


#: Notes written into a recording's metadata for the channels it carries.
CHANNEL_NOTES = {
    "ethanol": ("PID 52 is the ECM's ethanol ESTIMATE (this truck has no "
                "flex-fuel sensor): it read 0 then 0-11.4 % noise on the "
                "2026-10-25 drive. Recorded as the ECM reports it."),
    "app": ("PID 49 APP D, the pedal sensor's position as decoded (about 19 % "
            "at rest and 61 % flat out on this truck), not driver demand."),
    "pedal": ("DERIVED, not requested: clamp((APP D - rest) / (full - rest) "
              "* 100, 0, 100) with the pedal calibration in force when the "
              "sample was taken (pedal_calibration). Blank while the "
              "calibration is pending. In the .long.csv its lines carry the "
              "APP D reading's time and 'derived:app' in the Request column."),
}


def _pedal_settings_group(vin: str) -> str:
    """Settings group for a VIN. Calibrations are stored ONLY under an
    examined VIN: without one there is no way to know which vehicle a value
    belongs to, so it stays session-only (never a shared fallback key)."""
    if not vin or not vin.isalnum():
        raise ValueError(f"not a VIN: {vin!r}")
    return f"pedal_cal/{vin}"


def load_pedal_cal(vin: str) -> Optional[PedalCal]:
    """The stored pedal calibration for a VIN, or None."""
    st = app_settings()
    st.beginGroup(_pedal_settings_group(vin))
    cal = PedalCal.from_dict({"rest": st.value("rest"),
                              "full": st.value("full"),
                              "source": st.value("source")})
    st.endGroup()
    return None if cal.rest is None and cal.full is None else cal


def save_pedal_cal(vin: str, cal: PedalCal) -> None:
    st = app_settings()
    st.beginGroup(_pedal_settings_group(vin))
    for name, val in (("rest", cal.rest), ("full", cal.full),
                      ("source", cal.source)):
        if val is None:
            st.remove(name)
        else:
            st.setValue(name, val)
    st.endGroup()
    st.sync()


def poll_cost_model() -> dict:
    """The cost model the poll plan's predictions come from."""
    return {"request_cost_points_s": [list(p) for p in
                                      pollplan.REQUEST_COST_POINTS],
            "atrv_cost_s": pollplan.ATRV_COST_S,
            "basis": "truck-mcp cassette no-accel-1000rpm 2026-09-18 "
                     "(1 PID 55.7 ms, 3 PIDs 55.0 ms, 6 PIDs 79.9 ms); "
                     "predictions only, nothing waits on them"}


class Dashboard(QWidget):
    # GT connect runs on a worker thread (serial open + first poll can take
    # seconds); these marshal the result back onto the GUI thread.
    gt_ready = Signal(object)
    gt_error = Signal(str)
    # The recording-start vehicle read (identity + DTCs, poll thread held)
    # runs on a worker thread too: (recorder, context dict).
    record_ctx_ready = Signal(object)

    def __init__(self, style: str = "classic"):
        super().__init__()
        self.style = style if style in DASHBOARD_STYLES else "classic"
        self.source: Optional[DataSource] = None
        self.gauges: dict[str, GaugeBase] = {}
        self.recording = False
        self._recorder: Optional[DriveRecorder] = None
        self.last_record_path: Optional[str] = None
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.gt_ready.connect(self._on_gt_ready)
        self.gt_error.connect(self._on_gt_error)
        self.record_ctx_ready.connect(self._on_record_context)
        # The VIN the GT's pedal calibration persists under, read on THIS
        # connection (GtDataSource.link). None = not identified: the
        # calibration is then session-only and nothing is loaded or saved.
        self._pedal_key: Optional[str] = None
        self._pedal_seen = -1
        self._link_seen = None

        root = QVBoxLayout(self)
        bar = QHBoxLayout()
        self.status = QLabel("No source. Load a log, then Start replay.")
        self.btn_start = QPushButton("▶ Start")
        self.btn_stop = QPushButton("⏹ Stop")
        self.btn_start.clicked.connect(self.start)
        self.btn_stop.clicked.connect(self.stop)
        self.btn_stop.setEnabled(False)
        self.btn_connect = QPushButton("🔌 Connect GT Pro")
        self.btn_connect.clicked.connect(self.connect_gt)
        self.btn_tm = QPushButton("🗄 Truck Session")
        self.btn_tm.clicked.connect(self._pick_tm_session)
        self.btn_record = QPushButton("⏺ Record")
        self.btn_record.clicked.connect(self.toggle_record)
        self.btn_mark = QPushButton("📍 Mark")
        self.btn_mark.clicked.connect(self.add_marker)
        self.btn_mark.setEnabled(False)
        bar.addWidget(self.status, 1)
        bar.addWidget(self.btn_connect)
        bar.addWidget(self.btn_tm)
        bar.addWidget(self.btn_record)
        bar.addWidget(self.btn_mark)
        bar.addWidget(self.btn_start)
        bar.addWidget(self.btn_stop)
        root.addLayout(bar)

        # replay transport controls (hidden for live sources)
        self.replay_ctl = QWidget()
        rb = QHBoxLayout(self.replay_ctl)
        rb.setContentsMargins(0, 0, 0, 0)
        self.btn_pause = QPushButton("⏸ Pause")
        self.btn_pause.clicked.connect(self._toggle_pause)
        rb.addWidget(self.btn_pause)
        rb.addWidget(QLabel("Speed:"))
        self.speed_combo = QComboBox()
        self.speed_combo.addItems(["0.5×", "1×", "2×", "4×", "8×"])
        self.speed_combo.setCurrentText("4×")
        self.speed_combo.currentTextChanged.connect(self._on_speed)
        rb.addWidget(self.speed_combo)
        self.pos_slider = QSlider(Qt.Horizontal)
        self.pos_slider.sliderReleased.connect(self._on_seek)
        self.pos_slider.sliderMoved.connect(
            lambda v: self.time_label.setText(self._fmt_pos(v / 10.0)))
        rb.addWidget(self.pos_slider, 1)
        self.time_label = QLabel("0.0 / 0.0s")
        rb.addWidget(self.time_label)
        self.replay_ctl.setVisible(False)
        root.addWidget(self.replay_ctl)

        self.cluster_host = QWidget()
        self._cluster_lay = QVBoxLayout(self.cluster_host)
        self._cluster_lay.setContentsMargins(0, 0, 0, 0)
        self._cluster: Optional[QWidget] = None
        root.addWidget(self.cluster_host, 1)

        # The full cluster is visible from the start:
        # every gauge shows "—" / a parked needle until data arrives.
        self._rebuild_cluster()
        self._init_live_ui()

    # -- dashboard visuals (Settings → Preferences) --------------------------- #
    def set_style(self, style: str):
        """Switch the dashboard visual. The bound source stays bound: the new
        cluster picks values back up from latest() on the next tick, and
        freshness states are re-applied there too."""
        if style not in DASHBOARD_STYLES or style == self.style:
            return
        self.style = style
        self._rebuild_cluster()

    def _rebuild_cluster(self):
        if self._cluster is not None:
            self._cluster_lay.removeWidget(self._cluster)
            self._cluster.deleteLater()
        self.gauges.clear()
        build = (self._build_modern_cluster if self.style == "modern"
                 else self._build_classic_cluster)
        self._cluster = build()
        self._cluster_lay.addWidget(self._cluster)
        if self.source is not None:
            avail = set(self.source.channels())
            for key, g in self.gauges.items():
                g.set_available(key in avail)

    def _build_classic_cluster(self) -> QWidget:
        return build_classic_cluster(self.gauges)

    def _build_modern_cluster(self) -> QWidget:
        return build_modern_cluster(self.gauges)

    # -- gauge feed: per-channel latest for the tiered GT ------------------- #
    def _gauge_states(self, s: Optional[Sample], states: Optional[dict]):
        """The GT polls in tiers: a cycle's Sample holds only the channels
        due in it, so drawing gauges from the newest Sample would blank every
        MEDIUM/SLOW gauge three cycles in four. For the GT the gauges read
        the source's per-channel LAST READING instead, with the source's own
        per-channel freshness (fresh within twice the tier period, then
        stale; failed while polls fail). truck-mcp sources already classify
        per channel through tmstore and replays have no live clock, so both
        pass through untouched."""
        if not isinstance(self.source, GtDataSource):
            self._derived_values = None
            return states
        values = self.source.channel_values()
        states = states or {}
        keys = set(self.gauges) | {"pedal"}
        self._derived_values = {
            k: (None if states.get(k) in (None, "failed") else values.get(k))
            for k in keys}
        return {k: states.get(k) for k in keys}

    def _gauge_value(self, key: str, s: Sample) -> Optional[float]:
        derived = getattr(self, "_derived_values", None)
        if derived is not None:
            return derived.get(key)
        return s.values.get(key) if s is not None else None

    def _pedal_feed(self, s: Optional[Sample]) -> dict:
        """Which channel the Pedal gauge (key "app") shows: the normalized
        "pedal" when the source has a value for it, else raw APP D with the
        label suffix "raw". Never a value from a pending calibration: the
        source only produces "pedal" from a ready one."""
        g = self.gauges.get("app")
        if g is None:
            return {}
        normalized = self._gauge_value("pedal", s) is not None
        g.set_label_suffix(None if normalized else "raw")
        if normalized:
            g.setToolTip("Pedal, normalized from APP D with the learned "
                         "calibration — click to reset min/max capture")
        else:
            why = ""
            if isinstance(self.source, GtDataSource):
                why = " · " + self.source.pedal_calibration().status()
            g.setToolTip("Pedal raw: APP D as decoded (about 19 % at rest on "
                         f"this truck){why} — click to reset min/max capture")
        return {"app": "pedal"} if normalized else {}

    def _sync_link(self) -> None:
        """GUI thread: follow the GT's connection. On every new link (start,
        re-open) the source has already reset its learner; here the key is
        re-derived from THAT link's VIN and, only for an examined VIN, the
        stored calibration is seeded. A recording notes the change."""
        src = self.source
        if not isinstance(src, GtDataSource):
            return
        link = src.link
        if link[0] == self._link_seen:
            return
        first = self._link_seen is None
        self._link_seen = link[0]
        self._pedal_key = src.vin()
        self._pedal_seen = src.pedal.version
        if self._pedal_key:
            try:
                stored = load_pedal_cal(self._pedal_key)
            except Exception:
                stored = None
            if stored is not None:
                src.seed_pedal_calibration(stored)
        rec = self._recorder
        if not first and self.recording and rec is not None:
            events = rec.meta.setdefault("link_events", [])
            events.append({"t": rec.recording_time(src.clock()),
                           "event": "GT link re-opened; pedal calibration "
                                    "reset, VIN re-read",
                           "vin": link[1]})
            pc = rec.meta.setdefault("pedal_calibration", {})
            pc["persistence"] = self._persistence()
            rec.update_meta()

    def _persistence(self) -> str:
        return (f"settings pedal_cal/{self._pedal_key}" if self._pedal_key
                else "session-only (VIN not examined on this connection)")

    def _persist_pedal(self) -> None:
        """GUI thread: when the GT's calibration changed, store it under the
        connection's examined VIN (never without one) and write the change
        into an active recording's metadata at once (atomic rewrite)."""
        src = self.source
        if not isinstance(src, GtDataSource):
            return
        self._sync_link()
        ver = src.pedal.version
        if ver == self._pedal_seen:
            return
        self._pedal_seen = ver
        cal = src.pedal_calibration()
        if cal.rest is None and cal.full is None:
            return
        if self._pedal_key:
            try:
                save_pedal_cal(self._pedal_key, cal)
            except Exception as exc:       # settings store unavailable
                self.status.setText(
                    f"Could not store the pedal calibration: {exc}")
        rec = self._recorder
        if self.recording and rec is not None:
            entry = {k: v for k, v in cal.to_dict().items() if k != "rules"}
            entry["t"] = rec.recording_time(src.clock())
            pc = rec.meta.setdefault("pedal_calibration", {})
            pc.setdefault("changes", []).append(entry)
            pc["persistence"] = self._persistence()
            rec.update_meta()

    # -- responsive toolbar + keyboard shortcuts for the live actions -------- #
    def _init_live_ui(self):
        # The status line carries long messages (connect errors, excluded
        # channels). Unwrapped, its text width became the Dashboard's minimum
        # width; wrapped, it grows downward and the window can narrow.
        self.status.setWordWrap(True)
        self.status.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.status.setMinimumWidth(160)
        self._install_shortcuts()

    def _install_shortcuts(self):
        """Window-wide shortcuts owned by the Dashboard: Qt only fires them
        while the Dashboard is visible, so they don't leak into other tabs.
        Each goes through the button's click(), which is a no-op while the
        button is disabled (e.g. Connect during a connect attempt).

        The key hint is appended when the tooltip is SHOWN (eventFilter), not
        baked into the tooltip text: the recorder rewrites btn_record's
        tooltip with the current/last log path, and the hint must survive
        that."""
        bindings = [
            (self.btn_connect, "Ctrl+G", "Connect the OBDX Pro GT"),
            (self.btn_tm, "Ctrl+T", "Bind a truck-mcp drive-log session"),
            (self.btn_record, "Ctrl+R", "Start / stop recording a datalog"),
            (self.btn_mark, "M", "Drop a marker into the recording"),
            (self.btn_start, "F5", "Start the bound source"),
            (self.btn_stop, "Shift+F5", "Stop the bound source"),
            (self.btn_pause, "P", "Pause / resume the replay"),
        ]
        self._shortcuts = []
        self._shortcut_hints = {}
        for btn, keys, what in bindings:
            seq = QKeySequence(keys)
            sc = QShortcut(seq, self)
            sc.setContext(Qt.WindowShortcut)
            sc.activated.connect(btn.click)
            self._shortcut_hints[btn] = (
                what, seq.toString(QKeySequence.NativeText))
            btn.installEventFilter(self)
            self._shortcuts.append(sc)

    def shortcut_tooltip(self, btn) -> str:
        """The tooltip a button shows: its current tooltip text (or the
        action description when none is set) plus the shortcut key."""
        what, key = self._shortcut_hints[btn]
        return f"{btn.toolTip() or what} ({key})"

    def eventFilter(self, obj, event):
        if (event.type() == QEvent.ToolTip
                and obj in getattr(self, "_shortcut_hints", {})):
            QToolTip.showText(event.globalPos(), self.shortcut_tooltip(obj),
                              obj)
            return True
        return super().eventFilter(obj, event)

    def _teardown_source(self):
        """Stop and drop the current source. Always called before a new one is
        bound so a live GT poll thread can't keep running invisibly."""
        if self.recording:
            self._finalize_record()
        self.timer.stop()
        if self.source:
            try:
                self.source.stop()
            except Exception:
                pass
        self._demote_live_badges()
        self.source = None
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)

    def bind_source(self, source: DataSource):
        self._teardown_source()
        self.source = source
        avail = set(source.channels())
        # The cluster is permanent — reset every gauge and dim the ones this
        # source can't feed instead of rebuilding a filtered grid.
        for key, g in self.gauges.items():
            g.reset()
            g.set_available(key in avail)
        n = sum(1 for k in self.gauges if k in avail)
        self.status.setText(f"Source bound: {n} of {len(self.gauges)} gauges live.")

        # transport controls only make sense for a seekable replay
        is_replay = hasattr(source, "seek")
        self.replay_ctl.setVisible(is_replay)
        if is_replay:
            dur = source.duration()
            self.pos_slider.setRange(0, int(dur * 10))
            self.btn_pause.setText("⏸ Pause")
            self.speed_combo.blockSignals(True)
            self.speed_combo.setCurrentText(f"{source.speed:g}×")
            self.speed_combo.blockSignals(False)
            self.time_label.setText(self._fmt_pos(0.0))

    # -- replay transport ---------------------------------------------------- #
    def _fmt_pos(self, pos: float) -> str:
        dur = self.source.duration() if hasattr(self.source, "duration") else 0.0
        return f"{pos:.1f} / {dur:.1f}s"

    def _toggle_pause(self):
        if not (self.source and hasattr(self.source, "pause")):
            return
        if self.source.playing:
            self.source.pause()
            self.btn_pause.setText("▶ Resume")
        else:
            self.source.resume()
            self.btn_pause.setText("⏸ Pause")

    def _on_speed(self, text: str):
        if self.source and hasattr(self.source, "set_speed"):
            try:
                self.source.set_speed(float(text.rstrip("×")))
            except ValueError:
                pass

    def _on_seek(self):
        if self.source and hasattr(self.source, "seek"):
            self.source.seek(self.pos_slider.value() / 10.0)

    def start(self):
        if not self.source:
            self.status.setText("Load a log first (Log tab → Load Log).")
            return
        try:
            # A restart can fail for real reasons (a stopped tm source reopens
            # its session file, which may have been rotated away) — a button
            # slot must report that, not raise through Qt.
            self.source.start()
        except Exception as exc:
            self.status.setText(f"Start failed: {exc}")
            return
        self.timer.start(100)
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.status.setText("Replaying…")

    def stop(self):
        if self.recording:
            self._finalize_record()
        self.timer.stop()
        if self.source:
            try:
                self.source.stop()
            except Exception:
                pass
        self._demote_live_badges()
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.status.setText("Stopped.")

    def _demote_live_badges(self):
        """A stopped source is no longer advancing: any gauge still showing a
        live-styled value must say so, not keep rendering as a live needle.
        Applies to EVERY source — a frozen GT needle after a disconnect is the
        same lie as a frozen store value."""
        if self.source is not None:
            for g in self.gauges.values():
                if (g.available and g.value is not None
                        and g.state in (None, "fresh")):
                    g.set_state("stale")

    def connect_gt(self):
        """Connect the OBDX Pro GT on a worker thread (serial IO blocks)."""
        src = self.source
        if isinstance(src, GtDataSource) and src._thread is not None:
            # Already connected: a second open of the same COM port can only
            # fail ("Access is denied") — and a successful rebind would end an
            # active recording. A down link is re-opened by the poll thread;
            # pressing Connect just skips its backoff wait.
            if src.failing():
                src.request_reconnect()
                self.status.setText("Reconnecting to the OBDX Pro GT…")
            else:
                self.status.setText(
                    f"Already connected to the GT @ {src.port_name or '?'}.")
            return
        self.status.setText("Connecting to OBDX Pro GT…")
        self.btn_connect.setEnabled(False)

        def worker():
            try:
                src = GtDataSource()
                src.start()
            except Exception as e:
                try:
                    self.gt_error.emit(str(e))
                except RuntimeError:
                    pass  # widget destroyed while connecting
                return
            try:
                self.gt_ready.emit(src)
            except RuntimeError:
                src.stop()

        threading.Thread(target=worker, name="gt-connect", daemon=True).start()

    def _on_gt_ready(self, src):
        self.bind_source(src)          # tears down the old source first
        self.timer.start(100)
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.btn_connect.setEnabled(True)
        dev = getattr(src, "device", "OBDX Pro GT")
        self._live_status = (
            f"{dev} live — {len(self.gauges)} gauges @ {getattr(src, 'port_name', '')}")
        plan = getattr(src, "plan_text", None)
        if plan:
            # The poll plan, once per connect: tiers, requests per cycle and
            # the predicted cycle time this link should reach.
            self._live_status += f" · {plan}"
        self._pedal_key = None
        self._pedal_seen = -1
        self._link_seen = None
        self._sync_link()            # VIN read at connect -> seed if stored
        if isinstance(src, GtDataSource):
            vin = src.vin()
            self._live_status += (f" · VIN {vin}" if vin else
                                  " · VIN not read (pedal calibration is "
                                  "session-only)")
            cal = src.pedal_calibration()
            if not cal.ready:
                self._live_status += (" · pedal cal pending (Pedal gauge "
                                      "shows raw APP D)")
        self._source_alarm = False
        self.status.setText(self._live_status)

    def _on_gt_error(self, msg):
        self.btn_connect.setEnabled(True)
        self.status.setText(f"GT connect failed: {msg}")

    # -- truck-mcp drive-log sessions as a gauge source ---------------------- #
    def _pick_tm_session(self):
        """Menu of truck-mcp sessions, listed fresh on every click. Read-only
        store access — binding a session never touches the serial port."""
        try:
            sessions = tmstore.list_sessions()
        except OSError as exc:
            self.status.setText(f"Could not scan truck-mcp sessions: {exc}")
            return
        menu = QMenu(self)
        if not sessions:
            act = menu.addAction("No drive-log sessions found")
            act.setEnabled(False)
        for s in sessions[:25]:
            tag = tmstore.session_tag(s)
            label = s.get("label") or s.get("name")
            started = (s.get("started_utc") or "")[:19].replace("T", " ")
            act = menu.addAction(
                f"{tag}{label}  ·  {started}  ·  {s.get('channel_count', 0)} ch")
            if s.get("error"):
                act.setEnabled(False)
                act.setText(act.text() + "  [unreadable]")
            else:
                act.triggered.connect(
                    lambda _=False, entry=s: self._bind_tm_session(entry))
        menu.exec(self.btn_tm.mapToGlobal(self.btn_tm.rect().bottomLeft()))
        menu.deleteLater()   # parented to self — would otherwise leak per click

    def _bind_tm_session(self, entry: dict):
        path = entry["path"]
        name = entry.get("label") or entry.get("name")
        # Stop the paint timer before any teardown/modal work — _tick must not
        # keep polling a source that is mid-teardown behind a dialog. Remember
        # whether it was running: a FAILED bind leaves the old source bound and
        # must resume it, not strand a live view (or an active recording) with
        # a dead timer.
        was_active = self.timer.isActive()
        self.timer.stop()
        try:
            if entry.get("live"):
                src = tmsource.TmLiveSource(path)
                excluded = src.excluded
                self.bind_source(src)
                src.start()
                self.timer.start(100)
                self.btn_start.setEnabled(False)
                self.btn_stop.setEnabled(True)
                note = "live follow — store poll ~1 Hz"
            else:
                log, resolved = tmsource.build_replay_log(path)
                excluded = resolved.excluded
                self.bind_source(tmsource.TmReplaySource(log, speed=1.0))
                note = "archived — press ▶ Start to replay"
        except Exception as exc:
            if was_active and self.source is not None:
                self.timer.start(100)   # old source untouched — keep it live
            self.status.setText(f"Could not bind session: {exc}")
            return
        # Provenance rides on the source, so it is replaced with the source.
        self.source.provenance = entry.get("provenance") or (
            tmstore.PROVENANCE_SIMULATED if entry.get("simulated")
            else tmstore.PROVENANCE_REAL)
        if self.source.provenance == tmstore.PROVENANCE_SIMULATED:
            note = "SIMULATED demo data, not the truck — " + note
        elif self.source.provenance == tmstore.PROVENANCE_UNKNOWN:
            note = "PROVENANCE UNKNOWN, may not be the truck — " + note
        n = sum(1 for k in self.gauges if k in set(self.source.channels()))
        skip = ("  ·  excluded: " + ", ".join(
            f"{nm} ({why})" for nm, why in excluded)) if excluded else ""
        self.status.setText(
            f"🗄 {name} — {n} of {len(self.gauges)} gauges ({note}){skip}")

    def toggle_record(self):
        if not self.source:
            self.status.setText("Connect the GT (or load a log) before recording.")
            return
        if not self.recording and getattr(self.source, "is_store_record", False):
            # A truck-mcp session IS already a record, with per-sample
            # freshness/quality this CSV cannot carry — re-recording it would
            # persist carried/archived values as if they were measurements,
            # decimated to the poll rate on the observer's clock.
            self.status.setText(tmsource.record_refusal_message(
                live=isinstance(self.source, tmsource.TmLiveSource),
                provenance=getattr(self.source, "provenance",
                                   tmstore.PROVENANCE_REAL),
                connect_label=self.btn_connect.text()))
            return
        if not self.recording:
            # Columns = everything the source can EVER produce, not just what
            # answered so far, so a channel that comes alive mid-drive is kept.
            avail = set(self.source.recordable_channels())
            keys = [k for k in RECORD_ORDER if k in avail]
            try:
                # The final file, in a durable folder, from the first sample:
                # there is no temp file to move and no save dialog to cancel.
                self._recorder = DriveRecorder(
                    keys, RECORD_LABELS, self._record_dirs(),
                    meta=self._recording_meta(keys))
            except Exception as e:
                self.status.setText(f"Can't start recording: {e}")
                return
            self.last_record_path = self._recorder.path
            self.source.drain()  # discard the pre-record backlog
            self.recording = True
            self.btn_mark.setEnabled(True)
            self.btn_record.setText("⏺ REC 0 samples (click to stop)")
            self.btn_record.setToolTip(f"Recording to {self._recorder.path}")
            self.status.setText(f"Recording → {self._recorder.path}")
            if isinstance(self.source, GtDataSource):
                self._read_record_context(self.source, self._recorder)
        else:
            self._finalize_record(interactive=True)

    #: Placeholder for vehicle facts while the recording-start read runs.
    _CTX_PENDING = {"examined": False,
                    "why": "being read at recording start (poll held)"}

    def _recording_meta(self, keys: list[str]) -> dict:
        """What is known synchronously at recording start. Vehicle facts
        that need the bus (identity, DTCs) arrive from
        _read_record_context and are rewritten into the file."""
        src = self.source
        notes = {k: v for k, v in CHANNEL_NOTES.items() if k in keys}
        derived = {k: {"from": f, "label": RECORD_LABELS[k],
                       "long_csv": "own line form: source reading's time, "
                                   "Request 'derived:" + f + "'"}
                   for k, f in getattr(src, "DERIVED", {}).items()
                   if k in keys}
        if isinstance(src, GtDataSource):
            cal = src.pedal_calibration()
            gt = src._gt
            supported = (gt.supported_report()
                         if hasattr(gt, "supported_report") else
                         dict(self._CTX_PENDING))
            return {
                "source": {"kind": "gt", "device": src.device,
                           "port": src.port_name,
                           "plan": src.plan_summary(),
                           "plan_text": src.plan_text,
                           "poll_cost_model": poll_cost_model()},
                "vehicle": dict(self._CTX_PENDING),
                "supported_pids": supported,
                "dtcs_at_start": dict(self._CTX_PENDING),
                "pedal_calibration": {"at_start": cal.to_dict(),
                                      "vin": src.link[1],
                                      "persistence": self._persistence(),
                                      "changes": []},
                "channel_notes": notes,
                "derived_channels": derived,
            }
        kind = type(src).__name__
        why = f"the source is a {kind}, not a vehicle link"
        log = getattr(src, "log", None)
        return {
            "source": {"kind": kind,
                       "log": getattr(log, "source", None) if log else None},
            "vehicle": {"examined": False, "why": why},
            "supported_pids": {"examined": False, "why": why},
            "dtcs_at_start": {"examined": False, "why": why},
            "pedal_calibration": {"at_start": None, "why": why},
            "channel_notes": notes,
            "derived_channels": derived,
        }

    def _read_record_context(self, src: GtDataSource,
                             rec: DriveRecorder) -> None:
        """Identity + DTCs at recording start, off the GUI thread (serial IO
        can take seconds; the poll thread is held for it, so no samples are
        produced meanwhile and none are lost)."""
        def worker():
            try:
                ctx = src.read_recording_context()
            except Exception as exc:
                why = f"{type(exc).__name__}: {exc}"
                ctx = {"identity": {"examined": False, "why": why},
                       "dtcs_at_start": {"examined": False, "error": why}}
            try:
                self.record_ctx_ready.emit((src, rec, ctx))
            except RuntimeError:
                pass   # widget destroyed meanwhile; the file stays valid

        threading.Thread(target=worker, name="gt-record-ctx",
                         daemon=True).start()

    def _on_record_context(self, payload) -> None:
        src, rec, ctx = payload
        ident = ctx.get("identity") or {}
        vin_entry = ident.get("vin") or {}
        vin = vin_entry.get("value") if vin_entry.get("examined") else None
        current = (src is self.source and isinstance(src, GtDataSource)
                   and ctx.get("epoch") == src.link[0])
        if current and vin and not self._pedal_key:
            # The VIN did not read at connect but did now, on the SAME link:
            # this connection is identified from here on.
            self._pedal_key = vin
            try:
                stored = load_pedal_cal(vin)
            except Exception:
                stored = None
            if stored is not None:
                src.seed_pedal_calibration(stored)
        fields = {"vehicle": ident,
                  "dtcs_at_start": ctx.get("dtcs_at_start")}
        if ctx.get("supported"):
            fields["supported_pids"] = ctx["supported"]
        pc = dict(rec.meta.get("pedal_calibration") or {})
        if current:
            pc["vin"] = vin_entry
            pc["persistence"] = self._persistence()
            pc["after_identity"] = src.pedal_calibration().to_dict()
        fields["pedal_calibration"] = pc
        if rec.meta_path and os.path.exists(rec.meta_path):
            rec.update_meta(**fields)
        if rec is self._recorder:
            who = (f"VIN {vin}" if vin else "VIN not read: "
                   + str((ident.get("vin") or ident).get("why", "unknown")))
            self.status.setText(f"Recording → {rec.path} · {who}")

    def add_marker(self):
        """Drop a marker at the current recording time (M). Only the
        metadata changes; the CSVs are untouched."""
        rec = self._recorder
        if not self.recording or rec is None:
            self.status.setText("Markers are added while recording.")
            return
        self._record_samples(self.source.drain())   # date it after them
        clock = getattr(self.source, "clock", None)
        mark = rec.add_marker(clock() if clock else None)
        if mark is None:
            self.status.setText("No sample recorded yet — marker not added.")
            return
        msg = f"📍 {mark['label']} at {mark['t']:.1f} s"
        if rec.meta_error:
            msg += f" (metadata write failed: {rec.meta_error})"
        self.status.setText(msg)

    def _record_dirs(self) -> list[str]:
        """Where recordings go: Documents\\OpenOBD\\Logs, resolved through
        Qt's known-folder lookup (follows a OneDrive-redirected Documents,
        which ~/Documents does not), else %LOCALAPPDATA%\\OpenOBD\\Logs."""
        docs = QStandardPaths.writableLocation(QStandardPaths.DocumentsLocation)
        data = QStandardPaths.writableLocation(QStandardPaths.GenericDataLocation)
        return default_log_dirs(docs or None,
                                os.path.join(data, "OpenOBD") if data else None)

    def _finalize_record(self, interactive: bool = False):
        """Close the recording. Every caller gets the same durable result; only
        a user-initiated stop (interactive) offers to open it in Log Analysis —
        teardown, rebind and app close must never block on a dialog."""
        self.recording = False
        self.btn_record.setText("⏺ Record")
        self.btn_mark.setEnabled(False)
        rec, self._recorder = self._recorder, None
        if rec is None:
            return
        for key in ("vehicle", "dtcs_at_start"):
            if rec.meta.get(key) == self._CTX_PENDING:
                # The late result still lands here if it arrives (it is
                # rewritten into the file); until then, say why it is absent.
                rec.meta[key] = {"examined": False,
                                 "why": "recording stopped before the "
                                        "recording-start read finished"}
        if self.source:  # flush anything produced since the last tick
            try:
                rec.write(self.source.drain())
            except Exception as exc:   # a broken source must not lose the file
                rec.error = rec.error or f"{type(exc).__name__}: {exc}"
        path, n = rec.close()
        if not n and rec.error is None:
            self.btn_record.setToolTip("")
            self.status.setText("Nothing recorded.")
            return
        self.btn_record.setToolTip(f"Last recording: {path}")
        if rec.error:
            self.status.setText(
                f"Recording stopped on an error ({rec.error}) — {n} samples "
                f"saved → {path}")
            return
        self.status.setText(f"Saved {n} samples → {path}")
        win = self.window()
        if interactive and hasattr(win, "load_log_path") and QMessageBox.question(
                self, "Datalog saved",
                f"Saved {n} samples to:\n{path}\n\nLoad it into Log Analysis now?"
                ) == QMessageBox.Yes:
            win.load_log_path(path)

    def _record_samples(self, samples):
        rec = self._recorder
        if rec is None:
            return
        rec.write(samples)
        if rec.error:
            # Disk full / folder gone: stop visibly; rows so far are on disk.
            self._finalize_record()
            return
        self.btn_record.setText(f"⏺ REC {rec.count} samples (click to stop)")

    def shutdown(self):
        """App close: finalize an active recording (no dialogs) and release
        the source, so the GT port and the log file are closed cleanly."""
        if self.recording:
            self._finalize_record()
        self.timer.stop()
        if self.source:
            try:
                self.source.stop()
            except Exception:
                pass

    def _tick(self):
        if not self.source:
            return
        s: Optional[Sample] = self.source.latest()
        # tmstore freshness vocabulary, when the source has one. Applied even
        # when latest() is None: a failed store read must flip the gauges to
        # "read failed", not freeze them on the last values as if still live.
        states = (self.source.channel_states()
                  if hasattr(self.source, "channel_states") else None)
        states = self._gauge_states(s, states)
        feed = self._pedal_feed(s)
        if states is not None:
            for key, g in self.gauges.items():
                g.set_state(states.get(feed.get(key, key)))
        self._persist_pedal()
        # A source that can say WHY it stopped (the GT poll loop) says so in
        # the status line; when it recovers, the live caption comes back.
        msg = (self.source.status_message()
               if hasattr(self.source, "status_message") else None)
        if msg:
            if self.recording and self._recorder is not None:
                msg += (f" — still recording; {self._recorder.count} samples "
                        f"so far are on disk")
            self.status.setText(msg)
            self._source_alarm = True
        elif getattr(self, "_source_alarm", False):
            self._source_alarm = False
            self.status.setText(getattr(self, "_live_status", ""))
        if self.recording:
            # Drain on EVERY tick, before the no-sample early return: drain()
            # is lossless and duplicate-free (unlike sampling latest() at the
            # 10 Hz paint rate), and a source between good samples must not
            # leave its queue to overflow.
            self._record_samples(self.source.drain())
        if not s:
            if states is not None:
                for key, g in self.gauges.items():
                    if states.get(key) == "failed":
                        g.set_value(None)
            return
        for key, g in self.gauges.items():
            g.set_value(self._gauge_value(feed.get(key, key), s))
        if self.replay_ctl.isVisible() and not self.pos_slider.isSliderDown():
            pos = self.source.position()
            self.pos_slider.blockSignals(True)
            self.pos_slider.setValue(int(pos * 10))
            self.pos_slider.blockSignals(False)
            self.time_label.setText(self._fmt_pos(pos))


# --------------------------------------------------------------------------- #
# Undo command shared by every table mutation (cell edit, math, paste, revert)
# --------------------------------------------------------------------------- #
class BulkEditCommand(QUndoCommand):
    """Applies a {(r, c): (old, new)} change map to a Table. Holds the Table
    itself (not the Qt model) so undo keeps working after the user switches
    tables and back."""

    def __init__(self, win: "MainWindow", table, changes, text: str):
        super().__init__(text)
        self.win = win
        self.table = table
        self.changes = changes

    def redo(self):
        for (r, c), (_old, new) in self.changes.items():
            self.table.values[r][c] = new
        self.win._table_touched(self.table)

    def undo(self):
        for (r, c), (old, _new) in self.changes.items():
            self.table.values[r][c] = old
        self.win._table_touched(self.table)


# --------------------------------------------------------------------------- #
# Main window
# --------------------------------------------------------------------------- #
class MainWindow(QMainWindow):
    def __init__(self, cal: Calibration, path: Optional[str] = None):
        super().__init__()
        self.cal = cal
        self.path = path
        self.log: Optional[Log] = None
        self.shift_events = cal.metadata.get("shift_events")
        self.current_model: Optional[CalTableModel] = None
        self.dirty = False
        self.undo_stack = QUndoStack(self)

        self.setWindowTitle(self._title())
        self.resize(1180, 760)
        self._build_menu()

        # Top level: Dashboard opens first; Tuning holds the monitor/scan/tune
        # workflow; Diagnostics holds troubleshooting/active tests/codes.
        self.settings = app_settings()
        self.dashboard = Dashboard(
            style=str(self.settings.value("dashboard/style", "classic")))
        self.main_tabs = QTabWidget()
        self.main_tabs.addTab(self.dashboard, "Dashboard")

        tuning = QWidget()
        tl = QHBoxLayout(tuning); tl.setContentsMargins(0, 0, 0, 0)
        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._build_tree())
        self.tune_tabs = QTabWidget()
        self.tune_tabs.addTab(self._build_editor_tab(), "Editor")
        self.tune_tabs.addTab(self._build_scalars_tab(), "Scalars")
        self.tune_tabs.addTab(self._build_log_tab(), "Log Analysis")
        splitter.addWidget(self.tune_tabs)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([300, 880])
        tl.addWidget(splitter)
        self.main_tabs.addTab(tuning, "Tuning")

        self.diag = DiagnosticsPage(self.dashboard)
        self.main_tabs.addTab(self.diag, "Diagnostics")

        # Live Data + Active Tests read truck-mcp's drive-log store and control
        # journal off disk (never the serial port). Appended AFTER Diagnostics
        # so the existing index-0/1/2 navigation stays valid.
        self.livedata = LiveDataPage()
        self.main_tabs.addTab(self.livedata, "Live Data")
        self.active_tests = ActiveTestsPage()
        self.main_tabs.addTab(self.active_tests, "Active Tests")

        self.main_tabs.setCurrentIndex(0)
        self.setCentralWidget(self.main_tabs)

        # select first table
        if self.cal.tables:
            self._select_table(self.cal.tables[0].name)

    # -- chrome ------------------------------------------------------------ #
    def _title(self) -> str:
        veh = self.cal.metadata.get("vehicle", "calibration")
        name = os.path.basename(self.path) if self.path else "unsaved"
        star = " *" if self.dirty else ""
        return f"OpenOBD — {veh}  [{name}]{star}"

    def _mark_dirty(self, *_a):
        if not self.dirty:
            self.dirty = True
            self.setWindowTitle(self._title())

    def closeEvent(self, event):
        if not self.dirty:
            self._shutdown_pages()
            return super().closeEvent(event)
        ans = QMessageBox.question(
            self, "Unsaved changes",
            "The calibration has unsaved edits. Save before closing?",
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
            QMessageBox.Save)
        if ans == QMessageBox.Cancel:
            return event.ignore()
        if ans == QMessageBox.Save:
            self.save_cal()
            if self.dirty:  # save failed or Save As was cancelled
                return event.ignore()
        self._shutdown_pages()
        super().closeEvent(event)

    def _shutdown_pages(self):
        # A child inside a QTabWidget never gets its own closeEvent, so stop the
        # Live Data / Active Tests timers and release their DB readers explicitly.
        # The Dashboard goes first: closing mid-drive must finalize the
        # recording and release the GT port, not abandon them.
        dash = getattr(self, "dashboard", None)
        if dash is not None:
            dash.shutdown()
        for page in (getattr(self, "livedata", None),
                     getattr(self, "active_tests", None)):
            if page is not None:
                page.shutdown()

    def _build_menu(self):
        m = self.menuBar().addMenu("&File")
        for label, slot, keys in [
            ("Open .cal.json…", self.open_cal, "Ctrl+O"),
            ("Save", self.save_cal, "Ctrl+S"),
            ("Save As…", self.save_cal_as, "Ctrl+Shift+S"),
            ("Export table → CSV…", self.export_csv, None),
            ("Load Log…", self.load_log, "Ctrl+L"),
        ]:
            a = QAction(label, self)
            if keys:
                a.setShortcut(keys)
            a.triggered.connect(slot)
            m.addAction(a)
        m.addSeparator()
        qa = QAction("Quit", self); qa.triggered.connect(self.close); m.addAction(qa)

        e = self.menuBar().addMenu("&Edit")
        undo = self.undo_stack.createUndoAction(self, "Undo")
        undo.setShortcut("Ctrl+Z")
        redo = self.undo_stack.createRedoAction(self, "Redo")
        redo.setShortcut("Ctrl+Y")
        e.addAction(undo); e.addAction(redo)
        e.addSeparator()
        for label, slot, keys in [
            ("Copy selection", self.copy_selection, "Ctrl+C"),
            ("Paste", self.paste_selection, "Ctrl+V"),
        ]:
            a = QAction(label, self)
            a.setShortcut(keys)
            a.triggered.connect(slot)
            e.addAction(a)
        e.addSeparator()
        rs = QAction("Revert selection → stock", self)
        rs.triggered.connect(self._revert_selected); e.addAction(rs)
        rt = QAction("Revert whole table → stock", self)
        rt.triggered.connect(self._revert_table); e.addAction(rt)

        s = self.menuBar().addMenu("&Settings")
        pref = QAction("Preferences…", self)
        pref.setShortcut("Ctrl+,")
        pref.triggered.connect(self._open_settings)
        s.addAction(pref)

        h = self.menuBar().addMenu("&Help")
        ab = QAction("About", self); ab.triggered.connect(self._about); h.addAction(ab)

    def _open_settings(self):
        """Settings dialog. Currently houses the dashboard-visual selector;
        the choice applies immediately and persists via QSettings."""
        dlg = QDialog(self)
        dlg.setWindowTitle("Settings")
        form = QFormLayout(dlg)
        combo = QComboBox()
        for key, label in DASHBOARD_STYLES.items():
            combo.addItem(label, key)
        combo.setCurrentIndex(max(0, combo.findData(self.dashboard.style)))
        form.addRow("Dashboard visual:", combo)
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        form.addRow(btns)
        if dlg.exec() != QDialog.Accepted:
            return
        style = combo.currentData()
        self.dashboard.set_style(style)
        self.settings.setValue("dashboard/style", style)
        self.statusBar().showMessage(
            f"Dashboard visual: {DASHBOARD_STYLES[style]}", 4000)

    def _about(self):
        md = self.cal.metadata
        QMessageBox.information(
            self, "About OpenOBD",
            f"OpenOBD {__version__} — calibration viewer/editor + log "
            "overlay for truck-mcp.\n\n"
            f"Vehicle: {md.get('vehicle','')}\n"
            f"Controllers: {md.get('controllers','')}\n\n"
            + md.get("safety", ""),
        )

    # -- left tree --------------------------------------------------------- #
    def _build_tree(self) -> QWidget:
        host = QWidget(); lay = QVBoxLayout(host)
        lay.setContentsMargins(0, 0, 0, 0); lay.setSpacing(2)
        self.tree_filter = QLineEdit()
        self.tree_filter.setPlaceholderText("Filter tables…")
        self.tree_filter.setClearButtonEnabled(True)
        self.tree_filter.textChanged.connect(self._filter_tree)
        lay.addWidget(self.tree_filter)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Tables"])
        cats: dict[str, QTreeWidgetItem] = {}
        for t in self.cal.tables:
            cat = t.category.split("/")[0] or "Uncategorized"
            if cat not in cats:
                node = QTreeWidgetItem([cat]); self.tree.addTopLevelItem(node)
                cats[cat] = node
            leaf = QTreeWidgetItem([t.name.replace("WOT Shift Speed — ", "")])
            leaf.setData(0, Qt.UserRole, t.name)
            cats[cat].addChild(leaf)
        self.tree.expandAll()
        self.tree.itemClicked.connect(self._on_tree_click)
        lay.addWidget(self.tree, 1)
        return host

    def _filter_tree(self, text: str):
        needle = text.strip().lower()
        for i in range(self.tree.topLevelItemCount()):
            cat = self.tree.topLevelItem(i)
            any_visible = False
            for j in range(cat.childCount()):
                leaf = cat.child(j)
                full = (leaf.data(0, Qt.UserRole) or "").lower()
                hit = not needle or needle in full or needle in leaf.text(0).lower()
                leaf.setHidden(not hit)
                any_visible = any_visible or hit
            cat.setHidden(not any_visible)

    def _on_tree_click(self, item, _col):
        name = item.data(0, Qt.UserRole)
        if name:
            self._select_table(name)
            self.main_tabs.setCurrentIndex(1)   # Tuning workspace
            self.tune_tabs.setCurrentIndex(0)   # Editor

    # -- editor tab -------------------------------------------------------- #
    def _build_editor_tab(self) -> QWidget:
        w = QWidget(); lay = QVBoxLayout(w)

        ctl = QHBoxLayout()
        self.heat_chk = QCheckBox("Heatmap"); self.heat_chk.setChecked(True)
        self.heat_chk.stateChanged.connect(self._toggle_heat)
        ctl.addWidget(self.heat_chk)

        ctl.addWidget(QLabel("Overlay:"))
        self.overlay_mode = QComboBox()
        self.overlay_mode.addItems(["None", "Log count", "Log mean value"])
        self.overlay_mode.currentIndexChanged.connect(self._apply_overlay)
        ctl.addWidget(self.overlay_mode)

        ctl.addWidget(QLabel("X ch:"))
        self.x_ch = QComboBox(); ctl.addWidget(self.x_ch)
        ctl.addWidget(QLabel("Y ch:"))
        self.y_ch = QComboBox(); ctl.addWidget(self.y_ch)
        ctl.addWidget(QLabel("Value:"))
        self.v_ch = QComboBox(); ctl.addWidget(self.v_ch)
        for cb in (self.x_ch, self.y_ch, self.v_ch):
            cb.addItem("(none)")
            cb.currentIndexChanged.connect(self._apply_overlay)

        self.next_delta_btn = QPushButton("Next Δ")
        self.next_delta_btn.setToolTip("Jump to the next changed-vs-stock cell")
        self.next_delta_btn.clicked.connect(self._goto_next_changed)
        ctl.addWidget(self.next_delta_btn)
        ctl.addStretch(1)
        lay.addLayout(ctl)

        # selection-math toolbar (HP Tuners staples). +/- keys nudge by Amount.
        math_bar = QHBoxLayout()
        math_bar.addWidget(QLabel("Amount:"))
        self.amount_edit = QLineEdit("1")
        self.amount_edit.setFixedWidth(70)
        self.amount_edit.setToolTip(
            "Operand for the math buttons and the +/- key nudge")
        math_bar.addWidget(self.amount_edit)
        for label, tip, fn in [
            ("=", "Set selection to Amount", lambda: self._math_op("set")),
            ("+", "Add Amount to selection", lambda: self._math_op("add")),
            ("−", "Subtract Amount from selection",
             lambda: self._math_op("add", negate=True)),
            ("×", "Multiply selection by Amount", lambda: self._math_op("mul")),
            ("%", "Scale selection by Amount percent",
             lambda: self._math_op("pct")),
        ]:
            b = QPushButton(label); b.setFixedWidth(34); b.setToolTip(tip)
            b.clicked.connect(fn)
            math_bar.addWidget(b)
        math_bar.addSpacing(12)
        for label, mode in [("Interp ↔", "h"), ("Interp ↕", "v"),
                            ("Interp 2D", "2d")]:
            b = QPushButton(label)
            b.setToolTip("Linear interpolation across the selection")
            b.clicked.connect(lambda _=False, m=mode: self._interp(m))
            math_bar.addWidget(b)
        math_bar.addSpacing(12)
        rb = QPushButton("Revert sel → stock")
        rb.clicked.connect(self._revert_selected)
        math_bar.addWidget(rb)
        math_bar.addStretch(1)
        lay.addLayout(math_bar)

        self.view = QTableView()
        self.view.setItemDelegate(HeatmapDelegate(self.view))
        self.view.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.view.installEventFilter(self)  # +/- nudge keys
        lay.addWidget(self.view, 1)

        self.table_info = QLabel(""); self.table_info.setWordWrap(True)
        self.table_info.setStyleSheet("color:#9aa0a6; padding:4px;")
        lay.addWidget(self.table_info)
        return w

    def _select_table(self, name: str):
        t = self.cal.table(name)
        if not t:
            return
        events = self.shift_events if "Shift Speed" in name else None
        self.current_model = CalTableModel(t, shift_events=events)
        self.current_model.dataChanged.connect(self._mark_dirty)
        self.current_model.edit_hook = self._on_cell_edit
        self.current_model.show_heatmap = self.heat_chk.isChecked()
        self.view.setModel(self.current_model)
        self._update_table_info()
        self._apply_overlay()

    def _update_table_info(self):
        if not self.current_model:
            return
        t = self.current_model.table
        changed = len(editops.changed_cells(t))
        pid = f" · HPT id {t.param_id}" if t.param_id else ""
        self.table_info.setText(
            f"<b>{t.name}</b> ({t.unit}){pid} · {changed} cell(s) changed vs "
            f"stock<br>{t.note}"
        )

    def _toggle_heat(self):
        if self.current_model:
            self.current_model.set_heatmap(self.heat_chk.isChecked())

    # -- editing: undo plumbing --------------------------------------------- #
    def _on_cell_edit(self, r, c, old, new):
        self.undo_stack.push(BulkEditCommand(
            self, self.current_model.table, {(r, c): (old, new)}, "edit cell"))

    def _table_touched(self, table):
        """Called by undo commands after writing to a Table."""
        self._mark_dirty()
        if self.current_model and self.current_model.table is table:
            self.current_model.refresh_all()
            self._update_table_info()

    def _push_changes(self, changes, text):
        if not changes:
            self.statusBar().showMessage(f"{text}: no cells affected", 3000)
            return
        self.undo_stack.push(BulkEditCommand(
            self, self.current_model.table, changes, text))
        self.statusBar().showMessage(f"{text}: {len(changes)} cell(s)", 3000)

    def _selected_cells(self):
        if not self.current_model:
            return []
        sel = self.view.selectionModel()
        idxs = sel.selectedIndexes() if sel else []
        if not idxs:
            cur = self.view.currentIndex()
            idxs = [cur] if cur.isValid() else []
        return [(i.row(), i.column()) for i in idxs]

    def _amount(self) -> Optional[float]:
        try:
            return float(self.amount_edit.text())
        except ValueError:
            self.statusBar().showMessage(
                f"Amount is not a number: {self.amount_edit.text()!r}", 4000)
            return None

    def _math_op(self, op: str, negate: bool = False):
        cells = self._selected_cells()
        amt = self._amount()
        if not cells or amt is None:
            return
        if negate:
            amt = -amt
        label = {"set": "set", "add": "add", "mul": "multiply", "pct": "scale %"}[op]
        self._push_changes(
            editops.apply_math(self.current_model.table, cells, op, amt), label)

    def _interp(self, mode: str):
        cells = self._selected_cells()
        if len(cells) < 2:
            self.statusBar().showMessage("Select a range to interpolate", 3000)
            return
        self._push_changes(
            editops.interpolate(self.current_model.table, cells, mode),
            f"interpolate {mode}")

    def _revert_selected(self):
        cells = self._selected_cells()
        if not cells:
            return
        self._push_changes(
            editops.revert_cells(self.current_model.table, cells),
            "revert selection to stock")

    def _revert_table(self):
        if not self.current_model:
            return
        t = self.current_model.table
        self._push_changes(
            editops.revert_cells(t, editops.all_cells(t)),
            "revert table to stock")

    # -- clipboard ----------------------------------------------------------- #
    def copy_selection(self):
        cells = self._selected_cells()
        if not cells:
            return
        QApplication.clipboard().setText(
            editops.to_tsv(self.current_model.table, cells))
        self.statusBar().showMessage(f"Copied {len(cells)} cell(s)", 3000)

    def paste_selection(self):
        if not self.current_model:
            return
        grid = editops.parse_tsv(QApplication.clipboard().text())
        if not grid:
            self.statusBar().showMessage("Clipboard has no numeric grid", 3000)
            return
        cur = self.view.currentIndex()
        r0, c0 = (cur.row(), cur.column()) if cur.isValid() else (0, 0)
        self._push_changes(
            editops.paste_grid(self.current_model.table, r0, c0, grid), "paste")

    # -- navigation ----------------------------------------------------------- #
    def _goto_next_changed(self):
        if not self.current_model:
            return
        t = self.current_model.table
        changed = editops.changed_cells(t)
        if not changed:
            self.statusBar().showMessage("No changed cells in this table", 3000)
            return
        cur = self.view.currentIndex()
        pos = (cur.row(), cur.column()) if cur.isValid() else (-1, -1)
        nxt = next((rc for rc in changed if rc > pos), changed[0])
        idx = self.current_model.index(*nxt)
        self.view.setCurrentIndex(idx)
        self.view.scrollTo(idx)

    def eventFilter(self, obj, event):
        # +/- on the grid nudges the selection by Amount (HP Tuners style).
        if obj is self.view and event.type() == QEvent.KeyPress:
            if event.key() in (Qt.Key_Plus, Qt.Key_Minus) and self.current_model:
                self._math_op("add", negate=event.key() == Qt.Key_Minus)
                return True
        return super().eventFilter(obj, event)

    def _refresh_channel_combos(self):
        keys = ["(none)"] + (self.log.canonical_keys() if self.log else [])
        for cb in (self.x_ch, self.y_ch, self.v_ch):
            cur = cb.currentText()
            cb.blockSignals(True)
            cb.clear(); cb.addItems(sorted(set(keys), key=keys.index))
            i = cb.findText(cur)
            cb.setCurrentIndex(i if i >= 0 else 0)
            cb.blockSignals(False)
        # sensible defaults for a shift table: x=vss
        if self.log and self.x_ch.currentText() == "(none)":
            i = self.x_ch.findText("vss")
            if i >= 0:
                self.x_ch.setCurrentIndex(i)

    def _apply_overlay(self):
        if not self.current_model:
            return
        mode = self.overlay_mode.currentIndex()
        if mode == 0 or not self.log:
            self.current_model.set_overlay(None, CalTableModel.OVERLAY_NONE)
            return
        xk = self.x_ch.currentText()
        if xk == "(none)":
            self.current_model.set_overlay(None, CalTableModel.OVERLAY_NONE)
            return
        yk = self.y_ch.currentText()
        vk = self.v_ch.currentText()
        yk = None if yk == "(none)" else yk
        vk = None if vk == "(none)" else vk
        try:
            ov = bin_log_to_table(
                self.log, self.current_model.table,
                x_channel=xk, y_channel=yk, value_channel=vk,
            )
        except Exception as e:
            # Clear rather than leave a stale overlay on screen, and say why.
            self.current_model.set_overlay(None, CalTableModel.OVERLAY_NONE)
            self.statusBar().showMessage(f"Overlay failed: {e}", 6000)
            return
        m = (CalTableModel.OVERLAY_COUNT if mode == 1
             else CalTableModel.OVERLAY_MEAN)
        self.current_model.set_overlay(ov, m)

    # -- scalars tab ------------------------------------------------------- #
    def _build_scalars_tab(self) -> QWidget:
        w = QWidget(); lay = QVBoxLayout(w)
        self.scalar_tbl = QTableWidget(len(self.cal.scalars), 6)
        self.scalar_tbl.setHorizontalHeaderLabels(
            ["Parameter", "Value", "Unit", "Stock", "Δ", "Category"])
        self.scalar_tbl.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.Stretch)
        for r, s in enumerate(self.cal.scalars):
            self._set_scalar_row(r, s)
        self.scalar_tbl.itemChanged.connect(self._on_scalar_edit)
        lay.addWidget(self.scalar_tbl)
        note = QLabel(self.cal.metadata.get("safety", ""))
        note.setWordWrap(True); note.setStyleSheet("color:#e07b72; padding:6px;")
        lay.addWidget(note)
        return w

    def _set_scalar_row(self, r, s):
        def item(text, editable=False, color=None):
            it = QTableWidgetItem(text)
            if not editable:
                it.setFlags(it.flags() & ~Qt.ItemIsEditable)
            if color:
                it.setForeground(color)
            return it
        self.scalar_tbl.blockSignals(True)
        self.scalar_tbl.setItem(r, 0, item(s.name))
        vi = item(f"{s.value:g}", editable=True)
        vi.setData(Qt.UserRole, r)
        self.scalar_tbl.setItem(r, 1, vi)
        self.scalar_tbl.setItem(r, 2, item(s.unit))
        self.scalar_tbl.setItem(
            r, 3, item("" if s.stock_value is None else f"{s.stock_value:g}"))
        delta = ""
        if s.stock_value is not None:
            d = s.value - s.stock_value
            delta = f"{d:+g}" if d else ""
        self.scalar_tbl.setItem(
            r, 4, item(delta, color=QColor(235, 110, 100) if delta else None))
        self.scalar_tbl.setItem(r, 5, item(s.category))
        self.scalar_tbl.blockSignals(False)

    def _on_scalar_edit(self, it):
        if it.column() != 1:
            return
        r = it.data(Qt.UserRole)
        if r is None:
            r = it.row()
        text = it.text()
        try:
            v = float(text)
        except ValueError:
            # Restore the display so the cell never shows a value the model
            # doesn't hold. _set_scalar_row replaces the row's items, deleting
            # `it` — so only the pre-captured text may be used after it.
            self._set_scalar_row(r, self.cal.scalars[r])
            self.statusBar().showMessage(
                f"Not a number: {text!r} — edit reverted", 4000)
            return
        if v != self.cal.scalars[r].value:
            self._mark_dirty()
        self.cal.scalars[r].value = v
        self._set_scalar_row(r, self.cal.scalars[r])

    # -- log tab ----------------------------------------------------------- #
    def _build_log_tab(self) -> QWidget:
        w = QWidget(); lay = QVBoxLayout(w)
        bar = QHBoxLayout()
        b = QPushButton("Load Log…"); b.clicked.connect(self.load_log)
        bar.addWidget(b)
        self.events_chk = QCheckBox("Event markers")
        self.events_chk.setChecked(True)
        self.events_chk.setToolTip("Knock events (red), shifts (cyan) and "
                                   "recording markers (amber, from the "
                                   ".meta.json beside a recording)")
        self.events_chk.toggled.connect(self._rebuild_plots)
        bar.addWidget(self.events_chk)
        self.btn_learn_pedal = QPushButton("Learn pedal calibration from this log")
        self.btn_learn_pedal.setToolTip(
            "Derive pedal rest / full from this log's idle and WOT samples "
            "(same rules as the live learner) and store them for its VIN")
        self.btn_learn_pedal.clicked.connect(self.learn_pedal_from_log)
        bar.addWidget(self.btn_learn_pedal)
        self.cursor_label = QLabel("")
        self.cursor_label.setStyleSheet("color:#9aa0a6;")
        bar.addWidget(self.cursor_label, 1)
        lay.addLayout(bar)

        # chart state
        self._log_times: list[float] = []
        self._plots: list = []
        self._vlines: list = []
        self._knock_times: list[float] = []
        self._shift_times: list[float] = []
        self._marker_times: list[tuple[float, str]] = []

        split = QSplitter(Qt.Vertical)
        top = QSplitter(Qt.Horizontal)
        self.chan_list = QListWidget()
        self.chan_list.setMaximumWidth(180)
        self.chan_list.itemChanged.connect(self._rebuild_plots)
        top.addWidget(self.chan_list)
        self.plot_area = pg.GraphicsLayoutWidget()
        self.plot_area.scene().sigMouseMoved.connect(self._on_chart_mouse)
        top.addWidget(self.plot_area)
        top.setStretchFactor(0, 0); top.setStretchFactor(1, 1)
        split.addWidget(top)

        self.log_report = QTextEdit(); self.log_report.setReadOnly(True)
        self.log_report.setFont(QFont("Consolas", 10))
        self.log_report.setPlainText(
            "No log loaded.\n\nLoad a VCM Scanner CSV export (Log File → Export "
            "Log File → CSV) or a plain CSV. Channels plot above with a synced "
            "cursor; this report shows regime trims, knock events, and "
            "fuel-pressure flags; the Editor tab can then overlay the log onto "
            "any table.")
        split.addWidget(self.log_report)
        split.setSizes([460, 220])
        lay.addWidget(split, 1)
        return w

    # -- charting ------------------------------------------------------------ #
    _PLOT_DEFAULTS = ["rpm", "vss", "app", "tps", "spark", "knock_retard"]

    def _populate_channels(self):
        self.chan_list.blockSignals(True)
        self.chan_list.clear()
        keys = [k for k in self.log.canonical_keys() if k != "time"]
        present = set(keys)
        # default to the tuning staples that are actually in the log (max 4)
        want = [k for k in self._PLOT_DEFAULTS if k in present][:4] or keys[:3]
        for k in keys:
            it = QListWidgetItem(k)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked if k in want else Qt.Unchecked)
            self.chan_list.addItem(it)
        self.chan_list.blockSignals(False)

    def _checked_channels(self) -> list[str]:
        return [self.chan_list.item(i).text()
                for i in range(self.chan_list.count())
                if self.chan_list.item(i).checkState() == Qt.Checked]

    def _rebuild_plots(self, *_a):
        self.plot_area.clear()
        self._plots = []; self._vlines = []
        if not self.log or not self._log_times:
            return
        times = np.asarray(self._log_times, dtype=float)
        keys = self._checked_channels()
        first = None
        for i, key in enumerate(keys):
            p = self.plot_area.addPlot(row=i, col=0)
            ser = self.log.series(key)
            y = np.array([np.nan if v is None else v for v in ser], dtype=float)
            n = min(len(times), len(y))
            p.plot(times[:n], y[:n],
                   pen=pg.mkPen((120, 170, 240), width=1.4), connect="finite")
            p.setLabel("left", key)
            p.showGrid(x=True, y=True, alpha=0.15)
            if first is None:
                first = p
            else:
                p.setXLink(first)
            if i < len(keys) - 1:
                p.getAxis("bottom").setStyle(showValues=False)
            if self.events_chk.isChecked():
                for t_ev in self._knock_times:
                    p.addItem(pg.InfiniteLine(
                        pos=t_ev, angle=90,
                        pen=pg.mkPen((220, 70, 60, 150), width=1,
                                     style=Qt.DashLine)), ignoreBounds=True)
                for t_ev in self._shift_times:
                    p.addItem(pg.InfiniteLine(
                        pos=t_ev, angle=90,
                        pen=pg.mkPen((80, 200, 220, 130), width=1,
                                     style=Qt.DashLine)), ignoreBounds=True)
                for t_ev, label in self._marker_times:
                    p.addItem(pg.InfiniteLine(
                        pos=t_ev, angle=90,
                        pen=pg.mkPen((235, 170, 60, 200), width=1.5),
                        label=label if i == 0 else None,
                        labelOpts={"position": 0.92,
                                   "color": (235, 170, 60)}),
                        ignoreBounds=True)
            vline = pg.InfiniteLine(
                angle=90, movable=False,
                pen=pg.mkPen((235, 235, 235, 120), width=1))
            vline.setVisible(False)
            p.addItem(vline, ignoreBounds=True)
            self._vlines.append(vline)
            self._plots.append(p)

    def _on_chart_mouse(self, scene_pos):
        if not self._plots or not self.log or not self._log_times:
            return
        x = self._plots[0].vb.mapSceneToView(scene_pos).x()
        import bisect
        i = bisect.bisect_right(self._log_times, x) - 1
        if i < 0 or i >= self.log.n_samples:
            for vl in self._vlines:
                vl.setVisible(False)
            self.cursor_label.setText("")
            return
        for vl in self._vlines:
            vl.setPos(x); vl.setVisible(True)
        parts = [f"t={self._log_times[i]:.2f}s"]
        for key in self._checked_channels():
            ser = self.log.series(key)
            v = ser[i] if i < len(ser) else None
            parts.append(f"{key}={v:g}" if v is not None else f"{key}=—")
        self.cursor_label.setText("   ".join(parts))

    def _render_report(self, rep=None):
        if not self.log:
            return
        rep = rep or analyze_log(self.log)
        L = []
        L.append(f"Source: {self.log.source}"
                 + ("  ·  per-channel times from the .long.csv sidecar"
                    if self.log.timed else ""))
        L.append(f"Samples: {rep.n_samples}"
                 + (f"  ·  Duration: {rep.duration_s:.1f}s"
                    if rep.duration_s else ""))
        L.append(f"Channels: {', '.join(rep.channels_present)}")
        L.append("")
        L.append("── Regime trims ──")
        L.append(f"  WOT criterion: {rep.wot_criterion_text}")
        L.append(f"  WOT/PE samples: {rep.wot_samples}")
        for rg in rep.regimes:
            s = f"  {rg.name:22s} n={rg.n:<6d}"
            if rg.combined_trim_mean is not None:
                s += f" trim {rg.combined_trim_mean:+.1f}%"
                if rg.stft_mean is not None:
                    s += f"  (STFT {rg.stft_mean:+.1f}, LTFT {rg.ltft_mean or 0:+.1f})"
            L.append(s)
        L.append("")
        L.append("── Knock ──")
        if rep.max_knock_retard is not None:
            L.append(f"  max KR: {rep.max_knock_retard:.1f}°  ·  "
                     f"{len(rep.knock_events)} event(s)")
        for e in rep.knock_events[:12]:
            if e.get("inferred"):
                L.append(f"    ~t={e.get('time_s')}  spark drop "
                         f"{e.get('spark_drop_deg')}°  @ {e.get('rpm')} rpm (inferred)")
            else:
                L.append(f"    t={e.get('time_s')}  KR {e.get('retard_deg')}°  "
                         f"@ {e.get('rpm')} rpm")
        if rep.max_knock_retard is None and not rep.knock_events:
            L.append("  none / not logged")
        L.append("")
        if rep.fuel_pressure_min is not None:
            L.append(f"── Fuel pressure ── min {rep.fuel_pressure_min:.0f}"
                     + ("  ⚠ SAG" if rep.fuel_pressure_sag else ""))
            L.append("")
        # Observed shift points — the meaningful comparison for the shift tables.
        shifts = detect_shift_points(self.log)
        wot_shifts = [s for s in shifts if s.wot]
        L.append("── Observed shift points ──")
        if not shifts:
            L.append("  none detected (need a 'gear' channel or a clear RPM drop)")
        else:
            src = "gear channel" if not shifts[0].inferred else "inferred (RPM drop)"
            L.append(f"  {len(shifts)} shift(s) via {src}; "
                     f"{len(wot_shifts)} at WOT")
            for s in (wot_shifts or shifts)[:12]:
                gear = (f"{s.from_gear}->{s.to_gear}"
                        if s.from_gear is not None else "up")
                rpm = f"{s.rpm:.0f} rpm" if s.rpm is not None else "?"
                vss = f"{s.vss:.0f} mph" if s.vss is not None else "?"
                flag = " ⚑WOT" if s.wot else ""
                L.append(f"    {gear:6s} @ {rpm:>10s}  {vss:>7s}{flag}")
            L.append("  (compare WOT shifts to the setpoint tables: target "
                     "1-2/2-3 ~5,400-5,500 RPM on 4.11/35s)")
        L.append("")
        markers = (self.log.meta or {}).get("markers") or []
        if markers:
            L.append("── Recording markers ──")
            for m in markers:
                L.append(f"    t={m.get('t')}  {m.get('label')}")
            L.append("")
        if rep.notes:
            L.append("── Notes ──")
            for n in rep.notes:
                L.append(f"  • {n}")
        self.log_report.setPlainText("\n".join(L))

    # -- file ops ---------------------------------------------------------- #
    def open_cal(self):
        p, _ = QFileDialog.getOpenFileName(
            self, "Open calibration", "", "Calibration (*.cal.json *.json)")
        if not p:
            return
        try:
            self.cal = Calibration.load(p)
        except Exception as e:
            QMessageBox.critical(self, "Open failed", str(e)); return
        self.path = p
        # rebuild UI by re-instantiating (simplest, robust). Keep a Python
        # reference on the QApplication or the new window gets GC'd.
        new = MainWindow(self.cal, p)
        QApplication.instance()._openobd_main = new
        new.show()
        self.close()

    def save_cal(self):
        if not self.path:
            return self.save_cal_as()
        try:
            self.cal.save(self.path)
            self.dirty = False
            self.setWindowTitle(self._title())
            self.statusBar().showMessage(f"Saved {self.path}", 4000)
        except Exception as e:
            QMessageBox.critical(self, "Save failed", str(e))

    def save_cal_as(self):
        p, _ = QFileDialog.getSaveFileName(
            self, "Save calibration", self.path or "edited.cal.json",
            "Calibration (*.cal.json *.json)")
        if not p:
            return
        self.path = p
        self.save_cal()

    def export_csv(self):
        if not self.current_model:
            return
        t = self.current_model.table
        p, _ = QFileDialog.getSaveFileName(
            self, "Export table CSV", f"{t.name}.csv", "CSV (*.csv)")
        if not p:
            return
        import csv
        with open(p, "w", newline="", encoding="utf-8") as fh:
            wtr = csv.writer(fh)
            hdr = [""] + (
                self.shift_events if ("Shift" in t.name and self.shift_events)
                else [f"{v:g}" for v in t.x_axis.values])
            wtr.writerow(hdr)
            for r in range(t.n_rows):
                ylab = (f"{t.y_axis.values[r]:g}" if t.y_axis else t.unit)
                wtr.writerow([ylab] + [f"{v:g}" for v in t.values[r]])
        self.statusBar().showMessage(f"Exported {p}", 4000)

    def load_log(self):
        p, _ = QFileDialog.getOpenFileName(
            self, "Load log", "", "Logs (*.csv);;All files (*)")
        if p:
            self.load_log_path(p)

    def load_log_path(self, p):
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            self.log = parse_csv(text, source=os.path.basename(p))
        except Exception as e:
            QMessageBox.critical(self, "Load log failed", str(e)); return
        # A Dashboard recording's long-format sidecar, when it sits next to
        # the CSV: regime and shift analysis then use each channel's own
        # request times instead of the wide row's cycle midpoint.
        self._log_sidecar = None
        side = sidecar_path(p)
        if os.path.exists(side) and os.path.abspath(side) != os.path.abspath(p):
            try:
                with open(side, "r", encoding="utf-8", errors="replace") as fh:
                    attach_long(self.log, fh.read())
                self._log_sidecar = side
            except (OSError, ValueError):
                self.log.timed = None     # unreadable sidecar: wide CSV only
        # A Dashboard recording's metadata: markers for the chart, the VIN,
        # and the pedal calibration the recording used.
        self.log.meta = None
        try:
            self.log.meta = read_meta(p)
        except (OSError, ValueError) as exc:
            self.statusBar().showMessage(
                f"Ignored unreadable {os.path.basename(meta_path(p))}: {exc}",
                6000)
        self.log.pedal_cal = self._known_pedal_cal(self.log)
        self._analyze_loaded_log()
        self.dashboard.bind_source(LogReplaySource(self.log, speed=4.0))
        self.main_tabs.setCurrentIndex(1)   # Tuning workspace
        self.tune_tabs.setCurrentIndex(2)   # Log Analysis
        self.statusBar().showMessage(
            f"Loaded log: {self.log.n_samples} samples, "
            f"{len(self.log.canonical_keys())} mapped channels", 6000)

    @staticmethod
    def _log_vin(log: Log) -> Optional[str]:
        vin = ((log.meta or {}).get("vehicle") or {}).get("vin") or {}
        return vin.get("value") if vin.get("examined") else None

    @staticmethod
    def _recorded_pedal_cal(log: Log) -> Optional[PedalCal]:
        """The calibration the recording itself measured and used, most
        recent first: its last change, then after_identity, then at_start."""
        pc = (log.meta or {}).get("pedal_calibration") or {}
        cands = (list(reversed(pc.get("changes") or []))
                 + [pc.get("after_identity"), pc.get("at_start")])
        for cand in cands:
            cal = PedalCal.from_dict(cand)
            if cal.ready:
                return cal
        return None

    def _known_pedal_cal(self, log: Log) -> Optional[PedalCal]:
        """A pedal calibration KNOWN for this log. The VIN gate comes first:
        a stored calibration applies only to a log whose metadata carries an
        examined VIN. A VIN-less log gets only what its own recording
        measured (that was this drive); a log with neither gets none."""
        recorded = self._recorded_pedal_cal(log)
        vin = self._log_vin(log)
        if not vin:
            return recorded
        if recorded is not None:
            return recorded
        try:
            stored = load_pedal_cal(vin)
        except Exception:
            stored = None
        return stored if stored is not None and stored.ready else None

    def learn_pedal_from_log(self):
        """User action: derive the pedal calibration from the loaded log and
        re-analyze the log with it. Stored ONLY under the log's examined
        VIN, merged so a stored rest is never raised and a stored full never
        lowered; a VIN-less log's calibration applies to its own analysis
        only. Not a hidden constant: values and source are shown."""
        if not self.log:
            self.statusBar().showMessage("Load a log first.", 4000)
            return
        cal = calibrate_pedal_from_log(self.log)
        if not cal.ready:
            msg = (f"No pedal calibration in {self.log.source}: "
                   f"{cal.status()}. It needs a stopped idle of 2 s or more "
                   "and a pull with load >= 98 % or MAP >= 95 kPa, with APP D "
                   "logged.")
            self.statusBar().showMessage(msg, 10000)
            self.log_report.append("\n" + msg)
            return
        vin = self._log_vin(self.log)
        if not vin:
            self.log.pedal_cal = cal
            self._analyze_loaded_log()
            msg = (f"Pedal calibration {cal.status()} learned. No VIN in this "
                   "log; calibration applies to this log's analysis only "
                   "(not stored).")
            self.statusBar().showMessage(msg, 10000)
            self.log_report.append("\n" + msg)
            return
        try:
            stored = load_pedal_cal(vin)
        except Exception:
            stored = None
        merged = cal
        if stored is not None:
            rests = [v for v in (stored.rest, cal.rest) if v is not None]
            fulls = [v for v in (stored.full, cal.full) if v is not None]
            merged = PedalCal(min(rests), max(fulls),
                              f"{stored.source} + {cal.source}"
                              if stored.source else cal.source)
        try:
            save_pedal_cal(vin, merged)
        except Exception as exc:
            self.statusBar().showMessage(f"Could not store it: {exc}", 8000)
            return
        self.log.pedal_cal = merged
        src = self.dashboard.source
        if isinstance(src, GtDataSource) and self.dashboard._pedal_key == vin:
            src.seed_pedal_calibration(merged)
        self._analyze_loaded_log()
        old = stored.status() if stored is not None else "none stored"
        msg = (f"Pedal calibration for VIN {vin}: was {old}; now "
               f"{merged.status()} (learned {cal.status()})")
        self.statusBar().showMessage(msg, 10000)
        self.log_report.append("\n" + msg)

    def _analyze_loaded_log(self):
        """Regime report, event markers and charts for self.log."""
        self._log_times = time_axis(self.log)
        rep = analyze_log(self.log)
        # Event marker positions. The chart axis is zero-based (time_axis
        # subtracts the log's first timestamp); analyzer events carry RAW
        # timestamps, so index into _log_times (knock has a sample index)
        # or subtract the base (shifts only carry time_s).
        raw_t = [v for v in self.log.series("time") if v is not None]
        t_base = raw_t[0] if raw_t else 0.0
        self._knock_times = []
        for e in rep.knock_events:
            i = e.get("sample")
            if i is not None and i < len(self._log_times):
                self._knock_times.append(self._log_times[i])
            elif e.get("time_s") is not None:
                self._knock_times.append(float(e["time_s"]) - t_base)
        self._shift_times = [float(s.time_s) - t_base
                             for s in detect_shift_points(self.log)
                             if s.time_s is not None]
        # Recording markers are on the recording's zero, which is the CSV's
        # Time (s) zero: shift them by the same base as the shifts.
        self._marker_times = []
        for m in (self.log.meta or {}).get("markers") or []:
            try:
                self._marker_times.append((float(m["t"]) - t_base,
                                           str(m.get("label", ""))))
            except (KeyError, TypeError, ValueError):
                continue
        self._render_report(rep)
        self._populate_channels()
        self._rebuild_plots()
        self._refresh_channel_combos()


def load_default_cal() -> tuple[Calibration, Optional[str]]:
    # Candidate locations: dev tree, and PyInstaller's bundled data dir.
    candidates = []
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidates.append(os.path.join(here, "data", "2010_silverado_full.cal.json"))
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(os.path.join(meipass, "data",
                                       "2010_silverado_full.cal.json"))
    for seed_path in candidates:
        if os.path.exists(seed_path):
            # A frozen bundle's seed is read-only; return no path so Save prompts
            # the user for a writable location instead of failing on the bundle.
            writable_path = None if meipass and seed_path.startswith(meipass) \
                else seed_path
            return Calibration.load(seed_path), writable_path
    from . import seed_2010_silverado as s
    return s.build_with_labels(), None


def apply_dark_theme(app: QApplication) -> None:
    """App-wide dark Fusion palette (dark mode is the primary theme)."""
    app.setStyle("Fusion")
    p = QPalette()
    window = QColor(37, 39, 43)
    base = QColor(28, 30, 34)
    text = QColor(222, 224, 228)
    disabled = QColor(120, 124, 130)
    highlight = QColor(53, 106, 195)
    p.setColor(QPalette.Window, window)
    p.setColor(QPalette.WindowText, text)
    p.setColor(QPalette.Base, base)
    p.setColor(QPalette.AlternateBase, window)
    p.setColor(QPalette.ToolTipBase, QColor(48, 50, 56))
    p.setColor(QPalette.ToolTipText, text)
    p.setColor(QPalette.Text, text)
    p.setColor(QPalette.PlaceholderText, disabled)
    p.setColor(QPalette.Button, QColor(45, 48, 53))
    p.setColor(QPalette.ButtonText, text)
    p.setColor(QPalette.BrightText, QColor(255, 120, 110))
    p.setColor(QPalette.Link, QColor(120, 170, 240))
    p.setColor(QPalette.Highlight, highlight)
    p.setColor(QPalette.HighlightedText, QColor(245, 246, 248))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        p.setColor(QPalette.Disabled, role, disabled)
    app.setPalette(p)


def app_icon_path() -> Optional[str]:
    # Candidate locations: dev tree, and PyInstaller's bundled data dir.
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidates = [os.path.join(here, "assets", "openobd.ico")]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(os.path.join(meipass, "assets", "openobd.ico"))
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


def main(argv=None):
    argv = argv if argv is not None else sys.argv
    app = QApplication(argv)
    icon_path = app_icon_path()
    if icon_path:
        app.setWindowIcon(QIcon(icon_path))
    apply_dark_theme(app)
    args = argv[1:]
    want_gt = "--gt" in args
    paths = [a for a in args if not a.startswith("-") and os.path.exists(a)]
    if paths:
        cal, path = Calibration.load(paths[0]), paths[0]
    else:
        cal, path = load_default_cal()
    win = MainWindow(cal, path)
    win.show()
    if want_gt:
        try:
            win.main_tabs.setCurrentIndex(0)   # Dashboard
        except Exception:
            pass
        QTimer.singleShot(300, win.dashboard.connect_gt)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
