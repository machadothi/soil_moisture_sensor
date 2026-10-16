# The board's sensors: the capacitive soil probe, the DHT11 and (if the board
# has one) a battery divider. See docs/hardware.md for the pins.
import time

import dht
from machine import ADC, Pin

import convert

_SAMPLES = 15


def _adc(pin):
    adc = ADC(Pin(pin))
    adc.atten(ADC.ATTN_11DB)   # 0-3.3 V range (about 150-3100 mV usable)
    return adc


def _millivolts(adc):
    # read_uv() uses the chip's eFuse calibration. The probe's output wanders a
    # little from sample to sample, so take the median.
    return convert.median([adc.read_uv() // 1000 for _ in range(_SAMPLES)])


class Readings:
    def __init__(self):
        self.soil_mv = None
        self.moisture = None       # %
        self.temperature = None    # °C
        self.humidity = None       # %
        self.battery_v = None
        self.battery = None        # %

    def recalculate(self, soil):
        """After a new calibration: the moisture from the same probe reading."""
        self.moisture = convert.moisture_percent(self.soil_mv, soil["dry_mv"], soil["wet_mv"])

    def __str__(self):
        def show(value, unit):
            return "-" if value is None else "%s%s" % (value, unit)
        return "soil %s (%s)  %s  %s  battery %s (%s)" % (
            show(self.soil_mv, " mV"), show(None if self.moisture is None else round(self.moisture, 1), " %"),
            show(self.temperature, " C"), show(self.humidity, " %RH"),
            show(None if self.battery_v is None else round(self.battery_v, 2), " V"), show(self.battery, " %"))


class Sensors:
    def __init__(self, cfg):
        pins = cfg["pins"]
        self.cfg = cfg
        self.soil = _adc(pins["soil"])
        self.dht = dht.DHT11(Pin(pins["dht11"]))
        battery_pin = pins.get("battery")
        self.battery = _adc(battery_pin) if battery_pin is not None else None
        self.errors = 0            # DHT11 reads that failed, since start

    def soil_mv(self):
        return _millivolts(self.soil)

    def read(self):
        r = Readings()
        r.soil_mv = self.soil_mv()
        r.recalculate(self.cfg["soil"])
        r.temperature, r.humidity = self._dht()
        if self.battery is not None:
            battery = self.cfg["battery"]
            volts = _millivolts(self.battery) * battery["divider"] / 1000
            # A pin with nothing on it reads a few hundred mV: no battery
            # reading rather than a wrong one.
            if battery["min_v"] <= volts <= battery["max_v"]:
                r.battery_v = volts
                r.battery = convert.battery_percent(volts)
        return r

    def _dht(self):
        # The DHT11 answers at most once a second and sometimes misses a read.
        for attempt in range(3):
            try:
                self.dht.measure()
                return self.dht.temperature(), self.dht.humidity()
            except OSError:
                self.errors += 1
                time.sleep_ms(1100)
        print("DHT11: no answer")
        return None, None
