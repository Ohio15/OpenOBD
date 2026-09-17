"""Self-checks for the STOCK -> TUNE #24 calibration delta.

The six pins are the known-true anchor for this whole derivation: five must
fall out as changes and AFM must fall out as NOT a change. AFM is the one
that catches a broken differ -- it was already disabled in the Oct-2024
stock read, so a delta that reports #24 disabling AFM is wrong.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.delta_stock_to_24 import (  # noqa: E402
    DATA,
    build_delta,
    decimals_of,
    decimals_of_value,
    differs_at_display_precision,
    normalise_shift_label,
    quantize,
)

DELTA_FILE = DATA / "2010_silverado_delta_stock_to_24.json"

# (module, param_id, name, stock, tune24, must_be_a_change)
PINS = [
    ("ECM", 9050, "Final Drive Ratio", 3.08, 4.11, True),
    ("ECM", 9052, "Final Drive Ratio - VSS Error", 3.08, 4.11, True),
    ("TCM", 5004, "Final Drive Ratio - Trans", 3.08, 4.11, True),
    ("ECM", 9054, "Driven Tire Circumference", 2475, 2742, True),
    ("ECM", 9056, "Non-Driven Tire Circumference", 2475, 2742, True),
    # Already 0/"Disable" in stock. #24 did NOT disable AFM.
    ("ECM", 246, "DoD Enable", 0, 0, False),
]


@pytest.fixture(scope="module")
def delta():
    return json.loads(DELTA_FILE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def by_key(delta):
    return {(e["module"], e["param_id"], e["kind"]): e for e in delta["parameters"]}


# ---------------------------------------------------------------------------
# the six pins
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("module,pid,name,stock,tune,is_change", PINS,
                         ids=[f"{p[0]}:{p[1]}" for p in PINS])
def test_pin_reconciles(by_key, module, pid, name, stock, tune, is_change):
    entry = by_key.get((module, pid, "scalar"))
    assert entry is not None, f"pin {module}:{pid} missing from the delta entirely"

    assert entry["stock_value"] == pytest.approx(stock), (
        f"{module}:{pid} stock value disagrees with the pin"
    )
    assert entry["tune24_value"] == pytest.approx(tune), (
        f"{module}:{pid} #24 value disagrees with the pin"
    )
    if is_change:
        assert entry["status"] == "changed", (
            f"{module}:{pid} must be a #24 change, got {entry['status']}"
        )
    else:
        assert entry["status"] != "changed", (
            f"{module}:{pid} must NOT be a #24 change, got {entry['status']}. "
            "AFM was already disabled in stock; reporting it as a #24 change "
            "means the differ is wrong."
        )


def test_afm_was_already_disabled_in_stock(by_key):
    """The single most load-bearing negative result in the whole delta."""
    afm = by_key[("ECM", 246, "scalar")]
    assert afm["stock_value"] == 0
    assert afm["stock_raw"].casefold() == "disable"
    assert afm["status"] == "unchanged"


def test_afm_name_alias_trap(by_key, delta):
    """ECM:246 is 'DoD Enable' in stock and 'DoD (AFM) Enable' on the pin sheet.

    Matching by name scores it MISSING; matching by (module, param_id) finds it.
    """
    afm = by_key[("ECM", 246, "scalar")]
    assert afm["stock_name"] == "DoD Enable"
    assert afm["tune24_name"] == "DoD (AFM) Enable"
    assert afm["name_differs"] is True
    assert afm["sources"]["tune24_sheet"] is True

    resolution = [r for r in delta["diagnostics"]["sheet24_resolution"]
                  if r["sheet_name"] == "DoD (AFM) Enable"]
    assert len(resolution) == 1
    assert resolution[0]["resolved_module"] == "ECM"
    assert resolution[0]["resolved_param_id"] == 246


def test_final_drive_ratio_is_two_distinct_parameters(by_key):
    """'Final Drive Ratio' exists in both modules; a name match collapses them."""
    ecm = by_key[("ECM", 9050, "scalar")]
    tcm = by_key[("TCM", 5004, "scalar")]
    assert ecm["param_id"] != tcm["param_id"]
    assert ecm["module"] != tcm["module"]
    for entry in (ecm, tcm):
        assert entry["status"] == "changed"
        assert entry["stock_value"] == pytest.approx(3.08)
        assert entry["tune24_value"] == pytest.approx(4.11)


# ---------------------------------------------------------------------------
# join-key integrity
# ---------------------------------------------------------------------------

def test_no_module_conflicts(delta):
    assert delta["diagnostics"]["module_conflicts"] == []


def test_every_swept24_entry_resolved_a_module(delta):
    assert delta["diagnostics"]["swept24_entries_with_unresolved_module"] == []


def test_every_sheet24_entry_resolved(delta):
    unresolved = [r for r in delta["diagnostics"]["sheet24_resolution"]
                  if r["resolved_module"] is None or r["resolved_param_id"] is None]
    assert unresolved == [], f"unresolved sheet24 entries: {unresolved}"


def test_join_key_is_unique(delta):
    keys = [(e["module"], e["param_id"], e["kind"]) for e in delta["parameters"]]
    assert len(keys) == len(set(keys))


def test_best_cal_json_is_not_a_source(delta):
    """best.cal.json's 'stock' key was swept from #24 itself. It is obsolete."""
    excluded = delta["metadata"]["excluded_source"]
    assert excluded["file"] == "2010_silverado_best.cal.json"
    assert "OBSOLETE" in excluded["reason"]
    sources = delta["metadata"]["tune24_sources"]
    assert set(sources) == {"swept", "sheet"}
    assert "best" not in json.dumps(sources)


# ---------------------------------------------------------------------------
# display-precision comparison -- the rule that decides 11 changes vs 16
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("stock,decimals,tune", [
    (6042, 0, 6041.924736342039),      # thousands-separator table
    (19.9688, 4, 19.96875),            # Base Dwell Time
    (-0.0312, 4, -0.03125),            # Engine Torque Coeff Spark A
    (1.8438, 4, 1.84375),              # only half-to-even reproduces "1.8438"
    (1.5312, 4, 1.53125),              # only half-to-even reproduces "1.5312"
    (16.2, 1, 16.25),                  # only half-to-even reproduces "16.2"
    (19.2, 1, 19.25),
    (13.8, 1, 13.75),
    (0.1, 1, 0.095),
    (0.97, 2, 0.975),
    (7832.5, 1, 7832.533571542027),
])
def test_display_rounding_is_not_a_change(stock, decimals, tune):
    assert not differs_at_display_precision(stock, tune, decimals)


@pytest.mark.parametrize("stock,decimals,tune", [
    (33, 0, 35.000674187743584),       # WOT 1-2 upshift, a real #24 edit
    (105, 0, 89.0017143631194),        # WOT 3-4 upshift
    (3.08, 2, 4.11),                   # axle
    (2475, 0, 2742),                   # tire circumference
])
def test_real_edits_survive_display_rounding(stock, decimals, tune):
    assert differs_at_display_precision(stock, tune, decimals)


def test_thousands_separator_parses():
    """'6,042' must read as 6042 with zero decimals, not as non-numeric."""
    assert decimals_of("6,042") == 0
    assert decimals_of("7,832.5") == 1
    assert decimals_of("-36.9") == 1
    assert decimals_of("Max Torque") is None


def test_decimals_of_value_is_never_tighter_than_display():
    assert decimals_of_value(6042.0) == 0
    assert decimals_of_value(19.9688) == 4
    assert decimals_of_value(3.08) == 2
    # 0.10 displayed to 2dp parses to 0.1; 1dp is wider, which is the safe way
    # to be wrong.
    assert decimals_of_value(0.1) == 1


def test_quantize_uses_bankers_rounding():
    assert str(quantize(16.25, 1)) == "16.2"
    assert str(quantize(19.25, 1)) == "19.2"
    assert str(quantize(1.84375, 4)) == "1.8438"


# ---------------------------------------------------------------------------
# table alignment
# ---------------------------------------------------------------------------

def test_shift_labels_normalise_across_both_layouts():
    assert normalise_shift_label("1 -> 2 Shift") == "1-2"
    assert normalise_shift_label("1-2") == "1-2"
    assert normalise_shift_label("4 -> 3 Shift") == "4-3"
    assert normalise_shift_label("Max Torque") is None


def test_hot_engine_table_aligned_by_label(by_key):
    """TCM:15017 exists only on the hand-entered sheet, in a 1x10 layout.

    Stock stores it 6x1. Positional comparison is impossible; alignment is by
    the gear-change label both files state explicitly.
    """
    entry = by_key[("TCM", 15017, "table")]
    assert entry["status"] == "changed"
    assert entry["alignment"] == "by gear-change label"
    assert entry["primary_source"] == "tune24_sheet"
    assert entry["cells_compared"] == 6


def test_all_six_wot_tables_are_changes(by_key):
    wot = [15010, 15012, 15015, 15017, 15323, 15326]
    for pid in wot:
        entry = by_key[("TCM", pid, "table")]
        assert entry["status"] == "changed", f"TCM:{pid} should be a #24 change"
        assert entry["cells_changed"] == 6
        # #24 lowered every WOT shift speed it touched except the 1-2/2-1 pair
        # on the Normal table; nothing was raised above 1.1x.
        assert entry["max_ratio"] < 1.1


def test_sheet24_stock_claims_agree_with_the_independent_sweep(by_key):
    """The hand-entered sheet and the UIA sweep never saw each other.

    Where both assert a stock value for the same cell, they must agree, or one
    of the two sources is wrong and the delta cannot be trusted.
    """
    checked = agreeing = 0
    for pid in (15010, 15012, 15015, 15017, 15323, 15326):
        check = by_key[("TCM", pid, "table")].get("sheet24_stock_claim_check")
        assert check is not None, f"TCM:{pid} lost its cross-check"
        checked += check["cells_checked"]
        agreeing += check["cells_agreeing"]
    assert checked == 36
    assert agreeing == 36, f"only {agreeing}/{checked} stock claims agree"


def test_sheet_only_gear_events_are_flagged_not_promoted(by_key):
    """4-5, 5-6, 5-4 and 6-5 have no stock sweep reading behind them.

    VCM Editor never exposed those rows, so the sheet's stock claim for them is
    unverifiable. They must be kept separate from the six confirmed cells.
    """
    entry = by_key[("TCM", 15010, "table")]
    extra = {c["label"] for c in entry["sheet24_only_cells"]}
    assert extra == {"4-5", "5-6", "5-4", "6-5"}
    for cell in entry["sheet24_only_cells"]:
        assert "NO stock sweep reading" in cell["evidence"]
    # and they are not counted as confirmed changed cells
    assert entry["cells_changed"] == 6


# ---------------------------------------------------------------------------
# classification discipline
# ---------------------------------------------------------------------------

def test_unmeasured_is_never_reported_as_unchanged(delta):
    for entry in delta["parameters"]:
        if entry["status"] == "stock_only_tune24_unmeasured":
            assert entry["sources"]["tune24_swept"] is False
            assert entry["sources"]["tune24_sheet"] is False
            assert "UNKNOWN" in entry["note"]
        if entry["status"] == "tune24_only_stock_unmeasured":
            assert entry["sources"]["stock"] is False


def test_changed_entries_always_have_both_sides(delta):
    for entry in delta["parameters"]:
        if entry["status"] == "changed":
            assert entry["sources"]["stock"] is True
            assert entry["sources"]["tune24_swept"] or entry["sources"]["tune24_sheet"]


def test_no_source_disagreements(delta):
    disagreements = [e for e in delta["parameters"]
                     if e.get("source_agreement") == "DISAGREE"]
    assert disagreements == [], (
        f"{len(disagreements)} parameters where the swept and hand-entered #24 "
        "readings disagree on whether #24 changed them"
    )


def test_change_count_is_exactly_eleven(delta):
    """Guards the headline result against a silent regression in the differ."""
    assert delta["summary"]["by_status"]["changed"] == 11
    assert delta["summary"]["by_kind_status"]["scalar/changed"] == 5
    assert delta["summary"]["by_kind_status"]["table/changed"] == 6


def test_no_swept_scalar_changed(delta):
    """All 369 scalars the #24 sweep covers are byte-identical to stock.

    Every scalar change #24 made is in the axle/tire group, which that sweep
    does not contain at all.
    """
    changed = [e for e in delta["parameters"]
               if e["status"] == "changed" and e["kind"] == "scalar"]
    assert all(e["sources"]["tune24_sheet"] for e in changed)


def test_delta_file_is_current(delta):
    """The committed artefact must match what the differ produces now."""
    assert build_delta()["summary"] == delta["summary"]
