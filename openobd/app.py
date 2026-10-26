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
                live link (auto-connected when its USB ID appears), or a
                truck-mcp drive-log session (tmsource): live
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
import html
import logging
import os
import sys
import threading
import time
from typing import Optional

from PySide6.QtCore import Qt, QTimer, QEvent, QPoint, QSize, Signal
from PySide6.QtGui import (
    QAction, QActionGroup, QCursor, QIcon, QKeySequence, QShortcut, QFont,
    QUndoCommand, QUndoStack,
)
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QTreeWidget, QTreeWidgetItem, QTableView, QTabWidget, QSplitter, QLabel,
    QComboBox, QCheckBox, QPushButton, QFileDialog, QTextEdit, QTableWidget,
    QTableWidgetItem, QHeaderView, QMessageBox, QGroupBox, QLineEdit,
    QSlider, QListWidget, QListWidgetItem, QMenu, QDialog, QDialogButtonBox,
    QFormLayout, QSizePolicy, QToolTip, QDoubleSpinBox, QStackedWidget,
    QToolBar, QToolButton,
)

import numpy as np
import pyqtgraph as pg

from . import __version__, appsettings, editops, theme
from .appsettings import (SettingsError, app_settings, read_settings,
                          save_pref, set_on_pref_write_failed,
                          write_settings)
from .calspec import Calibration
from .diagui import DiagnosticsPage
from .activetests import ActiveTestsPage
from .livedata import LiveDataPage
from .logbin import (
    Log, Overlay, parse_csv, analyze_log, bin_log_to_table, detect_shift_points,
    CANONICAL, time_axis, attach_long, calibrate_pedal_from_log,
)
from . import pollplan
from .pedal import PedalCal

theme.configure_pyqtgraph()
from .model import CalTableModel, HeatmapDelegate, clean_unit
from .tuningui import (
    Breadcrumb, CalTableView, ChannelTree, CollapsiblePanel, CursorReadout,
    ElidedLabel, Navigator, ScalarsTree, empty_state_label, format_selection_stats,
    make_toolbar, plain_tooltip, table_category,
)
from .transport import DataSource, LogReplaySource, GtDataSource, Sample
from . import tmsource, tmstore
from .recorder import (
    DriveRecorder, default_log_dirs, meta_path, read_meta, sidecar_path,
)
from PySide6.QtCore import QStandardPaths
from .recorder import (  # noqa: E402  (log folder setting, v0.39.0)
    LOG_DIR_KEY, log_dir, log_dir_unavailable, probe_log_dir,
    resolve_log_dir, used_configured_dir,
)

# Gauge widgets, cluster layouts and the dashboard style registry live in
# gauges.py; the names are re-exported here for existing importers.
from .gauges import (  # noqa: E402
    DASHBOARD_STYLES, GaugeBase, build_classic_cluster, build_modern_cluster,
)

_log = logging.getLogger(__name__)


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
    """The stored pedal calibration for a VIN, or None when none is stored.
    Raises SettingsReadError when the store reports an error: a value missing
    from a store that could not be read is NOT "none stored", and treating it
    so would let a learned value be saved unmerged over the real one."""
    group = _pedal_settings_group(vin)

    def read(st) -> dict:
        st.beginGroup(group)
        vals = {name: st.value(name) for name in ("rest", "full", "source")}
        st.endGroup()
        return vals
    cal = PedalCal.from_dict(read_settings(
        read, what=f"pedal calibration for VIN {vin}"))
    return None if cal.rest is None and cal.full is None else cal


def try_load_pedal_cal(vin: str) -> tuple[Optional[PedalCal], Optional[str]]:
    """(calibration or None, error or None). A read FAILURE is not "none
    stored": the caller must then neither seed nor save for that VIN, or a
    live/learned value would overwrite the real stored one."""
    try:
        return load_pedal_cal(vin), None
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def save_pedal_cal(vin: str, cal: PedalCal) -> None:
    """Store a VIN's pedal calibration durably, or raise SettingsWriteError
    (after appsettings' bounded retries): callers must then report it as NOT
    saved."""
    group = _pedal_settings_group(vin)

    def apply(st) -> None:
        st.beginGroup(group)
        for name, val in (("rest", cal.rest), ("full", cal.full),
                          ("source", cal.source)):
            if val is None:
                st.remove(name)
            else:
                st.setValue(name, val)
        st.endGroup()
    write_settings(apply, what=f"pedal calibration for VIN {vin}")


def configured_log_dir() -> str:
    """The ONE read of the Settings log folder (QSettings logs/dir): the
    user-chosen folder, or "" for the default (Documents\\OpenOBD\\Logs)."""
    value = app_settings().value(LOG_DIR_KEY, "")
    return value.strip() if isinstance(value, str) else ""


#: How often the Dashboard looks for the OBDX Pro GT while none is bound.
GT_SCAN_INTERVAL_MS = 2000
#: A connect that has not finished after this long is marked failed (and its
#: late result dropped). Every serial call in ObdxGt.open is bounded (2 s
#: write timeout, 1.2 s read deadline), but the OS-level port open is not,
#: and the sum of bounded steps can exceed this on a sick link.
GT_CONNECT_TIMEOUT_MS = 20_000
#: Wait before retrying after a failed open: 5 s, then 15 s, then every 30 s.
GT_RETRY_BACKOFF_S = (5.0, 15.0, 30.0)
#: Appended to the status line when the GT appears while the user has a log
#: replay or truck-mcp session bound (auto-connect leaves their choice be).
GT_DETECTED_HINT = ("· OBDX Pro GT detected — Settings → Reconnect GT now "
                    "to switch")
REPLAY_RECORD_REASON = "Recording needs a live GT; this is a replay."
GT_OPEN_HUNG = "previous GT open still hung; waiting for it to return"
#: Menu items the Dashboard's instructions name (MainWindow builds them).
RECONNECT_GT_LABEL = "Reconnect GT now"
DISCONNECT_GT_LABEL = "Disconnect GT"
OPEN_TM_SESSION_LABEL = "Open truck-mcp session…"
RECONNECT_GT_MENU_PATH = f"Settings → {RECONNECT_GT_LABEL}"
LOG_FOLDER_LABEL = "Log folder…"
LOOKING_FOR_GT = "Looking for OBDX Pro GT… (plug in the USB)"
TM_PICKER_HEADER = ("Stored sessions — not the truck; recording needs a "
                    "live GT")
RECORD_IDLE_TEXT = "⏺  RECORD"
MARK_TEXT = "📍  MARK"
#: Keyboard help, shown as the status line's tooltip.
DASHBOARD_KEYS_HELP = (
    "Keys: R record / stop · M mark (while recording) · Space pause / resume "
    "replay · F5 start replay · Shift+F5 stop replay · Ctrl+G "
    f"{RECONNECT_GT_LABEL.lower()} · Ctrl+T open truck-mcp session")

# The primary action is styled on the button object only; everything else
# keeps the app's dark Fusion palette (apply_dark_theme). Both come from the
# theme tokens (openobd.theme), values unchanged from v0.34.0.
RECORD_BUTTON_QSS = theme.record_button_qss()
MARK_BUTTON_QSS = theme.mark_button_qss()
#: The primary row's height: tall enough to hit while driving, small enough
#: not to take the cluster's room on a 600 px tablet screen.
PRIMARY_ROW_HEIGHT = theme.PRIMARY_HEIGHT

#: QSettings key: is the editor's Log overlay panel expanded ("true"/"false").
OVERLAY_PANEL_KEY = "tuning/overlay_panel_open"
#: The Log Analysis plot area before a log is loaded.
LOG_EMPTY_TEXT = ("No log loaded — Load Log… or record a drive on the "
                  "Dashboard")


def detect_gt_ports() -> list[str]:
    """COM ports carrying the OBDX Pro GT's USB VID/PID right now. Only
    enumerates (pyserial list_ports, cheap, safe on the GUI thread); the
    serial open stays on the connect worker. The Dashboard's default ports
    provider, looked up at call time so tests can swap it out."""
    from . import gt as _gt
    return _gt.find_gt_ports()


def format_elapsed(seconds: Optional[float]) -> str:
    """mm:ss, or h:mm:ss from an hour on; 00:00 before the first sample."""
    total = max(0, int(seconds or 0))
    hours, rem = divmod(total, 3600)
    mins, secs = divmod(rem, 60)
    return (f"{hours}:{mins:02d}:{secs:02d}" if hours
            else f"{mins:02d}:{secs:02d}")


def format_record_label(count: int, elapsed: Optional[float]) -> str:
    """The Record button's text while recording."""
    return (f"⏹  STOP  ·  {count:,} samples  ·  "
            f"{format_elapsed(elapsed)}")


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
    # Payloads carry the connect's token: (connect_id, source) / (connect_id,
    # message), so a result that outlived its connect (watchdog, cancel) is
    # recognised and dropped.
    gt_ready = Signal(object)
    gt_error = Signal(object)
    # The recording-start vehicle read (identity + DTCs, poll thread held)
    # runs on a worker thread too: (recorder, context dict).
    record_ctx_ready = Signal(object)

    def __init__(self, style: str = "classic", ports_provider=None,
                 scan_interval_ms: int = GT_SCAN_INTERVAL_MS,
                 gt_factory=None, now=None,
                 connect_timeout_ms: int = GT_CONNECT_TIMEOUT_MS):
        """ports_provider: callable returning the GT's COM ports (default
        detect_gt_ports, the live USB enumeration); scan_interval_ms: the
        auto-connect scan period; gt_factory: builds the source the connect
        worker start()s (default GtDataSource). They exist so tests can
        simulate a GT arriving, leaving and connecting without hardware.
        now: the monotonic clock the retry backoff runs on (default
        time.monotonic); connect_timeout_ms: the connect watchdog."""
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
        # Why the stored calibration for _pedal_key could not be READ on
        # this connection; while set, nothing is seeded or saved for it.
        self._pedal_store_error: Optional[str] = None
        # Why the latest calibration change for _pedal_key could NOT be
        # saved (cleared by the next successful save); recordings say so.
        self._pedal_save_error: Optional[str] = None
        # Metadata rewrites (markers, calibration changes, link events) are
        # coalesced: one atomic rewrite at most every 250 ms, so a held M
        # key or a run of calibration updates cannot stall the GUI thread
        # on fsync. close() always writes the final document.
        self._meta_timer = QTimer(self)
        self._meta_timer.setSingleShot(True)
        self._meta_timer.setInterval(250)
        self._meta_timer.timeout.connect(self._flush_meta)

        # GT auto-connect (see _scan_for_gt). All of this state is touched
        # on the GUI thread only; the connect worker reports back through
        # the gt_ready / gt_error signals.
        self._ports_provider = ports_provider
        self._gt_factory = gt_factory or GtDataSource
        self._now = now or time.monotonic
        # Token of the connect in flight; bumped by every new connect, a
        # cancel (Disconnect) and the watchdog, so a late result is dropped.
        self._connect_id = 0
        # Retry backoff after a failed open (GT_RETRY_BACKOFF_S), reset when
        # the port disappears, on success, and by "Reconnect GT now".
        self._fail_count = 0
        self._retry_at: Optional[float] = None
        self._gt_port: Optional[str] = None      # last port the scan saw
        # The latest connect worker; a new connect waits while it is alive.
        self._connect_thread: Optional[threading.Thread] = None
        # The recording-start read in flight: (thread, result box); the
        # result applied last, so the queued signal never applies it twice.
        self._ctx_worker = None
        self._ctx_applied = None
        self._connect_watchdog = QTimer(self)
        self._connect_watchdog.setSingleShot(True)
        self._connect_watchdog.setInterval(connect_timeout_ms)
        self._connect_watchdog.timeout.connect(self._on_connect_timeout)
        self._gt_connecting = False
        self._connect_auto = False
        self._connect_epoch = 0
        # Bumped on every bind: an automatic connect that finishes after the
        # user bound something else must not replace their choice.
        self._bind_epoch = 0
        # Set by a deliberate Disconnect: the GT is not re-opened while it
        # stays plugged in. Cleared by "Reconnect GT now" or when the scan
        # sees the port disappear (unplugged), so a replug connects again.
        self._auto_connect_suppressed = False
        self._shut_down = False
        self._record_tip = ""
        # Why this recording is not in the configured log folder (or None).
        self._rec_dir_note: Optional[str] = None

        root = QVBoxLayout(self)
        self.status = QLabel(LOOKING_FOR_GT)
        self.status.setToolTip(plain_tooltip(DASHBOARD_KEYS_HELP))
        root.addWidget(self.status)

        # The primary row: one big Record/Stop button, Mark beside it while
        # recording. Capped height so the cluster keeps its room on 600 px.
        primary = QHBoxLayout()
        primary.setSpacing(8)
        self.btn_record = QPushButton(RECORD_IDLE_TEXT)
        self.btn_record.setObjectName("recordButton")
        self.btn_record.setStyleSheet(RECORD_BUTTON_QSS)
        self.btn_record.setProperty("recording", False)
        self.btn_record.setMinimumHeight(PRIMARY_ROW_HEIGHT)
        self.btn_record.setMaximumHeight(PRIMARY_ROW_HEIGHT + 8)
        self.btn_record.setSizePolicy(QSizePolicy.Expanding,
                                      QSizePolicy.Fixed)
        self.btn_record.clicked.connect(self.toggle_record)
        self.btn_mark = QPushButton(MARK_TEXT)
        self.btn_mark.setObjectName("markButton")
        self.btn_mark.setStyleSheet(MARK_BUTTON_QSS)
        self.btn_mark.setMinimumHeight(PRIMARY_ROW_HEIGHT)
        self.btn_mark.setMaximumHeight(PRIMARY_ROW_HEIGHT + 8)
        self.btn_mark.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.btn_mark.clicked.connect(self.add_marker)
        self.btn_mark.setEnabled(False)
        self.btn_mark.setVisible(False)
        primary.addWidget(self.btn_record, 2)
        primary.addWidget(self.btn_mark, 1)
        root.addLayout(primary)

        # Replay transport (hidden for the live GT, which has no Stop button:
        # Settings → Disconnect GT, or unplug it). Start/Stop show for every
        # other source; Pause/Speed/slider only for a seekable replay.
        self.replay_ctl = QWidget()
        rb = QHBoxLayout(self.replay_ctl)
        rb.setContentsMargins(0, 0, 0, 0)
        self.btn_start = QPushButton("▶ Start")
        self.btn_stop = QPushButton("⏹ Stop")
        self.btn_start.clicked.connect(self.start)
        self.btn_stop.clicked.connect(self.stop)
        self.btn_stop.setEnabled(False)
        rb.addWidget(self.btn_start)
        rb.addWidget(self.btn_stop)
        self.btn_pause = QPushButton("⏸ Pause")
        self.btn_pause.clicked.connect(self._toggle_pause)
        rb.addWidget(self.btn_pause)
        self._speed_label = QLabel("Speed:")
        rb.addWidget(self._speed_label)
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
        self._sync_controls()

        # Look for the GT now and then every scan_interval_ms while none is
        # bound; _scan_for_gt decides on each tick whether to look at all.
        self._scan_timer = QTimer(self)
        self._scan_timer.setInterval(scan_interval_ms)
        self._scan_timer.timeout.connect(self._scan_for_gt)
        self._scan_for_gt()
        self._scan_timer.start()

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
            g.setToolTip(plain_tooltip(
                "Pedal raw: APP D as decoded (about 19 % at rest on "
                f"this truck){why} — click to reset min/max capture"))
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
        self._pedal_store_error = None
        self._pedal_save_error = None
        if self._pedal_key:
            self._seed_from_settings(src, self._pedal_key)
        rec = self._recorder
        if not first and self.recording and rec is not None:
            events = rec.meta.setdefault("link_events", [])
            events.append({"t": rec.recording_time(src.clock()),
                           "event": "GT link re-opened; pedal calibration "
                                    "reset, VIN re-read",
                           "vin": link[1]})
            pc = rec.meta.setdefault("pedal_calibration", {})
            pc["persistence"] = self._persistence()
            self._schedule_meta()

    def _seed_from_settings(self, src: GtDataSource, vin: str) -> None:
        stored, err = try_load_pedal_cal(vin)
        if err is not None:
            self._pedal_store_error = err
            self.status.setText(
                f"Stored pedal calibration for VIN {vin} is unreadable "
                f"({err}): not applied, and nothing will be saved over it "
                f"this session.")
            return
        if stored is not None:
            src.seed_pedal_calibration(stored)

    def _persistence(self) -> str:
        if self._pedal_key and self._pedal_store_error:
            return (f"settings pedal_cal/{self._pedal_key} UNREADABLE "
                    f"({self._pedal_store_error}): not seeded, not saved "
                    f"this session")
        if self._pedal_key and self._pedal_save_error:
            return (f"settings pedal_cal/{self._pedal_key} NOT SAVED "
                    f"({self._pedal_save_error})")
        return (f"settings pedal_cal/{self._pedal_key}" if self._pedal_key
                else "session-only (VIN not examined on this connection)")

    def _store_pedal_cal(self, vin: str, cal: PedalCal) -> Optional[str]:
        """The ONE save path for a pedal calibration (the live GT's and
        Learn's from a log): store it under `vin`, or report it NOT saved on
        the status line and in the log, and -- when `vin` is this
        connection's -- in _pedal_save_error, so the recording's
        pedal_calibration.persistence never claims "settings pedal_cal/<VIN>"
        for a value that is not there. Returns None when stored, else the
        error. Only a SettingsError is a save failure; anything else is a
        defect and propagates."""
        try:
            save_pedal_cal(vin, cal)
            err = None
        except SettingsError as exc:
            err = f"{type(exc).__name__}: {exc}"
            _log.warning("pedal calibration NOT saved: %s", exc)
            self.status.setText(
                f"Could not store the pedal calibration for VIN {vin}: {exc}")
        if vin == self._pedal_key and err != self._pedal_save_error:
            self._pedal_save_error = err
            rec = self._recorder
            if self.recording and rec is not None:
                pc = rec.meta.setdefault("pedal_calibration", {})
                pc["persistence"] = self._persistence()
                self._schedule_meta()
        return err

    def _schedule_meta(self) -> None:
        if self._recorder is not None and not self._meta_timer.isActive():
            self._meta_timer.start()

    def _flush_meta(self) -> None:
        """The coalesced metadata rewrite (atomic, recorder._write_meta)."""
        rec = self._recorder
        if rec is None:
            return
        if not rec.flush_meta() and rec.meta_error:
            self.status.setText(
                f"Recording metadata write failed: {rec.meta_error} — the "
                f"CSVs are unaffected")

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
        if self._pedal_key and not self._pedal_store_error:
            self._store_pedal_cal(self._pedal_key, cal)
        rec = self._recorder
        if self.recording and rec is not None:
            entry = {k: v for k, v in cal.to_dict().items() if k != "rules"}
            entry["t"] = rec.recording_time(src.clock())
            pc = rec.meta.setdefault("pedal_calibration", {})
            pc.setdefault("changes", []).append(entry)
            pc["persistence"] = self._persistence()
            self._schedule_meta()

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
        button is disabled (Record with no live source, Stop for the GT).
        The first key of each binding is the one advertised; the others are
        aliases kept for muscle memory (Ctrl+R, P). Connect GT (Ctrl+G) and
        truck-mcp session (Ctrl+T) are MainWindow menu actions now.

        The key hint is appended when the tooltip is SHOWN (eventFilter), not
        baked into the tooltip text: the recorder rewrites btn_record's
        tooltip with the current/last log path, and the hint must survive
        that."""
        bindings = [
            (self.btn_record, ("R", "Ctrl+R"),
             "Start / stop recording a datalog"),
            (self.btn_mark, ("M",), "Drop a marker into the recording"),
            (self.btn_start, ("F5",), "Start the replay"),
            (self.btn_stop, ("Shift+F5",), "Stop the replay"),
            (self.btn_pause, ("Space", "P"), "Pause / resume the replay"),
        ]
        self._shortcuts = []
        self._shortcut_hints = {}
        for btn, keys, what in bindings:
            for key in keys:
                sc = QShortcut(QKeySequence(key), self)
                sc.setContext(Qt.WindowShortcut)
                sc.activated.connect(btn.click)
                self._shortcuts.append(sc)
            self._shortcut_hints[btn] = (
                what, QKeySequence(keys[0]).toString(QKeySequence.NativeText))
            btn.installEventFilter(self)

    def shortcut_tooltip(self, btn) -> str:
        """The tooltip a button shows: its current tooltip text (or the
        action description when none is set) plus the shortcut key. The
        stored tooltip may already be plain_tooltip-escaped (the record
        button carries a file path); it is unwrapped and the whole composed
        text re-escaped, so the shown tooltip is literal end to end."""
        return plain_tooltip(self.shortcut_text(btn))

    def shortcut_text(self, btn) -> str:
        """shortcut_tooltip before escaping: the stored tooltip unwrapped
        from plain_tooltip (its <br> back to newlines, entities decoded;
        a literal "<br>" in the source text was escaped, so every raw <br>
        here is a converted newline) plus the shortcut key."""
        what, key = self._shortcut_hints[btn]
        tip = btn.toolTip()
        if tip.startswith("<qt>") and tip.endswith("</qt>"):
            tip = html.unescape(
                tip[len("<qt>"):-len("</qt>")].replace("<br>", "\n"))
        return f"{tip or what} ({key})"

    def _set_record_tooltip(self, text: str) -> None:
        """Record-button tooltip: a recording path or block reason is
        runtime text, so it renders literally. Empty clears the tooltip."""
        self.btn_record.setToolTip(plain_tooltip(text) if text else "")

    def eventFilter(self, obj, event):
        if (event.type() == QEvent.ToolTip
                and obj in getattr(self, "_shortcut_hints", {})):
            QToolTip.showText(event.globalPos(),
                              plain_tooltip(self.shortcut_text(obj)), obj)
            return True
        return super().eventFilter(obj, event)

    def _halt_source(self) -> bool:
        """Stop the bound source, THEN finalize an active recording (no
        dialog). The order matters: GtDataSource.stop() joins the poll thread
        for up to 3 s while it keeps publishing, and those samples are only
        on disk if the final drain runs after the thread has stopped. True
        when a recording was finalized (its outcome is in the status line).
        Every stop path goes through here: stop(), Disconnect GT, a rebind
        and app close."""
        self.timer.stop()
        if self.source:
            try:
                self.source.stop()
            except Exception:
                pass
        if self.recording:
            self._await_record_context()
            self._finalize_record()
            return True
        return False

    def _await_record_context(self) -> None:
        """Wait (bounded) for the recording-start read still in flight and
        apply its result to the open recording, so it never lands on a
        finalized one. The source is already stopped: GtDataSource.stop()
        leaves the port to that worker until it releases the IO lock."""
        pending, self._ctx_worker = self._ctx_worker, None
        if pending is None:
            return
        thread, box = pending
        thread.join(timeout=3.0)
        payload = box.get("payload")
        if payload is not None:
            self._on_record_context(payload)

    def _teardown_source(self):
        """Stop and drop the current source. Always called before a new one is
        bound so a live GT poll thread can't keep running invisibly."""
        self._halt_source()
        self._demote_live_badges()
        self.source = None
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self._sync_controls()

    def bind_source(self, source: DataSource):
        if self._gt_connecting:
            # The user chose this source while a GT was opening: that connect
            # is cancelled, its result released and its failure ignored.
            self._cancel_connect()
        self._teardown_source()
        self._bind_epoch += 1
        self.source = source
        avail = set(source.channels())
        # The cluster is permanent — reset every gauge and dim the ones this
        # source can't feed instead of rebuilding a filtered grid.
        for key, g in self.gauges.items():
            g.reset()
            g.set_available(key in avail)
        n = sum(1 for k in self.gauges if k in avail)
        self.status.setText(f"Source bound: {n} of {len(self.gauges)} gauges live.")

        # Pause/Speed/slider only make sense for a seekable replay;
        # _sync_controls decides what the transport strip shows.
        is_replay = hasattr(source, "seek")
        self._sync_controls()
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
        if isinstance(self.source, GtDataSource):
            # A GT is (re)opened through the connect worker, never by a
            # serial open on the GUI thread.
            self.status.setText(
                f'Use "{RECONNECT_GT_MENU_PATH}" to reconnect the GT.')
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
        self._sync_controls()

    def stop(self):
        """Stop the bound source (replay Stop, Settings → Disconnect GT).
        An active recording is finalized first, without a dialog; its
        outcome (saved path, or the error) stays in the status line."""
        saved = self.status.text() if self._halt_source() else None
        self._demote_live_badges()
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.status.setText(f"Stopped. {saved}" if saved else "Stopped.")
        self._sync_controls()

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

    # -- control state ------------------------------------------------------- #
    def _gt_live(self) -> bool:
        """A GT is bound and its poll thread runs (connected, or re-opening
        the link itself after a drop)."""
        src = self.source
        return isinstance(src, GtDataSource) and src._thread is not None

    def _record_block_reason(self) -> Optional[str]:
        """Why Record cannot start right now, or None when it can. Only a
        live GT records."""
        src = self.source
        if src is None:
            return ("No live source: plug in the OBDX Pro GT (it connects "
                    "by itself).")
        if getattr(src, "is_store_record", False):
            return tmsource.record_refusal_message(
                live=isinstance(src, tmsource.TmLiveSource),
                provenance=getattr(src, "provenance",
                                   tmstore.PROVENANCE_REAL),
                connect_label=RECONNECT_GT_MENU_PATH)
        if not isinstance(src, GtDataSource):
            return REPLAY_RECORD_REASON
        if src._thread is None:
            return (f'The GT is disconnected: choose "{RECONNECT_GT_MENU_PATH}"'
                    " (or unplug and replug it).")
        return None

    def _refresh_record_button(self) -> None:
        """Record button text and look for the current recording state."""
        rec = self._recorder
        recording = self.recording and rec is not None
        if recording:
            clock = getattr(self.source, "clock", None)
            now = clock() if clock else None
            text = format_record_label(rec.count, rec.recording_time(now))
        else:
            text = RECORD_IDLE_TEXT
        if self.btn_record.text() != text:
            self.btn_record.setText(text)
        if bool(self.btn_record.property("recording")) != recording:
            self.btn_record.setProperty("recording", recording)
            # A dynamic property only restyles after a re-polish.
            self.btn_record.style().unpolish(self.btn_record)
            self.btn_record.style().polish(self.btn_record)

    def _sync_controls(self) -> None:
        """Enable/show the controls for the bound source and the recording
        state. Cheap; called after every state change."""
        src = self.source
        is_gt = isinstance(src, GtDataSource)
        if self.recording:
            self.btn_record.setEnabled(True)    # Stop must always work
            self._set_record_tooltip(self._record_tip)
        else:
            why = self._record_block_reason()
            self.btn_record.setEnabled(why is None)
            self._set_record_tooltip(why or self._record_tip)
        self._refresh_record_button()
        self.btn_mark.setEnabled(self.recording)
        self.btn_mark.setVisible(self.recording)
        self.replay_ctl.setVisible(src is not None and not is_gt)
        seekable = hasattr(src, "seek")
        for w in (self.btn_pause, self._speed_label, self.speed_combo,
                  self.pos_slider, self.time_label):
            w.setVisible(seekable)
        if is_gt:
            # The GT has no transport: Stop is Settings → Disconnect GT, and
            # a start goes through the connect worker.
            self.btn_start.setEnabled(False)
            self.btn_stop.setEnabled(False)

    # -- OBDX Pro GT: auto-connect, connect, disconnect ---------------------- #
    def _user_source_bound(self) -> bool:
        """The user bound a non-GT source (log replay, truck-mcp session):
        auto-connect leaves it alone."""
        src = self.source
        return src is not None and not isinstance(src, GtDataSource)

    def _set_detected_hint(self, present: bool) -> None:
        """Show (GT port present while the user's source is bound) or clear
        the 'GT detected' hint on the status line. Re-appended when another
        message replaced the line (e.g. a replay's Stop), never doubled."""
        text = self.status.text()
        has = GT_DETECTED_HINT in text
        if present and not has:
            self.status.setText(f"{text} {GT_DETECTED_HINT}".strip())
        elif not present and has:
            self.status.setText(
                text.replace(f" {GT_DETECTED_HINT}", "")
                .replace(GT_DETECTED_HINT, "").strip())

    def _reset_retry(self) -> None:
        self._fail_count = 0
        self._retry_at = None

    def _scan_for_gt(self) -> None:
        """GUI-thread scan tick: enumerate ports (cheap, opens nothing).
        When the GT's USB ID is present and nothing stands in the way,
        connect through connect_gt (serial open on the worker). Nothing
        stands in the way unless: a connect is in flight, a GT is live, the
        user bound a log/session (then only the 'detected' hint shows), a
        Disconnect is in force, or a failed open's backoff has not run
        out. Port presence is checked even while a connect is in flight, so
        an unplug mid-open cancels it and resets the backoff."""
        if self._shut_down or self._gt_live():
            return
        provider = self._ports_provider or detect_gt_ports
        try:
            ports = list(provider())
        except Exception as exc:     # enumeration failed; try next tick
            self.status.setText(
                f"Could not list serial ports: {type(exc).__name__}: {exc}")
            return
        self._gt_port = ports[0] if ports else None
        user_bound = self._user_source_bound()
        if not ports:
            # Unplugged: a deliberate Disconnect, any retry backoff and a
            # connect still opening end here, so the next plug-in connects
            # at once (a hung worker still blocks it; see connect_gt).
            if self._gt_connecting:
                self._cancel_connect()
            self._auto_connect_suppressed = False
            self._reset_retry()
            self._set_detected_hint(False)
            if not user_bound and self.status.text() != LOOKING_FOR_GT:
                self.status.setText(LOOKING_FOR_GT)
            return
        if user_bound:
            self._set_detected_hint(True)
            return
        if self._gt_connecting or self._auto_connect_suppressed:
            return
        if self._retry_at is not None and self._now() < self._retry_at:
            return
        self.connect_gt(auto=True)

    def reconnect_gt_now(self) -> None:
        """Settings → Reconnect GT now: lifts a Disconnect and the retry
        backoff, and connects (or cuts a live link's reconnect wait)."""
        self._auto_connect_suppressed = False
        self._reset_retry()
        self.connect_gt()

    def _cancel_connect(self) -> None:
        """Forget the connect in flight: its late result is released, not
        bound (the token no longer matches)."""
        self._connect_id += 1
        self._gt_connecting = False
        self._connect_auto = False
        self._connect_watchdog.stop()

    def disconnect_gt(self) -> None:
        """Settings → Disconnect GT: stop the GT (an active recording is
        finalized after the poll thread stopped) and keep auto-connect from
        re-opening it until it is unplugged and replugged or "Reconnect GT
        now" is chosen. A connect still opening is cancelled the same way."""
        if self._gt_connecting:
            self._auto_connect_suppressed = True
            self._cancel_connect()
            self.status.setText("GT connect cancelled; it will not be bound. "
                                f'Choose "{RECONNECT_GT_MENU_PATH}" to connect.')
            self._sync_controls()
            return
        if not self._gt_live():
            self.status.setText("No GT is connected.")
            return
        self._auto_connect_suppressed = True
        self.stop()
        saved = self.status.text().removeprefix("Stopped.").strip()
        note = (f'GT disconnected. It stays disconnected until you choose '
                f'"{RECONNECT_GT_MENU_PATH}" or unplug and replug it.')
        self.status.setText(f"{saved} · {note}" if saved else note)

    def connect_gt(self, auto: bool = False):
        """Connect the OBDX Pro GT on a worker thread (serial IO blocks).
        auto: started by the scan, not by the user. Whoever started it, a
        connect that lands after the user bound another source or chose
        Disconnect (or after the watchdog gave up on it) is released, not
        bound."""
        src = self.source
        if isinstance(src, GtDataSource) and src._thread is not None:
            # Already connected: a second open of the same COM port can only
            # fail ("Access is denied") — and a successful rebind would end an
            # active recording. A down link is re-opened by the poll thread;
            # asking to reconnect just skips its backoff wait.
            if src.failing():
                src.request_reconnect()
                self.status.setText("Reconnecting to the OBDX Pro GT…")
            else:
                self.status.setText(
                    f"Already connected to the GT @ {src.port_name or '?'}.")
            return
        if self._gt_connecting:
            # One open at a time: a second would race the first for the port.
            if not auto:
                self.status.setText("Already connecting to the OBDX Pro GT…")
            return
        worker_thread = self._connect_thread
        if worker_thread is not None and worker_thread.is_alive():
            # A timed-out or cancelled open has not returned (a hung OS
            # open): starting another would pile up blocked workers. Its
            # result is released through the stale-token path when it
            # returns; every scan tick checks again.
            if self.status.text() != GT_OPEN_HUNG:
                self.status.setText(GT_OPEN_HUNG)
            return
        self._connect_id += 1
        cid = self._connect_id
        self._gt_connecting = True
        self._connect_auto = auto
        self._connect_epoch = self._bind_epoch
        self._connect_watchdog.start()
        if not (auto and self._fail_count):
            # An automatic retry after a failure keeps the one stable
            # "found but could not be opened" line instead of flickering.
            self.status.setText("Connecting to OBDX Pro GT…")
        factory = self._gt_factory

        def worker():
            try:
                src = factory()
                src.start()
            except Exception as e:
                try:
                    self.gt_error.emit((cid, str(e)))
                except RuntimeError:
                    pass  # widget destroyed while connecting
                return
            try:
                self.gt_ready.emit((cid, src))
            except RuntimeError:
                src.stop()

        self._connect_thread = threading.Thread(target=worker,
                                                name="gt-connect", daemon=True)
        self._connect_thread.start()

    @staticmethod
    def _release_off_thread(src) -> None:
        """Stop a GT nobody will bind, off the GUI thread (stop() joins the
        poll thread for up to 3 s)."""
        threading.Thread(target=src.stop, name="gt-release",
                         daemon=True).start()

    def _on_gt_ready(self, payload):
        """A connect landed. Bound only when it is still THE connect (not
        cancelled, not timed out), the app is open, the user has not chosen
        Disconnect, and no other source was bound while it opened."""
        cid, src = payload
        if cid != self._connect_id or not self._gt_connecting:
            self._release_off_thread(src)     # cancelled or timed out
            return
        self._gt_connecting = False
        self._connect_auto = False
        self._connect_watchdog.stop()
        if self._shut_down:
            self._release_off_thread(src)
            return
        if self._auto_connect_suppressed:
            self._release_off_thread(src)
            self.status.setText("OBDX Pro GT opened after Disconnect; "
                                "released, not bound.")
            return
        if self._bind_epoch != self._connect_epoch:
            self._release_off_thread(src)
            self.status.setText(
                f"{self.status.text()} · OBDX Pro GT opened while you chose "
                f'another source; released, not bound ("{RECONNECT_GT_MENU_PATH}"'
                " to switch).")
            return
        self._reset_retry()
        self._bind_gt(src)

    def _bind_gt(self, src):
        """Bind a connected GT and build its live status line."""
        self.bind_source(src)          # tears down the old source first
        self.timer.start(100)
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
            if self._pedal_store_error:
                self._live_status += (" · stored pedal calibration unreadable"
                                      f" ({self._pedal_store_error}): not "
                                      "applied or saved this session")
            cal = src.pedal_calibration()
            if not cal.ready:
                self._live_status += (" · pedal cal pending (Pedal gauge "
                                      "shows raw APP D)")
        self._source_alarm = False
        self.status.setText(self._live_status)
        self._sync_controls()

    def _on_gt_error(self, payload):
        cid, msg = payload
        if cid != self._connect_id or not self._gt_connecting:
            return                            # cancelled or timed out already
        self._gt_connecting = False
        self._connect_auto = False
        self._connect_watchdog.stop()
        self._connect_failed(msg)

    def _on_connect_timeout(self) -> None:
        """Watchdog: the connect worker has not answered in time. Mark it
        failed (backoff applies) and drop whatever it delivers later."""
        if not self._gt_connecting:
            return
        secs = self._connect_watchdog.interval() / 1000
        self._cancel_connect()
        self._connect_failed(f"no answer within {secs:g} s")

    def _connect_failed(self, msg: str) -> None:
        """A failed open: schedule the retry (GT_RETRY_BACKOFF_S) and show
        ONE stable status line that later identical failures don't rewrite.
        Not for a connect the user superseded by binding a log/session: their
        status line stays and no backoff accrues."""
        if self._user_source_bound():
            return
        self._fail_count += 1
        delay = GT_RETRY_BACKOFF_S[min(self._fail_count,
                                       len(GT_RETRY_BACKOFF_S)) - 1]
        self._retry_at = self._now() + delay
        if self._gt_port:
            text = (f"OBDX Pro GT found on {self._gt_port} but it could not be "
                    f"opened ({msg}) · retrying")
        else:
            text = f"GT connect failed: {msg}"
        if self.status.text() != text:
            self.status.setText(text)
        self._sync_controls()

    # -- truck-mcp drive-log sessions as a gauge source ---------------------- #
    def _build_tm_menu(self, sessions: list) -> QMenu:
        """The session picker for tmstore.list_sessions() entries. Simulated
        sessions carry "[SIM] ", and a disabled header says what these are:
        stored sessions, not a live link to the truck."""
        menu = QMenu(self)
        head = menu.addAction(TM_PICKER_HEADER)
        head.setEnabled(False)
        menu.addSeparator()
        if not sessions:
            act = menu.addAction("No drive-log sessions found")
            act.setEnabled(False)
        for s in sessions[:25]:
            tag = tmstore.session_tag(s, short=True)
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
        return menu

    def pick_tm_session(self, anchor: Optional[QPoint] = None):
        """File → Open truck-mcp session…: a menu of truck-mcp sessions,
        listed fresh every time, shown at `anchor` (global) or the cursor.
        Read-only store access — binding a session never touches the serial
        port."""
        try:
            sessions = tmstore.list_sessions()
        except OSError as exc:
            self.status.setText(f"Could not scan truck-mcp sessions: {exc}")
            return
        menu = self._build_tm_menu(sessions)
        menu.exec(anchor if anchor is not None else QCursor.pos())
        menu.deleteLater()   # parented to self — would otherwise leak per open

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
        self._sync_controls()     # Record's refusal names the provenance

    def toggle_record(self):
        if not self.recording:
            # One gate with the button (_record_block_reason): only a live
            # GT records. A truck-mcp session IS already a record, with
            # per-sample freshness/quality this CSV cannot carry, and a log
            # replay would only copy an existing log into a new CSV.
            why = self._record_block_reason()
            if why:
                self.status.setText(why)
                return
            # Columns = everything the source can EVER produce, not just what
            # answered so far, so a channel that comes alive mid-drive is kept.
            avail = set(self.source.recordable_channels())
            keys = [k for k in RECORD_ORDER if k in avail]
            dirs, dir_problem = self._record_target()
            try:
                # The final file, in a durable folder, from the first sample:
                # there is no temp file to move and no save dialog to cancel.
                self._recorder = DriveRecorder(
                    keys, RECORD_LABELS, dirs,
                    meta=self._recording_meta(keys))
            except Exception as e:
                self.status.setText(f"Can't start recording: {e}"
                                    + (f" — {dir_problem}" if dir_problem
                                       else ""))
                return
            self._rec_dir_note = self._log_dir_note(dir_problem,
                                                    self._recorder.path)
            self.last_record_path = self._recorder.path
            self.source.drain()  # discard the pre-record backlog
            self.recording = True
            self._record_tip = self._with_dir_note(
                f"Recording to {self._recorder.path}", "\n")
            self._sync_controls()
            self.status.setText(self._with_dir_note(
                f"Recording → {self._recorder.path}", " — "))
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
            payload = (src, rec, ctx)
            box["payload"] = payload
            try:
                self.record_ctx_ready.emit(payload)
            except RuntimeError:
                pass   # widget destroyed meanwhile; the file stays valid

        box: dict = {}
        thread = threading.Thread(target=worker, name="gt-record-ctx",
                                  daemon=True)
        self._ctx_worker = (thread, box)
        thread.start()

    def _on_record_context(self, payload) -> None:
        if payload is self._ctx_applied:
            return              # already applied by _await_record_context
        self._ctx_applied = payload
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
            self._pedal_store_error = None
            self._pedal_save_error = None
            self._seed_from_settings(src, vin)
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
        mark = rec.add_marker(clock() if clock else None, write=False)
        if mark is None:
            self.status.setText("No sample recorded yet — marker not added.")
            return
        self._schedule_meta()
        self.status.setText(f"📍 {mark['label']} at {mark['t']:.1f} s")

    def _record_dirs(self) -> list[str]:
        """Where recordings go: Documents\\OpenOBD\\Logs, resolved through
        Qt's known-folder lookup (follows a OneDrive-redirected Documents,
        which ~/Documents does not), else %LOCALAPPDATA%\\OpenOBD\\Logs."""
        docs = QStandardPaths.writableLocation(QStandardPaths.DocumentsLocation)
        data = QStandardPaths.writableLocation(QStandardPaths.GenericDataLocation)
        return default_log_dirs(docs or None,
                                os.path.join(data, "OpenOBD") if data else None)

    def _record_target(self) -> tuple[list[str], Optional[str]]:
        """(folders to try, problem) for the next recording: the Settings log
        folder when set and writable, else the default folders with the
        reason the configured one was skipped."""
        return log_dir(configured_log_dir(), self._record_dirs())

    @staticmethod
    def _log_dir_note(problem: Optional[str], path: str) -> Optional[str]:
        """The honest status when a recording did not land in the configured
        folder (probe failed, or the create there failed and a default took
        it); None when it did, or when no folder is configured."""
        if problem:
            return problem
        configured = configured_log_dir()
        if configured and not used_configured_dir(configured, path):
            return log_dir_unavailable(configured)
        return None

    def _with_dir_note(self, text: str, sep: str) -> str:
        return f"{self._rec_dir_note}{sep}{text}" if self._rec_dir_note else text

    def _finalize_record(self, interactive: bool = False):
        """Close the recording. Every caller gets the same durable result; only
        a user-initiated stop (interactive) offers to open it in Log Analysis —
        teardown, rebind and app close must never block on a dialog."""
        self.recording = False
        self._meta_timer.stop()          # close() writes the final document
        rec, self._recorder = self._recorder, None
        self._sync_controls()
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
            self._record_tip = ""
            self._set_record_tooltip(self._record_tip)
            self.status.setText("Nothing recorded.")
            return
        folder = os.path.dirname(path)
        self._record_tip = self._with_dir_note(
            f"Last recording: {path}\nLog folder: {folder}", "\n")
        self._set_record_tooltip(self._record_tip)
        if rec.error:
            self.status.setText(self._with_dir_note(
                f"Recording stopped on an error ({rec.error}) — {n} samples "
                f"saved → {path}", " — "))
            return
        self.status.setText(self._with_dir_note(
            f"Saved {n} samples → {path}  ·  log folder: {folder}", " — "))
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
        self._refresh_record_button()

    def shutdown(self):
        """App close: finalize an active recording (no dialogs) and release
        the source, so the GT port and the log file are closed cleanly.
        Auto-connect stops too; a connect still in flight is released when
        it lands (_on_gt_ready)."""
        self._shut_down = True
        self._scan_timer.stop()
        self._connect_watchdog.stop()
        self._halt_source()

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
        if self.pos_slider.isVisible() and not self.pos_slider.isSliderDown():
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
        # The single chokepoint every table write passes through (cell edit,
        # math, smooth, interpolate, paste, revert): a non-finite result
        # (e.g. repeated x1e9 overflowing to inf) is refused before it can
        # become an undoable edit.
        for _rc, (_old, new) in changes.items():
            if editops.parse_number(new) is None:
                raise ValueError(f"non-finite result: {new!r}")
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


class ScalarEditCommand(QUndoCommand):
    """One scalar parameter edit (value old -> new) on the shared undo
    stack. Holds the scalar's index into Calibration.scalars."""

    def __init__(self, win: "MainWindow", index: int, old: float, new: float):
        if editops.parse_number(new) is None:  # same gate as BulkEditCommand
            raise ValueError(f"non-finite result: {new!r}")
        super().__init__(f"edit {win.cal.scalars[index].name}")
        self.win = win
        self.index = index
        self.old = old
        self.new = new

    def redo(self):
        self.win.cal.scalars[self.index].value = self.new
        self.win._scalar_touched(self.index)

    def undo(self):
        self.win.cal.scalars[self.index].value = self.old
        self.win._scalar_touched(self.index)


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
        # The stack's clean index is the saved state: undoing back to it
        # clears dirty, moving off it sets dirty (_on_clean_changed).
        self.undo_stack.setClean()
        self.undo_stack.cleanChanged.connect(self._on_clean_changed)

        self.setWindowTitle(self._title())
        self.resize(1180, 760)
        self._build_menu()

        # Top level: Dashboard opens first; Tuning holds the monitor/scan/tune
        # workflow; Diagnostics holds troubleshooting/active tests/codes.
        self.settings = app_settings()
        # Every save_pref call site (splitters, recent list, collapsed
        # groups, layout, ...) surfaces a preference that was NOT saved
        # through this one rate-limited notice.
        self._pref_failed_hook = self._on_pref_write_failed
        set_on_pref_write_failed(self._pref_failed_hook)
        self.dashboard = Dashboard(
            style=str(self.settings.value("dashboard/style", "classic")))
        self.main_tabs = QTabWidget()
        self.main_tabs.addTab(self.dashboard, "Dashboard")

        # Tuning: navigator | Editor / Scalars / Log Analysis. One theme
        # stylesheet on the root styles every widget inside (theme.py).
        tuning = QWidget()
        tuning.setObjectName("tuningPage")
        tuning.setStyleSheet(theme.tuning_qss())
        self.tuning_page = tuning
        tl = QHBoxLayout(tuning); tl.setContentsMargins(0, 0, 0, 0)
        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._build_tree())
        self.tune_tabs = QTabWidget()
        self.tune_tabs.setDocumentMode(True)
        self.editor_tab = self._build_editor_tab()
        self.tune_tabs.addTab(self.editor_tab, "Editor")
        self.tune_tabs.addTab(self._build_scalars_tab(), "Scalars")
        self.tune_tabs.addTab(self._build_log_tab(), "Log Analysis")
        splitter.addWidget(self.tune_tabs)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([260, 920])
        self.tuning_splitter = splitter
        tl.addWidget(splitter)
        self.main_tabs.addTab(tuning, "Tuning")
        # Ctrl+F focuses the table search while the Tuning page is showing.
        self._find_shortcut = QShortcut(QKeySequence("Ctrl+F"), self)
        self._find_shortcut.activated.connect(self.focus_table_search)
        self.main_tabs.currentChanged.connect(self._sync_find_shortcut)
        # Copy / Paste act on the calibration grid, which is only on screen
        # on Tuning > Editor; anywhere else Ctrl+V would write into a table
        # the user cannot see.
        self.main_tabs.currentChanged.connect(self._sync_clipboard_actions)
        self.tune_tabs.currentChanged.connect(self._sync_clipboard_actions)

        self.diag = DiagnosticsPage(self.dashboard, self.settings)
        self.main_tabs.addTab(self.diag, "Diagnostics")
        self.diag.tabs.currentChanged.connect(self._sync_find_shortcut)

        # Live Data + Active Tests read truck-mcp's drive-log store and control
        # journal off disk (never the serial port). Appended AFTER Diagnostics
        # so the existing index-0/1/2 navigation stays valid.
        self.livedata = LiveDataPage()
        self.main_tabs.addTab(self.livedata, "Live Data")
        self.active_tests = ActiveTestsPage(self.settings)
        self.main_tabs.addTab(self.active_tests, "Active Tests")

        self.main_tabs.setCurrentIndex(0)
        self.setCentralWidget(self.main_tabs)
        self._sync_find_shortcut()
        self._sync_clipboard_actions()

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

    def _on_clean_changed(self, clean: bool) -> None:
        # Emitted after the undo/redo command ran, so it has the last word
        # over the _mark_dirty the command's own write triggered.
        self.dirty = not clean
        self.setWindowTitle(self._title())

    def _resolve_unsaved(self) -> bool:
        """Ask about unsaved edits to THIS window's calibration. True when
        it is safe to let them go (saved, discarded, or none), False when
        the user cancelled or the save did not happen."""
        if not self.dirty:
            return True
        ans = QMessageBox.question(
            self, "Unsaved changes",
            "The calibration has unsaved edits. Save before closing?",
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
            QMessageBox.Save)
        if ans == QMessageBox.Cancel:
            return False
        if ans == QMessageBox.Save:
            self.save_cal()
            return not self.dirty   # save failed or Save As was cancelled
        return True

    def closeEvent(self, event):
        if not self._resolve_unsaved():
            return event.ignore()
        self._shutdown_pages()
        if appsettings.on_pref_write_failed == self._pref_failed_hook:
            set_on_pref_write_failed(None)
        super().closeEvent(event)

    def _on_pref_write_failed(self, key: str, reason: str) -> None:
        """appsettings' notice that a UI preference was NOT saved."""
        self.statusBar().showMessage(
            f"Preference not saved: {key} \u2014 {reason}", 10000)

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
        diag = getattr(self, "diag", None)
        if diag is not None:
            diag.save_state()

    def _build_menu(self):
        m = self.menuBar().addMenu("&File")
        self._file_menu = m
        for label, slot, keys in [
            ("Open .cal.json…", self.open_cal, "Ctrl+O"),
            ("Save", self.save_cal, "Ctrl+S"),
            ("Save As…", self.save_cal_as, "Ctrl+Shift+S"),
            ("Export table → CSV…", self.export_csv, None),
            ("Load Log…", self.load_log, "Ctrl+L"),
            (OPEN_TM_SESSION_LABEL, self._open_tm_session, "Ctrl+T"),
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
        # The same QAction objects sit on the editor toolbar (one shortcut
        # owner each, so Ctrl+C / Ctrl+V are never ambiguous).
        self.act_copy = QAction(theme.glyph_icon("copy"), "Copy", self)
        self.act_copy.setShortcut("Ctrl+C")
        self.act_copy.setToolTip("Copy the selection as tab-separated values "
                                 "(Excel)  (Ctrl+C)")
        self.act_copy.triggered.connect(self.copy_selection)
        self.act_paste = QAction(theme.glyph_icon("paste"), "Paste", self)
        self.act_paste.setShortcut("Ctrl+V")
        self.act_paste.setToolTip("Paste a tab-separated grid at the current "
                                  "cell, clipped to the table  (Ctrl+V)")
        self.act_paste.triggered.connect(self.paste_selection)
        e.addAction(self.act_copy)
        e.addAction(self.act_paste)
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
        self.act_log_folder = QAction(LOG_FOLDER_LABEL, self)
        self.act_log_folder.triggered.connect(self.choose_log_folder)
        s.addAction(self.act_log_folder)
        self.act_log_folder_reset = QAction("Reset log folder to default", self)
        self.act_log_folder_reset.triggered.connect(self.reset_log_folder)
        s.addAction(self.act_log_folder_reset)
        s.addSeparator()
        # The GT connects by itself when plugged in (Dashboard auto-connect);
        # these are the manual overrides. The Dashboard is built after the
        # menu, so the slots resolve it when triggered.
        self.act_reconnect_gt = QAction(RECONNECT_GT_LABEL, self)
        self.act_reconnect_gt.setShortcut("Ctrl+G")
        self.act_reconnect_gt.triggered.connect(
            lambda: self.dashboard.reconnect_gt_now())
        s.addAction(self.act_reconnect_gt)
        self.act_disconnect_gt = QAction(DISCONNECT_GT_LABEL, self)
        self.act_disconnect_gt.triggered.connect(
            lambda: self.dashboard.disconnect_gt())
        s.addAction(self.act_disconnect_gt)

        h = self.menuBar().addMenu("&Help")
        ab = QAction("About", self); ab.triggered.connect(self._about); h.addAction(ab)

    def _open_tm_session(self):
        """File → Open truck-mcp session…: show the Dashboard and its session
        picker, anchored under the File menu."""
        self.main_tabs.setCurrentIndex(0)
        bar = self.menuBar()
        geo = bar.actionGeometry(self._file_menu.menuAction())
        self.dashboard.pick_tm_session(bar.mapToGlobal(geo.bottomLeft()))

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
        form.addRow("Log folder:", self._log_folder_row())
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        form.addRow(btns)
        if dlg.exec() != QDialog.Accepted:
            return
        style = combo.currentData()
        self.dashboard.set_style(style)
        if save_pref(self.settings, "dashboard/style", style):
            self.statusBar().showMessage(
                f"Dashboard visual: {DASHBOARD_STYLES[style]}", 4000)
        else:
            self.statusBar().showMessage(
                f"Dashboard visual: {DASHBOARD_STYLES[style]} applied for "
                f"this session, NOT SAVED (preference dashboard/style could "
                f"not be written; see the log)", 10000)

    # -- log folder (Settings → Log folder…) -------------------------------- #
    def log_folder_text(self) -> str:
        """The folder recordings go to, as shown in Settings: the configured
        folder as it resolves (variables and ~ expanded), or the default
        marked "(default)". A configured value that cannot resolve shows the
        default it falls back to plus why, never the raw setting as if it
        were where recordings land."""
        defaults = self.dashboard._record_dirs()
        default_text = (f"{defaults[0]} (default)" if defaults
                        else "default (no Documents folder found)")
        folder, problem = resolve_log_dir(configured_log_dir())
        if folder:
            return folder
        return f"{default_text} — {problem}" if problem else default_text

    def _resolved_log_folder(self) -> str:
        """The configured log folder as it resolves, or "" when unset or
        unusable (callers then use a default)."""
        folder, _problem = resolve_log_dir(configured_log_dir())
        return folder or ""

    def _log_folder_row(self) -> QWidget:
        """Preferences row: the current log folder (elided, plain text, full
        path in the tooltip) with Change… / Reset to default, which apply
        immediately like the Settings menu items."""
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        label = ElidedLabel(self.log_folder_text())
        label.setObjectName("logFolderLabel")
        label.setMinimumWidth(240)
        change = QPushButton("Change…")
        reset = QPushButton("Reset to default")

        def on_change():
            if self.choose_log_folder():
                label.setText(self.log_folder_text())

        def on_reset():
            self.reset_log_folder()
            label.setText(self.log_folder_text())

        change.clicked.connect(on_change)
        reset.clicked.connect(on_reset)
        lay.addWidget(label, 1)
        lay.addWidget(change)
        lay.addWidget(reset)
        return row

    def choose_log_folder(self) -> bool:
        """Pick the folder new recordings go to. Cancel changes nothing. The
        folder is created if missing and must pass a real write probe (a
        OneDrive placeholder can look writable and still refuse a file)."""
        start = self._resolved_log_folder() or next(
            iter(self.dashboard._record_dirs()), "")
        chosen = QFileDialog.getExistingDirectory(self, "Log folder", start)
        if not chosen:
            return False
        chosen = os.path.normpath(chosen)
        why = probe_log_dir(chosen)
        if why is not None:
            QMessageBox.warning(
                self, "Log folder",
                f"Recordings cannot be written to:\n{chosen}\n\n{why}\n\n"
                "The log folder is unchanged.")
            return False
        try:
            write_settings(lambda st: st.setValue(LOG_DIR_KEY, chosen),
                           self.settings, what="log folder")
        except SettingsError as exc:
            self.statusBar().showMessage(
                f"Log folder NOT SAVED: {exc.reason or exc}", 8000)
            return False
        self.statusBar().showMessage(f"Log folder: {chosen}", 6000)
        return True

    def reset_log_folder(self) -> None:
        """Back to the default folder (Documents\\OpenOBD\\Logs)."""
        try:
            write_settings(lambda st: st.remove(LOG_DIR_KEY),
                           self.settings, what="log folder")
        except SettingsError as exc:
            self.statusBar().showMessage(
                f"Log folder NOT reset: {exc.reason or exc}", 8000)
            return
        self.statusBar().showMessage(
            f"Log folder: {self.log_folder_text()}", 6000)

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

    # -- shared Tuning chrome --------------------------------------------- #
    @staticmethod
    def _make_toolbar() -> QToolBar:
        """The 36 px toolbar every Tuning sub-tab uses (tuningui.make_toolbar,
        shared with Diagnostics)."""
        return make_toolbar("tuningToolbar")

    @staticmethod
    def _toolbar_action(tb: QToolBar, action: QAction,
                        icon_only: bool = False) -> QAction:
        tb.addAction(action)
        btn = tb.widgetForAction(action)
        if btn is not None and icon_only:
            btn.setToolButtonStyle(Qt.ToolButtonIconOnly)
        return action

    def _new_action(self, glyph: str, text: str, tip: str, slot,
                    shortcut_hint: str = "") -> QAction:
        a = QAction(theme.glyph_icon(glyph), text, self)
        a.setToolTip(plain_tooltip(
            f"{tip}  ({shortcut_hint})" if shortcut_hint else tip))
        a.triggered.connect(slot)
        return a

    def _sync_find_shortcut(self, *_a) -> None:
        """Ctrl+F is live on Tuning (table search), on Diagnostics >
        Codes & Readiness (code search), on Live Data (tile filter) and on
        Active Tests (journal filter); nowhere else has a search field."""
        cur = self.main_tabs.currentWidget()
        diag = getattr(self, "diag", None)
        livedata = getattr(self, "livedata", None)
        active = getattr(self, "active_tests", None)
        self._find_shortcut.setEnabled(
            cur is self.tuning_page
            or (diag is not None and cur is diag and diag.has_search())
            or (livedata is not None and cur is livedata)
            or (active is not None and cur is active))

    def _editor_showing(self) -> bool:
        """True while Tuning > Editor (the calibration grid) is on screen."""
        return (self.main_tabs.currentWidget() is self.tuning_page
                and self.tune_tabs.currentWidget() is self.editor_tab)

    def _sync_clipboard_actions(self, *_a) -> None:
        on = self._editor_showing()
        self.act_copy.setEnabled(on)
        self.act_paste.setEnabled(on)

    # -- navigator (left pane) ------------------------------------------- #
    def _build_tree(self) -> QWidget:
        self.navigator = Navigator(self.cal.tables, self.settings)
        self.navigator.table_activated.connect(self._on_navigator_open)
        # The tree and its search field, under their long-standing names.
        self.tree = self.navigator.tree
        self.tree_filter = self.navigator.search
        return self.navigator

    def _on_navigator_open(self, name: str):
        self._select_table(name)
        self.main_tabs.setCurrentIndex(1)   # Tuning workspace
        self.tune_tabs.setCurrentIndex(0)   # Editor

    def focus_table_search(self):
        """Ctrl+F: the search field of the workspace on screen."""
        cur = self.main_tabs.currentWidget()
        if cur is getattr(self, "diag", None):
            self.diag.focus_search()
            return
        if cur is getattr(self, "livedata", None):
            self.livedata.focus_filter()
            return
        if cur is getattr(self, "active_tests", None):
            self.active_tests.focus_filter()
            return
        self.navigator.focus_search()

    # -- editor tab -------------------------------------------------------- #
    #: Grid keys -> selection math (op, negate). HP Tuners staples: +/-
    #: nudge by Amount, = sets, * and / multiply / divide, % scales.
    GRID_KEY_OPS = {
        Qt.Key_Plus: ("add", False),
        Qt.Key_Minus: ("add", True),
        Qt.Key_Equal: ("set", False),
        Qt.Key_Asterisk: ("mul", False),
        Qt.Key_Slash: ("div", False),
        Qt.Key_Percent: ("pct", False),
    }

    def _build_editor_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.editor_crumb = Breadcrumb()
        lay.addWidget(self.editor_crumb)

        tb = self._make_toolbar()
        self.editor_toolbar = tb
        tb.addWidget(QLabel("Amount"))
        self.amount_spin = QDoubleSpinBox()
        self.amount_spin.setRange(-1e9, 1e9)
        self.amount_spin.setDecimals(4)
        self.amount_spin.setValue(1.0)
        self.amount_spin.setFixedWidth(96)
        self.amount_spin.setToolTip(
            "Operand for the math actions and the grid keys "
            "(+ / - nudge, = set, * multiply, / divide, % scale)")
        tb.addWidget(self.amount_spin)
        A = self.editor_actions = {}
        for key, glyph, text, tip, hint, fn in [
            ("set", "set", "=", "Set selection to Amount", "=",
             lambda: self._math_op("set")),
            ("add", "add", "+", "Add Amount to selection", "+",
             lambda: self._math_op("add")),
            ("sub", "sub", "−", "Subtract Amount from selection", "-",
             lambda: self._math_op("add", negate=True)),
            ("mul", "mul", "×", "Multiply selection by Amount", "*",
             lambda: self._math_op("mul")),
            ("div", "div", "÷", "Divide selection by Amount", "/",
             lambda: self._math_op("div")),
            ("pct", "pct", "%", "Scale selection by Amount percent",
             "Shift+5", lambda: self._math_op("pct")),
        ]:
            A[key] = self._toolbar_action(
                tb, self._new_action(glyph, text, tip, fn, hint),
                icon_only=True)
        tb.addSeparator()
        for key, glyph, text, mode in [("interp_h", "interp_h", "Interp ↔", "h"),
                                       ("interp_v", "interp_v", "Interp ↕", "v"),
                                       ("interp_2d", "interp_2d", "Interp 2D",
                                        "2d")]:
            A[key] = self._toolbar_action(tb, self._new_action(
                glyph, text, "Linear interpolation across the selection",
                lambda _=False, m=mode: self._interp(m)))
        A["smooth"] = self._toolbar_action(tb, self._new_action(
            "smooth", "Smooth",
            "3×3 mean over the selection (neighbours inside the table)",
            self._smooth))
        tb.addSeparator()
        A["revert"] = self._toolbar_action(tb, self._new_action(
            "revert", "Revert sel → stock",
            "Revert the selected cells to stock", self._revert_selected))
        A["next"] = self._toolbar_action(tb, self._new_action(
            "next", "Next Δ", "Jump to the next changed-vs-stock cell",
            self._goto_next_changed, "F3"))
        A["next"].setShortcut(QKeySequence(Qt.Key_F3))
        A["copy"] = self._toolbar_action(tb, self.act_copy)
        A["paste"] = self._toolbar_action(tb, self.act_paste)
        tb.addSeparator()
        self.act_heatmap = QAction(theme.glyph_icon("heatmap"), "Heatmap", self)
        self.act_heatmap.setCheckable(True)
        self.act_heatmap.setChecked(True)
        self.act_heatmap.setToolTip("Colour cells by value (table min → max)")
        self.act_heatmap.toggled.connect(self._toggle_heat)
        A["heatmap"] = self._toolbar_action(tb, self.act_heatmap)
        self.diff_btn = QToolButton()
        self.diff_btn.setIcon(theme.glyph_icon("diff"))
        self.diff_btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.diff_btn.setPopupMode(QToolButton.InstantPopup)
        self.diff_btn.setToolTip("Show each cell's difference from stock")
        diff_menu = QMenu(self.diff_btn)
        group = QActionGroup(self)
        self.diff_actions: dict[int, QAction] = {}
        for mode, label in [(CalTableModel.DIFF_OFF, "Off"),
                            (CalTableModel.DIFF_ABS, "Absolute"),
                            (CalTableModel.DIFF_PCT, "Percent")]:
            a = diff_menu.addAction(label)
            a.setCheckable(True)
            group.addAction(a)
            a.triggered.connect(lambda _=False, m=mode: self.set_diff_mode(m))
            self.diff_actions[mode] = a
        self.diff_btn.setMenu(diff_menu)
        tb.addWidget(self.diff_btn)
        self._diff_mode = CalTableModel.DIFF_OFF
        lay.addWidget(tb)

        body = QHBoxLayout()
        body.setContentsMargins(theme.SPACING["sm"], theme.SPACING["sm"],
                                theme.SPACING["sm"], 0)
        body.setSpacing(theme.SPACING["sm"])
        grid_col = QVBoxLayout()
        grid_col.setSpacing(0)
        self.view = CalTableView()
        self.view.setItemDelegate(HeatmapDelegate(self.view))
        hh = self.view.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.Stretch)
        hh.setMinimumSectionSize(48)
        self.view.installEventFilter(self)  # grid math keys
        grid_col.addWidget(self.view, 1)
        # Elides rather than widening the page (a long readout once pushed
        # the window past a 1366 px screen).
        self.selection_readout = ElidedLabel("No selection")
        self.selection_readout.setObjectName("selectionReadout")
        grid_col.addWidget(self.selection_readout)
        body.addLayout(grid_col, 1)

        # Log overlay controls: a collapsible right-hand panel.
        ov = QWidget()
        form = QFormLayout(ov)
        form.setContentsMargins(0, 0, 0, 0)
        self.overlay_mode = QComboBox()
        self.overlay_mode.addItems(["None", "Log count", "Log mean value"])
        self.overlay_mode.currentIndexChanged.connect(self._apply_overlay)
        form.addRow("Mode", self.overlay_mode)
        self.x_ch = QComboBox()
        self.y_ch = QComboBox()
        self.v_ch = QComboBox()
        for label, cb in (("X channel", self.x_ch), ("Y channel", self.y_ch),
                          ("Value", self.v_ch)):
            cb.addItem("(none)")
            cb.currentIndexChanged.connect(self._apply_overlay)
            form.addRow(label, cb)
        help_lbl = QLabel("Bins the loaded log into this table's cells. "
                          "Log count tints by hits; Log mean value tints by "
                          "the Value channel's mean per cell.")
        help_lbl.setWordWrap(True)
        help_lbl.setStyleSheet(theme.muted_qss())
        form.addRow(help_lbl)
        open_ = str(self.settings.value(OVERLAY_PANEL_KEY, "false")).lower()
        self.overlay_panel = CollapsiblePanel(
            "Log overlay", ov, expanded=open_ == "true")
        self.overlay_panel.toggled.connect(
            lambda on: save_pref(self.settings, OVERLAY_PANEL_KEY,
                                 "true" if on else "false"))
        body.addWidget(self.overlay_panel, 0)
        lay.addLayout(body, 1)
        self.set_diff_mode(CalTableModel.DIFF_OFF)
        return w

    def set_diff_mode(self, mode: int):
        """Diff vs stock: CalTableModel.DIFF_OFF / DIFF_ABS / DIFF_PCT."""
        self._diff_mode = mode
        for m, a in self.diff_actions.items():
            a.setChecked(m == mode)
        self.diff_btn.setText({CalTableModel.DIFF_OFF: "Diff vs stock",
                               CalTableModel.DIFF_ABS: "Diff: Absolute",
                               CalTableModel.DIFF_PCT: "Diff: Percent"}[mode])
        if self.current_model:
            self.current_model.set_diff_mode(mode)

    def _select_table(self, name: str):
        t = self.cal.table(name)
        if not t:
            return
        events = self.shift_events if "Shift Speed" in name else None
        self.current_model = CalTableModel(t, shift_events=events)
        self.current_model.dataChanged.connect(self._mark_dirty)
        self.current_model.edit_hook = self._on_cell_edit
        self.current_model.show_heatmap = self.act_heatmap.isChecked()
        self.current_model.diff_mode = self._diff_mode
        old_sel = self.view.selectionModel()
        self.view.setModel(self.current_model)
        if old_sel is not None:
            old_sel.deleteLater()     # setModel never frees the old one
        self.view.selectionModel().selectionChanged.connect(
            self._update_selection_readout)
        self.view.selectionModel().currentChanged.connect(
            self._update_selection_readout)
        self.view.set_corner_text(self.current_model.corner_text())
        self.navigator.select_name(name)
        self._update_table_info()
        self._update_selection_readout()
        self._apply_overlay()

    def _update_table_info(self):
        if not self.current_model:
            return
        t = self.current_model.table
        changed = len(editops.changed_cells(t))
        self.editor_crumb.set_path([table_category(t), t.name])
        chips = []
        unit = clean_unit(t.unit)
        if unit:
            chips.append((unit, "neutral"))
        if t.param_id:
            chips.append((f"HPT id {t.param_id}", "neutral"))
        if t.stock_values is None:
            # No baseline: "0 cells changed" would claim it is stock.
            chips.append(("no stock baseline", "muted"))
        else:
            chips.append(
                (f"{changed} cell{'' if changed == 1 else 's'} changed",
                 "danger" if changed else "neutral"))
        self.editor_crumb.set_chips(chips)
        self.editor_crumb.set_note(t.note or "")
        self.navigator.refresh_changed(t.name)

    def _update_selection_readout(self, *_a):
        if not self.current_model:
            self.selection_readout.setText("No table")
            return
        t = self.current_model.table
        stats = editops.selection_stats(t, self._selected_cells())
        self.selection_readout.setText(format_selection_stats(stats, t.unit))

    def _toggle_heat(self, *_a):
        if self.current_model:
            self.current_model.set_heatmap(self.act_heatmap.isChecked())

    # -- editing: undo plumbing --------------------------------------------- #
    def _on_cell_edit(self, r, c, old, new):
        self._push_changes({(r, c): (old, new)}, "edit cell", quiet=True)

    def _table_touched(self, table):
        """Called by undo commands after writing to a Table."""
        self._mark_dirty()
        self.navigator.refresh_changed(table.name)
        if self.current_model and self.current_model.table is table:
            self.current_model.refresh_all()
            self._update_table_info()
            self._update_selection_readout()

    def _push_changes(self, changes, text, quiet: bool = False):
        if not changes:
            self.statusBar().showMessage(f"{text}: no cells affected", 3000)
            return False
        try:
            cmd = BulkEditCommand(self, self.current_model.table, changes, text)
        except ValueError:
            self.current_model.refresh_all()
            self.statusBar().showMessage(
                f"{text}: edit refused: non-finite result", 5000)
            return False
        self.undo_stack.push(cmd)
        if not quiet:
            self.statusBar().showMessage(
                f"{text}: {len(changes)} cell(s)", 3000)
        return True

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
        return float(self.amount_spin.value())

    def _math_op(self, op: str, negate: bool = False):
        cells = self._selected_cells()
        amt = self._amount()
        if not cells or amt is None:
            return
        if negate:
            amt = -amt
        label = {"set": "set", "add": "add", "mul": "multiply",
                 "div": "divide", "pct": "scale %"}[op]
        try:
            changes = editops.apply_math(self.current_model.table, cells,
                                         op, amt)
        except ZeroDivisionError:
            self.statusBar().showMessage("Divide: Amount is 0", 4000)
            return
        self._push_changes(changes, label)

    def _interp(self, mode: str):
        cells = self._selected_cells()
        if len(cells) < 2:
            self.statusBar().showMessage("Select a range to interpolate", 3000)
            return
        self._push_changes(
            editops.interpolate(self.current_model.table, cells, mode),
            f"interpolate {mode}")

    def _smooth(self):
        cells = self._selected_cells()
        if not cells:
            self.statusBar().showMessage("Select cells to smooth", 3000)
            return
        self._push_changes(editops.smooth(self.current_model.table, cells),
                           "smooth")

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
        if not self._editor_showing():
            self.statusBar().showMessage(
                "Copy works on the Tuning > Editor table", 3000)
            return
        cells = self._selected_cells()
        if not cells:
            return
        QApplication.clipboard().setText(
            editops.to_tsv(self.current_model.table, cells))
        self.statusBar().showMessage(f"Copied {len(cells)} cell(s)", 3000)

    def paste_selection(self):
        """Paste a tab-separated grid (Excel) at the current cell as ONE
        undoable edit, clipped to the table. Any non-numeric field rejects
        the whole paste (a header row would otherwise land shifted), and so
        does nan / inf / -inf. Refused unless the Editor is on screen: the
        target table is otherwise invisible."""
        if not self._editor_showing():
            self.statusBar().showMessage(
                "Paste refused: open the Tuning > Editor table first", 4000)
            return
        if not self.current_model:
            return
        grid, rejects = editops.parse_tsv_checked(
            QApplication.clipboard().text())
        if rejects:
            r, c, text = rejects[0]
            self.statusBar().showMessage(
                f"Paste rejected: {len(rejects)} cell(s) not a finite number "
                f"(text, nan and inf are refused), first {text!r} at row "
                f"{r + 1}, column {c + 1}", 6000)
            return
        if not grid or all(v is None for row in grid for v in row):
            self.statusBar().showMessage("Clipboard has no numeric grid", 3000)
            return
        cur = self.view.currentIndex()
        r0, c0 = (cur.row(), cur.column()) if cur.isValid() else (0, 0)
        t = self.current_model.table
        clipped = editops.paste_clipped(t, r0, c0, grid)
        changes = editops.paste_grid(t, r0, c0, grid)
        if self._push_changes(changes, "paste") and clipped:
            self.statusBar().showMessage(
                f"paste: {len(changes)} cell(s); {clipped} outside the "
                "table clipped", 5000)

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
        # Grid keys run the selection math with Amount (HP Tuners style).
        if (obj is self.view and event.type() == QEvent.KeyPress
                and self.current_model is not None
                and not event.modifiers() & (Qt.ControlModifier
                                             | Qt.AltModifier)):
            op = self.GRID_KEY_OPS.get(event.key())
            if op is not None:
                # Holding a key must not push one undo step per repeat;
                # the repeat is consumed so it never opens the cell editor.
                if not event.isAutoRepeat():
                    self._math_op(op[0], negate=op[1])
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
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.scalars_crumb = Breadcrumb()
        lay.addWidget(self.scalars_crumb)
        tb = self._make_toolbar()
        self.scalar_search = QLineEdit()
        self.scalar_search.setObjectName("scalarSearch")
        self.scalar_search.setPlaceholderText("Search parameters")
        self.scalar_search.setClearButtonEnabled(True)
        self.scalar_search.setFixedWidth(220)
        tb.addWidget(self.scalar_search)
        tb.addSeparator()
        tb.addWidget(QLabel("Category"))
        self.scalar_category = QComboBox()
        self.scalar_category.addItem("All categories", "")
        self.scalars_view = ScalarsTree(self.cal.scalars)
        for cat in self.scalars_view.categories():
            self.scalar_category.addItem(cat, cat)
        tb.addWidget(self.scalar_category)
        tb.addSeparator()
        self.act_scalar_revert = self._toolbar_action(tb, self._new_action(
            "revert", "Revert to stock",
            "Revert the selected parameter to its stock value",
            self._revert_scalar))
        lay.addWidget(tb)

        body = QVBoxLayout()
        m = theme.SPACING["sm"]
        body.setContentsMargins(m, m, m, m)
        body.setSpacing(m)
        self.scalars_view.edit_requested.connect(self._on_scalar_edit)
        body.addWidget(self.scalars_view, 1)
        self.safety_banner = QLabel(self.cal.metadata.get("safety", ""))
        self.safety_banner.setObjectName("safetyBanner")
        self.safety_banner.setWordWrap(True)
        self.safety_banner.setVisible(bool(self.safety_banner.text()))
        body.addWidget(self.safety_banner)
        lay.addLayout(body, 1)
        self.scalar_search.textChanged.connect(self._on_scalar_filter)
        self.scalar_category.currentIndexChanged.connect(
            self._on_scalar_filter)
        self._update_scalars_crumb()
        return w

    def _on_scalar_filter(self, *_a):
        self.scalars_view.set_search(self.scalar_search.text())
        self.scalars_view.set_category(self.scalar_category.currentData())
        self._update_scalars_crumb()

    def _update_scalars_crumb(self):
        cat = self.scalar_category.currentData() or ""
        self.scalars_crumb.set_path(["Scalars",
                                     cat or "All categories"])
        shown = self.scalars_view.visible_scalar_count()
        in_cat = [s for s in self.cal.scalars
                  if not cat or (s.category or "Uncategorized") == cat]
        changed = sum(1 for s in in_cat if s.changed)
        if any(s.stock_value is not None for s in in_cat):
            delta_chip = (f"{changed} changed",
                          "danger" if changed else "neutral")
        else:
            # No baseline to compare with: unknown, never "0 changed".
            delta_chip = ("no stock baseline", "muted")
        self.scalars_crumb.set_chips([
            (f"{shown} parameter{'' if shown == 1 else 's'}", "neutral"),
            delta_chip,
        ])
        self.scalars_crumb.set_note(
            "Double-click a value to edit (Ctrl+Z undoes). Δ is the "
            "difference from stock.")

    def _on_scalar_edit(self, i: int, text: str):
        s = self.cal.scalars[i]
        v = editops.parse_number(text)
        if v is None:
            # Restore the display so the cell never shows a value the model
            # doesn't hold.
            self.scalars_view.refresh(i)
            self.statusBar().showMessage(
                f"Not a number: {text!r} — edit reverted", 4000)
            return
        if v == s.value:
            self.scalars_view.refresh(i)
            return
        self._push_scalar(i, s.value, v)

    def _push_scalar(self, i: int, old: float, new: float):
        try:
            cmd = ScalarEditCommand(self, i, old, new)
        except ValueError:
            self.scalars_view.refresh(i)
            self.statusBar().showMessage(
                "edit refused: non-finite result", 5000)
            return
        self.undo_stack.push(cmd)

    def _revert_scalar(self):
        idx = self.scalars_view.view.currentIndex()
        if not idx.isValid():
            self.statusBar().showMessage("Select a parameter to revert", 3000)
            return
        src = self.scalars_view.proxy.mapToSource(idx)
        i = src.sibling(src.row(), 1).data(Qt.UserRole)
        if i is None:
            return
        s = self.cal.scalars[int(i)]
        if s.stock_value is None or s.value == s.stock_value:
            self.statusBar().showMessage(f"{s.name}: already stock", 3000)
            return
        self._push_scalar(int(i), s.value, s.stock_value)

    def _scalar_touched(self, i: int):
        """Called by ScalarEditCommand after writing a Scalar."""
        self._mark_dirty()
        self.scalars_view.refresh(i)
        self._update_scalars_crumb()

    # -- log tab ----------------------------------------------------------- #
    def _build_log_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.log_crumb = Breadcrumb()
        lay.addWidget(self.log_crumb)
        tb = self._make_toolbar()
        self.act_load_log = self._toolbar_action(tb, self._new_action(
            "open", "Load Log…", "Load a VCM Scanner CSV or a Dashboard "
            "recording", self.load_log, "Ctrl+L"))
        self.act_events = QAction(theme.glyph_icon("events"), "Events", self)
        self.act_events.setCheckable(True)
        self.act_events.setChecked(True)
        self.act_events.setToolTip(
            "Event markers: knock (red), shifts (cyan) and recording markers "
            "(amber, from the .meta.json beside a recording)")
        self.act_events.toggled.connect(self._rebuild_plots)
        self._toolbar_action(tb, self.act_events)
        self.act_learn_pedal = self._toolbar_action(tb, self._new_action(
            "learn", "Learn pedal calibration",
            "Derive pedal rest / full from this log's idle and WOT samples "
            "(same rules as the live learner) and store them for its VIN",
            self.learn_pedal_from_log))
        lay.addWidget(tb)

        # chart state
        self._log_times: list[float] = []
        self._plots: list = []
        self._vlines: list = []
        self._knock_times: list[float] = []
        self._shift_times: list[float] = []
        self._marker_times: list[tuple[float, str]] = []

        split = QSplitter(Qt.Vertical)
        top = QSplitter(Qt.Horizontal)
        top.setChildrenCollapsible(False)
        self.chan_tree = ChannelTree()
        self.chan_tree.changed.connect(self._rebuild_plots)
        top.addWidget(self.chan_tree)
        self.plot_area = pg.GraphicsLayoutWidget()
        self.plot_area.scene().sigMouseMoved.connect(self._on_chart_mouse)
        self.plot_empty = empty_state_label(LOG_EMPTY_TEXT)
        self.plot_stack = QStackedWidget()
        self.plot_stack.addWidget(self.plot_empty)
        self.plot_stack.addWidget(self.plot_area)
        top.addWidget(self.plot_stack)
        self.cursor_readout = CursorReadout()
        top.addWidget(self.cursor_readout)
        top.setStretchFactor(0, 0)
        top.setStretchFactor(1, 1)
        top.setStretchFactor(2, 0)
        top.setSizes([180, 640, 180])
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
        m = theme.SPACING["sm"]
        holder = QVBoxLayout()
        holder.setContentsMargins(m, m, m, m)
        holder.addWidget(split)
        lay.addLayout(holder, 1)
        self._update_log_crumb()
        return w

    def _update_log_crumb(self):
        if not self.log:
            self.log_crumb.set_path(["Log Analysis", "No log loaded"])
            self.log_crumb.set_chips([])
            self.log_crumb.set_note("Load a VCM Scanner CSV export or a "
                                    "Dashboard recording (Ctrl+L).")
            return
        self.log_crumb.set_path(["Log Analysis", self.log.source or "log"])
        chips = [(f"{self.log.n_samples:,} samples", "neutral")]
        if self._log_times:
            chips.append((f"{self._log_times[-1]:.0f} s", "neutral"))
        n_ch = len([k for k in self.log.canonical_keys() if k != "time"])
        chips.append((f"{n_ch} channels", "neutral"))
        if getattr(self, "_meta_problem", None):
            chips.append(("metadata unreadable", "danger"))
        self.log_crumb.set_chips(chips)
        note = []
        if self.log.timed:
            note.append("per-channel times from the .long.csv sidecar")
        if self.log.meta:
            note.append("recording metadata loaded")
        self.log_crumb.set_note(" · ".join(note) or
                                "Hover the chart for the cursor readout.")

    # -- charting ------------------------------------------------------------ #
    _PLOT_DEFAULTS = ["rpm", "vss", "app", "tps", "spark", "knock_retard"]

    def _populate_channels(self):
        keys = [k for k in self.log.canonical_keys() if k != "time"]
        present = set(keys)
        # default to the tuning staples that are actually in the log (max 4)
        want = [k for k in self._PLOT_DEFAULTS if k in present][:4] or keys[:3]
        self.chan_tree.populate(keys, want)

    def _checked_channels(self) -> list[str]:
        return self.chan_tree.checked_channels()

    def _rebuild_plots(self, *_a):
        self.plot_area.clear()
        self._plots = []; self._vlines = []
        if not self.log or not self._log_times:
            self.plot_stack.setCurrentWidget(self.plot_empty)
            self.cursor_readout.set_channels([])
            return
        self.plot_stack.setCurrentWidget(self.plot_area)
        times = np.asarray(self._log_times, dtype=float)
        keys = self._checked_channels()
        self.cursor_readout.set_channels(keys)
        first = None
        events = self.act_events.isChecked()
        for i, key in enumerate(keys):
            p = self.plot_area.addPlot(row=i, col=0)
            ser = self.log.series(key)
            y = np.array([np.nan if v is None else v for v in ser], dtype=float)
            n = min(len(times), len(y))
            p.plot(times[:n], y[:n],
                   pen=pg.mkPen(theme.pen_rgb("plot-trace"), width=1.4),
                   connect="finite")
            p.setLabel("left", key)
            p.showGrid(x=True, y=True, alpha=0.15)
            if first is None:
                first = p
            else:
                p.setXLink(first)
            if i < len(keys) - 1:
                p.getAxis("bottom").setStyle(showValues=False)
            if events:
                for t_ev in self._knock_times:
                    p.addItem(pg.InfiniteLine(
                        pos=t_ev, angle=90,
                        pen=pg.mkPen(theme.pen_rgb("plot-knock", 150), width=1,
                                     style=Qt.DashLine)), ignoreBounds=True)
                for t_ev in self._shift_times:
                    p.addItem(pg.InfiniteLine(
                        pos=t_ev, angle=90,
                        pen=pg.mkPen(theme.pen_rgb("plot-shift", 130), width=1,
                                     style=Qt.DashLine)), ignoreBounds=True)
                for t_ev, label in self._marker_times:
                    p.addItem(pg.InfiniteLine(
                        pos=t_ev, angle=90,
                        pen=pg.mkPen(theme.pen_rgb("plot-marker", 200),
                                     width=1.5),
                        label=label if i == 0 else None,
                        labelOpts={"position": 0.92,
                                   "color": theme.pen_rgb("plot-marker")}),
                        ignoreBounds=True)
            vline = pg.InfiniteLine(
                angle=90, movable=False,
                pen=pg.mkPen(theme.pen_rgb("plot-cursor", 120), width=1))
            vline.setVisible(False)
            p.addItem(vline, ignoreBounds=True)
            self._vlines.append(vline)
            self._plots.append(p)

    def _on_chart_mouse(self, scene_pos):
        if not self._plots or not self.log or not self._log_times:
            return
        x = self._plots[0].vb.mapSceneToView(scene_pos).x()
        self.show_cursor_at(x)

    def show_cursor_at(self, x: float):
        """Move the chart cursor to time x (s) and refresh the readout."""
        import bisect
        keys = self._checked_channels()
        i = bisect.bisect_right(self._log_times, x) - 1
        if i < 0 or i >= self.log.n_samples:
            for vl in self._vlines:
                vl.setVisible(False)
            self.cursor_readout.set_channels(keys)
            return
        for vl in self._vlines:
            vl.setPos(x); vl.setVisible(True)
        rows = []
        for key in keys:
            ser = self.log.series(key)
            rows.append((key, ser[i] if i < len(ser) else None))
        self.cursor_readout.show_values(self._log_times[i], rows)

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
        claimed = self._log_vin(self.log)
        if claimed:
            L.append(f"Vehicle: VIN {claimed} (from file, unverified)")
        problem = getattr(self, "_meta_problem", None)
        if problem:
            L.append(f"Metadata: {problem}")
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
        # Settle THIS window's unsaved edits while self.cal / self.path still
        # name its calibration: asking from closeEvent after the swap saved
        # the newly opened file instead and lost the old edits.
        if not self._resolve_unsaved():
            return
        try:
            cal = Calibration.load(p)
        except Exception as e:
            QMessageBox.critical(self, "Open failed", str(e)); return
        # rebuild UI by re-instantiating (simplest, robust). Keep a Python
        # reference on the QApplication or the new window gets GC'd.
        new = MainWindow(cal, p)
        QApplication.instance()._openobd_main = new
        new.show()
        self.dirty = False      # resolved above (saved or discarded)
        self.close()

    def save_cal(self):
        if not self.path:
            return self.save_cal_as()
        try:
            self.cal.save(self.path)
            self.undo_stack.setClean()
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
            self, "Load log", self._log_browse_dir(),
            "Logs (*.csv);;All files (*)")
        if p:
            self.load_log_path(p)

    def _log_browse_dir(self) -> str:
        """Load log opens in the folder recordings go to (the configured log
        folder, else the default), or Qt's default when none exists yet."""
        for d in [self._resolved_log_folder()] + self.dashboard._record_dirs():
            if d and os.path.isdir(d):
                return d
        return ""

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
        self._meta_problem = None
        try:
            self.log.meta = read_meta(p)
        except (OSError, ValueError) as exc:
            # The log still loads, without its metadata.
            self._meta_problem = (f"{os.path.basename(meta_path(p))}: "
                                  f"metadata unreadable: {exc}")
            self.statusBar().showMessage(self._meta_problem, 8000)
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
        """The VIN the log's .meta.json CLAIMS. A file on disk can say
        anything: this is for display ("from file, unverified") and for the
        comparison in learn_pedal_from_log, never a key that loads stored
        settings on its own."""
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
        """A pedal calibration KNOWN for this log: only what its own
        recording measured (recorded in its .meta.json; it was measured on
        that drive and applies to that drive's analysis). A stored
        pedal_cal/<VIN> is NEVER loaded on the file's say-so: the VIN in a
        file is unverified, so a doctored file cannot pull another
        vehicle's settings into the analysis (security gate SC-05)."""
        return self._recorded_pedal_cal(log)

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
        src = self.dashboard.source
        live = src.vin() if isinstance(src, GtDataSource) else None
        why = None
        if not vin:
            why = "No VIN in this log"
        elif live is None:
            why = (f"The log's VIN {vin} (from file, unverified) cannot be "
                   "confirmed: no identified GT connection")
        elif live != vin:
            why = (f"The log's VIN {vin} (from file, unverified) is not the "
                   f"connected vehicle's VIN {live}")
        elif getattr(self.dashboard, "_pedal_store_error", None):
            why = (f"The stored calibration for VIN {vin} is unreadable "
                   f"({self.dashboard._pedal_store_error})")
        stored = None
        if why is None:
            stored, err = try_load_pedal_cal(vin)
            if err is not None:
                why = f"The stored calibration for VIN {vin} is unreadable ({err})"
        if why is not None:
            self.log.pedal_cal = cal
            self._analyze_loaded_log()
            msg = (f"Pedal calibration {cal.status()} learned. {why}; "
                   "calibration applies to this log's analysis only "
                   "(not stored).")
            self.statusBar().showMessage(msg, 10000)
            self.log_report.append("\n" + msg)
            return
        merged = cal
        if stored is not None:
            rests = [v for v in (stored.rest, cal.rest) if v is not None]
            fulls = [v for v in (stored.full, cal.full) if v is not None]
            merged = PedalCal(min(rests), max(fulls),
                              f"{stored.source} + {cal.source}"
                              if stored.source else cal.source)
        err = self.dashboard._store_pedal_cal(vin, merged)
        if err is not None:
            msg = (f"Pedal calibration {merged.status()} for VIN {vin} was "
                   f"NOT stored: {err}")
            self.statusBar().showMessage(msg, 10000)
            self.log_report.append("\n" + msg)
            return
        self.log.pedal_cal = merged
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
        self._update_log_crumb()


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
    """App-wide dark Fusion palette (dark mode is the primary theme), built
    from the theme tokens."""
    theme.apply_dark_palette(app)


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
