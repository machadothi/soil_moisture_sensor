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
import protocol
import sensors
from ble import BONDS_FILE, Ble

SETTINGS_FILE = "settings.json"
_CONNECTED_MEASURE_MS = 2000
# Short, so the app can reconnect (e.g. Android re-connecting after pairing).
_DISCONNECT_GRACE_MS = 5000
_EXT0_WAKE = getattr(machine, "EXT0_WAKE", 2)

DEFAULTS = {
    # button: wakes the board from deep sleep (GPIO0 is the BOOT button).
    "pins": {"soil": 32, "dht11": 22, "battery": 34, "led": None, "button": 0},
    # Probe output in air and in water (mV). Measure yours: README.md, "Calibration".
    "soil": {"dry_mv": 2830, "wet_mv": 1300},
    # Battery through a divider, if the board has one. Readings outside
    # min_v-max_v are dropped (an unconnected pin reads ~0.2 V).
    "battery": {"divider": 2.0, "min_v": 2.8, "max_v": 4.5},
    # passkey: "" = made on the first start and kept in ble_passkey.
    "bluetooth": {"name": "", "passkey": ""},
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


# The BTHome packet id survives deep sleep in RTC memory, so every
# measurement gets a new one and Home Assistant never drops a reading.
def _next_packet_id():
    rtc = machine.RTC()
    memory = rtc.memory()
    packet_id = ((memory[0] if len(memory) == 1 else 0) + 1) & 0xFF
    rtc.memory(struct.pack("B", packet_id))
    return packet_id


class App:
    def __init__(self):
        self.cfg = load_config()
        self.started = time.ticks_ms()
        led_pin = self.cfg["pins"]["led"]
        self.led = Pin(led_pin, Pin.OUT, value=0) if led_pin is not None else None
        self.sensors = sensors.Sensors(self.cfg)
        self.readings = sensors.Readings()
        self.packet_id = 0
        self.reboot_at = None
        self.timer_wake = (machine.reset_cause() == machine.DEEPSLEEP_RESET
                           and machine.wake_reason() != _EXT0_WAKE)
        self.ble = Ble(self)

    # --- readings -------------------------------------------------------------------

    def measure(self):
        self.readings = self.sensors.read()
        self.packet_id = _next_packet_id()
        print(self.readings)
        self.publish()

    def publish(self):
        """The current readings and settings to GATT and the BTHome packet."""
        r, soil = self.readings, self.cfg["soil"]
        uptime = time.ticks_diff(time.ticks_ms(), self.started)
        self.ble.update({
            "env": protocol.env(uptime, r.temperature, r.humidity),
            "soil": protocol.soil(uptime, r.soil_mv, r.moisture, soil["dry_mv"], soil["wet_mv"],
                                  self.cfg["interval_s"], r.battery_v, r.battery),
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
        The passkey stays: it's on the board's label."""
        for name in (SETTINGS_FILE, BONDS_FILE):
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
        timer_wake = self.timer_wake
        self.ble.app_visible = not timer_wake
        if not timer_wake:
            print("Soil sensor: %s (%s), BLE Sensor app and BTHome" % (self.ble.name, self.ble.address))
            print("Bluetooth: pairing code %06d" % self.ble.passkey)
            if cfg["interval_s"]:
                print("Waiting %s for the app, then measuring every %d s in deep sleep. "
                      "BOOT wakes it. Ctrl+C for the REPL."
                      % ("%d s" % cfg["awake_s"] if cfg["awake_s"] else "until it connects", cfg["interval_s"]))
        now = time.ticks_ms()
        window_s = cfg["advertise_s"] if timer_wake else cfg["awake_s"]
        deadline = time.ticks_add(now, window_s * 1000) if window_s else None   # None: no limit
        was_connected = False
        next_measure = now
        while True:
            now = time.ticks_ms()
            connected = self.ble.connection is not None
            if time.ticks_diff(now, next_measure) >= 0:
                self.measure()
                period = _CONNECTED_MEASURE_MS if connected else cfg["awake_measure_s"] * 1000
                next_measure = time.ticks_add(now, period)
            self.ble.poll()
            if self.reboot_at is not None and time.ticks_diff(now, self.reboot_at) >= 0:
                self.ble.stop()
                machine.reset()
            if connected:
                was_connected, deadline = True, None
            elif was_connected:   # the phone left: sleep soon
                was_connected, deadline = False, time.ticks_add(now, _DISCONNECT_GRACE_MS)
            if cfg["interval_s"] and deadline is not None and time.ticks_diff(now, deadline) >= 0:
                break   # interval 0 = never sleep (can change from the app)
            time.sleep_ms(50)
        self.sleep()

    def sleep(self):
        cfg = self.cfg
        self.ble.stop()
        button = cfg["pins"]["button"]
        if button is not None:
            esp32.wake_on_ext0(pin=Pin(button, Pin.IN), level=esp32.WAKEUP_ALL_LOW)
        print("Deep sleep for %d s" % cfg["interval_s"])
        machine.deepsleep(cfg["interval_s"] * 1000)

def run():
    App().run()
