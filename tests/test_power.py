import power
from power import CRITICAL, CUTOFF, LOW, NORMAL, SAVING, VERY_LOW


def test_levels_falling():
    assert power.level(4.10) == NORMAL
    assert power.level(3.75) == NORMAL
    assert power.level(3.70) == SAVING
    assert power.level(3.60) == LOW
    assert power.level(3.50) == VERY_LOW
    assert power.level(3.40) == CRITICAL
    assert power.level(3.30) == CUTOFF
    # falling applies at once, whatever the previous level
    assert power.level(3.40, previous=NORMAL) == CRITICAL


def test_unknown_battery_means_no_protection():
    assert power.level(None) == NORMAL
    assert power.level(None, previous=CUTOFF) == NORMAL


def test_rising_needs_hysteresis():
    assert power.level(3.77, previous=SAVING) == SAVING     # 3.75 reached, not 3.80
    assert power.level(3.80, previous=SAVING) == NORMAL
    assert power.level(3.69, previous=LOW) == LOW           # needs 3.70
    assert power.level(3.70, previous=LOW) == SAVING
    assert power.level(3.90, previous=VERY_LOW) == NORMAL   # several levels at once


def test_leaving_cutoff_needs_charging():
    assert power.level(3.45, previous=CUTOFF) == CUTOFF
    assert power.level(3.59, previous=CUTOFF) == CUTOFF
    assert power.level(3.60, previous=CUTOFF) == LOW        # 3.60 is in low's band and clears its 3.60 margin
    assert power.level(4.10, previous=CUTOFF) == NORMAL


def test_intervals():
    assert power.interval_s(NORMAL, 60) == 60
    assert power.interval_s(SAVING, 60) == 120
    assert power.interval_s(LOW, 60) == 300
    assert power.interval_s(VERY_LOW, 60) == 900
    assert power.interval_s(CRITICAL, 60) == 3600
    assert power.interval_s(CUTOFF, 60) == 6 * 3600


def test_never_sleep_only_while_the_battery_is_fine():
    assert power.interval_s(NORMAL, 0) == 0
    assert power.interval_s(SAVING, 0) == 0
    assert power.interval_s(LOW, 0) == 300
    assert power.interval_s(CRITICAL, 0) == 3600


def test_radio_app_and_led():
    assert [power.radio(lvl) for lvl in range(6)] == ["full"] * 4 + ["bthome", None]
    assert power.awake_s(NORMAL, 300) == 300
    assert power.awake_s(LOW, 300) == 120
    assert power.awake_s(LOW, 0) == 120          # "until it connects" is capped too
    assert power.awake_s(VERY_LOW, 30) == 30
    assert power.awake_s(NORMAL, 0) == 0         # until it connects
    assert power.awake_s(CRITICAL, 300) is None  # no app
    periods = [power.blink_ms(lvl) for lvl in range(6)]
    assert periods[:2] == [0, 0]
    assert periods[2:] == sorted(periods[2:], reverse=True)   # faster as it discharges
