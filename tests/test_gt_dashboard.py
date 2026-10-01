"""The Dashboard's GT path: identify before configuring, never serve a frozen
needle as live. 2026-10-01: after an e38flash read the GT stays in its binary
J2534 mode; the Module Map caught that, but the Dashboard's ObdxGt.open()
configured the GT blind, used the binary AT@1 reply as the device name, and
the poll loop swallowed every exception, so the Dashboard sat empty (or frozen
on old values) with no explanation."""
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openobd import gt as gtmod
from openobd.gt import GtBinaryMode, ObdxGt
from openobd.transport import GtDataSource

from test_module_map_truck import FakeTruckGt


@pytest.fixture
def opened(monkeypatch):
    """open() a GT whose serial port is a FakeTruckGt built from **kw."""
    monkeypatch.setattr(gtmod.time, "sleep", lambda s: None)

    def factory(**kw):
        fake = FakeTruckGt(**kw)
        monkeypatch.setattr(gtmod, "serial",
                            SimpleNamespace(Serial=lambda *a, **k: fake))
        g = ObdxGt(port="FAKE")
        g.read_deadline_s = 0.02
        return g, fake
    return factory


def test_a_binary_mode_gt_is_refused_with_the_replug_remedy(opened):
    g, fake = opened(binary=True)
    with pytest.raises(GtBinaryMode) as exc:
        g.open()
    assert "binary (J2534) mode" in str(exc.value)
    assert "USB" in str(exc.value) and "10 s" in str(exc.value)
    assert fake.sent() == ["ATI"]          # identified FIRST, configured nothing
    assert g.ser is None                   # the port is released


def test_a_text_reply_that_is_not_an_elm_is_refused(opened):
    g, _fake = opened(ati=b"HELLO", at1=b"SOMETHING ELSE")
    with pytest.raises(RuntimeError, match="did not identify"):
        g.open()
    assert g.ser is None


def test_a_healthy_gt_opens_with_a_clean_device_name(opened):
    g, fake = opened()
    g.open()
    assert g.device == "OBDX Pro GT"
    assert fake.sent()[0] == "ATI" and "ATZ" in fake.sent()


@pytest.mark.parametrize("atrv, expect", [
    (b"12.6V", 12.6),
    (b"14.1 V", 14.1),
    (b"NO DATA 9V?", None),      # the old loose regex pulled 9.0 out of this
    (b"V", None),
])
def test_poll_once_reads_atrv_strictly(opened, atrv, expect):
    g, _fake = opened(atrv=atrv)
    g.open()
    assert g.poll_once().get("voltage") == expect


def _source_with(polls):
    """A GtDataSource whose GT answers the scripted polls, then stops the loop."""
    src = GtDataSource(poll_interval=0)
    script = list(polls)

    def poll_once():
        step = script.pop(0)
        if not script:
            src._stop.set()
        if isinstance(step, Exception):
            raise step
        return step
    src._gt = SimpleNamespace(poll_once=poll_once)
    src._t0 = 0.0
    src._keys = ["rpm", "voltage"]
    return src


def test_failing_polls_flip_every_gauge_to_failed_and_say_why():
    src = _source_with([{"rpm": 700.0}, OSError("port gone"), OSError("port gone")])
    src._run()
    assert src.failing()
    assert src.latest() is None                         # no frozen live needle
    assert src.channel_states() == {"rpm": "failed", "voltage": "failed"}
    msg = src.status_message()
    assert "GT not answering" in msg and "2 polls failed" in msg
    assert "OSError: port gone" in msg


def test_one_failed_poll_is_not_yet_failing():
    src = _source_with([{"rpm": 700.0}, OSError("blip")])
    src._run()
    assert not src.failing() and src.status_message() is None
    assert src.latest().values == {"rpm": 700.0}


def test_a_good_poll_recovers_the_source():
    src = _source_with([OSError("x"), OSError("x"), {"rpm": 650.0}])
    src._run()
    assert not src.failing() and src.status_message() is None
    assert src.channel_states() == {"rpm": "fresh", "voltage": "fresh"}
    assert src.latest().values == {"rpm": 650.0}
