"""GT mode-22 DIDs reach the gauges and the CSV only once verified live.

2026-10-25: ECM DID 1644 byte1, shown as "Trans Fluid Temp (F)" since commit
ee0189f, read raw 0x83 (195.8 F) on all 1463 rows of a 2107 s drive while
coolant rose 127 -> 203 F. A constant presented as a live gauge and CSV column
is a fake reading. These tests keep it (and anything else that failed
verification) out of the polled table, the channel list and the log header.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openobd import gt as gtmod  # noqa: E402
from openobd.gt import ObdxGt  # noqa: E402
from openobd.recorder import DriveRecorder  # noqa: E402
from openobd.transport import GtDataSource, Sample  # noqa: E402

from test_module_map_truck import FakeTruckGt  # noqa: E402


class StaticDidGt(FakeTruckGt):
    """The truck as measured: the ECM answers 22 1644 with 62 1644 00 83 on
    every request, whatever the engine is doing."""

    def _answer(self, cmd):
        if cmd == "221644" and self.header == "7E0":
            return self._frame("7E8", "0562164400 83".replace(" ", ""))
        return super()._answer(cmd)


@pytest.fixture
def static_gt(monkeypatch):
    monkeypatch.setattr(gtmod.time, "sleep", lambda s: None)
    g = ObdxGt(port="FAKE")
    g.ser = StaticDidGt()
    g.read_deadline_s = 0.02
    return g


def test_no_rejected_did_is_polled():
    assert not set(gtmod.DID_TABLE) & set(gtmod.REJECTED_DIDS)
    assert (None, "1644") in gtmod.REJECTED_DIDS
    assert gtmod.REJECTED_DIDS[(None, "1644")][0] == "tft"


def test_rejected_entries_carry_their_evidence():
    for (module, did), (key, why) in gtmod.REJECTED_DIDS.items():
        assert len(did) == 4 and int(did, 16) >= 0
        assert key and len(why) > 40, (module, did)


def test_the_gt_cannot_surface_trans_temp_without_a_verified_source():
    # tft comes back only by adding a verified DID_TABLE entry for it.
    did_keys = {key for key, _fn in gtmod.DID_TABLE.values()}
    pid_keys = {d.key for d in gtmod.PID_TABLE.values()}
    assert "tft" not in pid_keys
    assert ("tft" in gtmod.CANONICAL_KEYS) == ("tft" in did_keys)
    assert "tft" not in gtmod.CANONICAL_KEYS
    assert "tft" not in ObdxGt(port="X").possible_keys()


def test_poll_never_requests_1644_nor_reports_its_constant(static_gt):
    # Sanity: the double does answer 1644 when asked directly.
    assert static_gt.request_did("1644") == [0x00, 0x83]
    static_gt.ser.log.clear()
    out = static_gt.poll_once()
    assert "221644" not in static_gt.ser.sent()
    assert "tft" not in out
    assert 195.8 not in [round(v, 1) for v in out.values()]


def test_a_gt_recording_has_no_trans_temp_column(tmp_path):
    from openobd.app import RECORD_LABELS, RECORD_ORDER
    src = GtDataSource()
    keys = [k for k in RECORD_ORDER if k in set(src.recordable_channels())]
    assert "tft" not in keys and "rpm" in keys
    rec = DriveRecorder(keys, RECORD_LABELS, [str(tmp_path)])
    rec.write([Sample(1.0, {"rpm": 700.0})])
    rec.close()
    with open(rec.path, encoding="utf-8") as fh:
        header = fh.readline()
    assert "Trans Fluid Temp" not in header
    assert "Engine Speed (RPM)" in header
