"""Pure-logic tests for the Dashboard's fluent gauges (no Qt)."""
import math

import pytest

from openobd.gaugemotion import (
    EASE_TAU_S, NeedleMotion,
    ease_toward, fmt_reading, grid_cells, grid_shape, reading_decimals,
)


# --------------------------------------------------------------------------- #
# easing
# --------------------------------------------------------------------------- #
def test_ease_is_time_based_not_frame_based():
    # Two 8 ms frames must land exactly where one 16 ms frame does: the motion
    # looks the same at any frame rate and under timer jitter.
    one = ease_toward(0.0, 100.0, 0.016)
    two = ease_toward(ease_toward(0.0, 100.0, 0.008), 100.0, 0.008)
    assert one == pytest.approx(two, rel=1e-12)


def test_ease_covers_63_percent_per_tau_and_never_overshoots():
    assert ease_toward(0.0, 100.0, EASE_TAU_S) == pytest.approx(
        100.0 * (1 - math.exp(-1)))
    x = 0.0
    for _ in range(1000):
        x = ease_toward(x, 100.0, 0.016)
        assert x <= 100.0
    x = 100.0
    for _ in range(1000):
        x = ease_toward(x, 20.0, 0.016)
        assert x >= 20.0


def test_ease_ignores_non_positive_dt():
    assert ease_toward(5.0, 50.0, 0.0) == 5.0
    assert ease_toward(5.0, 50.0, -1.0) == 5.0


def test_needle_settles_and_stops_requesting_frames():
    m = NeedleMotion(0, 7000)
    assert m.set_target(3000) is True
    t, frames = 0.0, 0
    while m.moving:
        t += 1 / 60
        frames += 1
        m.step(t)
        assert frames < 120, "needle never settled"
    assert m.shown == 3000          # snaps exactly onto the real value
    # visually done at ~0.27 s (95%); a 3000 rpm step lands exactly inside
    # the settle band by ~0.5 s, after which no further frames are drawn
    assert frames <= 36
    assert m.step(t + 1) is False   # idle: no repaint owed


def test_needle_reaches_most_of_a_1hz_step_quickly():
    # A 1 Hz GT step must read as motion, but be ~done long before the next
    # sample arrives.
    m = NeedleMotion(0, 160)
    m.set_target(60)
    t = 0.0
    for _ in range(18):             # 0.3 s at 60 fps
        t += 1 / 60
        m.step(t)
    assert m.shown > 0.9 * 60


def test_needle_target_is_clamped_to_the_scale():
    m = NeedleMotion(0, 7000)
    m.set_target(9000)
    assert m.target == 7000          # off-scale: pin, don't travel past it
    m.set_target(-50)
    assert m.target == 0


def test_none_and_non_finite_park_the_needle():
    m = NeedleMotion(10, 15)
    m.set_target(13.8)
    m.snap()
    assert m.set_target(None) is True and m.target == 10
    m.snap()
    assert m.set_target(float("nan")) is False   # already parked
    assert m.set_target(float("inf")) is False


def test_same_target_does_not_restart_motion():
    m = NeedleMotion(0, 100)
    assert m.set_target(40) is True
    m.snap()
    assert m.set_target(40) is False


def test_long_frame_gap_catches_up_instead_of_crawling():
    m = NeedleMotion(0, 100)
    m.set_target(100)
    m.step(0.0)
    m.step(5.0)                      # window was dragged for 5 s
    assert m.shown == 100


def test_first_frame_of_a_motion_uses_a_nominal_dt():
    # The frame clock resets while idle: the first frame after a new target
    # must not integrate the whole idle period as one step (that would teleport).
    m = NeedleMotion(0, 100)
    m.set_target(50)
    m.snap()
    m.step(100.0)                    # idle; clock cleared
    m.set_target(100)
    m.step(200.0)
    assert 50 < m.shown < 60


# --------------------------------------------------------------------------- #
# readouts
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("vmin,vmax,key,dec", [
    (0, 7000, "rpm", 0),
    (0, 160, "vss", 0),
    (100, 260, "ect", 0),
    (0, 100, "maf", 1),
    (10, 15, "voltage", 1),
    (-25, 25, "stft", 1),
    (0, 6, "gear", 0),
])
def test_reading_decimals(vmin, vmax, key, dec):
    assert reading_decimals(vmin, vmax, key) == dec


def test_fmt_reading_is_the_true_sample_rounded():
    assert fmt_reading(13.7254901, 1) == "13.7"
    assert fmt_reading(2048.6, 0) == "2049"
    assert fmt_reading(None, 1) == "—"
    assert fmt_reading(float("nan"), 1) == "—"
    assert fmt_reading(-0.04, 1) == "0.0"     # no "-0.0" noise
    assert fmt_reading(-3.26, 1) == "-3.3"


# --------------------------------------------------------------------------- #
# responsive grid
# --------------------------------------------------------------------------- #
def test_grid_rebalances_rows():
    assert grid_shape(12, 7 * 70 + 6 * 4, 70, 4) == (6, 2)     # not 7 + 5
    assert grid_shape(12, 2000, 70, 4) == (12, 1)
    assert grid_shape(12, 10, 70, 4) == (1, 12)                # never 0 cols
    assert grid_shape(0, 500, 70, 4) == (0, 0)
    assert grid_shape(9, 5 * 78 + 4 * 6, 78, 6) == (5, 2)


def test_grid_cells_fill_and_center_short_row():
    cells = grid_cells(5, 0, 0, 304, 100, cols=3, rows=2, spacing=4)
    assert len(cells) == 5
    cw = (304 - 2 * 4) / 3
    assert cells[0] == (0, 0, cw, 48)
    assert cells[2][0] == pytest.approx(2 * (cw + 4))
    # second row has 2 of 3 cells: shifted right by half a cell + spacing
    assert cells[3][0] == pytest.approx((cw + 4) / 2)
    assert cells[3][1] == 52
    assert grid_cells(0, 0, 0, 100, 100, 0, 0, 4) == []
