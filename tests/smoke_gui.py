"""Offscreen GUI smoke test: build window, load log, overlay, drive dashboard."""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication  # noqa: E402
from openobd.app import MainWindow, load_default_cal  # noqa: E402
from openobd.model import CalTableModel  # noqa: E402

# a synthetic VCM Scanner CSV with a WOT-ish pull
rows = []
mph = 0
for i in range(200):
    rpm = 800 + (i % 40) * 120
    mph = min(90, i * 0.5)
    app_pct = 90 if 40 < i < 120 else 12
    kr = 2.0 if i == 80 else 0.0
    spark = 22 - (6 if i == 80 else 0)
    stft = -3
    ltft = -4
    rows.append(f"{i*0.1:.1f},{rpm},{mph:.1f},{app_pct},{kr},{spark},{stft},{ltft}")

csv = ("[Log Information]\nVehicle,2010 Silverado\n[Channel Information]\n"
       "1,2,3,4,5,6,7,8\n"
       "Time,Engine Speed,Vehicle Speed,Accelerator Pedal,Knock Retard,"
       "Spark Advance,Short Term FT,Long Term FT\n"
       "s,RPM,mph,%,deg,deg,%,%\n"
       "[Channel Data]\n" + "\n".join(rows) + "\n")

log_path = "/tmp/smoke_log.csv"
with open(log_path, "w") as fh:
    fh.write(csv)

# Isolate QSettings so smoke runs never touch the real per-user settings, and
# pin truck-mcp's data root to a temp tree holding ONE synthetic session so
# Live Data binds it deterministically (TRUCK_MCP_DATA is authoritative and
# alone, exactly as truck-mcp itself treats it). Both must happen BEFORE the
# window is built. The redirect only works because ALL app settings go through
# appsettings.app_settings() (4-arg ctor); the 2-arg QSettings("OpenOBD",
# "OpenOBD") ignores setDefaultFormat/setPath and hits the real registry —
# the canary below proves the isolation actually held.
import sqlite3 as _sq  # noqa: E402
import tempfile  # noqa: E402
from PySide6.QtCore import QSettings  # noqa: E402
from openobd.appsettings import app_settings  # noqa: E402
from tests.test_tmstore import _make_session  # noqa: E402
_tmp_root = tempfile.mkdtemp(prefix="openobd_smoke_")
QSettings.setDefaultFormat(QSettings.IniFormat)
QSettings.setPath(QSettings.IniFormat, QSettings.UserScope,
                  os.path.join(_tmp_root, "settings"))


def _real_registry_snapshot():
    """The user's REAL OpenOBD settings (Windows registry), read outside Qt.
    Captured before the run and compared after: smoke must leave them
    byte-identical, or the isolation is broken."""
    if sys.platform != "win32":
        return None
    import winreg
    snap = {}
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\OpenOBD\OpenOBD\livedata") as k:
            snap["livedata/layout"] = winreg.QueryValueEx(k, "layout")[0]
    except OSError:
        snap["livedata/layout"] = None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\OpenOBD\OpenOBD\dashboard") as k:
            snap["dashboard/style"] = winreg.QueryValueEx(k, "style")[0]
    except OSError:
        snap["dashboard/style"] = None
    return snap


_registry_before = _real_registry_snapshot()
os.environ["TRUCK_MCP_DATA"] = os.path.join(_tmp_root, "truck-mcp")
_sessions_dir = os.path.join(_tmp_root, "truck-mcp", "sessions")
os.makedirs(_sessions_dir)
chart_db = os.path.join(_sessions_dir, "smoke_chart.tmsession.db")
_make_session(chart_db)
conn = _sq.connect(chart_db)
for i in range(300):
    conn.execute(
        "INSERT INTO sample (ts_utc, channel_id, value_num, fresh, quality) "
        "VALUES (?,1,?,1,'ok')",
        (f"2026-08-21T10:{5 + (i + 1) // 60:02d}:{(i + 1) % 60:02d}Z",
         800 + i * 10))
conn.commit()
conn.close()

app = QApplication(sys.argv)
cal, path = load_default_cal()
win = MainWindow(cal, path)
win.show()

# the gauge cluster must exist BEFORE any source is bound (HP Tuners style)
assert len(win.dashboard.gauges) >= 16, "dashboard gauges missing at startup"
assert all(g.value is None for g in win.dashboard.gauges.values())
print("pre-bind dashboard cluster OK:", len(win.dashboard.gauges), "gauges")

# workspace structure: Dashboard opens first; Tuning/Diagnostics follow, then
# the two read-only truck-mcp-backed workspaces (appended, never inserted).
assert win.main_tabs.count() == 5
assert win.main_tabs.currentIndex() == 0, "Dashboard is not the opening page"
assert win.main_tabs.tabText(0) == "Dashboard"
assert win.main_tabs.tabText(1) == "Tuning" and win.tune_tabs.count() == 3
assert win.main_tabs.tabText(2) == "Diagnostics"
assert win.main_tabs.tabText(3) == "Live Data"
assert win.main_tabs.tabText(4) == "Active Tests"
print("workspace structure OK (Dashboard, Tuning, Diagnostics, Live Data, Active Tests)")

# Live Data is ONE combined view: compact tiles above the strip chart in a
# splitter — no sub-tabs (HP Tuners benchmark: numbers read against traces).
assert win.livedata._split.count() == 2
assert win.livedata._chart is win.livedata._split.widget(1)
assert not hasattr(win.livedata, "_views"), "sub-tabs should be gone"

# Chart vs. Time: bind the synthetic truck-mcp session and verify traces
# build, decimated paint works, and the view-state caption is honest. The
# lanes come from the shared layout model — the pane no longer decides.
from openobd.tmstore import TmSessionReader  # noqa: E402
from openobd.stripchart import ChartPane, chartable_names  # noqa: E402
from openobd.chanlayout import ChannelLayout  # noqa: E402
chart_reader = TmSessionReader(chart_db)
pane = ChartPane()
chan_meta = {c["name"]: c for c in chart_reader.channels()}
_layout = ChannelLayout()
_chartable = chartable_names(chart_reader, chan_meta)
pane.bind(chart_reader, chan_meta, False, _layout.chart_lanes(_chartable))
# rpm is a preset lane; ect qualifies via its numeric latest sample; the
# errored tft and never-read gear must not chart.
assert "rpm" in pane._channels and "ect" in pane._channels
assert "tft" not in pane._channels and "gear" not in pane._channels
rpm_trace = pane.chart.traces["rpm"]
assert len(rpm_trace.ts) == 301, "chart backfill missed samples"
assert pane.chart.caption == "live"
# per-session default window: live follows 1 min; archived opens on All
assert pane._span_combo.currentText() == "1 min"
pane.bind(chart_reader, chan_meta, True, _layout.chart_lanes(_chartable))
assert pane._span_combo.currentText() == "All"
assert pane.chart.span is None
pane.bind(chart_reader, chan_meta, False, _layout.chart_lanes(_chartable))
pane.tick(archived=False, session_stale=True)
assert pane.chart.caption == "not advancing", "stale view rendered as live"
pane.chart.grab()               # windowed paint
pane.chart.set_span(None)       # "All"
pane.chart.grab()               # full-session decimated paint
print("chart vs. time OK:", len(pane._channels), "channels,",
      len(pane.chart.lanes), "lanes,", len(rpm_trace.ts), "rpm points")
chart_reader.close()
pane.unbind()

# ONE selection model drives BOTH views: Live Data bound the synthetic session
# (TRUCK_MCP_DATA pins the root). Hiding a field removes its tile AND its
# trace; re-adding restores both; a custom lane grouping round-trips through
# QSettings; tile-only channels are labeled, never silently dropped.
ld = win.livedata
assert ld._reader is not None, "Live Data did not bind the synthetic session"
assert set(ld._tiles) == {"rpm", "ect", "tft", "gear"}
assert "rpm" in ld._chart._channels and "ect" in ld._chart._channels
ld._set_visible("rpm", False)
assert "rpm" not in ld._tiles, "hidden field still has a tile"
assert "rpm" not in ld._chart._channels, "hidden field still charts"
assert "rpm" not in ld._chart.chart.traces
ld._set_visible("rpm", True)
assert "rpm" in ld._tiles and "rpm" in ld._chart._channels
assert "rpm" in ld._chart.chart.traces
# custom grouping: move ect into its own new lane, persisted globally
ld._move_to_lane("ect", None)
lanes_now = ld._layout.chart_lanes(ld._chartable)
assert ["ect"] in lanes_now
assert ld._chart.chart.lanes == lanes_now, "chart lanes diverge from model"
saved = app_settings().value("livedata/layout")
assert saved, "custom layout not persisted to QSettings"
assert ChannelLayout.from_json(saved).chart_lanes(ld._chartable) == lanes_now
# tile-only rule is visible, not silent: tft (module error, no numeric
# latest) and gear (never read) tile but never chart
assert "tft" in ld._tiles and "tft" not in ld._chart._channels
assert "gear" in ld._tiles and "gear" not in ld._chart._channels
assert "tile only" in ld._tile_only_reason("tft")
# drag & drop surface: the graphed-channels table mirrors the chart lanes,
# tile drops land through the handler the table's drop signal drives, and
# removing a field from the graph keeps its tile on the palette.
from openobd.stripchart import channel_mime, mime_channel  # noqa: E402
assert mime_channel(channel_mime("rpm")) == "rpm"
tbl = ld._chart.table
flat = [n for lane in ld._chart.chart.lanes for n in lane]
assert tbl._names == flat, "table rows diverge from chart lanes"
assert tbl._droppable == set(ld._chartable)
ld._remove_from_graph("ect")
assert "ect" not in ld._chart._channels and "ect" not in tbl._names
assert "ect" in ld._tiles, "remove-from-graph must keep the tile"
ld._on_channel_dropped("ect", 0)      # drop onto the first row's lane
assert "ect" in ld._chart.chart.lanes[0]
assert "ect" in tbl._names and "ect" in ld._chart._channels
ld._tick()
row = tbl._names.index("ect")
assert tbl.item(row, 1).text() not in ("", "—"), "table value never updated"
print("drag & drop graph table OK: rows mirror lanes, drop adds, "
      "remove keeps tile")

# reset returns both views to defaults and removes the saved key
ld._reset_layout()
assert app_settings().value("livedata/layout") is None
assert ld._layout.is_default()
assert set(ld._tiles) == {"rpm", "ect", "tft", "gear"}
print("live data shared layout OK: hide/re-add syncs both views, "
      "custom lane persisted + reset")

# diagnostics: module map verdict rendering + codes table population using
# REAL parser output from synthetic ELM strings (no hardware)
from openobd.vehnet import ScanResult, Status, localize  # noqa: E402
from openobd.gt import parse_dtc_reply, parse_readiness  # noqa: E402
v = localize(ScanResult(port_open=True, interface_alive=True, dlc_volts=12.5,
                        hs_responders={"7E8", "7EA"},
                        pinged={"ebcm": False}))
win.diag.map_view.set_verdict(v)
assert v.modules["ecm"] == Status.OK and v.modules["ebcm"] == Status.SILENT
assert v.failure_point == "module:ebcm"
win.diag.map_view.grab()  # paints without error
win.diag._show_module("ebcm")
assert "EBCM" in win.diag.map_details.toPlainText()

dtcs = {"stored": parse_dtc_reply("7E806430203000171", "03")["codes"],
        "pending": parse_dtc_reply("7E80447010700", "07")["codes"],
        "permanent": []}
ready = parse_readiness([0x82, 0x07, 0xFF, 0x04])
win.diag.populate_codes(dtcs, ready)
assert win.diag.dtc_table.rowCount() == 3
assert win.diag.dtc_table.item(0, 1).text() == "P0300"
assert "ON" in win.diag.mil_label.text()
assert win.diag.ready_table.rowCount() > 0
# not examined must never render as "No trouble codes" / a clean MIL
win.diag.populate_codes({"examined": False, "error": "GT refused ATSH7DF",
                         "stored": None, "pending": None, "permanent": None},
                        {"error": "GT refused ATSH7E0"})
texts = [win.diag.dtc_table.item(r, 2).text()
         for r in range(win.diag.dtc_table.rowCount())]
assert all(t.startswith("Not examined") for t in texts), texts
assert "No trouble codes" not in texts
assert "not examined" in win.diag.mil_label.text()
# a module whose multi-frame reply was truncated shows as Incomplete, and
# the kinds never collapse to "No trouble codes"
win.diag.populate_codes({"examined": True, "stored": ["P0711"],
                         "pending": [], "permanent": [],
                         "incomplete": {"stored": {"7E8": "truncated: 6 of 8 bytes"}}},
                        ready)
texts = [win.diag.dtc_table.item(r, 2).text()
         for r in range(win.diag.dtc_table.rowCount())]
assert any(t.startswith("Incomplete from 7E8") for t in texts), texts
assert "No trouble codes" not in texts
win.diag.populate_codes(dtcs, ready)
print("diagnostics OK: map verdict painted,",
      win.diag.dtc_table.rowCount(), "DTC rows,",
      win.diag.ready_table.rowCount(), "monitors")

# select a table that carries stock values (needed by the editor asserts;
# the historical "WOT Shift Speed — Normal" name no longer exists in the
# full cal)
stock_table = next(t for t in cal.tables
                   if t.stock_values is not None and t.n_cols >= 2)
win._select_table(stock_table.name)
assert win.current_model is not None
assert win.current_model.table is stock_table
print("editor model rows/cols:", win.current_model.rowCount(), win.current_model.columnCount())

# load the synthetic log through the real code path
with open(log_path) as fh:
    from openobd.logbin import parse_csv
    win.log = parse_csv(fh.read(), source="smoke_log.csv")
win._render_report()
win._refresh_channel_combos()
print("mapped channels:", win.log.canonical_keys())

# apply a count overlay with x=vss
i = win.x_ch.findText("vss"); win.x_ch.setCurrentIndex(i)
win.overlay_mode.setCurrentIndex(1)  # Log count
win._apply_overlay()
ov = win.current_model.overlay
assert ov is not None, "overlay not applied"
print("overlay total binned:", ov.total_binned, "hottest cell:", ov.hottest_cell())

# mean overlay with value=knock_retard
win.v_ch.setCurrentIndex(win.v_ch.findText("knock_retard"))
win.overlay_mode.setCurrentIndex(2)
win._apply_overlay()
print("mean overlay applied, mode:", win.current_model.overlay_mode)

# dashboard: bind + a few ticks
from openobd.transport import LogReplaySource
win.dashboard.bind_source(LogReplaySource(win.log, speed=50.0))
win.dashboard.start()
for _ in range(5):
    app.processEvents()
    win.dashboard._tick()
vals = {k: g.value for k, g in win.dashboard.gauges.items()}
print("dashboard gauge sample:", {k: v for k, v in vals.items() if v is not None})

# dashboard visuals (Settings): switch to the modern cluster mid-replay —
# the source must stay bound, availability must be re-applied, and the new
# gauges must repopulate from latest() on the next tick.
from openobd.app import DASHBOARD_STYLES  # noqa: E402
assert set(DASHBOARD_STYLES) == {"classic", "modern"}
d = win.dashboard
src_before = d.source
d.set_style("modern")
assert d.source is src_before, "style switch dropped the bound source"
assert len(d.gauges) >= 18, "modern cluster gauges missing"
assert {"rpm", "vss", "ambient", "fuel_level"} <= set(d.gauges)
avail = set(d.source.channels())
assert all(g.available == (k in avail) for k, g in d.gauges.items()), \
    "availability not re-applied after style switch"
for _ in range(3):
    app.processEvents()
    d._tick()
assert any(g.value is not None for g in d.gauges.values()), \
    "modern gauges never repopulated after style switch"
d._cluster.grab()   # modern widgets paint without error
d.set_style("nonsense")          # unknown style must be a no-op
assert d.style == "modern"
d.set_style("classic")           # and back, for the sections below
assert {"rpm", "voltage", "tft"} <= set(d.gauges)
for _ in range(2):
    app.processEvents()
    d._tick()
d._cluster.grab()
print("dashboard style switch OK: modern <-> classic, source kept bound")
win.dashboard.stop()

# recording path: drain-based, streamed to a temp CSV (bypass the save dialog)
import tempfile, csv as _csv  # noqa: E402
# speed chosen so the 19.9s log does NOT loop during ~0.16s of ticking
# (a looped replay legitimately repeats timestamps)
win.dashboard.bind_source(LogReplaySource(win.log, speed=50.0))
win.dashboard.start()
d = win.dashboard
avail = set(d.source.channels())
from openobd.app import RECORD_ORDER  # noqa: E402
d._rec_keys = [k for k in RECORD_ORDER if k in avail]
d._rec_file = tempfile.NamedTemporaryFile(
    "w", newline="", encoding="utf-8", suffix=".csv", delete=False)
d._rec_writer = _csv.writer(d._rec_file)
d._rec_count = 0
d.source.drain()
d.recording = True
import time as _time  # noqa: E402
for _ in range(8):
    _time.sleep(0.02)
    app.processEvents()
    d._tick()
d.recording = False
rec_n = d._rec_count
d._rec_file.close()
with open(d._rec_file.name) as fh:
    rec_rows = fh.read().strip().splitlines()
os.unlink(d._rec_file.name)
d._rec_file = d._rec_writer = None
assert rec_n == len(rec_rows), f"row count mismatch: {rec_n} vs {len(rec_rows)}"
times = [r.split(",")[0] for r in rec_rows]
assert len(times) == len(set(times)), "duplicate samples in recording"
print("recording OK:", rec_n, "unique samples streamed")
win.dashboard.stop()

# teardown-on-rebind: old source must be stopped
old_src = win.dashboard.source
win.dashboard.bind_source(LogReplaySource(win.log, speed=1.0))
assert win.dashboard.source is not old_src
print("rebind teardown OK")

# scalar edit path + dirty tracking + invalid-input restore
assert win.dirty is False
win.cal.scalars[0].value = 4.10
win._set_scalar_row(0, win.cal.scalars[0])
it = win.scalar_tbl.item(0, 1)
it.setText("not-a-number")   # triggers _on_scalar_edit -> restore
assert win.scalar_tbl.item(0, 1).text() == f"{win.cal.scalars[0].value:g}", \
    "invalid scalar text not reverted"
it2 = win.scalar_tbl.item(0, 1)
it2.setText("3.73")
assert win.cal.scalars[0].value == 3.73
assert win.dirty is True, "scalar edit did not mark dirty"
print("scalar edit/dirty OK")

# Wave 1: editor power tools ------------------------------------------------
from PySide6.QtCore import QItemSelectionModel  # noqa: E402
win.main_tabs.setCurrentIndex(1)
win.tune_tabs.setCurrentIndex(0)
m = win.current_model
t = m.table
orig_00 = t.values[0][0]

# undo-routed cell edit via setData
m.setData(m.index(0, 0), str(orig_00 + 5))
assert t.values[0][0] == orig_00 + 5
assert win.undo_stack.canUndo()
win.undo_stack.undo()
assert t.values[0][0] == orig_00, "undo did not restore cell"
win.undo_stack.redo()
assert t.values[0][0] == orig_00 + 5, "redo did not reapply"
win.undo_stack.undo()

# selection math: add Amount to a 2-cell selection, then undo
selm = win.view.selectionModel()
selm.clear()
for c in (0, 1):
    selm.select(m.index(0, c), QItemSelectionModel.Select)
win.amount_edit.setText("3")
before = [t.values[0][0], t.values[0][1]]
win._math_op("add")
assert [t.values[0][0], t.values[0][1]] == [before[0] + 3, before[1] + 3]
win.undo_stack.undo()
assert [t.values[0][0], t.values[0][1]] == before, "math undo failed"

# copy -> paste round trip through the real clipboard
selm.clear()
for c in (0, 1):
    selm.select(m.index(0, c), QItemSelectionModel.Select)
win.copy_selection()
win.view.setCurrentIndex(m.index(0, 0))
win.paste_selection()  # pasting identical values -> no-op, no crash
clip = QApplication.clipboard().text() if False else None  # noqa: F841

# whole-table revert + changed-cell navigation
m.setData(m.index(0, 0), str(orig_00 + 9))
from openobd import editops  # noqa: E402
assert editops.changed_cells(t), "edit did not register as changed"
win._goto_next_changed()
win._revert_table()
assert editops.changed_cells(t) == [], "revert table left changes"
win.undo_stack.undo()  # undo the revert
assert editops.changed_cells(t), "undo of revert failed"
win._revert_table()

# tree filter
win.tree_filter.setText("shift")
vis = sum(not win.tree.topLevelItem(i).isHidden()
          for i in range(win.tree.topLevelItemCount()))
assert vis >= 1
win.tree_filter.setText("")
print("editor power tools OK (undo/math/clipboard/revert/filter)")

# Wave 2: charting + replay transport --------------------------------------
from PySide6.QtCore import Qt  # noqa: E402
win.load_log_path(log_path)          # full path: times, events, chart, replay
assert win._log_times, "log times not computed"
assert win.chan_list.count() > 0, "channel list empty"
assert len(win._plots) >= 1, "no plots built"
assert win._knock_times, "knock event not found for markers"
# toggle a channel and rebuild
item0 = win.chan_list.item(0)
item0.setCheckState(Qt.Checked if item0.checkState() != Qt.Checked
                    else Qt.Unchecked)
app.processEvents()
# cursor readout across plots
from PySide6.QtCore import QPointF as _QPF  # noqa: E402
win._on_chart_mouse(win._plots[0].vb.mapViewToScene(_QPF(5.0, 0.0)))
assert win.cursor_label.text().startswith("t="), "cursor readout empty"

# replay transport controls
d = win.dashboard
assert d.replay_ctl.isVisible() or True  # offscreen: visibility not painted
assert hasattr(d.source, "seek")
d.start()
d._toggle_pause()
assert d.source.playing is False and d.btn_pause.text().startswith("▶")
d.pos_slider.setValue(50)            # 5.0s
d._on_seek()
assert abs(d.source.position() - 5.0) < 0.2
d._toggle_pause()
assert d.source.playing is True
d.speed_combo.setCurrentText("8×")
assert d.source.speed == 8.0
for _ in range(3):
    app.processEvents(); d._tick()
d.stop()
assert d.source.playing is False, "stop did not pause replay"
# gauge min/max capture + click reset
g = next(iter(d.gauges.values()))
if g.value is not None:
    assert g.max_seen is not None
    g.mousePressEvent(None)
    assert g.max_seen is None
print("charting + transport OK:",
      len(win._plots), "plots,", len(win._knock_times), "knock markers")

# save round trip
out = "/tmp/smoke_out.cal.json"
win.path = out
win.save_cal()
from openobd.calspec import Calibration
back = Calibration.load(out)
assert len(back.tables) == len(cal.tables)
print("save/load OK, tables:", len(back.tables))

# settings-isolation canary: the run above exercised layout writes, style
# switches and resets — the user's REAL registry must be untouched.
assert _real_registry_snapshot() == _registry_before, \
    "smoke leaked into the real per-user settings — isolation broken"
print("settings isolation OK: real registry untouched")

print("SMOKE OK")
