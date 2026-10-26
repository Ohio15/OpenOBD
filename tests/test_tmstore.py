"""
Tests for the truck-mcp store/journal readers — no hardware, no truck-mcp
package, no Qt. Fixtures build a *.tmsession.db with truck-mcp's schema and a
control-journal.jsonl with its records; the safety-critical rules (never render
a non-measurement as live, never report clean from absence of evidence, never
miss a second data root, never write the file) are asserted directly.
"""
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openobd import ctljournal, tmstore  # noqa: E402
from openobd.tmstore import (  # noqa: E402
    TmSessionReader, display_state, sample_age_s,
)


def _s(value_num=None, value=None, fresh=True, quality="ok", stale_s=0.0,
       ts_utc="2026-08-21T10:05:00Z"):
    d = {"fresh": fresh, "quality": quality, "stale_s": stale_s, "ts_utc": ts_utc}
    if value_num is not None:
        d["value_num"] = value_num
    if value is not None:
        d["value"] = value
    return d


# --- display_state: the five/seven-facts contract -------------------------- #
def test_notread_when_sample_missing():
    assert display_state(None, {"name": "rpm"}).state == "notread"


def test_fresh_carries_unit():
    ds = display_state(_s(value_num=1234), {"name": "rpm", "unit": "rpm", "precision": 0})
    assert ds.state == "fresh" and ds.text == "1234" and ds.unit == "rpm"


def test_carried_keeps_age():
    ds = display_state(_s(value_num=180, fresh=False, stale_s=3.5), {"name": "ect"})
    assert ds.state == "carried" and ds.age == 3.5


def test_error_is_not_a_value():
    ds = display_state(_s(quality="error"), {"name": "map"})
    assert ds.state == "error" and ds.text == "ERR"


def test_unavailable_distinct_from_notread():
    ds = display_state(_s(quality="unavailable"), {"name": "tft"})
    assert ds.state == "unavail" and ds.text == "N/A"


def test_ok_but_no_value_is_notread():
    assert display_state(_s(quality="ok"), {"name": "gear"}).state == "notread"


def test_empty_text_is_notread():
    assert display_state(_s(value=""), {"name": "vin"}).state == "notread"


def test_errored_sample_with_value_raises():
    try:
        display_state(_s(value_num=42, quality="error"), {"name": "rpm"})
    except ValueError:
        return
    assert False, "errored sample carrying a value must raise"


def test_unknown_quality_raises():
    try:
        display_state(_s(value_num=1, quality="timeout"), {"name": "rpm"})
    except ValueError:
        return
    assert False, "unknown quality must raise, not render as fine"


def test_archived_session_never_renders_fresh():
    ds = display_state(_s(value_num=1450), {"name": "rpm"}, archived=True)
    assert ds.state == "archive"   # a finished drive is not a live measurement


def test_stale_session_never_renders_fresh():
    ds = display_state(_s(value_num=1450), {"name": "rpm"}, session_stale=True)
    assert ds.state == "stale"


def test_non_finite_value_is_badvalue_not_error():
    ds = display_state(_s(value_num=float("inf")), {"name": "map"})
    assert ds.state == "badvalue"   # a tool fault, distinct from a module error
    ds2 = display_state(_s(value_num=float("nan")), {"name": "map"})
    assert ds2.state == "badvalue"


def test_sample_age_parses_z_suffix():
    from datetime import datetime, timezone
    now = datetime(2026, 8, 21, 10, 5, 10, tzinfo=timezone.utc)
    age = sample_age_s({"ts_utc": "2026-08-21T10:05:00Z"}, now=now)
    assert abs(age - 10.0) < 0.01


# --- fixture DB ------------------------------------------------------------ #
_SCHEMA = """
CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE session (id INTEGER PRIMARY KEY CHECK (id=1), session_uid TEXT,
  label TEXT, started_utc TEXT, ended_utc TEXT, vin TEXT, adapter_id TEXT,
  truck_mcp_version TEXT, engine_state TEXT DEFAULT 'unknown',
  notes TEXT NOT NULL DEFAULT '',
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


def _make_session(path, uid="uid-1", ended=None, notes="", sweep_config="{}",
                  source="live", extra="{}"):
    conn = sqlite3.connect(str(path))
    conn.executescript(_SCHEMA)
    conn.execute("INSERT INTO schema_meta VALUES ('schema_version','1')")
    conn.execute("INSERT INTO session (id, session_uid, label, started_utc, "
                 "ended_utc, vin, truck_mcp_version, source, notes, "
                 "sweep_config, extra) VALUES "
                 "(1, ?, 'drive', '2026-08-21T10:00:00Z', ?, "
                 "'3GCRKTE35AG150432', '0.11.0a7', ?, ?, ?, ?)",
                 (uid, ended, source, notes, sweep_config, extra))
    for cid, name, unit in [(1, "rpm", "rpm"), (2, "ect", "F"),
                            (3, "tft", "F"), (4, "gear", "")]:
        conn.execute("INSERT INTO channel (id, name, unit) VALUES (?,?,?)",
                     (cid, name, unit))
    rows = [
        (1, "2026-08-21T10:05:00Z", 900.0, None, 1, 0.0, "ok"),
        (2, "2026-08-21T10:04:57Z", 190.0, None, 0, 3.0, "ok"),
        (3, "2026-08-21T10:05:00Z", None, None, 1, 0.0, "error"),
    ]
    for cid, ts, num, text, fresh, stale, q in rows:
        conn.execute("INSERT INTO channel_latest (channel_id, sample_id, ts_utc, "
                     "value_num, value_text, fresh, stale_s, quality) "
                     "VALUES (?,?,?,?,?,?,?,?)", (cid, cid, ts, num, text, fresh, stale, q))
        conn.execute("INSERT INTO sample (ts_utc, channel_id, value_num, "
                     "value_text, fresh, stale_s, quality) VALUES (?,?,?,?,?,?,?)",
                     (ts, cid, num, text, fresh, stale, q))
    conn.execute("INSERT INTO event (ts_utc, kind, label, detail) VALUES "
                 "('2026-08-21T10:05:00Z', 'marker', 'wot', '{\"note\":\"pull\"}')")
    conn.commit()
    conn.close()


def test_reader_metadata_and_channels(tmp_path):
    db = tmp_path / "drive.tmsession.db"
    _make_session(db)
    with TmSessionReader(db) as r:
        assert r.metadata()["vin"] == "3GCRKTE35AG150432"
        assert {c["name"] for c in r.channels()} == {"rpm", "ect", "tft", "gear"}
        assert r.channel_count() == 4
        assert r.schema_version() == 1


def test_reader_latest_maps_to_display_states(tmp_path):
    db = tmp_path / "drive.tmsession.db"
    _make_session(db)
    with TmSessionReader(db) as r:
        latest = r.latest(["rpm", "ect", "tft", "gear"])
        assert display_state(latest.get("rpm"), {"name": "rpm"}).state == "fresh"
        assert display_state(latest.get("ect"), {"name": "ect"}).state == "carried"
        assert display_state(latest.get("tft"), {"name": "tft"}).state == "error"
        assert display_state(latest.get("gear"), {"name": "gear"}).state == "notread"


def test_reader_cannot_write_query_only_and_authorizer(tmp_path):
    db = tmp_path / "drive.tmsession.db"
    _make_session(db)
    with TmSessionReader(db) as r:
        for stmt in ("INSERT INTO channel (name) VALUES ('hack')",
                     "PRAGMA journal_mode=DELETE",
                     "UPDATE session SET vin='x' WHERE id=1"):
            try:
                r._conn.execute(stmt)
            except sqlite3.Error:
                continue
            assert False, f"reader must reject: {stmt}"


def test_connect_does_not_mutate_a_non_wal_file(tmp_path):
    # C2 regression: opening the reader must not rewrite the DB header (e.g.
    # flip a DELETE-mode DB to WAL). Bytes must be identical after a read.
    db = tmp_path / "drive.tmsession.db"
    _make_session(db)
    before = db.read_bytes()
    with TmSessionReader(db) as r:
        r.latest(["rpm"])
    assert db.read_bytes() == before, "reader mutated the session file"


def test_latest_ts(tmp_path):
    db = tmp_path / "drive.tmsession.db"
    _make_session(db)
    with TmSessionReader(db) as r:
        assert r.latest_ts() == "2026-08-21T10:05:00Z"


def test_series_after_defaults_are_chart_shaped(tmp_path):
    # Only fresh (measured-now) numeric samples: the carried ect (fresh=0) and
    # the errored tft (value_num NULL) must not come back as plottable points.
    db = tmp_path / "drive.tmsession.db"
    _make_session(db)
    with TmSessionReader(db) as r:
        out = r.series_after(["rpm", "ect", "tft", "gear"])
        assert [s["value_num"] for s in out["rpm"]] == [900.0]
        assert out["ect"] == [] and out["tft"] == [] and out["gear"] == []
        assert out["rpm"][0]["id"] is not None


def test_series_after_cursor_advances(tmp_path):
    db = tmp_path / "drive.tmsession.db"
    _make_session(db)
    with TmSessionReader(db) as r:
        first = r.series_after(["rpm"])
        cursor = first["rpm"][-1]["id"]
        assert r.series_after(["rpm"], after_id=cursor) == {"rpm": []}


def test_series_after_relaxed_filters_and_min_ts(tmp_path):
    db = tmp_path / "drive.tmsession.db"
    _make_session(db)
    with TmSessionReader(db) as r:
        out = r.series_after(["rpm", "ect"], fresh_only=False)
        assert [s["value_num"] for s in out["ect"]] == [190.0]
        # min_ts bounds the backfill: ect's sample (10:04:57) is older
        out = r.series_after(["rpm", "ect"], fresh_only=False,
                             min_ts="2026-08-21T10:05:00Z")
        assert out["ect"] == [] and len(out["rpm"]) == 1


def test_reader_ended_reflects_ended_utc(tmp_path):
    # NEW-3 building block: a live drive reads not-ended; a finished one ended.
    live = tmp_path / "live.tmsession.db"
    _make_session(live, uid="live", ended=None)
    done = tmp_path / "done.tmsession.db"
    _make_session(done, uid="done", ended="2026-08-21T11:00:00Z")
    with TmSessionReader(live) as r:
        assert r.ended() is False
    with TmSessionReader(done) as r:
        assert r.ended() is True


def test_list_sessions_newest_first_and_reports_junk(tmp_path):
    _make_session(tmp_path / "a.tmsession.db")
    (tmp_path / "broken.tmsession.db").write_bytes(b"not a database")
    sessions = tmstore.list_sessions(tmp_path)
    good = [s for s in sessions if not s["error"]]
    bad = [s for s in sessions if s["error"]]
    assert len(good) == 1 and good[0]["vin"] == "3GCRKTE35AG150432"
    assert len(bad) == 1


# --- simulated provenance --------------------------------------------------- #
# Exactly what truck-mcp scripts/seed_demo_data.py writes into each session.
_SEED_NOTE = "SIMULATED demonstration data written by scripts/seed_demo_data.py"
_SEED_SWEEP = json.dumps({"preset": "towing", "started_by": "seed_demo_data"})


def test_list_sessions_flags_seeded_session_simulated(tmp_path):
    _make_session(tmp_path / "real.tmsession.db", uid="real")
    _make_session(tmp_path / "seed.tmsession.db", uid="seed",
                  ended="2026-08-21T10:30:00Z", notes=_SEED_NOTE,
                  sweep_config=_SEED_SWEEP)
    by_uid = {s["session_uid"]: s for s in tmstore.list_sessions(tmp_path)}
    assert by_uid["seed"]["simulated"] is True
    assert by_uid["real"]["simulated"] is False
    # simulated is provenance, independent of live/archived
    assert by_uid["seed"]["live"] is False and by_uid["real"]["live"] is True


def test_simulated_from_sweep_config_alone():
    assert tmstore.session_simulated(
        {"sweep_config": {"started_by": "seed_demo_data"}, "notes": ""})


def test_simulated_from_notes_alone_survives_corrupt_sweep_config():
    meta = {"sweep_config": {"_decode_error": True, "_raw": "{bad"},
            "notes": _SEED_NOTE}
    assert tmstore.session_simulated(meta)


def test_simulated_from_source_alone():
    # truck-mcp v0.11.0a9: session.source is authoritative; a simulator drive
    # carries neither legacy marker.
    meta = {"source": "simulated", "sweep_config": {"preset": "towing"},
            "extra": {}, "notes": "a drive"}
    assert tmstore.session_provenance(meta) == "simulated"
    assert tmstore.session_simulated(meta)


def test_simulated_from_extra_flag_alone():
    meta = {"source": "live", "extra": {"simulated": True}, "sweep_config": {}}
    assert tmstore.session_provenance(meta) == "simulated"
    # only the boolean True counts
    meta = {"source": "live", "extra": {"simulated": "yes"}, "sweep_config": {}}
    assert tmstore.session_provenance(meta) == "real"


def test_simulated_source_beats_corrupt_columns():
    bad = {"_decode_error": True, "_raw": "{bad"}
    assert tmstore.session_provenance(
        {"source": "simulated", "sweep_config": bad, "extra": bad}) == "simulated"


def test_real_sources_are_real():
    for src in ("live", "import", "replay", None, ""):
        assert tmstore.session_provenance(
            {"source": src, "sweep_config": {}, "extra": {}}) == "real", src


def test_unknown_when_sweep_config_or_extra_unreadable():
    bad = {"_decode_error": True, "_raw": "{bad"}
    assert tmstore.session_provenance(
        {"source": "live", "sweep_config": bad, "notes": ""}) == "unknown"
    assert tmstore.session_provenance(
        {"source": "live", "sweep_config": {}, "extra": bad}) == "unknown"
    assert not tmstore.session_simulated(
        {"source": "live", "sweep_config": bad})


def test_unknown_when_source_value_unrecognised():
    for src in ("martian", 7, ["live"]):
        assert tmstore.session_provenance(
            {"source": src, "sweep_config": {}, "extra": {}}) == "unknown", src


def test_list_sessions_carries_three_valued_provenance(tmp_path):
    _make_session(tmp_path / "a.tmsession.db", uid="real")
    _make_session(tmp_path / "b.tmsession.db", uid="sim", source="simulated",
                  ended="2026-08-21T10:30:00Z")
    _make_session(tmp_path / "c.tmsession.db", uid="flag",
                  extra='{"simulated": true}', ended="2026-08-21T10:30:00Z")
    _make_session(tmp_path / "d.tmsession.db", uid="corrupt",
                  sweep_config="{bad", ended="2026-08-21T10:30:00Z")
    _make_session(tmp_path / "e.tmsession.db", uid="oddsrc", source="martian",
                  ended="2026-08-21T10:30:00Z")
    by = {s["session_uid"]: s["provenance"]
          for s in tmstore.list_sessions(tmp_path)}
    assert by == {"real": "real", "sim": "simulated", "flag": "simulated",
                  "corrupt": "unknown", "oddsrc": "unknown"}


def test_session_tag_marks_unknown_provenance():
    assert tmstore.session_tag({"live": False, "provenance": "unknown"}) == \
        "[PROVENANCE UNKNOWN]  "
    assert tmstore.session_tag({"live": True, "provenance": "unknown"}) == \
        "● LIVE  [PROVENANCE UNKNOWN]  "
    assert tmstore.session_tag({"live": False, "provenance": "simulated"}) == \
        "[SIMULATED]  "
    assert tmstore.session_tag({"live": False, "provenance": "real"}) == ""


def test_not_simulated_without_a_marker():
    assert not tmstore.session_simulated({})
    assert not tmstore.session_simulated(
        {"sweep_config": {"preset": "towing", "started_by": "session_drive_log"},
         "notes": "towing up the grade, SIMULATED nothing"})
    assert not tmstore.session_simulated({"sweep_config": None, "notes": None})


def test_session_tag_shows_live_and_simulated():
    assert tmstore.session_tag({"live": True}) == "● LIVE  "
    assert tmstore.session_tag({"live": False, "simulated": True}) == "[SIMULATED]  "
    assert tmstore.session_tag({"live": True, "simulated": True}) ==         "● LIVE  [SIMULATED]  "
    assert tmstore.session_tag({}) == ""


def test_session_tag_short_form_for_the_dashboard_menu():
    assert tmstore.session_tag({"simulated": True}, short=True) == "[SIM] "
    assert tmstore.session_tag({"provenance": "simulated", "live": True},
                               short=True) == "● LIVE [SIM] "
    # unknown provenance is never abbreviated, real stays untagged
    assert (tmstore.session_tag({"provenance": "unknown"}, short=True)
            == "[PROVENANCE UNKNOWN] ")
    assert tmstore.session_tag({"provenance": "real"}, short=True) == ""


def test_candidate_roots_env_is_authoritative(tmp_path, monkeypatch):
    monkeypatch.setenv("TRUCK_MCP_DATA", str(tmp_path))
    roots = tmstore.candidate_roots()
    assert roots == [tmp_path.resolve()]   # single, resolved, authoritative


# --- control journal ------------------------------------------------------- #
def _write_journal(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n",
                    encoding="utf-8")


def _isolate_roots(monkeypatch, tmp_path):
    """Point every non-env truck-mcp root at empty/absent dirs so a control-state
    test reads ONLY the trees it explicitly sets up — the safety verdict
    deliberately unions real machine roots, which would otherwise leak in."""
    monkeypatch.setattr(tmstore, "DEV_CHECKOUT", tmp_path / "no-dev-checkout")
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    home = tmp_path / "no-home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))


def test_outstanding_tracks_unreleased(tmp_path):
    j = tmp_path / "control-journal.jsonl"
    _write_journal(j, [
        {"at": 1, "event": "activate", "module": "7E0", "cpid": "12", "release": "AE00"},
        {"at": 2, "event": "activate", "module": "7E0", "cpid": "34", "release": "AE00"},
        {"at": 3, "event": "released", "module": "7E0", "cpid": "12"},
    ])
    out = ctljournal.outstanding(j)
    assert len(out) == 1 and out[0]["cpid"] == "34"


def test_outstanding_empty_when_all_released(tmp_path):
    j = tmp_path / "control-journal.jsonl"
    _write_journal(j, [
        {"at": 1, "event": "activate", "module": "7E0", "cpid": "12"},
        {"at": 2, "event": "return_to_normal", "module": "7E0", "cpid": "12"},
    ])
    assert ctljournal.outstanding(j) == []


def test_control_state_clean(tmp_path, monkeypatch):
    _isolate_roots(monkeypatch, tmp_path)
    monkeypatch.setenv("TRUCK_MCP_DATA", str(tmp_path))
    _write_journal(tmp_path / "control" / "control-journal.jsonl", [
        {"at": 1, "event": "activate", "module": "7E0", "cpid": "12"},
        {"at": 2, "event": "released", "module": "7E0", "cpid": "12"},
    ])
    st = ctljournal.control_state()
    assert st.verdict == "clean"


def test_control_state_outstanding(tmp_path, monkeypatch):
    _isolate_roots(monkeypatch, tmp_path)
    monkeypatch.setenv("TRUCK_MCP_DATA", str(tmp_path))
    _write_journal(tmp_path / "control" / "control-journal.jsonl", [
        {"at": 1, "event": "activate", "module": "7E0", "cpid": "12", "release": "AE00"},
    ])
    st = ctljournal.control_state()
    assert st.verdict == "outstanding" and len(st.outstanding) == 1


def test_control_state_unknown_when_no_journal(tmp_path, monkeypatch):
    # absence of evidence is NOT a clean bill of health
    _isolate_roots(monkeypatch, tmp_path)
    monkeypatch.setenv("TRUCK_MCP_DATA", str(tmp_path))
    st = ctljournal.control_state()
    assert st.verdict == "unknown"


def test_control_state_unknown_when_damaged(tmp_path, monkeypatch):
    # An otherwise-clean journal with a damaged line is UNKNOWN, not clean — a
    # lost record could have hidden an activation.
    _isolate_roots(monkeypatch, tmp_path)
    monkeypatch.setenv("TRUCK_MCP_DATA", str(tmp_path))
    j = tmp_path / "control" / "control-journal.jsonl"
    j.parent.mkdir(parents=True)
    j.write_text('{"at":1,"event":"activate","module":"7E0","cpid":"12"}\n'
                 '{"at":2,"event":"released","module":"7E0","cpid":"12"}\n'
                 '{"at":3,"event":"acti', encoding="utf-8")   # torn line
    st = ctljournal.control_state()
    assert st.verdict == "unknown" and "damaged" in st.detail.lower()


def test_control_state_outstanding_beats_damaged(tmp_path, monkeypatch):
    # A confirmed outstanding activation is louder than 'unknown' and must win.
    monkeypatch.setenv("TRUCK_MCP_DATA", str(tmp_path))
    j = tmp_path / "control" / "control-journal.jsonl"
    j.parent.mkdir(parents=True)
    j.write_text('{"at":1,"event":"activate","module":"7E0","cpid":"12"}\n'
                 '{"at":2,"event":"acti', encoding="utf-8")
    assert ctljournal.control_state().verdict == "outstanding"


def test_control_state_non_object_line_is_unknown(tmp_path, monkeypatch):
    _isolate_roots(monkeypatch, tmp_path)
    monkeypatch.setenv("TRUCK_MCP_DATA", str(tmp_path))
    j = tmp_path / "control" / "control-journal.jsonl"
    j.parent.mkdir(parents=True)
    j.write_text('5\n{"at":1,"event":"activate","module":"7E0","cpid":"12"}\n'
                 '{"at":2,"event":"released","module":"7E0","cpid":"12"}\n',
                 encoding="utf-8")
    st = ctljournal.control_state()
    assert st.verdict == "unknown"   # a bare number is a damaged record


def test_entries_missing_file_is_empty(tmp_path):
    assert ctljournal.entries(tmp_path / "nope.jsonl") == []


def test_control_state_orders_across_journals_by_timestamp(tmp_path, monkeypatch):
    # NEW-1 regression: a chronologically OLD released in one journal must not
    # pop a NEWER activate in another just because of file/root order.
    _isolate_roots(monkeypatch, tmp_path)
    monkeypatch.setenv("TRUCK_MCP_DATA", str(tmp_path))
    # override root: newer activate, unreleased
    _write_journal(tmp_path / "control" / "control-journal.jsonl", [
        {"at": 100, "event": "activate", "module": "A", "cpid": "B", "release": "AE00"},
    ])
    # a different root read later, with an OLD already-resolved release
    fk = tmp_path / "fieldkit"
    monkeypatch.setenv("LOCALAPPDATA", str(fk))
    (fk / "Programs" / "truck-mcp-app").mkdir(parents=True)
    _write_journal(fk / "Programs" / "truck-mcp-app" / "control" / "control-journal.jsonl", [
        {"at": 50, "event": "released", "module": "A", "cpid": "B"},
    ])
    st = ctljournal.control_state()
    # global time order: released@50 then activate@100 -> still outstanding
    assert st.verdict == "outstanding", st.detail


def test_control_verdict_unions_field_kit_even_under_override(tmp_path, monkeypatch):
    # NEW-2 regression: an override must not blind the SAFETY verdict to the
    # field kit where the actuating build writes.
    _isolate_roots(monkeypatch, tmp_path)
    override = tmp_path / "override"
    override.mkdir()
    _write_journal(override / "control" / "control-journal.jsonl", [
        {"at": 1, "event": "activate", "module": "A", "cpid": "B"},
        {"at": 2, "event": "released", "module": "A", "cpid": "B"},
    ])  # override tree is clean
    fk = tmp_path / "local"
    (fk / "Programs" / "truck-mcp-app").mkdir(parents=True)
    _write_journal(fk / "Programs" / "truck-mcp-app" / "control" / "control-journal.jsonl", [
        {"at": 1, "event": "activate", "module": "C", "cpid": "D", "release": "AE00"},
    ])  # field kit has an outstanding activation
    monkeypatch.setenv("TRUCK_MCP_DATA", str(override))
    monkeypatch.setenv("LOCALAPPDATA", str(fk))
    st = ctljournal.control_state()
    assert st.verdict == "outstanding", st.detail   # field kit was NOT missed


def test_control_state_unorderable_entry_is_unknown(tmp_path, monkeypatch):
    # An otherwise-clean journal with a record lacking a usable `at` is UNKNOWN.
    _isolate_roots(monkeypatch, tmp_path)
    monkeypatch.setenv("TRUCK_MCP_DATA", str(tmp_path))
    _write_journal(tmp_path / "control" / "control-journal.jsonl", [
        {"at": 1, "event": "activate", "module": "A", "cpid": "B"},
        {"at": 2, "event": "released", "module": "A", "cpid": "B"},
        {"event": "note", "module": "A"},   # no `at`
    ])
    assert ctljournal.control_state().verdict == "unknown"


def test_bad_precision_does_not_mislabel_reading(tmp_path):
    # NEW-6: a bad precision in channel meta is a metadata fault, not a bad value.
    ds = display_state(_s(value_num=42), {"name": "rpm", "precision": "oops"})
    assert ds.state == "fresh" and ds.text == "42"
    ds2 = display_state(_s(value_num=42), {"name": "rpm", "precision": -3})
    assert ds2.state == "fresh" and ds2.text == "42"
