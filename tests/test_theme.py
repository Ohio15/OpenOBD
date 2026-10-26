"""openobd.theme: colour scales, contrast, tokens and the QSS/palette helpers
(brief item 1). The scale functions are pure; the palette test needs a
QApplication (offscreen)."""
import glob
import os
import re
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from openobd import theme  # noqa: E402


# --- heatmap ---------------------------------------------------------------- #
def test_heat_rgb_endpoints_are_blue_and_red():
    assert theme.heat_rgb(0.0) == theme.hex_to_rgb(theme.HEAT_STOPS[0][1])
    assert theme.heat_rgb(1.0) == theme.hex_to_rgb(theme.HEAT_STOPS[-1][1])
    lo, hi = theme.heat_rgb(0.0), theme.heat_rgb(1.0)
    assert lo[2] > lo[0]          # blue dominates the low end
    assert hi[0] > hi[2]          # red dominates the high end


def test_heat_rgb_passes_through_every_stop_in_order():
    # blue -> cyan -> green -> yellow -> red
    names = [theme.heat_rgb(p) for p, _ in theme.HEAT_STOPS]
    assert names == [theme.hex_to_rgb(c) for _, c in theme.HEAT_STOPS]
    assert [p for p, _ in theme.HEAT_STOPS] == sorted(
        p for p, _ in theme.HEAT_STOPS)
    assert len(theme.HEAT_STOPS) == 5


def test_heat_rgb_clamps_and_tolerates_nan():
    assert theme.heat_rgb(-3) == theme.heat_rgb(0.0)
    assert theme.heat_rgb(7) == theme.heat_rgb(1.0)
    assert theme.heat_rgb(float("nan")) == theme.heat_rgb(0.0)


# --- diff ------------------------------------------------------------------- #
def test_diff_rgb_is_diverging_blue_grey_red():
    below, at, above = theme.diff_rgb(-1), theme.diff_rgb(0), theme.diff_rgb(1)
    assert below == theme.hex_to_rgb(theme.DIFF_STOPS[0][1])
    assert at == theme.hex_to_rgb(theme.DIFF_STOPS[1][1])
    assert above == theme.hex_to_rgb(theme.DIFF_STOPS[-1][1])
    assert below[2] > below[0] and above[0] > above[2]
    assert max(at) - min(at) < 20          # neutral grey at stock


def test_diff_rgb_clamps_and_tolerates_nan():
    assert theme.diff_rgb(-9) == theme.diff_rgb(-1)
    assert theme.diff_rgb(9) == theme.diff_rgb(1)
    assert theme.diff_rgb(float("nan")) == theme.diff_rgb(0)


# --- contrast ---------------------------------------------------------------- #
def test_contrast_text_rgb_endpoints():
    dark_text = theme.token_rgb("text-on-light")
    light_text = theme.token_rgb("text-on-dark")
    assert theme.contrast_text_rgb((255, 255, 255)) == dark_text
    assert theme.contrast_text_rgb((0, 0, 0)) == light_text
    # yellow is light, deep blue is dark
    assert theme.contrast_text_rgb(theme.heat_rgb(0.75)) == dark_text
    assert theme.contrast_text_rgb(theme.heat_rgb(0.0)) == light_text


def test_contrast_text_reads_on_every_heat_colour():
    for i in range(21):
        bg = theme.heat_rgb(i / 20)
        fg = theme.contrast_text_rgb(bg)
        assert abs(theme.luminance(fg) - theme.luminance(bg)) > 60, (i, bg)


# --- tokens ------------------------------------------------------------------ #
def test_heights_and_tokens():
    assert theme.TOOLBAR_HEIGHT == 36
    assert theme.PRIMARY_HEIGHT == 56
    for v in theme.COLORS.values():
        assert re.fullmatch(r"#[0-9a-fA-F]{6}", v), v
    assert theme.rgb_to_hex(theme.hex_to_rgb("#1a2b3c")) == "#1a2b3c"
    with pytest.raises(KeyError):
        theme.token("no-such-token")


def test_qss_helpers_use_tokens_only():
    qss = (theme.tuning_qss() + theme.record_button_qss()
           + theme.mark_button_qss())
    tokens = {v.lower() for v in theme.COLORS.values()}
    used = {m.lower() for m in re.findall(r"#[0-9a-fA-F]{6}\b", qss)}
    assert used and used <= tokens
    assert "#tuningToolbar" in qss and "recordButton" in qss


def test_no_raw_colour_literals_outside_theme():
    """Every colour lives in theme.py. Scans every module in openobd/ (so a
    new module is covered without editing this list) for both spellings of
    a raw colour: a '#rrggbb' string and a QColor(<int>, ...) constructor.

    gauges.py is the one exemption: the Dashboard dial artwork (bezels,
    needles, zone arcs, skin palettes) is illustration, not UI chrome, and
    is drawn per skin rather than from the shared token set."""
    pkg = os.path.join(ROOT, "openobd")
    exempt = {"theme.py", "gauges.py"}
    pattern = re.compile(r"#[0-9a-fA-F]{6}\b|QColor\(\s*\d+\s*,")
    files = sorted(os.path.basename(p)
                   for p in glob.glob(os.path.join(pkg, "*.py")))
    assert "app.py" in files and "model.py" in files    # glob is live
    offenders = []
    for name in files:
        if name in exempt:
            continue
        with open(os.path.join(pkg, name), encoding="utf-8") as f:
            for n, line in enumerate(f, 1):
                if pattern.search(line):
                    offenders.append(f"{name}:{n}")
    assert offenders == []


def test_dark_palette_comes_from_tokens():
    from PySide6.QtGui import QPalette
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    old = app.palette()
    try:
        theme.apply_dark_palette(app)
        p = app.palette()
        assert p.color(QPalette.Window) == theme.qcolor("surface")
        assert p.color(QPalette.Text) == theme.qcolor("text")
        assert p.color(QPalette.Highlight) == theme.qcolor("accent")
        assert p.color(QPalette.Window).lightness() < 80     # dark first
    finally:
        app.setPalette(old)
