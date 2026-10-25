"""
tmstore — read truck-mcp drive-log sessions without touching the vehicle.

truck-mcp records every drive as one SQLite file (``*.tmsession.db``) in WAL
mode: "the database IS the channel", chosen so a second GUI can read a drive
while the logger is still writing it, and so a finished drive reads identically
to a live one. OpenOBD is that second reader. This module opens the same files
truck-mcp writes — no socket, no IPC, no shared serial port — and never holds
the adapter, so it cannot contend with a running logger for COM3.

Three contracts are copied deliberately from truck-mcp; getting any of them
subtly wrong would make the tool lie about the truck:

  * The connection is opened READ-ONLY three ways over: ``PRAGMA query_only=ON``
    is set FIRST (before any other statement), a SQLite authorizer rejects every
    non-read action, and no schema-changing pragma is ever issued. ``query_only``
    (not URI ``mode=ro``) is used so a WAL left by a killed writer can still be
    recovered on first access — ``mode=ro`` would refuse exactly the session a
    crash makes most interesting.

  * display_state() is the ONLY path that turns a stored sample into something
    displayable, so the facts the store distinguishes stay distinct: measured
    now (fresh), measured earlier and carried forward (carried, with its age),
    a module error, a module that answered with nothing (unavailable), a channel
    nobody ever asked (not read) — PLUS the fact the raw store cannot carry: how
    old the whole view is (a finished or stalled drive is `archive`/`stale`,
    never `fresh`). A carried, stale or archived value that renders like a live
    measurement is not a cosmetic defect; it is the tool lying about the truck.

  * The data root is resolved to agree with truck-mcp AND to never miss the
    frozen field kit — the build that actually actuates — whose data lives beside
    its executable, not in the dev checkout. When it can't be resolved uniquely,
    all candidate roots are read, never one guessed silently.

No Qt in this module — pure stdlib, unit-testable with a fixture DB.
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional, Sequence

SESSION_SUFFIX = ".tmsession.db"

# Known truck-mcp locations checked when $TRUCK_MCP_DATA is unset (or, for the
# safety verdict, in addition to it). Named so they are patchable in tests and
# there is one place to change if the checkout moves.
DEV_CHECKOUT = Path("D:/Projects/truck-mcp")
FIELD_KIT_SUBPATH = ("Programs", "truck-mcp-app")   # under %LOCALAPPDATA%

# Reading status (truck-mcp sessionstore QUALITY_*): orthogonal to freshness.
QUALITY_OK = "ok"
QUALITY_ERROR = "error"
QUALITY_UNAVAILABLE = "unavailable"
_KNOWN_QUALITY = {QUALITY_OK, QUALITY_ERROR, QUALITY_UNAVAILABLE}

# A live tiered drive commits its fast tier well under a second; if the newest
# committed sample in a session claiming to be live is older than this in
# wall-clock, the logger is not advancing (finished or dead) and nothing in it
# may render as a live measurement.
STALE_SESSION_LIMIT_S = 5.0

# SQLite bind-variable ceiling (default 999 pre-3.32, 32766 after). Chunk any
# IN(...) list below the conservative floor so a probe-heavy session can't throw.
_MAX_BIND_VARS = 900


# --------------------------------------------------------------------------- #
# Locating truck-mcp's data — agree with it, and never miss the field kit
# --------------------------------------------------------------------------- #
def candidate_roots() -> list[Path]:
    """Every plausible truck-mcp data root that exists on this box, in priority
    order and de-duplicated.

    Resolution mirrors truck-mcp's own ($TRUCK_MCP_DATA wins, expanded and
    resolved exactly as truck-mcp's paths.data_root does) but adds what a
    separate reader process must know that truck-mcp's in-process logic cannot
    tell it: the frozen field kit writes beside its OWN executable
    (%LOCALAPPDATA%\\Programs\\truck-mcp-app), which is a different tree from a
    dev checkout. When $TRUCK_MCP_DATA is set it is authoritative and alone —
    that is the single source of truth both tools share. Otherwise every
    existing candidate is returned so callers can read them all rather than
    silently pick one and possibly report the wrong truck.
    """
    override = os.environ.get("TRUCK_MCP_DATA")
    if override:
        # Authoritative and alone, exactly as truck-mcp treats it. Returned even
        # if absent — a caller that got here explicitly must not silently read a
        # different tree than the writer used.
        return [Path(override).expanduser().resolve()]

    roots: list[Path] = []
    seen: set[str] = set()
    local = os.environ.get("LOCALAPPDATA")
    if local:
        _add_root(Path(local).joinpath(*FIELD_KIT_SUBPATH), roots, seen)  # field kit
    _add_root(DEV_CHECKOUT, roots, seen)                                  # dev checkout
    _add_root(Path.home() / "truck-mcp", roots, seen)                    # fallback
    return roots


def control_root_candidates() -> list[Path]:
    """Every root that could hold a control journal, ALWAYS unioned — never
    collapsed to one, even under $TRUCK_MCP_DATA.

    The control-state verdict is a safety indicator: missing the field kit's
    journal because an override points somewhere else would be a false 'clean'
    while an actuator is held. Live-data reads use candidate_roots() (which
    honors the override as authoritative, matching the writer); the safety
    verdict deliberately does NOT — it reads the override root AND the field kit
    AND the known checkouts, so no crashed control session hides behind an env
    var."""
    roots: list[Path] = []
    seen: set[str] = set()
    override = os.environ.get("TRUCK_MCP_DATA")
    if override:
        _add_root(Path(override), roots, seen)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        _add_root(Path(local).joinpath(*FIELD_KIT_SUBPATH), roots, seen)
    _add_root(DEV_CHECKOUT, roots, seen)
    _add_root(Path.home() / "truck-mcp", roots, seen)
    return roots


def _add_root(p: Optional[Path], roots: list, seen: set) -> None:
    if p is None:
        return
    try:
        rp = p.expanduser().resolve()
    except (OSError, RuntimeError):
        return
    key = str(rp).lower()   # Windows path compare is case-insensitive
    if key in seen or not rp.is_dir():
        return
    seen.add(key)
    roots.append(rp)


def data_root() -> Optional[Path]:
    """The single highest-priority existing root, or None. Prefer
    candidate_roots() for anything that must not miss a second tree."""
    roots = candidate_roots()
    if not roots:
        return None
    # $TRUCK_MCP_DATA is returned even when absent; honor "not found" honestly.
    return roots[0] if roots[0].is_dir() else None


def sessions_dirs() -> list[Path]:
    """Every existing sessions/ directory across all candidate roots."""
    out = []
    for root in candidate_roots():
        d = root / "sessions"
        if d.is_dir():
            out.append(d)
    return out


def sessions_dir() -> Optional[Path]:
    dirs = sessions_dirs()
    return dirs[0] if dirs else None


# --------------------------------------------------------------------------- #
# Display state — the one rule this module exists to keep
# --------------------------------------------------------------------------- #
@dataclass
class DisplayState:
    state: str      # fresh | stale | archive | carried | error | unavail | notread | badvalue
    text: str
    unit: str
    age: Optional[float] = None   # seconds, for carried values


def display_state(sample: Optional[dict], channel: Optional[dict] = None, *,
                  archived: bool = False, session_stale: bool = False
                  ) -> DisplayState:
    """Turn a stored sample into a display, preserving every fact.

    `archived` (the session has ended) and `session_stale` (a live session whose
    newest sample is older than STALE_SESSION_LIMIT_S in wall-clock) override the
    `fresh` branch only: a value the store marked fresh must NOT render as a live
    measurement when the whole view is old. Carried / error / unavailable /
    not-read already say they are not live and are unaffected.
    """
    ch = channel or {}
    unit = ch.get("unit") or ""
    if sample is None:
        return DisplayState("notread", "— not read", "")

    # Value selection by KEY PRESENCE (matching truck-mcp app.js), not nullness.
    if "value_num" in sample and sample["value_num"] is not None:
        value = sample["value_num"]
    else:
        value = sample.get("value")

    quality = sample.get("quality", QUALITY_OK)
    if quality not in _KNOWN_QUALITY:
        # Unknown quality is unknown, not "fine". Fail loud rather than render a
        # future producer's error string as a good reading.
        raise ValueError(f"unknown quality {quality!r} for {ch.get('name', '?')}")

    if quality == QUALITY_ERROR:
        if value is not None:
            raise ValueError(
                f"errored sample carries a value: {ch.get('name', '?')}")
        return DisplayState("error", "ERR", "")

    if quality == QUALITY_UNAVAILABLE:
        return DisplayState("unavail", "N/A", "")

    if value is None or (isinstance(value, str) and value.strip() == ""):
        return DisplayState("notread", "— not read", "")

    text, ok = _format_number(value, ch.get("precision"))
    if not ok:
        # Non-finite or non-numeric where a number was expected: a formatting
        # fault, kept distinct from a module error so the operator isn't told the
        # truck is faulty when the tool is.
        return DisplayState("badvalue", text, "")

    if sample.get("fresh"):
        if archived:
            return DisplayState("archive", text, unit)
        if session_stale:
            return DisplayState("stale", text, unit)
        return DisplayState("fresh", text, unit)
    return DisplayState("carried", text, unit, age=sample.get("stale_s"))


def _format_number(value, precision) -> tuple[str, bool]:
    """(text, is_valid_number). Non-finite and non-numeric return is_valid=False
    with a descriptive text so the caller can render them distinctly."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return (str(value)[:24], False)
    if not math.isfinite(v):
        return ("∞/NaN", False)
    # A bad/negative precision in channel metadata is a metadata fault, not a bad
    # reading — fall back to default formatting rather than mislabel the value.
    p = None
    if precision is not None:
        try:
            p = int(precision)
            if p < 0:
                p = None
        except (TypeError, ValueError):
            p = None
    if p is None:
        if v == int(v):
            return (str(int(v)), True)
        return (f"{v:.2f}".rstrip("0").rstrip("."), True)
    return (f"{v:.{p}f}", True)


def sample_age_s(sample: Optional[dict], now: Optional[datetime] = None
                 ) -> Optional[float]:
    """Wall-clock age of a sample from its ts_utc, or None if unparseable."""
    if not sample:
        return None
    ts = _parse_ts(sample.get("ts_utc"))
    if ts is None:
        return None
    now = now or datetime.now(timezone.utc)
    return (now - ts).total_seconds()


def _parse_ts(ts: Optional[str]) -> Optional[datetime]:
    if not ts or not isinstance(ts, str):
        return None
    raw = ts.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# --------------------------------------------------------------------------- #
# Reader
# --------------------------------------------------------------------------- #
class TmSessionError(RuntimeError):
    pass


def _deny_writes(action, *_args):
    # SQLite authorizer: allow only reads and the transaction/function plumbing
    # a SELECT needs; deny everything that could mutate the file or schema.
    if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ,
                  sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_TRANSACTION,
                  sqlite3.SQLITE_RECURSIVE):
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def _connect(path: Path, timeout: float = 5.0) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=timeout, isolation_level=None)
    conn.row_factory = sqlite3.Row
    # query_only FIRST, before any statement that could touch the file header.
    # We deliberately do NOT issue `PRAGMA journal_mode=WAL`: SQLite reads WAL
    # from the header automatically, and setting it would rewrite the header of
    # a non-WAL file — a write this reader promises it cannot do.
    conn.execute("PRAGMA query_only=ON")
    conn.set_authorizer(_deny_writes)
    return conn


def _row_to_sample(row: sqlite3.Row) -> dict:
    keys = row.keys()
    if "fresh" not in keys or "quality" not in keys:
        # Missing freshness/quality is unknown, not fresh+ok. Refuse to fabricate.
        raise TmSessionError("sample row missing fresh/quality columns")
    text = row["value_text"] if "value_text" in keys else None
    num = row["value_num"] if "value_num" in keys else None
    value = num if num is not None else text
    return {
        "id": row["id"] if "id" in keys else None,
        "ts_utc": row["ts_utc"] if "ts_utc" in keys else None,
        "value": value,
        "value_num": num,
        "value_text": text,
        "fresh": bool(row["fresh"]),
        "stale_s": row["stale_s"] if "stale_s" in keys else 0.0,
        "quality": row["quality"],
    }


def _load_json(raw, _label: str) -> dict:
    try:
        return json.loads(raw or "{}")
    except (TypeError, ValueError):
        # Corrupt provenance is not "no provenance". Preserve the raw text and a
        # marker rather than substituting {}.
        return {"_decode_error": True, "_raw": raw}


# Provenance of a truck-mcp session, three-valued so that a store we cannot
# read is never presented as a real drive.
PROVENANCE_SIMULATED = "simulated"
PROVENANCE_REAL = "real"
PROVENANCE_UNKNOWN = "unknown"

# session.source values truck-mcp writes (sessionstore.SOURCES). Anything else
# is a value this reader does not understand, hence unknown provenance.
KNOWN_SOURCES = frozenset({"live", "import", "simulated", "replay"})
SOURCE_SIMULATED = "simulated"

# Legacy markers, for files written before truck-mcp v0.11.0a9 stamped
# session.source / extra.simulated. The seeder (scripts/seed_demo_data.py) set
# sweep_config.started_by = "seed_demo_data" and notes beginning "SIMULATED".
# Provenance comes from the store itself, never inferred from a label.
SIMULATED_STARTED_BY = frozenset({"seed_demo_data"})
SIMULATED_NOTES_PREFIX = "SIMULATED"


def _unreadable(value) -> bool:
    return isinstance(value, dict) and value.get("_decode_error") is True


def session_provenance(meta: dict) -> str:
    """'simulated', 'real' or 'unknown' from a session's own metadata.

    Positive simulated signals win, in order: session.source == 'simulated'
    (authoritative), extra.simulated is True, then the legacy seeder markers
    (sweep_config.started_by, notes prefix). With none of them:
      * 'unknown' when the evidence could not be read: a corrupt sweep_config
        or extra column (a marker may be hiding there), or a session.source
        value this reader does not recognise;
      * 'real' otherwise (an absent source is an older file; it is real only
        because every readable marker says so)."""
    if meta.get("source") == SOURCE_SIMULATED:
        return PROVENANCE_SIMULATED
    extra = meta.get("extra")
    if isinstance(extra, dict) and extra.get("simulated") is True:
        return PROVENANCE_SIMULATED
    sweep = meta.get("sweep_config")
    if isinstance(sweep, dict) and sweep.get("started_by") in SIMULATED_STARTED_BY:
        return PROVENANCE_SIMULATED
    notes = meta.get("notes")
    if isinstance(notes, str) and notes.lstrip().upper().startswith(
            SIMULATED_NOTES_PREFIX):
        return PROVENANCE_SIMULATED
    if _unreadable(sweep) or _unreadable(extra):
        return PROVENANCE_UNKNOWN
    source = meta.get("source")
    if source not in (None, "") and not (
            isinstance(source, str) and source in KNOWN_SOURCES):
        return PROVENANCE_UNKNOWN
    return PROVENANCE_REAL


def session_simulated(meta: dict) -> bool:
    """True only when the session is positively marked simulated."""
    return session_provenance(meta) == PROVENANCE_SIMULATED


def session_tag(entry: dict) -> str:
    """Picker prefix for a tmstore.list_sessions() entry: LIVE, SIMULATED and
    UNKNOWN provenance are all shown, so a fabricated or unreadable session
    can never pass for a drive."""
    tag = "● LIVE  " if entry.get("live") else ""
    prov = entry.get("provenance")
    if prov is None:  # entry built without provenance: fall back to the flag
        prov = PROVENANCE_SIMULATED if entry.get("simulated") else PROVENANCE_REAL
    if prov == PROVENANCE_SIMULATED:
        tag += "[SIMULATED]  "
    elif prov == PROVENANCE_UNKNOWN:
        tag += "[PROVENANCE UNKNOWN]  "
    return tag


class TmSessionReader:
    """Read-only view of one *.tmsession.db, safe while the writer runs."""

    def __init__(self, path: str | Path, timeout: float = 5.0):
        """`timeout` is the SQLite busy wait. Callers polling on a GUI thread
        should pass a short one — a paint-timer slot must not sit out a 5 s
        WAL-checkpoint lock."""
        self.path = Path(path)
        if not self.path.exists():
            raise TmSessionError(f"no session file at {self.path}")
        self._conn = _connect(self.path, timeout=timeout)

    def __enter__(self) -> "TmSessionReader":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self._conn.close()

    def metadata(self) -> dict:
        row = self._conn.execute("SELECT * FROM session WHERE id = 1").fetchone()
        if row is None:
            raise TmSessionError(f"{self.path} has no session row")
        meta = dict(row)
        for k in ("sweep_config", "extra"):
            if k in meta:
                meta[k] = _load_json(meta[k], k)
        meta["path"] = str(self.path)
        return meta

    def schema_version(self) -> Optional[int]:
        try:
            row = self._conn.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()
        except sqlite3.Error:
            return None
        if row is None:
            return None
        try:
            return int(row[0])
        except (TypeError, ValueError):
            return None

    def channel_count(self) -> int:
        """Cheap — one COUNT over the small channel table, never a sample scan."""
        row = self._conn.execute("SELECT COUNT(*) FROM channel").fetchone()
        return int(row[0]) if row else 0

    def sample_count(self, channels: Optional[Sequence[str]] = None,
                     fresh_only: bool = False) -> int:
        """Sample rows, optionally with the same channel/fresh predicate
        series() applies — lets a caller refuse an unbounded load BEFORE
        reading, counting only the rows the load would actually touch."""
        if channels is None:
            sql = "SELECT COUNT(*) FROM sample s"
            if fresh_only:
                sql += " WHERE s.fresh = 1"
            row = self._conn.execute(sql).fetchone()
            return int(row[0]) if row else 0
        total = 0
        for chunk in _chunks(list(channels), _MAX_BIND_VARS):
            sql = ("SELECT COUNT(*) FROM sample s "
                   "JOIN channel c ON c.id = s.channel_id "
                   f"WHERE c.name IN ({','.join('?' * len(chunk))})")
            if fresh_only:
                sql += " AND s.fresh = 1"
            row = self._conn.execute(sql, list(chunk)).fetchone()
            total += int(row[0]) if row else 0
        return total

    def channels(self) -> list[dict]:
        rows = self._conn.execute("SELECT * FROM channel ORDER BY name").fetchall()
        out = []
        for row in rows:
            entry = dict(row)
            entry["meta"] = _load_json(entry.get("meta"), "meta")
            out.append(entry)
        return out

    def latest_ts(self) -> Optional[str]:
        """Newest committed sample timestamp — how current the whole view is."""
        row = self._conn.execute(
            "SELECT MAX(ts_utc) FROM channel_latest").fetchone()
        return row[0] if row and row[0] else None

    def ended(self) -> bool:
        """Whether the drive has finished (ended_utc set). Cheap single-row read,
        polled each tick so a drive that ends while bound stops rendering live."""
        row = self._conn.execute(
            "SELECT ended_utc FROM session WHERE id = 1").fetchone()
        return bool(row and row[0])

    def latest(self, channels: Optional[Sequence[str]] = None) -> dict[str, dict]:
        """Most recent sample per channel, from channel_latest (no scan)."""
        base = ("SELECT c.name, l.* FROM channel_latest l "
                "JOIN channel c ON c.id = l.channel_id")
        out: dict[str, dict] = {}
        if channels is None:
            for row in self._conn.execute(base):
                out[row["name"]] = _row_to_sample(row)
            return out
        names = list(channels)
        for chunk in _chunks(names, _MAX_BIND_VARS):
            sql = base + f" WHERE c.name IN ({','.join('?' * len(chunk))})"
            for row in self._conn.execute(sql, chunk):
                out[row["name"]] = _row_to_sample(row)
        return out

    def series(self, channels: Sequence[str] | str,
               fresh_only: bool = False, limit: Optional[int] = None
               ) -> dict[str, list[dict]]:
        names = [channels] if isinstance(channels, str) else list(channels)
        out: dict[str, list[dict]] = {n: [] for n in names}
        for chunk in _chunks(names, _MAX_BIND_VARS - 2):
            sql = ("SELECT c.name, s.* FROM sample s "
                   "JOIN channel c ON c.id = s.channel_id "
                   f"WHERE c.name IN ({','.join('?' * len(chunk))})")
            params: list = list(chunk)
            if fresh_only:
                sql += " AND s.fresh = 1"
            sql += " ORDER BY s.ts_utc ASC, s.id ASC"
            if limit:
                sql += " LIMIT ?"
                params.append(int(limit))
            for row in self._conn.execute(sql, params):
                out[row["name"]].append(_row_to_sample(row))
        return out

    def series_after(self, channels: Sequence[str] | str, after_id: int = 0,
                     min_ts: Optional[str] = None, fresh_only: bool = True,
                     numeric_only: bool = True, limit: Optional[int] = None
                     ) -> dict[str, list[dict]]:
        """Samples per channel with sample id > after_id, ascending by id —
        the incremental cursor read a scrolling chart needs. Each returned
        sample carries its "id"; the caller advances the cursor to the max id
        seen. Defaults keep the read chart-shaped: only fresh (measured-now)
        numeric samples, so carried repeats and text sidecars never plot as
        measurements. min_ts (ISO) additionally bounds the initial backfill."""
        names = [channels] if isinstance(channels, str) else list(channels)
        out: dict[str, list[dict]] = {n: [] for n in names}
        for chunk in _chunks(names, _MAX_BIND_VARS - 3):
            sql = ("SELECT c.name, s.* FROM sample s "
                   "JOIN channel c ON c.id = s.channel_id "
                   f"WHERE c.name IN ({','.join('?' * len(chunk))}) "
                   "AND s.id > ?")
            params: list = list(chunk) + [int(after_id)]
            if min_ts is not None:
                sql += " AND s.ts_utc >= ?"
                params.append(min_ts)
            if fresh_only:
                sql += " AND s.fresh = 1"
            if numeric_only:
                sql += " AND s.value_num IS NOT NULL"
            sql += " ORDER BY s.id ASC"
            if limit:
                sql += " LIMIT ?"
                params.append(int(limit))
            for row in self._conn.execute(sql, params):
                out[row["name"]].append(_row_to_sample(row))
        return out

    def events(self, kinds: Optional[Sequence[str]] = None, limit: int = 200
               ) -> list[dict]:
        sql = "SELECT ts_utc, kind, label, detail FROM event"
        params: list = []
        if kinds:
            ks = list(kinds)
            sql += f" WHERE kind IN ({','.join('?' * len(ks))})"
            params = ks
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        out = []
        for row in self._conn.execute(sql, params):
            entry = dict(row)
            entry["detail"] = _load_json(entry.get("detail"), "detail")
            out.append(entry)
        return out


def _chunks(seq: Sequence, n: int) -> Iterable[list]:
    seq = list(seq)
    if not seq:
        return
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def list_sessions(directory: Optional[str | Path] = None) -> list[dict]:
    """Session files, newest first. With no directory, aggregates EVERY sessions/
    dir across all candidate roots (deduped by session_uid, higher-priority root
    winning) so a drive recorded by the field kit is never invisible. A file that
    is not a readable session DB is reported with its error, not skipped."""
    if directory is not None:
        dirs = [Path(directory)]
    else:
        dirs = sessions_dirs()
    out: list[dict] = []
    seen_uids: set[str] = set()
    for d in dirs:
        if not Path(d).is_dir():
            continue
        try:
            paths = sorted(Path(d).glob(f"*{SESSION_SUFFIX}"))
        except OSError as exc:
            out.append({"path": str(d), "name": str(d), "error": f"dir: {exc}"})
            continue
        for path in paths:
            entry = {"path": str(path), "name": path.name,
                     "root": str(d.parent), "error": None}
            try:
                with TmSessionReader(path) as reader:
                    meta = reader.metadata()
                    uid = meta.get("session_uid")
                    if uid and uid in seen_uids:
                        continue
                    if uid:
                        seen_uids.add(uid)
                    entry.update({
                        "session_uid": uid,
                        "label": meta.get("label") or "",
                        "started_utc": meta.get("started_utc"),
                        "ended_utc": meta.get("ended_utc"),
                        "vin": meta.get("vin"),
                        "source": meta.get("source") or "live",
                        "engine_state": meta.get("engine_state") or "unknown",
                        "live": meta.get("ended_utc") in (None, ""),
                        "provenance": session_provenance(meta),
                        "simulated": session_simulated(meta),
                        "channel_count": reader.channel_count(),
                    })
            except (TmSessionError, sqlite3.Error) as exc:
                entry["error"] = str(exc)
            except OSError as exc:
                entry["error"] = f"os: {exc}"
            out.append(entry)
    out.sort(key=lambda e: (e.get("started_utc") or ""), reverse=True)
    return out
