"""Diagnostics tab UX (v0.36.0): shared chrome, module map chips / card /
tooltips, grouped DTC tree, readiness tiles, MIL + error banners, busy
state, F5 / Ctrl+F routing, minimum width and splitter persistence.

Offscreen Qt; settings redirected to throwaway INI files; no sleeps, no
hardware: GtJob.run is replaced by a recorder and results are delivered by
emitting GtJob.finished, exactly as the worker thread would."""
import os
import sys
import tempfile

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openobd import theme, vehnet  # noqa: E402
from openobd.gt import DTC_DESCRIPTIONS  # noqa: E402

SCAN = dict(port_open=True, interface_alive=True, dlc_volts=12.6,
            hs_responders={"7E8", "7EA"},
            pinged={"ecm": True, "tcm": True, "ebcm": False, "bcm": None,
                    "tccm": True})
DTCS = {"examined": True, "error": None,
        "stored": ["P0300", "P0420"], "pending": ["P0171"], "permanent": [],
        "incomplete": {"pending": {"7EA": "missing consecutive frame"}},
        "by_module": {"stored": {"7E8": ["P0300", "P0420"]},
                      "pending": {"7E8": ["P0171"]}}}
MONITORS = [("Misfire", True), ("Fuel system", True), ("Components", True),
            ("Catalyst", False), ("Heated catalyst", True),
            ("Evap system", False), ("Secondary air", True),
            ("O2 sensor", True), ("O2 heater", True), ("EGR/VVT", True)]
READY = {"mil": True, "dtc_count": 2, "monitors": MONITORS}
NOT_EXAMINED = {"examined": False, "error": "GT refused ATSH7DF",
                "stored": None, "pending": None, "permanent": None}


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope,
                      tempfile.mkdtemp(prefix="openobd_diagui_"))
    app = QApplication.instance() or QApplication([])
    theme.apply_dark_palette(app)
    return app


def ini(tmp_path, name="diag.ini"):
    from PySide6.QtCore import QSettings
    return QSettings(str(tmp_path / name), QSettings.IniFormat)


class Recorder:
    def __init__(self):
        self.fns = []

    def __call__(self, fn):
        self.fns.append(fn)


@pytest.fixture
def page(qapp, tmp_path):
    from openobd.diagui import DiagnosticsPage
    p = DiagnosticsPage(None, ini(tmp_path))
    p.job.run = Recorder()
    p.resize(1366, 700)
    p.show()
    qapp.processEvents()
    yield p
    p.close()
    p.deleteLater()


@pytest.fixture(scope="module")
def win(qapp):
    import openobd.app as A
    A.detect_gt_ports = lambda: []
    cal, _ = A.load_default_cal()
    w = A.MainWindow(cal, None)
    w.resize(1366, 768)
    w.show()
    qapp.processEvents()
    yield w
    w.dirty = False
    w._shutdown_pages()
    w.close()


def groups(page):
    """{group label: [child row dicts]} from the source model."""
    from openobd.diagui import ROW_KIND_ROLE
    out = {}
    m = page.dtc_model
    for r in range(m.rowCount()):
        g = m.item(r, 0)
        if g.data(ROW_KIND_ROLE) != "group":
            continue
        rows = []
        for c in range(g.rowCount()):
            rows.append({
                "items": [g.child(c, k) for k in range(4)],
                "text": [g.child(c, k).text() for k in range(4)],
                "row": g.child(c, 0).data(ROW_KIND_ROLE)})
        out[g.text()] = rows
    return out


def summary_texts(page):
    from openobd.diagui import ROW_KIND_ROLE
    m = page.dtc_model
    return [m.item(r, 1).text() for r in range(m.rowCount())
            if m.item(r, 0).data(ROW_KIND_ROLE) == "summary"]


def group_index(page, prefix):
    m = page.dtc_model
    for r in range(m.rowCount()):
        if m.item(r, 0).text().startswith(prefix):
            return page.dtc_proxy.mapFromSource(m.index(r, 0))
    raise AssertionError(prefix)


# --------------------------------------------------------------------------- #
# Codes tree
# --------------------------------------------------------------------------- #
def test_grouping_counts_and_default_expansion(page):
    page.populate_codes(DTCS, READY)
    g = groups(page)
    assert list(g) == ["Stored (2)", "Pending (1 + incomplete)",
                       "Permanent (0)"]
    assert [r["text"][0] for r in g["Stored (2)"]] == ["P0300", "P0420"]
    assert [r["text"][3] for r in g["Stored (2)"]] == ["Stored", "Stored"]
    pend = g["Pending (1 + incomplete)"]
    assert [r["row"] for r in pend] == ["incomplete", "code"]
    assert page.dtc_tree.isExpanded(group_index(page, "Stored"))
    # the pending group holds an incomplete row: unknowns stay on screen
    assert page.dtc_tree.isExpanded(group_index(page, "Pending"))
    assert not page.dtc_tree.isExpanded(group_index(page, "Permanent"))
    assert summary_texts(page) == []
    # status chips: stored danger, pending warning
    from openobd.tuningui import CHIP_KIND_ROLE
    assert g["Stored (2)"][0]["items"][3].data(CHIP_KIND_ROLE) == "danger"
    assert pend[1]["items"][3].data(CHIP_KIND_ROLE) == "warning"


def test_permanent_codes_are_muted_chips_and_collapsed(page):
    from openobd.tuningui import CHIP_KIND_ROLE
    page.populate_codes({"stored": [], "pending": [],
                         "permanent": ["P0420"]}, READY)
    g = groups(page)
    assert g["Permanent (1)"][0]["items"][3].data(CHIP_KIND_ROLE) == "muted"
    assert not page.dtc_tree.isExpanded(group_index(page, "Permanent"))
    assert summary_texts(page) == []


def test_not_examined_rows_are_present_and_styled(page):
    from openobd.tuningui import CHIP_KIND_ROLE
    page.populate_codes(NOT_EXAMINED, {"error": "GT refused ATSH7E0"})
    g = groups(page)
    assert list(g) == ["Stored (not examined)", "Pending (not examined)",
                       "Permanent (not examined)"]
    for rows in g.values():
        assert len(rows) == 1
        r = rows[0]
        assert r["row"] == "not_examined"
        assert r["text"][1] == "Not examined — GT refused ATSH7DF"
        assert r["text"][3] == "not examined"
        assert r["items"][3].data(CHIP_KIND_ROLE) == "muted"
        assert all(it.font().italic() for it in r["items"])
        assert (r["items"][1].foreground().color()
                == theme.diag_qcolor("not-examined-text"))
        assert not r["items"][0].icon().isNull()
    # never "No trouble codes", never a clean MIL
    assert summary_texts(page) == []
    chips = page.codes_crumb.chips()
    assert ("MIL not examined", "muted") in chips
    for kind in ("Stored", "Pending", "Permanent"):
        assert (f"{kind} not examined", "muted") in chips
    assert not page.mil_banner.isHidden()
    assert page.mil_banner.property("banner") == "muted"
    assert "GT refused ATSH7E0" in page.mil_banner.text()
    # every group is opened: nothing unknown hides in a collapsed node
    for prefix in ("Stored", "Pending", "Permanent"):
        assert page.dtc_tree.isExpanded(group_index(page, prefix))


def test_not_examined_without_error_says_no_module_answered(page):
    page.populate_codes({"stored": None, "pending": [], "permanent": []},
                        READY)
    rows = groups(page)["Stored (not examined)"]
    assert rows[0]["text"][1] == "Not examined — no module answered"
    assert summary_texts(page) == ["No codes in the examined kinds"]


def test_incomplete_rows_keep_exact_semantics(page):
    from openobd.tuningui import CHIP_KIND_ROLE
    page.populate_codes(
        {"examined": True, "stored": ["P0711"], "pending": [],
         "permanent": [],
         "incomplete": {"stored": {"7E8": "truncated: 6 of 8 bytes"}}},
        READY)
    rows = groups(page)["Stored (1 + incomplete)"]
    inc = [r for r in rows if r["row"] == "incomplete"]
    assert len(inc) == 1
    assert inc[0]["text"][1] == "Incomplete from 7E8 — truncated: 6 of 8 bytes"
    assert inc[0]["text"][2] == "ECM (E38)"
    assert inc[0]["text"][3] == "incomplete"
    assert inc[0]["items"][3].data(CHIP_KIND_ROLE) == "muted"
    assert inc[0]["items"][0].font().italic()
    assert summary_texts(page) == []
    assert ("Stored 1 + incomplete", "danger") in page.codes_crumb.chips()
    # a kind whose only answer was incomplete gets NO extra not-examined row
    page.populate_codes(
        {"stored": None, "pending": [], "permanent": [],
         "incomplete": {"stored": {"7E8": "missing consecutive frame"}}},
        READY)
    rows = groups(page)["Stored (incomplete)"]
    assert [r["row"] for r in rows] == ["incomplete"]
    assert ("Stored incomplete", "muted") in page.codes_crumb.chips()
    # zero codes + incomplete is never an ok chip
    page.populate_codes(
        {"stored": [], "pending": [], "permanent": [],
         "incomplete": {"stored": {"7E8": "missing consecutive frame"}}},
        READY)
    assert ("Stored 0 + incomplete", "muted") in page.codes_crumb.chips()
    assert summary_texts(page) == ["No codes in the examined kinds"]


def test_no_trouble_codes_logic(page):
    from openobd.tuningui import CHIP_KIND_ROLE
    from openobd.diagui import ROW_KIND_ROLE
    page.populate_codes({"stored": [], "pending": [], "permanent": []},
                        {"mil": False, "dtc_count": 0, "monitors": MONITORS})
    assert summary_texts(page) == ["No trouble codes"]
    m = page.dtc_model
    assert m.item(0, 0).data(ROW_KIND_ROLE) == "summary"   # shown first
    assert m.item(0, 3).data(CHIP_KIND_ROLE) == "ok"
    chips = page.codes_crumb.chips()
    assert ("MIL off", "ok") in chips and ("Stored 0", "ok") in chips
    # one kind unexamined: only the examined kinds can be called clean
    page.populate_codes({"stored": [], "pending": None, "permanent": []},
                        READY)
    assert summary_texts(page) == ["No codes in the examined kinds"]
    assert m.item(0, 3).data(CHIP_KIND_ROLE) == "muted"
    # codes present: no summary row at all
    page.populate_codes(DTCS, READY)
    assert summary_texts(page) == []


def test_description_fallback_is_never_blank(page):
    assert "P1234" not in DTC_DESCRIPTIONS
    page.populate_codes({"stored": ["P1234", "P0300"], "pending": [],
                         "permanent": []}, READY)
    rows = groups(page)["Stored (2)"]
    assert rows[0]["text"][1] == "No local description"
    assert rows[0]["items"][1].foreground().color() == theme.qcolor("text-muted")
    assert rows[1]["text"][1] == DTC_DESCRIPTIONS["P0300"]


def test_module_column_maps_response_ids(page):
    from openobd.diagui import module_name
    assert module_name("7e8") == "ECM (E38)"
    assert module_name("643") == "EBCM (ABS)"
    assert module_name("5EC") == "TCCM (4WD)"       # GMLAN-routed report id
    assert module_name("7EB") == "7EB"              # unidentified: raw id
    page.populate_codes(
        {"stored": ["P0300", "C0035", "P0700", "C0306"], "pending": [],
         "permanent": [],
         "by_module": {"stored": {"7E8": ["P0300", "P0700"],
                                  "7EA": ["P0700"], "643": ["C0035"]}}},
        READY)
    mods = {r["text"][0]: r["text"][2]
            for r in groups(page)["Stored (4)"]}
    assert mods == {"P0300": "ECM (E38)", "C0035": "EBCM (ABS)",
                    "P0700": "ECM (E38), TCM (T43)", "C0306": "\u2014"}


def test_search_filters_across_columns_and_esc_clears(page, qapp):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    page.populate_codes(DTCS, READY)
    page.dtc_search.setText("lean")
    assert [r["code"] for r in page.visible_rows()] == ["P0171"]
    page.dtc_search.setText("misfire")
    assert [r["code"] for r in page.visible_rows()] == ["P0300"]
    page.dtc_search.setText("ecm (e38)")                 # module column
    assert {r["code"] for r in page.visible_rows()} == {"P0300", "P0420",
                                                        "P0171"}
    page.dtc_search.setText("7ea")                       # the incomplete row
    assert [r["row"] for r in page.visible_rows()] == ["incomplete"]
    page.dtc_search.setText("pending")                   # a whole group
    assert [r["kind"] for r in page.visible_rows()] == ["Pending", "Pending"]
    assert page.dtc_tree.isExpanded(group_index(page, "Pending"))
    page.dtc_search.setText("zzz-nothing")
    assert page.visible_rows() == []
    page.tabs.setCurrentIndex(1)
    page.dtc_search.setFocus()
    QTest.keyClick(page.dtc_search, Qt.Key_Escape)
    assert page.dtc_search.text() == ""
    assert len(page.visible_rows()) == 4


def test_copy_codes_tsv(page, qapp):
    from PySide6.QtWidgets import QApplication
    assert not page.act_copy_codes.isEnabled()          # nothing read yet
    page.populate_codes(DTCS, READY)
    assert page.act_copy_codes.isEnabled()
    page.act_copy_codes.trigger()
    lines = QApplication.clipboard().text().split("\n")
    assert lines[0] == "kind\tcode\tdescription\tmodule"
    assert lines[1] == ("Stored\tP0300\t" + DTC_DESCRIPTIONS["P0300"]
                        + "\tECM (E38)")
    assert ("Pending\t?\tIncomplete from 7EA — missing consecutive frame"
            "\tTCM (T43)") in lines
    assert len(lines) == 5
    page.dtc_search.setText("P0420")                     # copies what shows
    page.act_copy_codes.trigger()
    assert QApplication.clipboard().text().split("\n")[1:] == [
        "Stored\tP0420\t" + DTC_DESCRIPTIONS["P0420"] + "\tECM (E38)"]
    page.dtc_search.clear()


def test_mil_banner_states(page):
    page.populate_codes(DTCS, READY)
    assert not page.mil_banner.isHidden()
    assert page.mil_banner.property("banner") == "danger"
    assert page.mil_banner.text() == "MIL is ON — 2 code(s)"
    assert page.codes_crumb.chips()[0] == ("MIL ON", "danger")
    page.populate_codes(DTCS, {"mil": False, "dtc_count": 0,
                               "monitors": MONITORS})
    assert page.mil_banner.isHidden()
    assert page.codes_crumb.chips()[0] == ("MIL off", "ok")
    page.populate_codes(DTCS, {"error": "GT refused ATSH7E0"})
    assert page.mil_banner.property("banner") == "muted"
    assert page.mil_banner.text() == "MIL not examined — GT refused ATSH7E0"
    page.populate_codes(DTCS, {})
    assert page.mil_banner.text() == "MIL not examined — no readiness reply"
    assert page.codes_crumb.chips()[0] == ("MIL not examined", "muted")


def test_readiness_tiles_and_summary(page):
    page.populate_codes(DTCS, READY)
    assert page.readiness_summary() == ("8/10 ready", "warning")
    assert len(page.monitor_tiles) == 10
    cat = [t for t in page.monitor_tiles if t.name.text() == "Catalyst"][0]
    assert cat.property("state") == "notready"
    assert cat.chip.text() == "Not ready"
    assert cat.chip.property("chip") == "warning"
    assert page.monitor_tiles[0].chip.property("chip") == "ok"
    all_ready = [(n, True) for n, _ in MONITORS]
    page.populate_codes(DTCS, {"mil": False, "monitors": all_ready})
    assert page.readiness_summary() == ("10/10 ready", "ok")
    assert len(page.monitor_tiles) == 10                  # rebuilt, not added
    page.populate_codes(DTCS, {"error": "no reply to 0101"})
    assert page.readiness_summary() == ("readiness not examined", "muted")
    assert page.monitor_tiles == []
    assert not page.ready_empty.isHidden()
    assert "no reply to 0101" in page.ready_empty.text()


def test_codes_note_and_header_before_and_after_read(page):
    assert page.codes_crumb.path_text() == "Diagnostics › Codes & Readiness"
    assert page.codes_crumb.chips() == [("not read", "muted")]
    page.populate_codes(DTCS, READY)
    import re
    assert re.search(r"read at \d\d:\d\d:\d\d", page.codes_crumb.note.full_text())


# --------------------------------------------------------------------------- #
# Module map
# --------------------------------------------------------------------------- #
def test_map_chips_and_legend(page):
    from openobd.diagui import LEGEND
    assert page.map_crumb.path_text() == "Diagnostics › Module Map"
    assert page.map_crumb.chips() == [("not scanned", "muted")]
    assert not page.act_copy_report.isEnabled()
    assert page.legend.labels() == [t for _s, t in LEGEND]
    page._apply_scan(vehnet.ScanResult(**SCAN), None)
    chips = page.map_crumb.chips()
    assert chips[0] == ("DLC 12.6 V", "neutral")
    assert ("3 responding", "ok") in chips
    assert ("1 not examined", "muted") in chips
    assert ("fault at: EBCM (ABS)", "danger") in chips
    assert page.map_banner.isHidden()
    assert page.act_copy_report.isEnabled()
    rep = page.scan_report()
    assert "Fault at: EBCM (ABS)" in rep
    assert "EBCM (ABS)\tHS-GMLAN (500k)\t243/643\tno response" in rep


def test_map_chips_low_voltage_and_unreadable(page):
    page._apply_scan(vehnet.ScanResult(**dict(SCAN, dlc_volts=3.1)), None)
    chips = page.map_crumb.chips()
    assert chips[0] == ("DLC 3.1 V", "danger")
    assert ("fault at: DLC power", "danger") in chips
    page._apply_scan(vehnet.ScanResult(**dict(SCAN, dlc_volts=None)), None)
    assert page.map_crumb.chips()[0] == ("DLC not read", "muted")


def test_module_card_on_click_and_tooltips(page, qapp):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    assert page.card.empty.isVisible()                   # empty state
    assert "Not scanned yet" in page.card.text()
    page._apply_scan(vehnet.ScanResult(**SCAN), None)
    v = page.map_view.verdict
    assert page.card.notes_list() == v.notes
    assert page.card.notes.text().startswith("\u2022 ")
    page.map_view.grab()                                 # lays out node rects
    centre = page.map_view.node_center("ebcm")
    assert centre is not None
    QTest.mouseClick(page.map_view, Qt.LeftButton, Qt.NoModifier,
                     centre.toPoint())
    assert page.map_view.selected == "ebcm"
    assert page.card.details.isVisible()
    assert page.card.title.text() == "EBCM (ABS)"
    assert page.card.status_chip.text() == "no response"
    assert page.card.status_chip.property("chip") == "danger"
    text = page.card.text()
    assert "Bus: HS-GMLAN (500k)" in text
    assert "Diagnostic address: request 243 / response 643" in text
    assert "Role: Brake control" in text
    tip = page.map_view.tooltip_for("ebcm")
    for part in ("EBCM (ABS)", "Status: no response", "Bus: HS-GMLAN (500k)",
                 "request 243 / response 643", "Role: Brake control"):
        assert part in tip
    assert "single-wire" in page.map_view.tooltip_for("ipc")
    assert page.map_view.node_at(centre) == "ebcm"
    # a rescan keeps the selected module's card current
    page._apply_scan(vehnet.ScanResult(**dict(SCAN, pinged={
        "ecm": True, "tcm": True, "ebcm": True, "bcm": None,
        "tccm": True})), None)
    assert page.card.status_chip.text() == "responding"
    assert page.card.status_chip.property("chip") == "ok"


def test_scan_notes_panel_state_persists(page, qapp, tmp_path):
    from openobd.diagui import NOTES_OPEN_KEY
    page._apply_scan(vehnet.ScanResult(**SCAN), None)
    assert page.card.notes_panel.is_expanded()
    page.card.notes_panel.set_expanded(False)
    assert str(page.settings.value(NOTES_OPEN_KEY)) == "false"


def test_scan_error_and_binary_mode_banners(page):
    from openobd.diagui import BINARY_MODE_ADVICE
    page._apply_scan(None, "could not open COM7")
    assert not page.map_banner.isHidden()
    assert page.map_banner.property("banner") == "danger"
    assert page.map_banner.text() == "Scan failed at the tool link: could not open COM7"
    assert ("fault at: USB link (PC to GT)", "danger") in page.map_crumb.chips()
    page._apply_scan(vehnet.ScanResult(**dict(SCAN, binary_mode=True)), None)
    assert page.map_banner.text() == BINARY_MODE_ADVICE
    assert BINARY_MODE_ADVICE == (
        "OBDX GT is stuck in binary (J2534) mode — unplug it from USB and "
        "the OBD port for 10 s, then rescan.")
    page._apply_scan(vehnet.ScanResult(**SCAN), None)
    assert page.map_banner.isHidden()


# --------------------------------------------------------------------------- #
# Jobs, busy state, banners, clear
# --------------------------------------------------------------------------- #
def test_busy_state_read(page):
    page.read_codes()
    assert len(page.job.run.fns) == 1
    assert page.busy() == "read"
    assert not page.act_read.isEnabled()
    assert not page.act_clear.isEnabled()
    assert not page.act_scan.isEnabled()              # one job at a time
    assert ("Reading\u2026", "muted") in page.codes_crumb.chips()
    page.read_codes()                                  # ignored while busy
    page.run_primary()
    assert len(page.job.run.fns) == 1
    page.job.finished.emit({"dtcs": DTCS, "ready": READY}, None)
    assert page.busy() is None
    assert page.act_read.isEnabled() and page.act_scan.isEnabled()
    assert ("Reading\u2026", "muted") not in page.codes_crumb.chips()
    assert "Stored (2)" in groups(page)


def test_busy_state_scan(page):
    page.scan_modules()
    assert page.job.run.fns == [vehnet.scan_pipeline]
    assert page.busy() == "scan"
    assert not page.act_scan.isEnabled() and not page.act_read.isEnabled()
    assert page.map_crumb.chips()[-1] == ("Scanning\u2026", "muted")
    page.job.finished.emit(vehnet.ScanResult(**SCAN), None)
    assert page.busy() is None
    assert page.act_scan.isEnabled()
    assert ("Scanning\u2026", "muted") not in page.map_crumb.chips()
    assert ("3 responding", "ok") in page.map_crumb.chips()


def test_link_error_is_an_inline_banner_not_a_dialog(page, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    def no_dialog(*_a, **_k):
        raise AssertionError("link errors must not open a dialog")
    monkeypatch.setattr(QMessageBox, "warning", no_dialog)
    page.read_codes()
    page.job.finished.emit(None, "port busy")
    assert page.busy() is None and page.act_read.isEnabled()
    assert not page.codes_banner.isHidden()
    assert page.codes_banner.property("banner") == "danger"
    assert page.codes_banner.text() == "Couldn't reach the truck: port busy"
    # after a successful read the banner goes; a later error names the
    # read the rows still come from
    page.read_codes()
    page.job.finished.emit({"dtcs": DTCS, "ready": READY}, None)
    assert page.codes_banner.isHidden()
    page.read_codes()
    page.job.finished.emit(None, "port busy")
    assert "rows below are from the previous read" in page.codes_banner.text()
    # a scan error goes to the map banner
    page.scan_modules()
    page.job.finished.emit(None, "no such port")
    assert page.map_banner.text() == "Scan failed at the tool link: no such port"


def test_clear_codes_is_confirmed_and_reports_outcome(page, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    asked = []

    def answer(reply):
        def warning(_parent, title, text, *rest):
            asked.append((title, text))
            return reply
        return warning
    monkeypatch.setattr(QMessageBox, "warning", answer(QMessageBox.Cancel))
    page.act_clear.trigger()
    assert asked[0][0] == "Clear trouble codes"
    assert asked[0][1].startswith("Clear ALL stored and pending DTCs?")
    assert "resets every readiness monitor" in asked[0][1]
    assert page.job.run.fns == [] and page.busy() is None
    monkeypatch.setattr(QMessageBox, "warning", answer(QMessageBox.Yes))
    page.act_clear.trigger()
    assert page.busy() == "clear"
    assert ("Clearing\u2026", "muted") in page.codes_crumb.chips()
    asked.clear()
    page.job.finished.emit({"clear": {"sent": False, "error": "no header"},
                            "dtcs": DTCS, "ready": READY}, None)
    assert asked and asked[0][0] == "Clear codes"
    assert asked[0][1].startswith("The clear was NOT sent")
    assert page.busy() is None
    assert "Stored (2)" in groups(page)


# --------------------------------------------------------------------------- #
# Chrome, width, persistence, routing
# --------------------------------------------------------------------------- #
def test_chrome_matches_tuning(page):
    from PySide6.QtWidgets import QToolBar
    assert page.map_crumb.height() == theme.BREADCRUMB_HEIGHT
    assert page.codes_crumb.height() == theme.BREADCRUMB_HEIGHT
    for tb in (page.map_toolbar, page.codes_toolbar):
        assert isinstance(tb, QToolBar)
        assert tb.objectName() == "diagToolbar"
        assert tb.height() == theme.TOOLBAR_HEIGHT
    for a in (page.act_scan, page.act_read):
        btn = (page.map_toolbar if a is page.act_scan
               else page.codes_toolbar).widgetForAction(a)
        assert btn.property("primary") is True
    for a in (page.act_scan, page.act_copy_report, page.act_read,
              page.act_clear, page.act_copy_codes):
        assert not a.icon().isNull()
        assert all(ord(ch) < 0x2190 for ch in a.text()), a.text()  # no emoji
    assert page.act_clear.text() == "Clear Codes\u2026"
    # the button keeps the ellipsis that says it asks before acting
    assert (page.codes_toolbar.widgetForAction(page.act_clear).text()
            == "Clear Codes\u2026")
    assert "#diagToolbar" in theme.diag_qss()


def test_diag_qss_uses_tokens_only_and_new_glyphs_render(qapp):
    import re
    qss = theme.diag_qss()
    tokens = {v.lower() for v in theme.COLORS.values()}
    used = {m.lower() for m in re.findall(r"#[0-9a-fA-F]{6}\b", qss)}
    assert used and used <= tokens
    for kind in ("scan", "read", "clear", "not_examined", "incomplete"):
        assert not theme.glyph_icon(kind).isNull()
    for kind in ("ok", "warning", "danger", "muted"):
        assert f'QLabel[chip="{kind}"]' in qss
    for kind in ("danger", "warning", "muted"):
        assert f'QLabel[banner="{kind}"]' in qss


def test_minimum_width_stays_narrow(page, qapp):
    # widest chrome: every kind not examined + a busy chip
    page.populate_codes(NOT_EXAMINED, {"error": "GT refused ATSH7E0"})
    page.read_codes()
    page._apply_scan(None, "could not open COM7")
    page.tabs.setCurrentIndex(1)
    qapp.processEvents()
    assert page.minimumSizeHint().width() < 700
    page.tabs.setCurrentIndex(0)
    qapp.processEvents()
    assert page.minimumSizeHint().width() < 700


def test_splitter_state_persists(qapp, tmp_path):
    from openobd.diagui import CODES_SPLIT_KEY, MAP_SPLIT_KEY, DiagnosticsPage
    settings = ini(tmp_path, "split.ini")
    p = DiagnosticsPage(None, settings)
    p.resize(1366, 700)
    p.show()
    qapp.processEvents()
    p.map_split.setSizes([700, 500])
    p.map_split.splitterMoved.emit(700, 1)               # a user drag
    assert settings.value(MAP_SPLIT_KEY) is not None
    p.tabs.setCurrentIndex(1)
    qapp.processEvents()
    p.codes_split.setSizes([600, 600])
    p.save_state()
    assert settings.value(CODES_SPLIT_KEY) is not None
    map_sizes, codes_sizes = p.map_split.sizes(), p.codes_split.sizes()
    p.close()
    settings.sync()
    q = DiagnosticsPage(None, ini(tmp_path, "split.ini"))
    q.resize(1366, 700)
    q.show()
    q.tabs.setCurrentIndex(1)
    qapp.processEvents()
    assert q.codes_split.sizes() == codes_sizes
    q.tabs.setCurrentIndex(0)
    qapp.processEvents()
    assert q.map_split.sizes() == map_sizes
    q.close()
    # only diag/ keys were written
    assert all(k.startswith("diag/") for k in settings.allKeys())


def test_f5_and_ctrl_f_routing(win, qapp):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    diag = win.diag
    rec = Recorder()
    diag.job.run = rec
    win.main_tabs.setCurrentWidget(diag)
    diag.tabs.setCurrentIndex(0)
    qapp.processEvents()
    win.activateWindow()
    assert not win._find_shortcut.isEnabled()          # no search on the map
    QTest.keyClick(win, Qt.Key_F5)
    assert rec.fns == [vehnet.scan_pipeline]
    assert diag.busy() == "scan"
    diag.job.finished.emit(vehnet.ScanResult(**SCAN), None)
    diag.tabs.setCurrentIndex(1)
    qapp.processEvents()
    QTest.keyClick(win, Qt.Key_F5)
    assert len(rec.fns) == 2 and diag.busy() == "read"
    diag.job.finished.emit({"dtcs": DTCS, "ready": READY}, None)
    assert win._find_shortcut.isEnabled()
    diag.dtc_tree.setFocus()
    QTest.keyClick(win, Qt.Key_F, Qt.ControlModifier)
    qapp.processEvents()
    assert QApplication.focusWidget() is diag.dtc_search
    QTest.keyClicks(diag.dtc_search, "lean")
    assert [r["code"] for r in diag.visible_rows()] == ["P0171"]
    QTest.keyClick(diag.dtc_search, Qt.Key_Escape)
    assert diag.dtc_search.text() == ""
    # F5 on the Dashboard is still the replay's, never a diagnostics read
    win.main_tabs.setCurrentIndex(0)
    qapp.processEvents()
    QTest.keyClick(win, Qt.Key_F5)
    assert len(rec.fns) == 2
    assert not win._find_shortcut.isEnabled()
    # back on Tuning, Ctrl+F is the table search again
    win.main_tabs.setCurrentIndex(1)
    qapp.processEvents()
    assert win._find_shortcut.isEnabled()
    QTest.keyClick(win, Qt.Key_F, Qt.ControlModifier)
    qapp.processEvents()
    assert QApplication.focusWidget() is win.navigator.search
    win.main_tabs.setCurrentIndex(0)


def test_window_minimum_width_with_diagnostics(win, qapp):
    win.main_tabs.setCurrentWidget(win.diag)
    qapp.processEvents()
    assert win.diag.minimumSizeHint().width() < 1000
    win.main_tabs.setCurrentIndex(0)
