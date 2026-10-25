# Configuration and the main cycle: measure, broadcast, deep-sleep.
#
# Two modes, chosen by how the board woke up:
#   timer (every "interval_s"): measure once, broadcast BTHome for
#     "advertise_s", sleep again. The app packet is left out: battery mode.
#   the BOOT button, the reset (EN) button, power-on or a deploy: advertise for
#     the app too and wait up to "awake_s" for a phone to connect (0 = until one
#     does), measuring every "awake_measure_s". Also the time to watch the log or
#     reach the REPL (Ctrl+C).
# While a phone is connected the board stays awake and measures every 2 s;
# _DISCONNECT_GRACE_MS after it disconnects it sleeps again. "interval_s": 0
# never sleeps (USB power).
#
# Battery protection (power.py, docs/battery.md) has the last word: the
# battery is read first, before the radio starts, and its level stretches the
# sleep, shortens or refuses the wait for the app, turns the radio off near
# empty, and blinks the LED faster as the battery runs down.
#
# Settings changed from the app (name, calibration, interval) go to
# settings.json, on top of config.json; a factory reset deletes it.
import gc
import json
import os
import struct
import sys
import time

import esp32
import machine
from machine import Pin

import bthome
import power
import protocol
import sensors
from ble import BONDS_FILE, Ble

SETTINGS_FILE = "settings.json"
_CONNECTED_MEASURE_MS = 2000
# Short, so the app can reconnect (e.g. Android re-connecting after pairing).
_DISCONNECT_GRACE_MS = 5000
_EXT0_WAKE = getattr(machine, "EXT0_WAKE", 2)
_FLASH_MS = 30          # one LED flash
_CUTOFF_SHOW_MS = 2000  # the LED shows "cutoff" this long after a button press

DEFAULTS = {
    # button: wakes the board from deep sleep (GPIO0 is the BOOT button).
    # led: the board's blue LED (battery warnings, identify).
    "pins": {"soil": 32, "dht11": 22, "battery": 34, "led": 16, "button": 0},
    # True if the LED lights with its pin low.
    "led_active_low": False,
    # Probe output in air and in water (mV). Measure yours: README.md, "Calibration".
    "soil": {"dry_mv": 2830, "wet_mv": 1300},
    # Battery through a divider, if the board has one. Readings outside
    # min_v-max_v are dropped (an unconnected pin reads ~0.2 V).
    "battery": {"divider": 2.0, "min_v": 2.8, "max_v": 4.5},
    "bluetooth": {"name": ""},
    "interval_s": 60,
    "advertise_s": 3,
    "awake_s": 300,
    "awake_measure_s": 5,
}


def _merge(base, override):
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value
    return base


def _read_json(name):
    try:
        with open(name) as f:
            return json.load(f)
    except OSError:
        return {}   # no such file
    except ValueError as e:
        print(name, "is not valid JSON, ignored:", e)
        return {}


def load_config():
    """Defaults, then config.json, then settings.json (changed from the app)."""
    cfg = json.loads(json.dumps(DEFAULTS))   # a deep copy
    _merge(cfg, _read_json("config.json"))
    _merge(cfg, _read_json(SETTINGS_FILE))
    return cfg


# RTC memory survives deep sleep: the BTHome packet id, so every measurement
# gets a new one and Home Assistant never drops a reading, and the previous
# battery level, for the hysteresis (0xFF = none).
class Led:
    """On/off whichever way the LED is wired."""

    def __init__(self, pin, active_low):
        self.active_low = active_low
        self.pin = Pin(pin, Pin.OUT, value=1 if active_low else 0)

    def set(self, on):
        self.pin.value(1 if bool(on) != self.active_low else 0)


def _rtc_state():
    memory = machine.RTC().memory()
    return tuple(memory) if len(memory) == 2 else (0, 0xFF)


def _save_rtc_state(packet_id, level):
    machine.RTC().memory(struct.pack("BB", packet_id, 0xFF if level is None else level))


class App:
    def __init__(self):
        self.cfg = load_config()
        self.started = time.ticks_ms()
        led_pin = self.cfg["pins"]["led"]
        self.led = Led(led_pin, self.cfg["led_active_low"]) if led_pin is not None else None
        self.sensors = sensors.Sensors(self.cfg)
        self.readings = sensors.Readings()
        self.reboot_at = None
        self.end_session = False
        self.timer_wake = (machine.reset_cause() == machine.DEEPSLEEP_RESET
                           and machine.wake_reason() != _EXT0_WAKE)
        # The battery first, with the radio still off: its resting voltage.
        self.packet_id, previous = _rtc_state()
        self.rest_v = self.sensors.battery_v()
        self.level = power.level(self.rest_v, None if previous == 0xFF else previous)
        _save_rtc_state(self.packet_id, self.level if self.rest_v is not None else None)
        if self.rest_v is not None and (self.level != previous or not self.timer_wake):
            print("Battery: %.2f V at rest, %s" % (self.rest_v, power.NAMES[self.level]))
        self.ble = Ble(self) if power.radio(self.level) else None

    # --- readings -------------------------------------------------------------------

    def measure(self):
        r = self.readings = self.sensors.read()
        self.packet_id = (self.packet_id + 1) & 0xFF
        _save_rtc_state(self.packet_id, self.level if self.rest_v is not None else None)
        print(r)
        if r.temperature is not None and r.temperature >= power.HOT_C:
            print("Battery: %d C next to the cell: too hot to charge a Li-ion cell safely" % r.temperature)
        if r.battery_v is not None and r.battery_v > power.OVERVOLTAGE_V:
            print("Battery: %.2f V is above a full cell: check the charger" % r.battery_v)
        # Over-discharge during a long app session: the voltage under load.
        if (self.ble.connection is not None and r.battery_v is not None
                and r.battery_v < power.SESSION_CUTOFF_V and not self.end_session):
            print("Battery: %.2f V under load, ending the session" % r.battery_v)
            self.end_session = True
            self.level = max(self.level, power.CRITICAL)   # sleep long; the next wake re-measures
            _save_rtc_state(self.packet_id, self.level)
        self.publish()

    def publish(self):
        """The current readings and settings to GATT and the BTHome packet."""
        r, soil = self.readings, self.cfg["soil"]
        known = self.rest_v is not None
        uptime = time.ticks_diff(time.ticks_ms(), self.started)
        self.ble.update({
            "env": protocol.env(uptime, r.temperature, r.humidity),
            "soil": protocol.soil(uptime, r.soil_mv, r.moisture, soil["dry_mv"], soil["wet_mv"],
                                  self.cfg["interval_s"], r.battery_v, r.battery,
                                  self.level if known else None),
            "system": protocol.system(uptime // 1000, gc.mem_free(), (esp32.raw_temperature() - 32) / 1.8,
                                      self.ble.connection is not None, machine.freq() // 1000000,
                                      machine.reset_cause(), sys.implementation.version[:3],
                                      self.sensors.errors),
            "bthome": {
                bthome.PACKET_ID: self.packet_id,
                bthome.BATTERY: r.battery,
                bthome.TEMPERATURE: r.temperature,
                bthome.HUMIDITY: r.humidity,
                bthome.VOLTAGE: r.battery_v,
                bthome.MOISTURE: r.moisture,
                bthome.BATTERY_LOW: (self.level >= power.LOW) if known else None,
                bthome.HEAT: None if r.temperature is None else r.temperature >= power.HOT_C,
                bthome.PROBLEM: None if r.battery_v is None else r.battery_v > power.OVERVOLTAGE_V,
            },
        })

    # --- settings (from the app) -------------------------------------------------------

    def save_settings(self):
        cfg = self.cfg
        with open(SETTINGS_FILE, "w") as f:
            json.dump({"bluetooth": {"name": cfg["bluetooth"]["name"]},
                       "soil": cfg["soil"], "interval_s": cfg["interval_s"]}, f)

    def set_name(self, name):
        self.cfg["bluetooth"]["name"] = name
        self.save_settings()

    def set_soil(self, dry_mv, wet_mv, interval_s):
        self.cfg["soil"] = {"dry_mv": dry_mv, "wet_mv": wet_mv}
        self.cfg["interval_s"] = interval_s
        self.save_settings()
        self.readings.recalculate(self.cfg["soil"])
        print("Soil: dry %d mV, wet %d mV, measuring every %d s" % (dry_mv, wet_mv, interval_s))

    def calibrate_from_probe(self, end):
        """The probe is in air ("dry") or water ("wet") now: take that end of the scale."""
        mv = self.sensors.soil_mv()
        soil = self.cfg["soil"]
        dry, wet = (mv, soil["wet_mv"]) if end == "dry" else (soil["dry_mv"], mv)
        if protocol.valid_calibration(dry, wet):
            self.set_soil(dry, wet, self.cfg["interval_s"])
        else:
            print("Soil: %s at %d mV refused (dry must be at least %d mV above wet)" % (end, mv, protocol.MV_GAP_MIN))

    def factory_reset(self):
        """Forget settings.json (back to config.json) and the paired phones, and restart.
        ble_passkey is from older firmware (pairing had a code then)."""
        for name in (SETTINGS_FILE, BONDS_FILE, "ble_passkey"):
            try:
                os.remove(name)
            except OSError:
                pass
        print("Factory reset")
        self.reboot_soon()

    def reboot_soon(self):
        self.reboot_at = time.ticks_add(time.ticks_ms(), 500)

    # --- main cycle -----------------------------------------------------------------

    def run(self):
        cfg = self.cfg
        if self.ble is None:   # cutoff: no radio at all, sleep long
            print("Battery: cutoff, sleeping %d h without radio; charge it" % (power.CUTOFF_INTERVAL_S // 3600))
            if not self.timer_wake:   # pressed BOOT or reset: show why nothing happens
                self.blink_for(_CUTOFF_SHOW_MS)
            self.sleep()
        app_s = None if self.timer_wake else power.awake_s(self.level, cfg["awake_s"])
        self.ble.app_visible = app_s is not None
        if app_s is not None:
            print("Soil sensor: %s (%s), BLE Sensor app and BTHome" % (self.ble.name, self.ble.address))
            print("Waiting %s for the app, then measuring every %d s in deep sleep. BOOT wakes it. "
                  "Ctrl+C for the REPL." % ("%d s" % app_s if app_s else "until it connects",
                                            power.interval_s(self.level, cfg["interval_s"])))
            window_s = app_s
        elif not self.timer_wake:   # pressed BOOT, but the battery is too low for the app
            window_s = _CUTOFF_SHOW_MS // 1000
        else:
            window_s = power.CRITICAL_ADVERTISE_S if self.level == power.CRITICAL else cfg["advertise_s"]
        now = time.ticks_ms()
        deadline = time.ticks_add(now, window_s * 1000) if window_s else None   # None: no limit
        was_connected = False
        next_measure = now
        while True:
            now = time.ticks_ms()
            # Only a paired phone keeps the board awake (and its leaving starts
            # the grace); a stranger is dropped by ble.py and changes nothing.
            connected = self.ble.connection is not None and self.ble.trusted
            if time.ticks_diff(now, next_measure) >= 0:
                self.measure()
                period = _CONNECTED_MEASURE_MS if connected else cfg["awake_measure_s"] * 1000
                next_measure = time.ticks_add(now, period)
            self.ble.poll()
            self.show_battery(now)
            if self.reboot_at is not None and time.ticks_diff(now, self.reboot_at) >= 0:
                self.ble.stop()
                machine.reset()
            if self.end_session:
                break
            if connected:
                was_connected, deadline = True, None
            elif was_connected:   # the phone left: sleep soon
                was_connected, deadline = False, time.ticks_add(now, _DISCONNECT_GRACE_MS)
            if self.ble.pairing_open() and self.ble.app_visible and deadline is not None \
                    and time.ticks_diff(self.ble.pairing_until, deadline) > 0:
                deadline = self.ble.pairing_until   # "add a phone": stay findable while the window is open
            if self.ble.checking():   # a phone is proving its bond (e.g. on a 3 s timed wake): wait for it
                time.sleep_ms(20)
                continue
            never = power.interval_s(self.level, cfg["interval_s"]) == 0   # can change from the app
            if not never and deadline is not None and time.ticks_diff(now, deadline) >= 0:
                break
            time.sleep_ms(20)
        self.sleep()

    # --- LED ------------------------------------------------------------------------

    def show_battery(self, now):
        """A short flash every power.blink_ms(level): faster as the battery runs
        down, off while it's fine. Identify (from the app) has the LED meanwhile."""
        period = power.blink_ms(self.level)
        if self.led is None or not period or (self.ble and self.ble.identify_until is not None):
            return
        self.led.set(time.ticks_diff(now, self.started) % period < _FLASH_MS)

    def blink_for(self, ms):
        end = time.ticks_add(time.ticks_ms(), ms)
        while time.ticks_diff(end, time.ticks_ms()) > 0:
            self.show_battery(time.ticks_ms())
            time.sleep_ms(10)

    def sleep(self):
        cfg = self.cfg
        if self.ble:
            self.ble.stop()
        if self.led:
            self.led.set(False)
        button = cfg["pins"]["button"]
        if button is not None:
            esp32.wake_on_ext0(pin=Pin(button, Pin.IN), level=esp32.WAKEUP_ALL_LOW)
        # Never 0 here: deepsleep(0) would sleep with no timer at all.
        seconds = power.interval_s(self.level, cfg["interval_s"]) or 60
        print("Deep sleep for %d s" % seconds)
        machine.deepsleep(seconds * 1000)


def run():
    App().run()
