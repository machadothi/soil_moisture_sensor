# Battery protection: from the battery's resting voltage (read before the radio
# starts, so it doesn't sag under load) a level that sets how often the board
# wakes, whether it may use the radio, how long it waits for the app and how
# fast the LED blinks. docs/battery.md has the reasoning.
#
#   level      resting V    wakes every            radio              app wait   LED period
#   normal     >= 3.75      interval_s             BTHome (+ app)     awake_s    off
#   saving     3.65-3.75    2 x interval_s         BTHome (+ app)     awake_s    off
#   low        3.55-3.65    5 x interval_s         BTHome (+ app)     120 s      2 s
#   very low   3.45-3.55    15 x interval_s        BTHome (+ app)     60 s       1 s
#   critical   3.35-3.45    1 h                    1 s of BTHome      none       0.5 s
#   cutoff     < 3.35       6 h                    none               none       0.25 s (BOOT only)
#
# Falling, a level applies at once; rising, it needs HYSTERESIS_V more, and
# leaving cutoff needs CUTOFF_RECOVER_V (in practice: charging). Without a
# battery reading (no divider) there is no protection: always normal.
#
# Pure Python: the PC tests import it as it is.

NORMAL, SAVING, LOW, VERY_LOW, CRITICAL, CUTOFF = range(6)
NAMES = ("normal", "saving", "low", "very low", "critical", "cutoff")

_EDGES_V = (3.75, 3.65, 3.55, 3.45, 3.35)   # lower edge of normal, saving, low, very low, critical
HYSTERESIS_V = 0.05
CUTOFF_RECOVER_V = 3.60
# Under load (radio on) the voltage reads lower; below this an app session ends.
SESSION_CUTOFF_V = 3.25
OVERVOLTAGE_V = 4.30      # a full cell is 4.20 V; above this the charger is suspect
HOT_C = 45                # no Li-ion charging above this (air temperature next to the cell)

CRITICAL_INTERVAL_S = 3600
CUTOFF_INTERVAL_S = 6 * 3600
CRITICAL_ADVERTISE_S = 1
_FACTOR = (1, 2, 5, 15)                     # normal .. very low
_AWAKE_MAX_S = (None, None, 120, 60, 0, 0)  # None: no limit beyond awake_s; 0: no app at all
_BLINK_MS = (0, 0, 2000, 1000, 500, 250)    # 0: LED off
_NEVER_SLEEP_BASE_S = 60                    # "interval_s": 0 is ignored from low down


def level(volts, previous=None):
    """The level for a resting battery voltage (None: unknown -> normal),
    given the level of the previous wake (None: none) for the hysteresis."""
    if volts is None:
        return NORMAL
    lower = sum(1 for edge in _EDGES_V if volts < edge)
    if previous is None or lower >= previous:
        return lower
    current = previous
    while current > lower:   # climb one level at a time, each with its margin
        need = CUTOFF_RECOVER_V if current == CUTOFF else _EDGES_V[current - 1] + HYSTERESIS_V
        if volts < need:
            break
        current -= 1
    return current


def interval_s(lvl, base_s):
    """Deep sleep between measurements for this level; base_s is the configured
    interval (0 = never sleep, honoured only while the battery is fine)."""
    if lvl == CUTOFF:
        return CUTOFF_INTERVAL_S
    if lvl == CRITICAL:
        return CRITICAL_INTERVAL_S
    if base_s == 0:
        return 0 if lvl <= SAVING else _NEVER_SLEEP_BASE_S * _FACTOR[lvl]
    return base_s * _FACTOR[lvl]


def radio(lvl):
    """"full" (BTHome, and the app packet when asked), "bthome" (a short
    broadcast only) or None (no radio at all)."""
    return None if lvl == CUTOFF else "bthome" if lvl == CRITICAL else "full"


def awake_s(lvl, base_s):
    """How long to wait for the app after the button, a reset or power-on;
    0 = until it connects (as "awake_s": 0), None = don't offer the app."""
    limit = _AWAKE_MAX_S[lvl]
    if limit is None:
        return base_s
    if limit == 0:
        return None
    return limit if base_s == 0 else min(base_s, limit)


def blink_ms(lvl):
    """LED period: one short flash every this many ms; 0 = off."""
    return _BLINK_MS[lvl]
