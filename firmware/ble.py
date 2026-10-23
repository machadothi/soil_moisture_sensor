# Bluetooth LE: the BLE Sensor GATT service, so the phone app finds and
# connects to this board, plus BTHome broadcasts for Home Assistant. The
# protocol is in protocol.py and docs/bluetooth.md.
#
# Advertising alternates every second between two packets (MicroPython has a
# single advertising slot):
#   app packet:    flags | 128-bit service UUID | manufacturer data
#                  (company 0x02FF, board id 0x0D, protocol version)
#   BTHome packet: flags | service data 0xFCD2 (bthome.py)
# The scan response carries the name. While a phone is connected, only the
# BTHome packet goes out (not connectable), so Home Assistant keeps getting data.
# On the timed wakes the app packet is left out too (app_visible = False): the
# app finds the board only after a button press, a reset or a deploy.
#
# Security, as on the ESP32 Air board: readings are public (BTHome broadcasts
# them anyway), but every write (name, calibration, commands) needs a paired
# phone. Pairing is LE Secure Connections with a 6-digit passkey the user
# types on the phone. This board has no display, so the passkey is fixed: from
# config.json, or made once and kept in ble_passkey (printed on the USB log;
# put it on a label). Someone nearby can't pair without it. Paired phones are
# kept in ble_bonds.json; a factory reset forgets them.
import binascii
import json
import os
import sys
import time

import bluetooth
from micropython import const

import bthome
import protocol

_IRQ_CENTRAL_CONNECT = const(1)
_IRQ_CENTRAL_DISCONNECT = const(2)
_IRQ_GATTS_WRITE = const(3)
_IRQ_ENCRYPTION_UPDATE = const(28)
_IRQ_GET_SECRET = const(29)
_IRQ_SET_SECRET = const(30)
_IRQ_PASSKEY_ACTION = const(31)
_PASSKEY_ACTION_DISPLAY = const(3)
_IO_DISPLAY_ONLY = const(0)

_FLAG_READ = const(0x0002)
_FLAG_WRITE = const(0x0008)
_FLAG_NOTIFY = const(0x0010)
_FLAG_WRITE_AUTHENTICATED = const(0x2000)

BONDS_FILE = "ble_bonds.json"
PASSKEY_FILE = "ble_passkey"
_BONDS_MAX = const(48)        # stored keys (a few per phone)
_PENDING_MAX = const(8)       # queued writes; more are dropped
_ADV_SWITCH_MS = const(1000)
_ADV_INTERVAL_US = const(100000)
_IDENTIFY_MS = const(3000)


def _service():
    """Writes need an authenticated (passkey) link; reads are public."""
    def uuid(short):
        return bluetooth.UUID(protocol.uuid(short))
    w = _FLAG_WRITE | _FLAG_WRITE_AUTHENTICATED
    return (bluetooth.UUID(protocol.SERVICE_UUID), (
        (uuid(0x01), _FLAG_READ | _FLAG_NOTIFY),         # Env
        (uuid(0x06), _FLAG_READ),                        # Info
        (uuid(0x07), w),                                 # Command
        (uuid(0x08), _FLAG_READ | w),                    # Name
        (uuid(0x0B), _FLAG_READ | _FLAG_NOTIFY),         # System
        (uuid(0x11), _FLAG_READ | w | _FLAG_NOTIFY),     # Soil
    ))


def load_passkey(configured):
    """The fixed pairing code: config.json's bluetooth.passkey, else the one made
    on the first start (kept across factory resets, it's on the label)."""
    text = str(configured or "")
    if len(text) == 6 and text.isdigit():
        return int(text)
    try:
        with open(PASSKEY_FILE) as f:
            text = f.read().strip()
        if len(text) == 6 and text.isdigit():
            return int(text)
    except OSError:
        pass
    passkey = int.from_bytes(os.urandom(4), "big") % 1000000
    with open(PASSKEY_FILE, "w") as f:
        f.write("%06d" % passkey)
    return passkey


class Ble:
    def __init__(self, app):
        self.app = app
        cfg = app.cfg["bluetooth"]
        self.ble = bluetooth.BLE()
        self.ble.active(True)
        mac = self.ble.config("mac")[1]
        self.address = ":".join("%02X" % b for b in mac)
        self.default_name = "Soil-%02X%02X" % (mac[4], mac[5])
        self.name = cfg["name"] or self.default_name
        self.passkey = load_passkey(cfg.get("passkey"))
        self.ble.config(gap_name=self.name)
        self.ble.config(bond=True, le_secure=True, mitm=True, io=_IO_DISPLAY_ONLY)
        self._bonds = self._load_bonds()
        self._bonds_dirty = False
        ((self.h_env, self.h_info, self.h_command, self.h_name, self.h_system, self.h_soil),) = \
            self.ble.gatts_register_services((_service(),))
        self.ble.gatts_set_buffer(self.h_name, protocol.NAME_MAX)
        self.ble.gatts_set_buffer(self.h_soil, protocol.SOIL_SIZE)
        self.connection = None
        self.pending = []          # writes from the IRQ, handled in poll()
        self.app_visible = True    # alternate with the app packet; False: BTHome only
        self.bthome_values = {}
        self._adv_index = 0
        self.identify_until = None
        self._next_adv = time.ticks_ms()
        self.ble.irq(self._irq)
        self.ble.gatts_write(self.h_info, protocol.info())
        self.ble.gatts_write(self.h_name, self.name.encode())

    # --- values -------------------------------------------------------------------

    def update(self, values):
        """New readings: values is {handle name: bytes} for env, soil, system,
        plus "bthome" (the BTHome objects). Notifies a connected phone."""
        self.bthome_values = values.pop("bthome")
        handles = {"env": self.h_env, "soil": self.h_soil, "system": self.h_system}
        for key, value in values.items():
            self.ble.gatts_write(handles[key], value)
            if self.connection is not None:
                try:
                    self.ble.gatts_notify(self.connection, handles[key], value)
                except OSError:   # not subscribed yet, or just disconnected
                    pass

    # --- main loop ---------------------------------------------------------------

    def poll(self):
        now = time.ticks_ms()
        if self._bonds_dirty:
            self._save_bonds()
        while self.pending:
            try:   # a bad write from a phone must not stop the board
                self._handle_write(*self.pending.pop(0))
            except Exception as e:
                sys.print_exception(e)
        if time.ticks_diff(now, self._next_adv) >= 0:
            self._next_adv = time.ticks_add(now, _ADV_SWITCH_MS)
            self._advertise()
        if self.identify_until is not None:
            led = self.app.led
            if time.ticks_diff(now, self.identify_until) >= 0:
                self.identify_until = None
                if led:
                    led.off()
            elif led:
                led.value(now // 100 % 2)

    def stop(self):
        if self._bonds_dirty:
            self._save_bonds()
        self.ble.gap_advertise(None)
        self.ble.active(False)

    # --- advertising --------------------------------------------------------------

    def _advertise(self):
        connected = self.connection is not None
        packets = [bthome.advertisement(self.bthome_values)]
        if self.app_visible and not connected:
            packets.append(protocol.app_advertisement())
        self._adv_index = (self._adv_index + 1) % len(packets)
        packet = packets[self._adv_index]
        try:
            self.ble.gap_advertise(_ADV_INTERVAL_US, adv_data=packet,
                                   resp_data=bthome.ad(0x09, self.name.encode()), connectable=not connected)
        except OSError as e:
            print("Bluetooth: advertising failed:", e)

    # --- pairing ------------------------------------------------------------------

    def _load_bonds(self):
        try:
            with open(BONDS_FILE) as f:
                return {tuple(k.split(":")): v for k, v in json.load(f).items()}
        except (OSError, ValueError):
            return {}

    def _save_bonds(self):
        with open(BONDS_FILE, "w") as f:
            json.dump({"%s:%s" % k: v for k, v in self._bonds.items()}, f)
        self._bonds_dirty = False

    def _set_secret(self, sec_type, key, value):
        key = (str(sec_type), binascii.hexlify(bytes(key)).decode())
        if value is None:
            if self._bonds.pop(key, None) is None:
                return False
        else:
            if key not in self._bonds and len(self._bonds) >= _BONDS_MAX:
                return False
            self._bonds[key] = binascii.hexlify(bytes(value)).decode()
        self._bonds_dirty = True   # written to flash in poll(), not in the callback
        return True

    def _get_secret(self, sec_type, index, key):
        if key is None:   # the index-th secret of this type
            i = 0
            for (t, _), value in self._bonds.items():
                if t == str(sec_type):
                    if i == index:
                        return binascii.unhexlify(value)
                    i += 1
            return None
        value = self._bonds.get((str(sec_type), binascii.hexlify(bytes(key)).decode()))
        return binascii.unhexlify(value) if value else None

    # --- events -------------------------------------------------------------------

    def _irq(self, event, data):
        # Runs in a callback: keep it short, the real work is done in poll().
        if event == _IRQ_CENTRAL_CONNECT:
            self.connection = data[0]
            self._next_adv = time.ticks_ms()
            print("Bluetooth: phone connected")
        elif event == _IRQ_CENTRAL_DISCONNECT:
            self.connection = None
            self._next_adv = time.ticks_ms()
            print("Bluetooth: phone disconnected")
        elif event == _IRQ_GATTS_WRITE:
            handle = data[1]
            if len(self.pending) < _PENDING_MAX:
                self.pending.append((handle, bytes(self.ble.gatts_read(handle))))
        elif event == _IRQ_PASSKEY_ACTION:
            conn, action, _ = data
            if action == _PASSKEY_ACTION_DISPLAY:
                self.ble.gap_passkey(conn, action, self.passkey)
                # USB access is full access anyway; the code is on the log at start-up too.
                print("Bluetooth: a phone is pairing, code %06d" % self.passkey)
        elif event == _IRQ_ENCRYPTION_UPDATE:
            _, encrypted, authenticated, bonded, _ = data
            print("Bluetooth: link", "encrypted" if encrypted else "not encrypted",
                  "(authenticated)" if authenticated else "", "(paired)" if bonded else "")
        elif event == _IRQ_GET_SECRET:
            return self._get_secret(*data)
        elif event == _IRQ_SET_SECRET:
            return self._set_secret(*data)

    def _handle_write(self, handle, value):
        # MicroPython can't refuse a write over the air: a rejected value is
        # replaced by the old one, and the app sees that when it reads back.
        app = self.app
        if handle == self.h_command and value:
            command = value[0]
            print("Bluetooth: command", command)
            if command == protocol.COMMAND_REBOOT:
                app.reboot_soon()
            elif command == protocol.COMMAND_IDENTIFY:
                self.identify_until = time.ticks_add(time.ticks_ms(), _IDENTIFY_MS)
            elif command == protocol.COMMAND_FACTORY_RESET:
                app.factory_reset()
        elif handle == self.h_name:
            try:
                name = value.decode().strip()
            except UnicodeError:
                name = ""
            if 0 < len(value) <= protocol.NAME_MAX and name:
                self.name = name
                app.set_name(name)
                self.ble.config(gap_name=name)
                print("Bluetooth: renamed to", name)
            self.ble.gatts_write(self.h_name, self.name.encode())
        elif handle == self.h_soil:
            soil, cfg = app.cfg["soil"], app.cfg
            action, values = protocol.parse_soil_write(value, soil["dry_mv"], soil["wet_mv"], cfg["interval_s"])
            if action in ("dry", "wet"):
                app.calibrate_from_probe(action)
            elif action == "set":
                app.set_soil(*values)
            else:
                print("Bluetooth: soil write ignored:", binascii.hexlify(value))
            app.publish()   # the value in use, accepted or not
