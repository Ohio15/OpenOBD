"""Pure-logic tests for the Dashboard's fluent gauges (no Qt)."""
import math

import pytest

from openobd.gaugemotion import (
    EASE_TAU_S, LiveFreshness, NeedleMotion, STALE_CAP_S, STALE_FLOOR_S,
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
# live freshness (GT path)
# --------------------------------------------------------------------------- #
def test_nothing_arrived_yet_passes_the_source_state_through():
    f = LiveFreshness()
    assert f.resolve("rpm", "fresh", 0.0) == ("fresh", None)
    assert f.resolve("rpm", None, 0.0) == (None, None)


def test_channel_in_newest_sample_is_fresh_with_its_true_value():
    f = LiveFreshness()
    assert f.observe(0.0, {"rpm": 812.0, "ect": 190.0}, now=10.0)
    assert f.resolve("rpm", "fresh", 10.2) == ("fresh", 812.0)


def test_missed_pid_is_carried_not_blanked():
    f = LiveFreshness()
    f.observe(0.0, {"rpm": 800.0, "maf": 4.8}, now=10.0)
    f.observe(1.0, {"rpm": 820.0}, now=11.0)          # MAF timed out
    assert f.resolve("maf", "fresh", 11.1) == ("carried", 4.8)
    assert f.resolve("rpm", "fresh", 11.1) == ("fresh", 820.0)


def test_none_values_count_as_absent():
    f = LiveFreshness()
    f.observe(0.0, {"maf": 4.8}, now=0.0)
    f.observe(1.0, {"maf": None, "rpm": 700.0}, now=1.0)
    assert f.resolve("maf", "fresh", 1.0) == ("carried", 4.8)


def test_repeated_sample_is_not_new():
    f = LiveFreshness()
    assert f.observe(5.0, {"rpm": 800.0}, now=0.0) is True
    assert f.observe(5.0, {"rpm": 800.0}, now=0.1) is False
    assert f.observe(None, None, now=0.2) is False


def test_rounds_stop_arriving_means_stale_with_last_value_held():
    f = LiveFreshness()
    f.observe(0.0, {"rpm": 800.0}, now=0.0)
    f.observe(1.0, {"rpm": 810.0}, now=1.0)
    limit = f.stale_after()
    # latest() keeps returning the same old sample: nothing new observed
    f.observe(1.0, {"rpm": 810.0}, now=1.0 + limit + 0.01)
    assert f.resolve("rpm", "fresh", 1.0 + limit + 0.01) == ("stale", 810.0)


def test_long_missing_channel_is_withdrawn():
    f = LiveFreshness()
    f.observe(0.0, {"rpm": 800.0, "maf": 4.8}, now=0.0)
    t = 0.0
    for i in range(1, 12):                 # 1 Hz rounds without MAF
        t = float(i)
        f.observe(t, {"rpm": 800.0}, now=t)
    assert f.resolve("maf", "fresh", t) == ("unavail", None)
    assert f.resolve("rpm", "fresh", t) == ("fresh", 800.0)


def test_source_failure_always_wins_and_shows_no_value():
    f = LiveFreshness()
    f.observe(0.0, {"rpm": 800.0}, now=0.0)
    assert f.resolve("rpm", "failed", 0.1) == ("failed", None)


def test_never_answered_channel_is_plain():
    f = LiveFreshness()
    f.observe(0.0, {"rpm": 800.0}, now=0.0)
    assert f.resolve("ethanol", "fresh", 0.1) == (None, None)


def test_stale_threshold_adapts_to_poll_rate_within_bounds():
    f = LiveFreshness()
    assert f.stale_after() == STALE_FLOOR_S
    t = 0.0
    for i in range(20):                    # fast 10 Hz link
        t = i * 0.1
        f.observe(t, {"rpm": 1.0}, now=t)
    assert f.stale_after() == STALE_FLOOR_S      # 3 x 0.1 s floors at 2 s
    g = LiveFreshness()
    for i in range(20):                    # slow 2 s rounds
        t = i * 2.0
        g.observe(t, {"rpm": 1.0}, now=t)
    assert g.stale_after() == pytest.approx(6.0, rel=0.05)
    h = LiveFreshness()
    for i in range(20):                    # pathological 30 s rounds
        t = i * 30.0
        h.observe(t, {"rpm": 1.0}, now=t)
    assert h.stale_after() == STALE_CAP_S


def test_resolve_all_covers_every_gauge():
    f = LiveFreshness()
    f.observe(0.0, {"rpm": 800.0}, now=0.0)
    states, values = f.resolve_all(["rpm", "vss"], {"rpm": "fresh",
                                                    "vss": "fresh"}, 0.1)
    assert states == {"rpm": "fresh", "vss": None}
    assert values == {"rpm": 800.0, "vss": None}
    states, _ = f.resolve_all(["rpm"], None, 0.1)
    assert states == {"rpm": "fresh"}


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
