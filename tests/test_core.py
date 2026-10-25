"""Headless tests for calspec + logbin. Run: python -m pytest tests/ -q"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openobd.calspec import Axis, Calibration, Scalar, Table
from openobd import logbin
from openobd import seed_2010_silverado as seed


# --------------------------------------------------------------------------- #
# calspec
# --------------------------------------------------------------------------- #
def test_axis_index_of_ascending():
    a = Axis("RPM", "RPM", [800, 1600, 2400, 3200, 4000])
    assert a.index_of(800) == 0
    assert a.index_of(1500) == 0
    assert a.index_of(1600) == 1
    assert a.index_of(4100) == 4      # clamps to top cell
    assert a.index_of(500) == 0       # clamps to bottom cell


def test_axis_single_cell():
    a = Axis("x", "", [0])
    assert a.index_of(999) == 0


def test_table_validate_and_diff():
    t = Table(
        name="t",
        x_axis=Axis("x", "", [0, 1, 2]),
        values=[[1.0, 2.0, 3.0]],
        stock_values=[[1.0, 9.0, 3.0]],
    )
    assert t.validate() == []
    assert t.cell_changed(0, 0) is False
    assert t.cell_changed(0, 1) is True


def test_table_validate_catches_shape():
    t = Table(name="t", x_axis=Axis("x", "", [0, 1, 2]), values=[[1.0, 2.0]])
    assert t.validate()  # non-empty == errors


def test_calibration_roundtrip(tmp_path):
    cal = seed.build_with_labels()
    p = tmp_path / "rt.cal.json"
    cal.save(str(p))
    back = Calibration.load(str(p))
    assert len(back.tables) == len(cal.tables)
    assert back.table("WOT Shift Speed — Normal").values == \
        cal.table("WOT Shift Speed — Normal").values
    assert back.metadata["shift_events"] == seed.SHIFT_EVENTS


def test_seed_values_match_change_sheet():
    cal = seed.build()
    normal = cal.table("WOT Shift Speed — Normal")
    # 1-2 upshift: stock 33 -> new 35 ; 2-3: 65 -> 57
    assert normal.stock_values[0][0] == 33 and normal.values[0][0] == 35
    assert normal.stock_values[0][1] == 65 and normal.values[0][1] == 57
    fdr = cal.scalar("Final Drive Ratio - Trans")
    assert fdr.value == 4.11 and fdr.stock_value == 3.08 and fdr.param_id == 5004


# --------------------------------------------------------------------------- #
# logbin — channel mapping
# --------------------------------------------------------------------------- #
def test_map_channel():
    assert logbin.map_channel("Engine Speed (RPM)") == "rpm"
    assert logbin.map_channel("Vehicle Speed (SAE)") == "vss"
    assert logbin.map_channel("Knock Retard") == "knock_retard"
    assert logbin.map_channel("Trans. Fluid Temperature") == "tft"
    assert logbin.map_channel("Totally Unknown Channel") is None


# --------------------------------------------------------------------------- #
# logbin — VCM Scanner CSV parse
# --------------------------------------------------------------------------- #
VCM_CSV = """[Log Information]
Vehicle,2010 Silverado
[Channel Information]
1,2,3
Engine Speed,Vehicle Speed,Knock Retard
RPM,mph,deg
[Channel Data]
800,0,0
1600,25,0
2400,45,2.5
3200,60,0
"""


def test_parse_vcm_scanner():
    log = logbin.parse_csv(VCM_CSV, "test")
    assert log.n_samples == 4
    assert log.has("rpm") and log.has("vss") and log.has("knock_retard")
    assert log.series("rpm") == [800, 1600, 2400, 3200]
    rep = logbin.analyze_log(log)
    assert rep.max_knock_retard == 2.5
    assert len(rep.knock_events) == 1
    assert rep.knock_events[0]["retard_deg"] == 2.5


def test_parse_plain_csv():
    plain = "RPM,MAP,Short Term Fuel Trim\n800,30,-2\n2000,60,3\n"
    log = logbin.parse_csv(plain)
    assert log.n_samples == 2
    assert log.has("rpm") and log.has("stft")


# --------------------------------------------------------------------------- #
# logbin — overlay binning against a real seed table
# --------------------------------------------------------------------------- #
def test_bin_log_to_table_counts():
    # A 1-D table with a 5-cell x axis on RPM.
    t = Table(name="t", x_axis=Axis("RPM", "RPM", [0, 1000, 2000, 3000, 4000]),
              values=[[0, 0, 0, 0, 0]])
    # Build a log with RPM samples that fall in known cells.
    plain = "RPM\n" + "\n".join(str(v) for v in [500, 1500, 1500, 2500, 4200])
    log = logbin.parse_csv(plain)
    ov = logbin.bin_log_to_table(log, t, x_channel="rpm")
    counts = ov.count_grid()[0]
    # 500->cell0, 1500->cell1 x2, 2500->cell2, 4200->cell4
    assert counts == [1, 2, 1, 0, 1]
    assert ov.total_binned == 5
    assert ov.hottest_cell()[2] == 2  # the busiest cell has 2 hits


def test_bin_value_mean():
    t = Table(name="t", x_axis=Axis("RPM", "RPM", [0, 2000]), values=[[0, 0]])
    plain = "RPM,Knock Retard\n1000,1.0\n1500,3.0\n3000,0.0\n"
    log = logbin.parse_csv(plain)
    ov = logbin.bin_log_to_table(log, t, x_channel="rpm", value_channel="knock_retard")
    means = ov.mean_grid()[0]
    assert means[0] == 2.0     # (1.0 + 3.0)/2 in cell 0
    assert means[1] == 0.0     # 3000 -> cell 1


# --------------------------------------------------------------------------- #
# logbin — observed shift-point detection
# --------------------------------------------------------------------------- #
def test_detect_shift_via_gear_channel():
    # gear goes 1->2 at the RPM peak; pedal is WOT
    lines = ["Time,Engine Speed,Vehicle Speed,Current Gear,Pedal Normalized"]
    data = [
        (0.0, 3000, 20, 1, 95),
        (0.1, 5400, 30, 1, 95),   # peak in 1st just before shift
        (0.2, 3800, 31, 2, 95),   # now in 2nd
        (0.3, 5400, 45, 2, 95),   # peak in 2nd
        (0.4, 3900, 46, 3, 95),   # now in 3rd
    ]
    for t, r, v, g, a in data:
        lines.append(f"{t},{r},{v},{g},{a}")
    log = logbin.parse_csv("\n".join(lines))
    shifts = logbin.detect_shift_points(log)
    assert len(shifts) == 2
    s = shifts[0]
    assert s.from_gear == 1 and s.to_gear == 2
    assert s.rpm == 5400 and s.wot is True


def test_detect_shift_inferred_rpm_drop():
    # no gear channel: RPM drops sharply while VSS keeps climbing
    lines = ["Time,Engine Speed,Vehicle Speed,Pedal Normalized"]
    seq = [(0.0, 3000, 20, 90), (0.1, 5300, 30, 90),
           (0.2, 3600, 31, 90), (0.3, 4200, 33, 90)]
    for t, r, v, a in seq:
        lines.append(f"{t},{r},{v},{a}")
    log = logbin.parse_csv("\n".join(lines))
    shifts = logbin.detect_shift_points(log)
    assert len(shifts) == 1
    assert shifts[0].inferred is True
    assert shifts[0].rpm == 5300 and shifts[0].wot is True
