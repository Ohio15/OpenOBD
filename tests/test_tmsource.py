"""tmsource — mapping, unit conversion, replay-log synthesis, live follow.

The invariants under test are the honesty contracts:
  * a channel reaches a gauge ONLY with a known name AND a convertible declared
    unit — an unknown or empty unit is excluded with a reason, never guessed;
  * conversion actually converts (°C→°F, km/h→mph) — a raw metric number on a
    US-unit gauge is the tool lying about the truck;
  * the live source classifies every value through tmstore.display_state and
    flips to an explicit failed state when the read itself fails.
"""
import sqlite3

import pytest

from openobd import tmsource, tmstore
from openobd.tmsource import (
    TmLiveSource, build_replay_log, resolve_channels,
)

# Same schema truck-mcp writes (mirrored from test_tmstore fixtures).
_SCHEMA = """
CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE session (id INTEGER PRIMARY KEY CHECK (id=1), session_uid TEXT,
  label TEXT, started_utc TEXT, ended_utc TEXT, vin TEXT, adapter_id TEXT,
  truck_mcp_version TEXT, engine_state TEXT DEFAULT 'unknown',
  sweep_config TEXT DEFAULT '{}', source TEXT DEFAULT 'live', extra TEXT DEFAULT '{}');
CREATE TABLE channel (id INTEGER PRIMARY KEY, name TEXT UNIQUE, unit TEXT DEFAULT '',
  kind TEXT DEFAULT 'numeric', source TEXT DEFAULT '', pid TEXT, did TEXT,
  module TEXT, confidence TEXT, meta TEXT DEFAULT '{}');
CREATE TABLE sample (id INTEGER PRIMARY KEY, ts_utc TEXT, elapsed_s REAL, cycle INTEGER,
  channel_id INTEGER, value_num REAL, value_text TEXT, fresh INTEGER DEFAULT 1,
  stale_s REAL DEFAULT 0.0, quality TEXT DEFAULT 'ok');
CREATE TABLE channel_latest (channel_id INTEGER PRIMARY KEY, sample_id INTEGER,
  ts_utc TEXT, elapsed_s REAL, value_num REAL, value_text TEXT, fresh INTEGER DEFAULT 1,
  stale_s REAL DEFAULT 0.0, quality TEXT DEFAULT 'ok');
CREATE TABLE event (id INTEGER PRIMARY KEY, ts_utc TEXT, kind TEXT, label TEXT DEFAULT '',
  detail TEXT DEFAULT '{}');
"""

# The real channel set of a 2026-08 truck-mcp drive session, real units.
_CHANNELS = [
    (1, "rpm", "rpm", "numeric"),
    (2, "vehicle_speed", "km/h", "numeric"),
    (3, "coolant_temp", "°C", "numeric"),
    (4, "intake_air_temp", "°C", "numeric"),
    (5, "throttle_pos", "%", "numeric"),
    (6, "control_voltage", "V", "numeric"),
    (7, "engine_load", "%", "numeric"),
    (8, "trans_fluid_temp", "", "numeric"),          # unverified DID — no unit
    (9, "trans_fluid_temp_status", "", "text"),      # sidecar
]


def _ts(sec: int) -> str:
    return f"2026-08-21T10:00:{sec:02d}Z"


def _make_session(path, ended=None, latest=None, samples=None):
    conn = sqlite3.connect(str(path))
    conn.executescript(_SCHEMA)
    conn.execute("INSERT INTO schema_meta VALUES ('schema_version','1')")
    conn.execute(
        "INSERT INTO session (id, session_uid, label, started_utc, ended_utc, "
        "vin, source) VALUES (1, 'uid-1', 'drive', ?, ?, "
        "'3GCRKTE35AG150432', 'live')", (_ts(0), ended))
    for cid, name, unit, kind in _CHANNELS:
        conn.execute("INSERT INTO channel (id, name, unit, kind) VALUES (?,?,?,?)",
                     (cid, name, unit, kind))
    for cid, ts, num, fresh, stale, q in (latest or []):
        conn.execute(
            "INSERT INTO channel_latest (channel_id, sample_id, ts_utc, "
            "value_num, fresh, stale_s, quality) VALUES (?,?,?,?,?,?,?)",
            (cid, cid, ts, num, fresh, stale, q))
    for cid, ts, num, fresh in (samples or []):
        conn.execute(
            "INSERT INTO sample (ts_utc, channel_id, value_num, fresh) "
            "VALUES (?,?,?,?)", (ts, cid, num, fresh))
    conn.commit()
    conn.close()


def _channel_dicts():
    return [{"name": n, "unit": u, "kind": k} for _i, n, u, k in _CHANNELS]


# --- mapping + conversion --------------------------------------------------- #
def test_conversions_are_real():
    assert tmsource._c_to_f(0.0) == 32.0
    assert tmsource._c_to_f(100.0) == 212.0
    assert abs(tmsource._kmh_to_mph(100.0) - 62.1371) < 1e-4


def test_resolve_maps_only_unit_verified_channels():
    r = resolve_channels(_channel_dicts())
    assert set(r.feed) == {"rpm", "vehicle_speed", "coolant_temp",
                           "intake_air_temp", "throttle_pos",
                           "control_voltage", "engine_load"}
    canon = {r.feed[n][0] for n in r.feed}
    assert canon == {"rpm", "vss", "ect", "iat", "tps", "voltage", "load"}


def test_unitless_trans_fluid_temp_is_excluded_with_reason():
    r = resolve_channels(_channel_dicts())
    excluded = dict(r.excluded)
    assert "trans_fluid_temp" in excluded
    assert "no unit" in excluded["trans_fluid_temp"]
    # the text sidecar never even reaches the exclusion list check for units
    assert "trans_fluid_temp_status" not in r.feed


def test_unknown_unit_is_excluded_not_guessed():
    r = resolve_channels([{"name": "coolant_temp", "unit": "K",
                           "kind": "numeric"}])
    assert not r.feed
    assert r.excluded and "not convertible" in r.excluded[0][1]


def test_unit_normalization_variants():
    for u in ("°C", "C", "degC", " c "):
        r = resolve_channels([{"name": "coolant_temp", "unit": u,
                               "kind": "numeric"}])
        assert "coolant_temp" in r.feed, u


# --- replay-log synthesis --------------------------------------------------- #
def test_build_replay_log_converts_and_forward_fills(tmp_path):
    db = tmp_path / "d.tmsession.db"
    _make_session(db, ended=_ts(30), samples=[
        (1, _ts(0), 1000.0, 1),          # rpm
        (3, _ts(0), 90.0, 1),            # coolant 90°C -> 194°F
        (1, _ts(1), 1500.0, 1),          # rpm advances; coolant forward-fills
        (2, _ts(2), 100.0, 1),           # speed 100 km/h -> 62.1371 mph
        (1, _ts(3), 2000.0, 0),          # NOT fresh — must be ignored
    ])
    log, resolved = build_replay_log(db)
    assert log.n_samples == 3            # ts 0,1,2 — the carried row adds none
    t = log.series("time")
    assert t == [0.0, 1.0, 2.0]
    assert log.series("rpm") == [1000.0, 1500.0, 1500.0]
    ect = log.series("ect")
    assert ect[0] == pytest.approx(194.0)
    assert ect[1] == pytest.approx(194.0)      # forward-filled
    vss = log.series("vss")
    assert vss[0] is None and vss[1] is None   # None before first sample
    assert vss[2] == pytest.approx(62.1371, abs=1e-3)
    assert dict(resolved.excluded).get("trans_fluid_temp")


def test_build_replay_log_refuses_empty(tmp_path):
    db = tmp_path / "e.tmsession.db"
    _make_session(db, ended=_ts(30))
    with pytest.raises(tmstore.TmSessionError):
        build_replay_log(db)


# --- live source ------------------------------------------------------------ #
def _fresh_latest(now_iso):
    return [
        (1, now_iso, 1100.0, 1, 0.0, "ok"),
        (2, now_iso, 40.0, 1, 0.0, "ok"),
        (3, now_iso, 94.0, 0, 3.0, "ok"),     # carried
        (6, now_iso, 14.3, 1, 0.0, "ok"),
    ]


def _now_iso():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_live_source_converts_and_states(tmp_path):
    db = tmp_path / "l.tmsession.db"
    now = _now_iso()
    _make_session(db, latest=_fresh_latest(now))
    src = TmLiveSource(db)
    try:
        assert set(src.channels()) == {"rpm", "vss", "ect", "iat", "tps",
                                       "voltage", "load"}
        src.start()
        s = src.latest()
        assert s is not None
        assert s.values["rpm"] == 1100.0
        assert s.values["vss"] == pytest.approx(24.85, abs=0.01)   # 40 km/h
        assert s.values["ect"] == pytest.approx(201.2, abs=0.01)   # 94°C carried
        states = src.channel_states()
        assert states["rpm"] == "fresh"
        assert states["ect"] == "carried"     # carried must not claim fresh
        assert states["iat"] == "notread"     # no channel_latest row
    finally:
        src.stop()


def test_live_source_archived_never_fresh(tmp_path):
    db = tmp_path / "a.tmsession.db"
    now = _now_iso()
    _make_session(db, ended=_ts(30), latest=_fresh_latest(now))
    src = TmLiveSource(db)
    try:
        src.start()
        states = src.channel_states()
        assert states["rpm"] == "archive"
    finally:
        src.stop()


def test_live_source_stale_session_never_fresh(tmp_path):
    db = tmp_path / "s.tmsession.db"
    _make_session(db, latest=_fresh_latest("2026-08-21T10:00:05Z"))  # old
    src = TmLiveSource(db)
    try:
        src.start()
        assert src.channel_states()["rpm"] == "stale"
    finally:
        src.stop()


def test_live_source_read_failure_flips_to_failed(tmp_path):
    db = tmp_path / "f.tmsession.db"
    _make_session(db, latest=_fresh_latest(_now_iso()))
    src = TmLiveSource(db)
    src.start()
    src._reader.close()                       # simulate the file going away
    src._read()                               # failure 1
    src._read()                               # failure 2 -> failed
    assert src.latest() is None
    assert set(src.channel_states().values()) == {"failed"}
    src.stop()


def test_live_source_refuses_unmappable_session(tmp_path):
    db = tmp_path / "u.tmsession.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(_SCHEMA)
    conn.execute("INSERT INTO schema_meta VALUES ('schema_version','1')")
    conn.execute("INSERT INTO session (id, session_uid, label, started_utc) "
                 "VALUES (1, 'u', 'x', ?)", (_ts(0),))
    conn.execute("INSERT INTO channel (id, name, unit) VALUES (1, 'mystery', '')")
    conn.commit(); conn.close()
    with pytest.raises(tmstore.TmSessionError):
        TmLiveSource(db)


def test_live_source_single_failure_demotes_fresh_immediately(tmp_path):
    db = tmp_path / "f1.tmsession.db"
    _make_session(db, latest=_fresh_latest(_now_iso()))
    src = TmLiveSource(db)
    src.start()
    assert src.channel_states()["rpm"] == "fresh"
    src._reader.close()
    src._read()                               # ONE failed read
    states = src.channel_states()
    assert states["rpm"] == "stale"           # fresh may not survive a failure
    assert states["ect"] == "carried"         # already-honest states unchanged
    src.stop()


def test_live_source_stop_start_reopens_reader(tmp_path):
    db = tmp_path / "r.tmsession.db"
    _make_session(db, latest=_fresh_latest(_now_iso()))
    src = TmLiveSource(db)
    src.start()
    src.stop()
    src.start()                               # must reopen, not serve a corpse
    s = src.latest()
    assert s is not None and s.values["rpm"] == 1100.0
    assert src.channel_states()["rpm"] == "fresh"
    assert src._fail_streak == 0
    src.stop()


def test_replay_error_quality_breaks_forward_fill(tmp_path):
    db = tmp_path / "err.tmsession.db"
    conn_rows = [
        (3, _ts(0), 90.0, 1),                 # coolant ok
        (1, _ts(0), 800.0, 1),
        (1, _ts(1), 900.0, 1),
        (1, _ts(2), 1000.0, 1),
    ]
    _make_session(db, ended=_ts(30), samples=conn_rows)
    # inject a module-fault row for coolant at t=1 (fresh, quality=error, NULL)
    conn = sqlite3.connect(str(db))
    conn.execute("INSERT INTO sample (ts_utc, channel_id, value_num, fresh, "
                 "quality) VALUES (?,?,NULL,1,'error')", (_ts(1), 3))
    conn.commit(); conn.close()
    log, _r = build_replay_log(db)
    ect = log.series("ect")
    assert ect[0] == pytest.approx(194.0)
    assert ect[1] is None                     # fault window replays as "—",
    assert ect[2] is None                     # never the last good value


def test_replay_source_reports_archive_states(tmp_path):
    db = tmp_path / "as.tmsession.db"
    _make_session(db, ended=_ts(30), samples=[(1, _ts(0), 1000.0, 1)])
    log, _r = build_replay_log(db)
    src = tmsource.TmReplaySource(log)
    assert set(src.channel_states().values()) == {"archive"}
    assert hasattr(src, "seek")               # transport bar still works


def test_replay_refuses_oversized_session(tmp_path, monkeypatch):
    db = tmp_path / "big.tmsession.db"
    _make_session(db, ended=_ts(30), samples=[(1, _ts(i % 60), 1000.0, 1)
                                              for i in range(10)])
    monkeypatch.setattr(tmsource, "MAX_REPLAY_SAMPLES", 5)
    with pytest.raises(tmstore.TmSessionError, match="too large"):
        build_replay_log(db)


def test_unverified_confidence_is_excluded_even_with_unit():
    r = resolve_channels([{"name": "trans_fluid_temp", "unit": "°C",
                           "kind": "numeric", "confidence": "candidate"}])
    assert not r.feed
    assert "not verified" in r.excluded[0][1]
    # verified (or absent) confidence passes the gate
    r2 = resolve_channels([{"name": "trans_fluid_temp", "unit": "°C",
                            "kind": "numeric", "confidence": "verified"}])
    assert "trans_fluid_temp" in r2.feed


def test_replay_drops_never_sampled_channels(tmp_path):
    db = tmp_path / "ns.tmsession.db"
    # 7 mapped channels declared, samples for rpm only
    _make_session(db, ended=_ts(30), samples=[(1, _ts(0), 800.0, 1)])
    log, resolved = build_replay_log(db)
    assert log.canonical_keys() == ["time", "rpm"]
    reasons = dict(resolved.excluded)
    assert reasons.get("vehicle_speed") == "no samples in this drive"
    assert "vehicle_speed" not in resolved.feed


def test_replay_cap_counts_only_gauge_rows(tmp_path):
    db = tmp_path / "cap.tmsession.db"
    # 5 usable rpm rows + a mountain of unmapped text-sidecar rows
    _make_session(db, ended=_ts(30),
                  samples=[(1, _ts(i), 800.0 + i, 1) for i in range(5)])
    conn = sqlite3.connect(str(db))
    conn.executemany(
        "INSERT INTO sample (ts_utc, channel_id, value_num, fresh) "
        "VALUES (?,?,?,1)", [(_ts(i % 60), 9, None) for i in range(50)])
    conn.commit(); conn.close()
    import openobd.tmsource as m
    old = m.MAX_REPLAY_SAMPLES
    try:
        m.MAX_REPLAY_SAMPLES = 10       # > 5 gauge rows, < 55 total rows
        log, _r = build_replay_log(db)  # must NOT be refused
        assert log.n_samples == 5
    finally:
        m.MAX_REPLAY_SAMPLES = old


def test_replay_wraps_sqlite_errors(tmp_path):
    db = tmp_path / "broken.tmsession.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(_SCHEMA)
    conn.execute("INSERT INTO session (id, session_uid, label, started_utc) "
                 "VALUES (1,'b','x',?)", (_ts(0),))
    for cid, name, unit, kind in _CHANNELS:
        conn.execute("INSERT INTO channel (id, name, unit, kind) "
                     "VALUES (?,?,?,?)", (cid, name, unit, kind))
    conn.execute("DROP TABLE sample")
    conn.commit(); conn.close()
    with pytest.raises(tmstore.TmSessionError, match="store read failed"):
        build_replay_log(db)


def test_live_source_start_failure_reports_all_stale(tmp_path, monkeypatch):
    db = tmp_path / "sf.tmsession.db"
    _make_session(db, latest=_fresh_latest(_now_iso()))
    src = TmLiveSource(db)
    # First-ever read fails: states must be non-empty and non-live.
    monkeypatch.setattr(src._reader, "latest",
                        lambda *_a, **_k: (_ for _ in ()).throw(
                            sqlite3.OperationalError("locked")))
    src.start()
    states = src.channel_states()
    assert states and set(states.values()) == {"stale"}
    src.stop()


def test_live_source_drain_dedups_by_store_timestamp(tmp_path):
    db = tmp_path / "q.tmsession.db"
    _make_session(db, latest=_fresh_latest(_now_iso()))
    src = TmLiveSource(db)
    src.start()
    src.drain()
    src._read()                               # same newest_ts — no new sample
    assert src.drain() == []
    src.stop()


# --- Record refusal: names what is bound and how to record the truck --------- #
_CONNECT = "🔌 Connect GT Pro"


def test_refusal_archived_real_session_points_at_gt_not_export():
    msg = tmsource.record_refusal_message(False, False, _CONNECT)
    assert "archived" in msg and "not the truck" in msg
    assert f'press "{_CONNECT}", then Record' in msg
    assert "session_export" not in msg and "SIMULATED" not in msg


def test_refusal_archived_simulated_session_says_simulated():
    msg = tmsource.record_refusal_message(False, True, _CONNECT)
    assert "archived SIMULATED" in msg and "demo data" in msg
    assert f'press "{_CONNECT}", then Record' in msg
    assert "session_export" not in msg


def test_refusal_live_session_keeps_export_hint_and_gt_path():
    msg = tmsource.record_refusal_message(True, False, _CONNECT)
    assert "live truck-mcp session" in msg and "session_export" in msg
    assert f'press "{_CONNECT}", then Record' in msg
    assert "SIMULATED" not in msg and "archived" not in msg


def test_refusal_live_simulated_session_flags_both():
    msg = tmsource.record_refusal_message(True, True, _CONNECT)
    assert "live SIMULATED truck-mcp session" in msg and "session_export" in msg


def test_refusal_names_the_button_it_is_given():
    msg = tmsource.record_refusal_message(False, False, "Connect")
    assert 'press "Connect", then Record' in msg
