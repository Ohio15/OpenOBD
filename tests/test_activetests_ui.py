"""Active Tests tab UX (v0.38.0): workspace chrome, verdict chips and banners,
read-failure honesty, the display-only filter and Show selector, shortcut
routing, the copy report, persisted settings and the min-width budget.

Offscreen. Every test runs against its own QSettings INI tree and its own
temp truck-mcp roots: ctljournal.control_root_candidates is redirected so the
real field kit / dev checkout journals are never read or written. The page is
read-only by design; these tests also assert it exposes no control action."""
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ACTIVETESTS_MAX_MIN_WIDTH = 700


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def env(tmp_path, monkeypatch, qapp):
    from PySide6.QtCore import QSettings
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope,
                      str(tmp_path / "settings"))
    roots = [tmp_path / "fieldkit", tmp_path / "checkout"]
    for r in roots:
        r.mkdir()
    from openobd import ctljournal
    monkeypatch.setattr(ctljournal, "control_root_candidates",
                        lambda: list(roots))
    pages = []

    def make():
        from openobd.activetests import ActiveTestsPage
        pg = ActiveTestsPage()
        pg.resize(1200, 700)
        pages.append(pg)
        return pg

    yield SimpleNamespace(roots=roots, make=make, app=qapp,
                          monkeypatch=monkeypatch)
    for pg in pages:
        pg.shutdown()
        pg.deleteLater()
    qapp.processEvents()


def _journal(root: Path, entries) -> Path:
    from openobd.ctljournal import JOURNAL_NAME
    p = root / "control" / JOURNAL_NAME
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write((e if isinstance(e, str) else json.dumps(e)) + "\n")
    return p


NOW = time.time()
CLEAN = [
    {"at": NOW - 60, "event": "arm", "module": "BCM", "cpid": "0x10"},
    {"at": NOW - 50, "event": "activate", "module": "BCM", "cpid": "0x10",
     "release": "AE 10 00"},
    {"at": NOW - 40, "event": "released", "module": "BCM", "cpid": "0x10"},
]
OUTSTANDING = [
    {"at": NOW - 30, "event": "activate", "module": "ECM", "cpid": "0x2A",
     "release": "AE 2A 00 00"},
]


def _chip(page, text):
    return [k for t, k in page.crumb.chips() if t == text]


# -- verdict chips / banners ------------------------------------------------- #
def test_clean_chip_no_banner(env):
    _journal(env.roots[0], CLEAN)
    pg = env.make()
    assert pg.crumb.path_text() == "Active Tests › Control journal"
    assert _chip(pg, "clean") == ["ok"]
    assert _chip(pg, "1 journal") == ["neutral"]
    assert _chip(pg, "3 records") == ["neutral"]
    assert pg.banner.isHidden()
    assert pg.explainer.text().startswith("No executable vehicle controls")
    note = pg.crumb.note.full_text()
    assert note.startswith("as of ") and str(env.roots[0]) in note


def test_outstanding_chip_and_danger_banner(env):
    path = _journal(env.roots[0], CLEAN + OUTSTANDING)
    pg = env.make()
    assert _chip(pg, "CONTROL LEFT ACTIVE") == ["danger"]
    assert not pg.banner.isHidden()
    assert pg.banner.property("banner") == "danger"
    text = pg.banner.text()
    for want in ("ECM", "0x2A", "AE 2A 00 00", str(path),
                 "OpenOBD will not send the release",
                 "truck-mcp recovery"):
        assert want in text


def test_unknown_chip_and_warning_banner(env):
    pg = env.make()                                   # no journal anywhere
    assert _chip(pg, "journal unknown") == ["warning"]
    assert pg.banner.property("banner") == "warning"
    assert "No control journal located" in pg.banner.text()


def test_damaged_journal_is_unknown_not_clean(env):
    _journal(env.roots[0], CLEAN + ["{not json"])
    pg = env.make()
    assert _chip(pg, "journal unknown") == ["warning"]
    assert not _chip(pg, "clean")
    assert "damaged" in pg.sources_text.text()


def test_read_failure_keeps_table_but_never_as_current(env):
    from openobd import ctljournal
    _journal(env.roots[0], CLEAN)
    pg = env.make()
    good_note = pg.crumb.note.full_text()
    good_time = good_note.split()[2]
    rows_before = pg.visible_rows()

    def boom():
        raise OSError("disk <b>gone</b>")
    env.monkeypatch.setattr(ctljournal, "control_state", boom)
    pg.refresh()
    assert pg.verdict() == "read failed"
    assert _chip(pg, "read failed") == ["danger"]
    assert not _chip(pg, "clean")                     # never a stale all-clear
    note = pg.crumb.note.full_text()
    assert "Read failed: disk <b>gone</b>" in note
    assert f"last good read {good_time}" in note
    assert pg.banner.property("banner") == "danger"
    assert "last good read" in pg.banner.text() and "not current" in pg.banner.text()
    assert pg.visible_rows() == rows_before           # old table kept


def test_read_failure_keeps_outstanding_visible(env):
    from openobd import ctljournal
    _journal(env.roots[0], OUTSTANDING)
    pg = env.make()
    env.monkeypatch.setattr(ctljournal, "control_state",
                            lambda: (_ for _ in ()).throw(RuntimeError("x")))
    pg.refresh()
    assert _chip(pg, "read failed") == ["danger"]
    assert _chip(pg, "CONTROL LEFT ACTIVE") == ["danger"]
    assert "OpenOBD will not send the release" in pg.banner.text()


def test_first_read_failure(env):
    from openobd import ctljournal
    env.monkeypatch.setattr(ctljournal, "control_state",
                            lambda: (_ for _ in ()).throw(OSError("denied")))
    pg = env.make()
    assert pg.verdict() == "read failed"
    assert "no successful read yet" in pg.crumb.note.full_text()
    assert pg.visible_rows() == []


# -- table ------------------------------------------------------------------- #
def test_table_newest_first_columns_and_colours(env):
    _journal(env.roots[0], CLEAN)
    pg = env.make()
    rows = pg.visible_rows()
    assert [r[1] for r in rows] == ["released", "activate", "arm"]
    assert rows[1][2:4] == ["BCM", "0x10"]
    assert rows[1][4] == "release=AE 10 00"
    assert len(rows[0][0]) == len("YYYY-MM-DD HH:MM:SS")   # date on first row
    assert len(rows[1][0]) == len("HH:MM:SS")              # same day: time only
    kinds = [pg.tree.topLevelItem(i).data(1, 0x0100) for i in range(3)]
    assert kinds == ["ok", "danger", "neutral"]
    from openobd import theme
    assert (pg.tree.topLevelItem(1).foreground(1).color()
            == theme.qcolor("danger-text"))


def test_empty_state_row(env):
    _journal(env.roots[0], [])
    pg = env.make()
    assert pg.tree.topLevelItemCount() == 1
    assert pg.tree.topLevelItem(0).text(0) == "No journal entries"
    assert pg.visible_rows() == []


def test_filter_is_display_only_and_esc_clears(env):
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent
    _journal(env.roots[0], CLEAN + OUTSTANDING)
    pg = env.make()
    total = pg._state.total_entries
    pg.filter.setText("ecm")
    assert [r[2] for r in pg.visible_rows()] == ["ECM"]
    assert pg.tree.topLevelItemCount() == 4           # rows hidden, not dropped
    assert pg._state.total_entries == total
    assert pg.verdict() == "outstanding"
    pg.eventFilter(pg.filter, QKeyEvent(QEvent.KeyPress, Qt.Key_Escape,
                                        Qt.NoModifier))
    assert pg.filter.text() == "" and len(pg.visible_rows()) == 4


def test_show_combo_modes_and_persist(env):
    from openobd.activetests import SHOW_KEY
    from openobd.appsettings import app_settings
    old = {"at": NOW - 3 * 86400, "event": "fire_refused:no_lease",
           "module": "TCM", "cpid": "0x01"}
    _journal(env.roots[0], [old] + CLEAN)
    pg = env.make()
    assert len(pg.visible_rows()) == 4
    pg.show_combo.setCurrentIndex(pg.show_combo.findData("controls"))
    assert sorted(r[1] for r in pg.visible_rows()) == [
        "activate", "fire_refused:no_lease"]
    assert app_settings().value(SHOW_KEY) == "controls"
    pg.show_combo.setCurrentIndex(pg.show_combo.findData("24h"))
    assert "TCM" not in [r[2] for r in pg.visible_rows()]
    assert pg.verdict() == "clean"                    # the read is unchanged
    pg2 = env.make()
    assert pg2.show_mode() == "24h"


def test_hostile_strings_render_literally(env):
    evil = "<img src=x onerror=alert(1)><b>BCM</b>"
    _journal(env.roots[0], [{"at": NOW, "event": "arm", "module": evil,
                             "cpid": "<i>1</i>", "note": "<a href=x>y</a>"}])
    pg = env.make()
    row = pg.visible_rows()[0]
    assert row[2] == evil and row[3] == "<i>1</i>"
    assert row[4] == "note=<a href=x>y</a>"
    from openobd.tuningui import plain_tooltip
    assert pg.tree.topLevelItem(0).toolTip(4).startswith("<qt>")
    assert plain_tooltip("<a href=x>y</a>").startswith("<qt>")


def test_fed_labels_are_plain_text(env):
    from PySide6.QtCore import Qt
    pg = env.make()
    for lab in (pg.banner, pg.sources_text, pg.explainer, pg.crumb.note,
                pg.crumb.title):
        assert lab.textFormat() == Qt.PlainText


def test_journal_sources_panel_and_persist(env):
    from openobd.activetests import SOURCES_COLLAPSED_KEY
    from openobd.appsettings import app_settings
    _journal(env.roots[1], CLEAN)
    pg = env.make()
    lines = pg.sources_text.text().splitlines()
    assert lines[0].startswith("no journal") and str(env.roots[0]) in lines[0]
    assert lines[1].startswith("journal") and "3 records" in lines[1]
    assert not pg.sources.is_expanded()               # collapsed by default
    pg.sources.set_expanded(True)
    assert app_settings().value(SOURCES_COLLAPSED_KEY) == "false"
    assert env.make().sources.is_expanded()


def test_settings_keys_are_only_activetests(env):
    from openobd.appsettings import app_settings
    pg = env.make()
    pg.show_combo.setCurrentIndex(1)
    pg.sources.set_expanded(True)
    s = app_settings()
    s.sync()
    assert sorted(k for k in s.allKeys() if "activetests" in k) == [
        "activetests/show", "activetests/sources_collapsed"]


def test_copy_report_content(env):
    from PySide6.QtWidgets import QApplication
    path = _journal(env.roots[0], CLEAN + OUTSTANDING)
    pg = env.make()
    pg.filter.setText("ecm")
    pg.copy_report()
    text = QApplication.clipboard().text()
    assert "Verdict: CONTROL LEFT ACTIVE" in text
    assert "AE 2A 00 00" in text and str(path) in text
    assert "OpenOBD will not send the release" in text
    assert "Entries shown (1, newest first):" in text   # what is on screen
    assert "Last successful read:" in text


# -- lifecycle / shortcuts --------------------------------------------------- #
def test_f5_and_polling_only_while_showing(env):
    _journal(env.roots[0], CLEAN)
    pg = env.make()
    assert not pg._f5.isEnabled() and not pg._poll.isActive()
    assert not _chip(pg, "Polling")
    pg.show()
    env.app.processEvents()
    assert pg._f5.isEnabled() and pg._poll.isActive()
    assert _chip(pg, "Polling") == ["muted"]
    pg.hide()
    env.app.processEvents()
    assert not pg._f5.isEnabled() and not pg._poll.isActive()
    assert not _chip(pg, "Polling")


def test_read_only_no_control_actions(env):
    pg = env.make()
    labels = [a.text().lower() for a in pg.toolbar.actions() if a.text()]
    assert labels == ["refresh", "copy report"]
    import openobd.activetests as mod
    src = Path(mod.__file__).read_text(encoding="utf-8")
    assert "ControlLease" not in src and "import truck" not in src


def test_ctrl_f_routes_to_journal_filter(env, monkeypatch):
    import openobd.app as app_mod
    from openobd.app import load_default_cal
    monkeypatch.setattr(app_mod, "detect_gt_ports", lambda: [])
    cal, path = load_default_cal()
    win = app_mod.MainWindow(cal, path)
    try:
        win.show()
        win.main_tabs.setCurrentIndex(4)
        env.app.processEvents()
        assert win.main_tabs.currentWidget() is win.active_tests
        assert win._find_shortcut.isEnabled()
        win.focus_table_search()
        env.app.processEvents()
        assert win.active_tests.filter.hasFocus()
        win.main_tabs.setCurrentIndex(0)
        assert not win._find_shortcut.isEnabled()
    finally:
        win.close()
        win.deleteLater()
        env.app.processEvents()


def test_min_width_within_budget(env):
    _journal(env.roots[0], CLEAN + OUTSTANDING)
    pg = env.make()
    pg.sources.set_expanded(True)
    env.app.processEvents()
    width = pg.minimumSizeHint().width()
    assert width < ACTIVETESTS_MAX_MIN_WIDTH, (
        f"Active Tests minimumSizeHint width {width} px; through QTabWidget "
        "this pins the whole main window")
