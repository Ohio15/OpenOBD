"""Live Data tab UX (v0.37.0): workspace chrome, Hold, tile filter, groups,
min / max footer, graphed-table statistics, the strip chart's time axis and
hover cursor, shortcut routing and persisted layout state.

Offscreen; every test runs against its own QSettings INI tree and its own
TRUCK_MCP_DATA temp tree built with tests.test_tmstore._make_session. No
sleeps: "live" sessions are made live by writing samples stamped now."""
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_tmstore import _make_session  # noqa: E402

VIN = "3GCRKTE35AG150432"


# --------------------------------------------------------------------------- #
# fixtures / helpers
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def env(tmp_path, monkeypatch, qapp):
    from PySide6.QtCore import QSettings
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope,
                      str(tmp_path / "settings"))
    monkeypatch.setenv("TRUCK_MCP_DATA", str(tmp_path / "truck-mcp"))
    sessions = tmp_path / "truck-mcp" / "sessions"
    sessions.mkdir(parents=True)
    pages = []

    def make():
        from openobd.livedata import LiveDataPage
        pg = LiveDataPage()
        pg.resize(1200, 800)
        pages.append(pg)
        return pg

    yield SimpleNamespace(sessions=sessions, make=make, root=tmp_path,
                          app=qapp)
    for pg in pages:
        pg.shutdown()
        pg.deleteLater()
    qapp.processEvents()


def _now_iso(offset_s: float = 0.0) -> str:
    t = datetime.now(timezone.utc).timestamp() + offset_s
    return datetime.fromtimestamp(t, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _session(env, name="drive", **kw):
    path = env.sessions / f"{name}.tmsession.db"
    kw.setdefault("uid", f"uid-{name}")   # list_sessions dedupes by uid
    _make_session(path, **kw)
    return path


def _add_rpm(path, values, ts=None):
    """Fresh rpm samples (channel 1), the newest stamped now unless ts is
    given, and channel_latest moved to the newest: a live, advancing drive."""
    conn = sqlite3.connect(str(path))
    n = len(values)
    last_id = None
    for i, v in enumerate(values):
        stamp = ts or _now_iso(-(n - 1 - i))
        cur = conn.execute(
            "INSERT INTO sample (ts_utc, channel_id, value_num, fresh, "
            "stale_s, quality) VALUES (?,1,?,1,0.0,'ok')", (stamp, float(v)))
        last_id = cur.lastrowid
    conn.execute("UPDATE channel_latest SET sample_id=?, ts_utc=?, "
                 "value_num=?, fresh=1, stale_s=0.0, quality='ok' "
                 "WHERE channel_id=1",
                 (last_id, ts or _now_iso(), float(values[-1])))
    conn.commit()
    conn.close()


def _pick(pg, path):
    """Select a session in the picker by file (the picker stores the path
    list_sessions resolved, so match on the file name)."""
    from pathlib import Path
    for i in range(pg._picker.count()):
        if Path(pg._picker.itemData(i)).name == Path(path).name:
            pg._picker.setCurrentIndex(i)
            return
    raise AssertionError(f"{path} not in the picker")


def _live(env, name="drive", values=(1000, 1500, 2500), **kw):
    path = _session(env, name, **kw)
    _add_rpm(path, list(values))
    return path


# --------------------------------------------------------------------------- #
# chrome: breadcrumb path, chips, note
# --------------------------------------------------------------------------- #
def test_live_session_chips_and_note(env):
    _live(env)
    pg = env.make()
    assert pg._reader is not None
    assert pg.crumb.path_text() == "Live Data › drive"
    chips = pg.crumb.chips()
    assert chips[0] == ("live", "ok")
    assert ("4 channels", "neutral") in chips
    assert (f"{len(pg._chart._channels)} graphed", "neutral") in chips
    assert not any(t in ("SIMULATED", "PROVENANCE UNKNOWN", "held")
                   for t, _k in chips)
    note = pg.crumb.note.full_text()
    assert VIN in note and "source=live" in note and "as of" in note
    assert pg.banner_text() is None
    assert pg.act_hold.isEnabled()


def test_stale_session_is_not_advancing(env):
    _session(env)            # newest sample is from 2026-08-21: not advancing
    pg = env.make()
    assert pg.crumb.chips()[0] == ("not advancing", "warning")
    assert pg._tiles["rpm"].state == "stale"
    assert pg._chart.chart.caption == "not advancing"


def test_archived_session_chip_and_hold_ignored(env):
    _session(env, ended="2026-08-21T10:30:00Z")
    pg = env.make()
    assert pg.crumb.chips()[0] == ("archived", "muted")
    assert not pg.act_hold.isEnabled()
    pg._set_hold(True)
    assert not pg.is_held() and not pg.act_hold.isChecked()
    assert pg._tiles["rpm"].state == "archive"


@pytest.mark.parametrize("source,chip", [
    ("simulated", ("SIMULATED", "danger")),
    ("telepathy", ("PROVENANCE UNKNOWN", "warning")),
])
def test_provenance_chips(env, source, chip):
    _session(env, source=source)
    pg = env.make()
    assert chip in pg.crumb.chips()
    assert f"source={source}" in pg.crumb.note.full_text()


def test_read_failure_banner_after_two_ticks_and_recovery(env):
    path = _live(env)
    pg = env.make()
    reader = pg._reader

    def boom(*_a, **_k):
        raise sqlite3.OperationalError("disk I/O error")

    reader.latest = boom
    pg._tick()
    assert pg.banner_text() is None, "one blip must not raise the banner"
    pg._tick()
    banner = pg.banner_text()
    assert banner and "Read failed (2×)" in banner and "disk I/O" in banner
    assert pg.crumb.chips()[0] == ("read failed", "danger")
    assert all(t.state == "failed" for t in pg._tiles.values())
    assert pg._chart.chart.caption == "read failed"
    del reader.latest                     # the store recovers
    _add_rpm(path, [2600])
    pg._tick()
    assert pg.banner_text() is None
    assert pg.crumb.chips()[0] == ("live", "ok")
    assert pg._tiles["rpm"].state == "fresh"


def test_unopenable_session_shows_danger_banner(env):
    (env.sessions / "broken.tmsession.db").write_bytes(b"not a database" * 64)
    pg = env.make()
    banner = pg.banner_text()
    assert banner and banner.startswith("Could not open session")
    assert pg._reader is None
    assert pg._banner.property("banner") == "danger"
    assert not pg._empty.isHidden()


def test_empty_states(env, monkeypatch):
    pg = env.make()
    assert not pg._empty.isHidden()
    assert "No drive-log sessions found" in pg._empty.text()
    assert pg.crumb.path_text() == "Live Data" and pg.crumb.chips() == []
    monkeypatch.setenv("TRUCK_MCP_DATA", str(env.root / "nowhere"))
    pg.rescan_sessions()
    assert "data root not found" in pg._empty.text()
    from PySide6.QtCore import Qt
    assert pg._empty.textFormat() == Qt.PlainText


# --------------------------------------------------------------------------- #
# Hold
# --------------------------------------------------------------------------- #
def test_hold_freezes_and_release_catches_up(env):
    from openobd import tmstore
    path = _live(env)
    pg = env.make()
    pane = pg._chart
    tile = pg._tiles["rpm"]
    cursor0 = pane._cursor
    points0 = len(pane.chart.traces["rpm"].ts)
    value0 = tile.value_text()
    table0 = pane.table.text("rpm", pane.table.COL_VALUE)
    assert tile.state == "fresh"

    pg.act_hold.setChecked(True)
    assert pg.is_held()
    assert ("held", "warning") in pg.crumb.chips()
    assert pane.chart.caption == "held"
    assert tile.shown_state == "stale", "a frozen live value kept the live palette"
    assert "held" in tile.caption_text()

    _add_rpm(path, [4321])
    pg._tick()
    assert pane._cursor == cursor0, "held page advanced the chart cursor"
    assert len(pane.chart.traces["rpm"].ts) == points0
    assert tile.value_text() == value0
    assert pane.table.text("rpm", pane.table.COL_VALUE) == table0

    pg.act_hold.setChecked(False)
    assert not pg.is_held()
    assert pane._cursor > cursor0, "release did not catch up"
    assert len(pane.chart.traces["rpm"].ts) == points0 + 1
    expect = tmstore.display_state(
        {"value_num": 4321.0, "fresh": True, "quality": "ok"},
        pg._channel_meta["rpm"]).text
    assert tile.value_text() == expect
    assert pane.table.text("rpm", pane.table.COL_VALUE) == expect
    assert pane.chart.caption == "live"
    assert ("held", "warning") not in pg.crumb.chips()


def test_hold_releases_when_the_drive_ends(env):
    path = _live(env)
    pg = env.make()
    pg.act_hold.setChecked(True)
    conn = sqlite3.connect(str(path))
    conn.execute("UPDATE session SET ended_utc=? WHERE id=1", (_now_iso(),))
    conn.commit()
    conn.close()
    pg._tick()
    assert not pg.is_held()
    assert pg.crumb.chips()[0] == ("archived", "muted")


# --------------------------------------------------------------------------- #
# tile filter, groups, min / max footer
# --------------------------------------------------------------------------- #
def test_tile_filter_is_display_only(env):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from openobd import chanlayout
    from openobd.appsettings import app_settings
    _live(env)
    pg = env.make()
    before = pg._layout.to_json()
    pg._filter.setText("rp")
    assert pg.visible_tile_names() == ["rpm"]
    assert pg._groups["Trans"].isHidden() and pg._groups["Sensors"].isHidden()
    pg._filter.setText("sensors")           # matches the group name
    assert pg.visible_tile_names() == ["ect"]
    pg._filter.setText("t")
    assert pg.visible_tile_names() == ["ect", "gear", "tft"]
    assert "2 of 2" not in pg._groups["Trans"].header.text()
    assert pg._layout.to_json() == before and pg._layout.is_default()
    assert app_settings().value(chanlayout.SETTINGS_KEY) is None
    assert set(pg._tiles) == {"rpm", "ect", "tft", "gear"}
    QTest.keyClick(pg._filter, Qt.Key_Escape)
    assert pg._filter.text() == ""
    assert pg.visible_tile_names() == ["ect", "gear", "rpm", "tft"]


def test_tiles_grouped_with_counts_and_collapse_persists(env):
    from openobd.appsettings import app_settings
    from openobd.livedata import GROUPS_KEY
    _live(env)
    pg = env.make()
    assert list(pg._groups) == ["Engine", "Trans", "Sensors"]
    assert pg._groups["Trans"].names == ["gear", "tft"]
    assert pg._groups["Trans"].header.text() == "Trans  ·  2"
    pg._groups["Trans"].set_collapsed(True)
    assert pg._groups["Trans"].body.isHidden()
    assert json.loads(app_settings().value(GROUPS_KEY)) == ["Trans"]
    pg2 = env.make()
    assert pg2._groups["Trans"].is_collapsed()
    assert not pg2._groups["Engine"].is_collapsed()
    pg2._groups["Trans"].set_collapsed(False)
    assert app_settings().value(GROUPS_KEY) is None


def test_min_max_footer_tracks_fresh_samples_and_resets(env):
    path = _live(env, values=(1000, 1500, 2500))
    pg = env.make()
    # The session's own rows: rpm 900 fresh at bind time too.
    assert pg.tile_range("rpm") == (900.0, 2500.0)
    assert pg._tiles["rpm"].range_text() == "min 900 · max 2,500"
    # ect's only sample is carried (fresh=0): it never counts.
    assert pg.tile_range("ect") is None
    assert pg._tiles["ect"].range_text() == "min — · max —"
    _add_rpm(path, [3100])
    pg._tick()
    assert pg.tile_range("rpm") == (900.0, 3100.0)
    # Rebinding (another session) starts the footer over.
    other = _session(env, "second")
    _add_rpm(other, [700])
    pg.rescan_sessions()
    _pick(pg, other)
    assert pg.tile_range("rpm") == (700.0, 900.0)


def test_range_after_is_incremental_and_fresh_only(env):
    from openobd.tmstore import TmSessionReader
    path = _live(env, values=(1000, 3000))
    with TmSessionReader(path) as r:
        out, cursor = r.range_after(["rpm", "ect", "tft", "gear"])
        assert out == {"rpm": (900.0, 3000.0)}
        again, cursor2 = r.range_after(["rpm"], cursor)
        assert again == {} and cursor2 == cursor


# --------------------------------------------------------------------------- #
# graphed-channels table
# --------------------------------------------------------------------------- #
def test_table_min_max_avg_over_visible_window(env):
    from openobd.stripchart import fmt_value, window_stats
    _live(env, values=(1000, 1500, 2600))
    pg = env.make()
    pane = pg._chart
    pane.set_span_text("All")
    tr = pane.chart.traces["rpm"]
    t0, t1 = pane.chart._window()
    lo, hi, avg = window_stats(tr.ts, tr.vs, t0, t1)
    assert (lo, hi) == (900.0, 2600.0)
    tbl = pane.table
    assert tbl.text("rpm", tbl.COL_MIN) == fmt_value(lo)
    assert tbl.text("rpm", tbl.COL_MAX) == fmt_value(hi)
    assert tbl.text("rpm", tbl.COL_AVG) == fmt_value(avg)
    assert tbl.text("rpm", tbl.COL_LANE) == "Lane 1"
    assert tbl.horizontalHeaderItem(tbl.COL_AVG).toolTip()
    assert not tbl.item(0, tbl.COL_FIELD).icon().isNull(), "no colour swatch"
    assert "■" not in tbl.item(0, tbl.COL_FIELD).text()


def test_table_empty_state_row_and_drop_semantics(qapp):
    from openobd.stripchart import ChannelTable
    tbl = ChannelTable()
    assert tbl.is_empty_state() and tbl.rowCount() == 1
    assert tbl.item(0, 0).text() == ChannelTable.EMPTY_TEXT
    assert tbl.channel_at(0) is None     # a drop there = new lane


# --------------------------------------------------------------------------- #
# strip chart: time axis, cursor, events (pure functions + widget mapping)
# --------------------------------------------------------------------------- #
def test_time_ticks_relative_for_live():
    from openobd.stripchart import time_ticks, tick_step
    ticks = time_ticks(1000.0, 1060.0, 6)
    assert [lab for _t, lab in ticks] == [
        "-1:00", "-0:50", "-0:40", "-0:30", "-0:20", "-0:10", "0:00"]
    assert ticks[-1][0] == 1060.0
    assert tick_step(60, 6) == 10 and tick_step(300, 6) == 60
    assert tick_step(30, 100) == 1
    assert time_ticks(5.0, 5.0, 4) == [] and time_ticks(0, 10, 0) == []
    long = time_ticks(0.0, 3723.0, 3)
    assert long[0][1] == "-1:00:00" and long[-1][1] == "0:00"


def test_time_ticks_clock_for_archived():
    from openobd.stripchart import TIME_MODE_CLOCK, fmt_clock, time_ticks
    t0 = datetime(2026, 8, 21, 10, 0, 5, tzinfo=timezone.utc).timestamp()
    ticks = time_ticks(t0, t0 + 120, 4, TIME_MODE_CLOCK, tz=timezone.utc)
    assert [lab for _t, lab in ticks] == [
        "10:00:30", "10:01:00", "10:01:30", "10:02:00"]
    assert all(t % 30 == 0 for t, _l in ticks)
    assert fmt_clock(t0, timezone.utc) == "10:00:05"


def test_cursor_mapping_pure():
    from openobd.stripchart import value_at, x_to_time
    assert x_to_time(150, 100, 300, 0.0, 60.0) == 15.0
    assert x_to_time(50, 100, 300, 0.0, 60.0) == 0.0       # clamped
    assert x_to_time(999, 100, 300, 0.0, 60.0) == 60.0
    ts, vs = [10.0, 20.0, 30.0], [1.0, 2.0, 3.0]
    assert value_at(ts, vs, 5.0) is None       # before the first measurement
    assert value_at(ts, vs, 10.0) == 1.0
    assert value_at(ts, vs, 25.0) == 2.0       # sample-and-hold
    assert value_at(ts, vs, 99.0) == 3.0
    assert value_at([], [], 1.0) is None


def test_chart_hover_drives_readout_and_table(env):
    from openobd.stripchart import fmt_value, value_at, x_to_time
    _live(env, values=(1000, 1500, 2600))
    pg = env.make()
    pane = pg._chart
    chart = pane.chart
    chart.resize(900, 300)
    pane.set_span_text("All")
    live_text = pane.table.text("rpm", pane.table.COL_VALUE)
    x0, x1 = chart.plot_bounds()
    x = (x0 + x1) / 2
    seen = []
    chart.hover_changed.connect(seen.append)
    chart.set_hover(x)
    t0, t1 = chart._window()
    t = x_to_time(x, x0, x1, t0, t1)
    tr = chart.traces["rpm"]
    assert seen[-1][0] == t
    assert seen[-1][1]["rpm"] == value_at(tr.ts, tr.vs, t)
    tbl = pane.table
    assert tbl.horizontalHeaderItem(tbl.COL_VALUE).text() == "@ Cursor"
    assert tbl.text("rpm", tbl.COL_VALUE) == fmt_value(value_at(tr.ts, tr.vs, t))
    chart.grab()                          # paints hairline + readout box
    chart.set_hover(None)
    assert seen[-1] is None
    assert tbl.horizontalHeaderItem(tbl.COL_VALUE).text() == "Value"
    assert tbl.text("rpm", tbl.COL_VALUE) == live_text
    chart.set_hover(x0 - 20)              # outside the plot = no cursor
    assert seen[-1] is None


def test_events_and_time_mode(env):
    from openobd.stripchart import TIME_MODE_CLOCK, TIME_MODE_RELATIVE
    _session(env, ended="2026-08-21T10:30:00Z")
    pg = env.make()
    chart = pg._chart.chart
    assert chart.time_mode == TIME_MODE_CLOCK
    assert [lab for _t, lab in chart.events] == ["wot"]
    chart.resize(900, 300)
    chart.grab()
    ticks = chart.axis_ticks(*chart._window(), 600)
    assert chart.axis_ticks(*chart._window(), 600) is ticks, "ticks not cached"
    _live(env, name="live2")
    pg.rescan_sessions()
    _pick(pg, env.sessions / "live2.tmsession.db")
    assert pg._chart.chart.time_mode == TIME_MODE_RELATIVE


def test_events_skipped_without_event_table(env, qapp):
    from openobd.stripchart import ChartPane
    pane = ChartPane()

    class NoEvents:
        def events(self, **_k):
            raise sqlite3.OperationalError("no such table: event")
    pane._reader = NoEvents()
    pane._load_events()
    assert pane._events_ok is False and pane.chart.events == []


# --------------------------------------------------------------------------- #
# shortcuts, width, persistence
# --------------------------------------------------------------------------- #
def test_f5_only_while_live_data_is_showing(env):
    from PySide6.QtWidgets import QTabWidget, QWidget
    _live(env)
    tabs = QTabWidget()
    other = QWidget()
    pg = env.make()
    tabs.addTab(other, "Other")
    tabs.addTab(pg, "Live Data")
    tabs.show()
    env.app.processEvents()
    assert not pg._f5.isEnabled()
    tabs.setCurrentWidget(pg)
    env.app.processEvents()
    assert pg._f5.isEnabled()
    rescans = []
    pg.rescan_sessions = lambda: rescans.append(1)
    pg._f5.activated.disconnect()
    pg._f5.activated.connect(pg.rescan_sessions)
    pg._f5.activated.emit()
    assert rescans == [1]
    tabs.setCurrentWidget(other)
    env.app.processEvents()
    assert not pg._f5.isEnabled()
    tabs.hide()
    tabs.removeTab(tabs.indexOf(pg))
    pg.setParent(None)                    # the fixture owns its teardown


def test_ctrl_f_routes_to_tile_filter_only_on_live_data(env):
    from openobd.app import MainWindow
    _live(env)
    pg = env.make()
    tuning, diag = object(), SimpleNamespace(has_search=lambda: False)
    calls = []
    shortcut = SimpleNamespace(enabled=None)
    shortcut.setEnabled = lambda on: setattr(shortcut, "enabled", on)
    cur = {"w": tuning}
    win = SimpleNamespace(
        main_tabs=SimpleNamespace(currentWidget=lambda: cur["w"]),
        tuning_page=tuning, diag=diag, livedata=pg,
        _find_shortcut=shortcut,
        navigator=SimpleNamespace(focus_search=lambda: calls.append("nav")))
    pg.focus_filter = lambda: calls.append("livedata")
    cur["w"] = pg
    MainWindow._sync_find_shortcut(win)
    assert shortcut.enabled is True
    MainWindow.focus_table_search(win)
    assert calls == ["livedata"]
    cur["w"] = object()                       # Dashboard / Active Tests
    MainWindow._sync_find_shortcut(win)
    assert shortcut.enabled is False


def test_min_width_budget(env):
    _live(env)
    pg = env.make()
    pg._tick()
    assert pg.minimumSizeHint().width() <= 700


def test_span_and_splitter_persist(env):
    from openobd.appsettings import app_settings
    from openobd.livedata import SPAN_KEY, SPLIT_KEY
    _live(env)
    pg = env.make()
    combo = pg._chart.span_combo
    assert pg.toolbar.isAncestorOf(combo), "span combo is not in the toolbar"
    idx = combo.findText("5 min")
    combo.setCurrentIndex(idx)
    combo.activated.emit(idx)
    assert app_settings().value(SPAN_KEY) == "5 min"
    pg._split.setSizes([120, 520])
    pg._split.splitterMoved.emit(120, 1)
    saved = app_settings().value(SPLIT_KEY)
    assert saved is not None
    pg2 = env.make()
    assert pg2._chart.span_text() == "5 min"
    assert pg2._split.saveState() == saved
    # An archived drive still opens on the whole drive.
    _session(env, "done", ended="2026-08-21T10:30:00Z")
    pg2.rescan_sessions()
    _pick(pg2, env.sessions / "done.tmsession.db")
    assert pg2._chart.span_text() == "All"


# --------------------------------------------------------------------------- #
# review round 1 + security-gate pass 1 regressions
# --------------------------------------------------------------------------- #
def _add_sample(path, channel_id, value, fresh=1, ts=None, latest=True):
    conn = sqlite3.connect(str(path))
    stamp = ts or _now_iso()
    stale = 0.0 if fresh else 2.0
    cur = conn.execute(
        "INSERT INTO sample (ts_utc, channel_id, value_num, fresh, stale_s, "
        "quality) VALUES (?,?,?,?,?,'ok')",
        (stamp, channel_id, float(value), fresh, stale))
    sid = cur.lastrowid
    if latest:
        conn.execute("UPDATE channel_latest SET sample_id=?, ts_utc=?, "
                     "value_num=?, fresh=?, stale_s=? WHERE channel_id=?",
                     (sid, stamp, float(value), fresh, stale, channel_id))
    conn.commit()
    conn.close()
    return sid


def test_r1_data_root_gone_unbinds_reader_and_chart(env, monkeypatch):
    from openobd import tmstore
    _live(env)
    pg = env.make()
    assert pg._reader is not None and pg._chart.chart.caption == "live"
    monkeypatch.setattr(tmstore, "sessions_dirs", lambda: [])
    pg.rescan_sessions()
    assert pg._reader is None
    assert pg._chart.chart.caption == ""
    assert pg._tiles == {} and pg._chart._channels == []
    assert "data root not found" in pg._empty.text()
    pg._tick()   # nothing bound: must not read or raise


def test_r2_layout_edit_while_held_does_not_advance(env):
    path = _live(env)
    pg = env.make()
    pane = pg._chart
    # ect off the graph first (unheld), so the held edit adds a NEW trace.
    pg._remove_from_graph("ect")
    assert "ect" not in pane.chart.traces
    _add_sample(path, 2, 195.0)            # fresh ect, recorded BEFORE hold
    pg._tick()
    pg.act_hold.setChecked(True)
    cursor0 = pane._cursor
    rpm_pts = len(pane.chart.traces["rpm"].ts)
    table_rpm = pane.table.text("rpm", pane.table.COL_VALUE)
    tile_rpm = pg._tiles["rpm"].value_text()

    _add_rpm(path, [4321])                 # recorded DURING the hold
    _add_sample(path, 2, 222.0)
    pg._on_channel_dropped("ect", None)    # drop a tile onto the table

    assert pg.is_held()
    assert cursor0 <= pane._cursor <= pg._range_cursor, \
        "held relayout read past the hold"
    assert len(pane.chart.traces["rpm"].ts) == rpm_pts
    assert pane.chart.traces["ect"].vs == [195.0], \
        "new trace backfilled past the hold"
    assert pane.table.text("rpm", pane.table.COL_VALUE) == table_rpm
    assert pg._tiles["rpm"].value_text() == tile_rpm
    assert pane.chart.caption == "held"
    assert pane.table.is_held()
    assert pane.table.horizontalHeaderItem(
        pane.table.COL_VALUE).text() == pane.table.HELD_HEADER
    assert "held" in pg._tiles["rpm"].caption_text()

    pg.act_hold.setChecked(False)
    assert not pane.table.is_held()
    assert pane.table.horizontalHeaderItem(
        pane.table.COL_VALUE).text() == "Value"
    assert pane.chart.traces["ect"].vs == [195.0, 222.0]
    assert len(pane.chart.traces["rpm"].ts) == rpm_pts + 1


def test_r3_tile_stylesheet_only_on_state_transition(env):
    _live(env)
    pg = env.make()
    tile = pg._tiles["rpm"]
    assert tile.state == "fresh"
    calls = []
    tile.setStyleSheet = lambda qss: calls.append(("frame", qss))
    tile._value.setStyleSheet = lambda qss: calls.append(("value", qss))
    pg._tick()
    pg._tick()
    assert calls == [], "unchanged state re-applied the stylesheet"
    pg.act_hold.setChecked(True)           # fresh -> held palette
    assert len(calls) == 2


def test_r4_hover_repaint_reuses_decimated_points(env, monkeypatch):
    from openobd import stripchart
    _live(env, values=list(range(1000, 1400, 10)))
    pg = env.make()
    chart = pg._chart.chart
    chart.resize(900, 400)
    n = []
    real = stripchart.window_points

    def counting(*a, **k):
        n.append(1)
        return real(*a, **k)

    monkeypatch.setattr(stripchart, "window_points", counting)
    chart.grab()
    first = len(n)
    assert first >= 1
    x0, x1 = chart.plot_bounds()
    chart.set_hover((x0 + x1) / 2)
    chart.grab()
    chart.set_hover(x0 + 5)
    chart.grab()
    assert len(n) == first, "hover repaint recomputed window_points"


def test_r5_clock_formatting_never_raises():
    from openobd.stripchart import (
        CLOCK_UNKNOWN, TIME_MODE_CLOCK, fmt_clock, time_ticks)
    assert time_ticks(-60, 0, 4, TIME_MODE_CLOCK) == []
    assert time_ticks(float("nan"), 10, 4, TIME_MODE_CLOCK) == []
    for bad in (-60, float("nan"), float("inf"), 1e18):
        assert fmt_clock(bad) == CLOCK_UNKNOWN


def test_r5_archived_no_fresh_samples_paints(env, monkeypatch):
    path = _session(env, ended="2026-08-21T10:30:00Z")
    conn = sqlite3.connect(str(path))
    conn.execute("UPDATE sample SET fresh=0")
    conn.commit()
    conn.close()
    errors = []
    monkeypatch.setattr(sys, "excepthook", lambda *exc: errors.append(exc))
    pg = env.make()
    chart = pg._chart.chart
    assert chart.lanes and not any(tr.ts for tr in chart.traces.values())
    assert chart.time_mode == "clock"
    chart.resize(900, 400)
    chart.grab()
    t0, t1 = chart._window()
    assert chart.axis_ticks(t0, t1, 600) == []
    assert errors == []


def test_r6_range_after_cursor_passes_carried_rows(env):
    from openobd.tmstore import TmSessionReader
    path = _live(env, values=(1000, 3000))
    with TmSessionReader(path) as r:
        _out, cursor = r.range_after(["rpm"])
    last_carried = None
    for v in (9999.0, 9998.0, 9997.0):
        last_carried = _add_sample(path, 1, v, fresh=0)
    with TmSessionReader(path) as r:
        out, c2 = r.range_after(["rpm"], cursor)
        assert out == {}, "carried rows counted toward min / max"
        assert c2 == last_carried, "cursor stalled behind carried rows"
        again, c3 = r.range_after(["rpm"], c2)
        assert again == {} and c3 == c2
    fresh_id = _add_sample(path, 1, 1234.0)
    with TmSessionReader(path) as r:
        out, c4 = r.range_after(["rpm", "ect"], c2)
        assert out == {"rpm": (1234.0, 1234.0)} and c4 == fresh_id


def test_r7_archived_span_choice_not_persisted(env):
    from openobd.appsettings import app_settings
    from openobd.livedata import SPAN_KEY
    _session(env, ended="2026-08-21T10:30:00Z")
    pg = env.make()
    combo = pg._chart.span_combo
    idx = combo.findText("5 min")
    combo.setCurrentIndex(idx)
    combo.activated.emit(idx)
    assert pg._chart.span_text() == "5 min"   # applied to the archived view
    assert app_settings().value(SPAN_KEY) is None


def test_r8_held_carried_tile_drops_its_age(env):
    _live(env)
    pg = env.make()
    tile = pg._tiles["ect"]
    assert tile.state == "carried" and "old" in tile.caption_text()
    pg.act_hold.setChecked(True)
    assert tile.caption_text() == "held · carried"
    assert tile.shown_state != "fresh"
    pg.act_hold.setChecked(False)
    assert "old" in tile.caption_text()


def test_g1_db_text_labels_are_plain_text(env):
    from PySide6.QtCore import Qt
    from openobd.livedata import ValueTile
    from openobd.stripchart import ChartPane
    from openobd.tuningui import Breadcrumb, ElidedLabel
    tile = ValueTile("x", "<b>x</b>")
    for lab in (tile._unit_lbl, tile._value, tile._state, tile._range,
                tile._label):
        assert lab.textFormat() == Qt.PlainText
    assert tile._unit_lbl.text() == "<b>x</b>"
    assert ChartPane()._note.textFormat() == Qt.PlainText
    assert ElidedLabel("<i>n</i>").textFormat() == Qt.PlainText
    crumb = Breadcrumb()
    crumb.set_chips([("<a href='x'>c</a>", "neutral")])
    assert all(lab.textFormat() == Qt.PlainText for lab in crumb._chips)
    assert crumb.title.textFormat() == Qt.PlainText
    assert crumb.note.textFormat() == Qt.PlainText


@pytest.mark.parametrize("bad", ["garbage", "1,2,3", 42])
def test_g3_tampered_splitter_state_falls_back(env, bad):
    from openobd.appsettings import app_settings
    from openobd.livedata import SPLIT_KEY
    app_settings().setValue(SPLIT_KEY, bad)
    app_settings().sync()
    pg = env.make()
    assert pg._restore_split(bad) is False
    assert len(pg._split.sizes()) == 2


# --------------------------------------------------------------------------- #
# review round 2 / gate pass 2
# --------------------------------------------------------------------------- #
def _raise_series_after(reader, times):
    """Make the reader's series_after raise for the next `times` calls."""
    real = reader.series_after
    left = [times]

    def flaky(*a, **kw):
        if left[0] > 0:
            left[0] -= 1
            raise sqlite3.OperationalError("database is locked")
        return real(*a, **kw)
    reader.series_after = flaky
    return left


def test_rr2_f1_failed_chart_read_then_held_relayout_has_no_gap(env):
    path = _live(env)
    pg = env.make()
    pane = pg._chart
    rpm0 = list(pane.chart.traces["rpm"].vs)
    cursor0 = pane._cursor
    _add_rpm(path, [3333])
    _raise_series_after(pane._reader, 1)
    pg._tick()                   # page range read sees 3333, chart read fails
    assert pane._cursor == cursor0
    assert pg._range_cursor > cursor0

    pg.act_hold.setChecked(True)
    pg._remove_from_graph("ect")             # layout edit while held
    assert pane._cursor == cursor0, "held relayout skipped unread rows"
    assert pane.chart.traces["rpm"].vs == rpm0

    pg.act_hold.setChecked(False)
    assert pane.chart.traces["rpm"].vs == rpm0 + [3333.0], \
        "surviving trace lost the row its failed read missed"


def test_rr2_f2_failed_frozen_backfill_is_marked_until_loaded(env):
    path = _live(env)
    pg = env.make()
    pane = pg._chart
    table = pane.table
    pg._remove_from_graph("ect")
    _add_sample(path, 2, 195.0)            # recorded BEFORE the hold
    pg._tick()
    pg.act_hold.setChecked(True)
    _add_sample(path, 2, 222.0)            # recorded DURING the hold
    # The held backfill fails, and so does the release tick's retry.
    _raise_series_after(pane._reader, 2)
    pg._on_channel_dropped("ect", None)

    assert pane.chart.traces["ect"].vs == []
    assert "ect" in table.incomplete()
    for col in (table.COL_MIN, table.COL_MAX, table.COL_AVG):
        assert table.text("ect", col) == table.NOT_LOADED
    assert pane.chart.caption == "held"

    pg.act_hold.setChecked(False)          # release tick: retry fails again
    assert pane.chart.traces["ect"].vs == [], "partial trace after failure"
    assert "ect" in table.incomplete()
    assert table.text("ect", table.COL_MIN) == table.NOT_LOADED
    assert pane.chart.caption == "read failed"

    pg._tick()                             # next good tick loads it all
    assert pane.chart.traces["ect"].vs == [195.0, 222.0]
    assert table.incomplete() == set()
    assert table.text("ect", table.COL_MIN) == "195"
    assert pane.chart.caption == "live"


def test_rr2_f3_session_names_in_tooltips_render_literally(env):
    from openobd.livedata import TileGroup, ValueTile
    from openobd.tuningui import plain_tooltip
    hostile = "<a href='x'>rpm</a>"
    escaped = "&lt;a href=&#x27;x&#x27;&gt;rpm&lt;/a&gt;"
    tip = ValueTile(hostile, "rpm").toolTip()
    assert tip.startswith("<qt>") and tip.endswith("</qt>")
    assert escaped in tip and hostile not in tip
    group = TileGroup(hostile)
    head = group.header.toolTip()
    assert head.startswith("<qt>") and escaped in head and hostile not in head
    assert plain_tooltip("a & b") == "<qt>a &amp; b</qt>"
