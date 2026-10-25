"""Layout regression: the Live Data page must not pin the main window's width.

QTabWidget's minimum width is the max over its pages, so any page whose
minimumSizeHint is wide sets the floor for the WHOLE window. Live Data used to
report 1522 px once a session was bound: the session picker sized itself to its
longest label and the status sentence ("VIN · source · as of … (note)") never
wrapped, both on one row. These tests bind a real (synthetic) truck-mcp session
with a deliberately long label and hold the page to a 1000 px budget, while
checking that no information was hidden to get there.
"""
import os
import sqlite3
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_tmstore import _make_session  # noqa: E402

# Generous but meaningful: the page measured 1522 px before the fix and ~936 px
# after (offscreen font metrics). Anything that pushes it past this pins the
# window wider than a typical laptop screen again.
LIVEDATA_MAX_MIN_WIDTH = 1000
LONG_LABEL = ("highway pull 3rd-gear WOT with TCC lockup and trailer "
              "brake-controller logging enabled")


@pytest.fixture(scope="module")
def page(tmp_path_factory):
    """A bound LiveDataPage, isolated from the user's real settings and from
    any real truck-mcp data root (both redirected BEFORE the page is built)."""
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    root = tmp_path_factory.mktemp("livedata_layout")
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope,
                      str(root / "settings"))
    old_data = os.environ.get("TRUCK_MCP_DATA")
    os.environ["TRUCK_MCP_DATA"] = str(root / "truck-mcp")
    sessions = root / "truck-mcp" / "sessions"
    sessions.mkdir(parents=True)
    db = sessions / "layout.tmsession.db"
    _make_session(db)
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE session SET label = ?", (LONG_LABEL,))
    conn.commit()
    conn.close()

    app = QApplication.instance() or QApplication([])
    from openobd.livedata import LiveDataPage
    pg = LiveDataPage()
    pg._tick()          # renders the full "… as of HH:MM:SS (note)" status
    app.processEvents()
    yield pg
    pg.shutdown()
    pg.deleteLater()
    app.processEvents()
    if old_data is None:
        os.environ.pop("TRUCK_MCP_DATA", None)
    else:
        os.environ["TRUCK_MCP_DATA"] = old_data


def test_bound_session_with_long_label(page):
    # Guard the premise: the width budget only means something if the long
    # label and the full status sentence are actually on screen.
    assert page._picker.count() == 1
    assert LONG_LABEL in page._picker.itemText(0)
    assert "3GCRKTE35AG150432" in page._status.text()
    assert "as of" in page._status.text()


def test_livedata_min_width_within_budget(page):
    width = page.minimumSizeHint().width()
    assert width <= LIVEDATA_MAX_MIN_WIDTH, (
        f"Live Data minimumSizeHint width {width} px exceeds "
        f"{LIVEDATA_MAX_MIN_WIDTH} px; through QTabWidget this pins the "
        f"whole main window")


def test_picker_does_not_size_to_longest_label(page):
    full = page._picker.fontMetrics().horizontalAdvance(page._picker.itemText(0))
    assert page._picker.minimumSizeHint().width() < full


def test_full_session_label_still_readable(page):
    text = page._picker.itemText(0)
    from PySide6.QtCore import Qt
    assert page._picker.toolTip() == text
    assert page._picker.itemData(0, Qt.ToolTipRole) == text
    view = page._picker.view()
    assert view.textElideMode() == Qt.ElideNone
    assert view.minimumWidth() >= view.sizeHintForColumn(0)


def test_status_wraps_instead_of_widening(page):
    status = page._status
    assert status.wordWrap()
    one_line = status.fontMetrics().horizontalAdvance(status.text())
    assert status.minimumSizeHint().width() < one_line
