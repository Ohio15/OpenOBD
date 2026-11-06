"""DeferredPref (appsettings): a drag-driven preference is synced to disk
once per drag, PREF_DEBOUNCE_MS after the last change, never per event; the
value is read at write time; flush() writes now and cancels the timer; the
timer dies with its parent widget."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QObject, QSettings, QTimer  # noqa: E402

from openobd import appsettings as aps  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _wait_ms(ms):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


@pytest.fixture
def store(tmp_path):
    return QSettings(str(tmp_path / "pref.ini"), QSettings.IniFormat)


def test_a_burst_is_written_once_after_the_window(qapp, store):
    values = iter([b"v1", b"v2", b"v3"])
    reads = []

    def value_of():
        v = next(values)
        reads.append(v)
        return v
    d = aps.DeferredPref(store, "ui/split", value_of, None, delay_ms=60)
    for _ in range(5):
        d.touch(123, 1)                    # signal args are ignored
    assert d.pending
    assert reads == []                     # nothing read, nothing written
    assert store.value("ui/split") is None
    _wait_ms(30)
    assert store.value("ui/split") is None  # still inside the window
    d.touch()                              # a late move restarts the window
    _wait_ms(40)
    assert store.value("ui/split") is None
    _wait_ms(80)
    assert not d.pending
    assert reads == [b"v1"]                # one read, at write time
    assert bytes(store.value("ui/split")) == b"v1"


def test_flush_writes_now_and_cancels_the_pending_write(qapp, store,
                                                        monkeypatch):
    calls = []
    real = aps.save_pref
    monkeypatch.setattr(aps, "save_pref",
                        lambda st, k, v: calls.append(v) or real(st, k, v))
    d = aps.DeferredPref(store, "ui/split", lambda: b"now", None, delay_ms=60)
    d.touch()
    assert d.flush() is True
    assert not d.pending
    assert calls == [b"now"]
    _wait_ms(120)
    assert calls == [b"now"]               # the timer did not fire again
    assert d.flush() is True               # flush without a touch is a write
    assert calls == [b"now", b"now"]


def test_flush_reports_a_failed_write_like_save_pref(qapp, store,
                                                     monkeypatch):
    monkeypatch.setattr(aps, "save_pref", lambda st, k, v: False)
    d = aps.DeferredPref(store, "ui/split", lambda: b"x", None, delay_ms=60)
    d.touch()
    assert d.flush() is False


def test_the_timer_dies_with_its_parent(qapp, store):
    import shiboken6
    parent = QObject()
    d = aps.DeferredPref(store, "ui/split", lambda: b"x", parent, delay_ms=30)
    d.touch()
    shiboken6.delete(parent)
    assert not shiboken6.isValid(d)
    _wait_ms(90)
    assert store.value("ui/split") is None  # no write after the widget is gone


def test_default_window_is_the_documented_constant():
    assert aps.PREF_DEBOUNCE_MS == 300
