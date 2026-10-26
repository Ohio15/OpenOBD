"""
ctljournal — read truck-mcp's control journal, so OpenOBD can DISPLAY vehicle
control state without ever commanding the vehicle.

truck-mcp's actuation gate writes an append-only JSONL journal: every arm, fire,
activation and release, each activation carrying its exact release bytes. That
file is the crash-recovery plan — after a killed control session, an activation
with no matching release means the truck may still be holding an actuator. It is
also the ONLY honest source of "what, if anything, is currently commanded on
this truck", because control state is a function of what the module actually
acknowledged, not of what any UI believes.

OpenOBD reads this file and nothing else on the control path. It holds no lease,
owns no console, sends no frame, and imports no truck-mcp code.

Two safety rules govern the read, learned from the adversarial review:
  * NEVER report "clean" from an absence of evidence. A journal that cannot be
    located, cannot be read, or contains a damaged record yields verdict
    ``unknown`` — rendered as a warning, never as the green all-clear. Unexamined
    is not clean.
  * NEVER miss the field kit. truck-mcp's frozen build (the one that actuates)
    writes its journal beside its own executable, a different tree from a dev
    checkout. outstanding() aggregates EVERY journal found across all candidate
    roots, so a crash in the field kit is not invisible to a reader run from the
    repo.

Pure stdlib, no Qt, unit-testable against fixture JSONL files.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .tmstore import control_root_candidates

JOURNAL_NAME = "control-journal.jsonl"


def journal_paths() -> list[Path]:
    """Every control journal that exists across ALL truck-mcp roots — the field
    kit's included and unioned even under $TRUCK_MCP_DATA, so a crashed control
    session cannot hide behind an env var (see control_root_candidates)."""
    out = []
    for root in control_root_candidates():
        p = root / "control" / JOURNAL_NAME
        if p.is_file():
            out.append(p)
    return out


def journal_path() -> Optional[Path]:
    """The highest-priority existing journal, or None if none exists."""
    paths = journal_paths()
    return paths[0] if paths else None


@dataclass
class JournalRead:
    """The result of reading one journal file, with its integrity noted."""
    path: Path
    entries: list[dict] = field(default_factory=list)
    unreadable_lines: int = 0
    error: Optional[str] = None   # set when the file could not be read at all


def read_one(path: Path) -> JournalRead:
    """Read one journal. A whole-file failure (missing, permission, non-UTF-8)
    sets `error`; individual malformed lines increment `unreadable_lines` and are
    counted, not silently discarded — a dropped `activate` is a false negative on
    the one thing this module reports."""
    result = JournalRead(path=Path(path))
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        result.error = str(exc)
        return result
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            result.unreadable_lines += 1
            continue
        if not isinstance(obj, dict):
            result.unreadable_lines += 1
            continue
        result.entries.append(obj)
    return result


@dataclass
class ControlState:
    """Aggregate verdict across every journal found.

    verdict:
      'clean'       — journals located, fully readable, no outstanding activation
      'outstanding' — at least one activation with no matching release
      'unknown'     — no journal located, or a read error / damaged line means
                      the answer cannot be trusted (rendered as a warning)
    """
    verdict: str
    outstanding: list[dict] = field(default_factory=list)
    reads: list[JournalRead] = field(default_factory=list)
    total_entries: int = 0
    detail: str = ""


def control_state() -> ControlState:
    """Read every journal across all roots and produce one honest verdict."""
    paths = journal_paths()
    if not paths:
        return ControlState(
            verdict="unknown",
            detail="No control journal located in any truck-mcp data root. "
                   "Control state cannot be confirmed — this is not the same as "
                   "'nothing is commanded'.")

    reads = [read_one(p) for p in paths]
    total = sum(len(r.entries) for r in reads)
    damaged = [r for r in reads if r.error or r.unreadable_lines]

    # Flatten every journal's entries and order them GLOBALLY by wall-clock `at`
    # before folding. Append order is chronological only WITHIN one file; folding
    # concatenated files in root order would let an old, already-resolved
    # `released` in one journal pop a newer `activate` in another — a false
    # clean. truck-mcp stamps every append with `at`; an entry without a usable
    # `at` cannot be ordered and is treated as damage (verdict downgrades to
    # unknown) rather than trusted in file order.
    flat: list[tuple[float, dict, str]] = []
    unorderable = 0
    for r in reads:
        for entry in r.entries:
            at = entry.get("at")
            # json.loads accepts NaN / Infinity and huge integer literals: a
            # NaN key makes the sort order undefined (a release could fold
            # before its activate - a false clean), so a non-finite or
            # unconvertible `at` is unorderable, never trusted.
            try:
                at_f = float(at)
            except (TypeError, ValueError, OverflowError):
                unorderable += 1
                continue
            if not math.isfinite(at_f):
                unorderable += 1
                continue
            flat.append((at_f, entry, str(r.path)))
    flat.sort(key=lambda t: t[0])

    # Keyed by (module, cpid) exactly as truck-mcp keys it: the truck has one
    # state per control, so a release by anyone — any file, any recovery run —
    # puts it back.
    live: dict[tuple, dict] = {}
    for _at, entry, journal in flat:
        # str(): a list / dict module or cpid from a hostile line must not
        # raise TypeError (unhashable) and take the safety verdict down.
        key = (str(entry.get("module")), str(entry.get("cpid")))
        event = entry.get("event")
        if event == "activate":
            d = dict(entry)
            d["_journal"] = journal
            live[key] = d
        elif event in ("released", "return_to_normal"):
            live.pop(key, None)
    outstanding = list(live.values())

    if outstanding:
        return ControlState("outstanding", outstanding=outstanding, reads=reads,
                            total_entries=total,
                            detail="An activation has no matching release.")
    if damaged or unorderable:
        bits = []
        for r in damaged:
            if r.error:
                bits.append(f"{r.path.name}: {r.error}")
            elif r.unreadable_lines:
                bits.append(f"{r.path.name}: {r.unreadable_lines} damaged line(s)")
        if unorderable:
            bits.append(f"{unorderable} record(s) with no usable timestamp")
        return ControlState(
            "unknown", reads=reads, total_entries=total,
            detail="Journal damaged or partly unreadable — a lost activation "
                   "record could hide a held actuator. " + "; ".join(bits))
    return ControlState("clean", reads=reads, total_entries=total,
                        detail="No unreleased activation is recorded.")


# -- thin helpers retained for callers/tests that want raw records ----------- #
def entries(path: Optional[Path] = None) -> list[dict]:
    """Records from one journal (highest-priority by default), oldest first.
    Malformed lines are dropped from the returned list but the caller should
    prefer control_state() for a verdict, which counts them instead."""
    path = path or journal_path()
    if path is None:
        return []
    return read_one(path).entries


def outstanding(path: Optional[Path] = None) -> list[dict]:
    """Outstanding activations in ONE journal. Prefer control_state() for the
    cross-root aggregate verdict; this stays for targeted/unit use."""
    if path is None:
        return control_state().outstanding
    live: dict[tuple, dict] = {}
    for entry in read_one(path).entries:
        # str(): a list / dict module or cpid from a hostile line must not
        # raise TypeError (unhashable) and take the safety verdict down.
        key = (str(entry.get("module")), str(entry.get("cpid")))
        event = entry.get("event")
        if event == "activate":
            live[key] = entry
        elif event in ("released", "return_to_normal"):
            live.pop(key, None)
    return list(live.values())


def recent(path: Optional[Path] = None, limit: int = 50) -> list[dict]:
    """The last `limit` records, newest first — a read-only audit tail."""
    return list(reversed(entries(path)))[:limit]
