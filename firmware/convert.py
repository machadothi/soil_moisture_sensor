# Turning raw readings into values. Pure Python: the PC tests import it as it is.


def median(values):
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def moisture_percent(mv, dry_mv, wet_mv):
    """Capacitive probe: the output voltage drops as the soil gets wetter.
    dry_mv is the probe in air, wet_mv in water; in between maps linearly to
    0-100 %, clamped."""
    if mv is None or dry_mv == wet_mv:
        return None
    percent = (dry_mv - mv) * 100 / (dry_mv - wet_mv)
    return min(100.0, max(0.0, percent))


# Li-ion 18650 resting voltage -> charge, roughly; good enough for "replace soon".
_LIION = ((4.20, 100), (4.00, 85), (3.85, 65), (3.75, 50), (3.65, 30), (3.50, 10), (3.30, 0))


def battery_percent(volts):
    if volts is None:
        return None
    if volts >= _LIION[0][0]:
        return 100
    for (v_high, p_high), (v_low, p_low) in zip(_LIION, _LIION[1:]):
        if volts >= v_low:
            return round(p_low + (volts - v_low) * (p_high - p_low) / (v_high - v_low))
    return 0
