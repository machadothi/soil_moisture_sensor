from convert import battery_percent, median, moisture_percent


def test_median():
    assert median([5, 1, 3]) == 3
    assert median([4, 1, 3, 2]) == 2.5


def test_moisture_is_inverted_and_clamped():
    assert moisture_percent(2800, 2800, 1300) == 0
    assert moisture_percent(1300, 2800, 1300) == 100
    assert moisture_percent(2050, 2800, 1300) == 50
    assert moisture_percent(3000, 2800, 1300) == 0     # drier than "dry"
    assert moisture_percent(1000, 2800, 1300) == 100   # wetter than "wet"
    assert moisture_percent(None, 2800, 1300) is None


def test_battery_percent():
    assert battery_percent(4.3) == 100
    assert battery_percent(3.75) == 50
    assert battery_percent(3.1) == 0
    assert battery_percent(None) is None
