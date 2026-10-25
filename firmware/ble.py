# Bluetooth LE: the BLE Sensor GATT service, so the phone app finds and
# connects to this board, plus BTHome broadcasts for Home Assistant. The
# protocol is in protocol.py and docs/bluetooth.md.
#
# Advertising alternates every second between two packets (MicroPython has a
# single advertising slot):
#   app packet:    flags | 128-bit service UUID | manufacturer data
#                  (company 0x02FF, board id 0x0D, protocol version, flags:
#                  bit 0 = pairing window open)
#   BTHome packet: flags | service data 0xFCD2 (bthome.py)
# The scan response carries the name. While a phone is connected, only the
# BTHome packet goes out (not connectable), so Home Assistant keeps getting data.
# On the timed wakes the app packet is left out too (app_visible = False): the
# app finds the board only after a button press, a reset or a deploy.
#
# Security, as on the ESP32 Air board (docs/bluetooth.md#pairing-and-security):
# - Readings are public: BTHome broadcasts them, for Home Assistant.
# - Only bonded phones stay connected, and only they can write. Pairing needs
#   no code ("Just Works", LE Secure Connections, encrypted) but works only
#   while the pairing window is open: the first 2 minutes after a start the
#   user caused (power-on, EN, BOOT; never the timed wakes), 10 minutes while
#   no phone is bonded yet, or 2 minutes after a bonded phone's "add a phone".
# - Every phone that connects is asked to encrypt with its bond (a security
#   request). One that can't is disconnected after a few seconds, unless the
#   window is open; a new bond outside the window is refused. The link is
#   trusted a second after encryption, because NimBLE stores new keys after
#   the encryption event. (MicroPython can't advertise to bonded phones only.)
# Bonds are kept in ble_bonds.json; a factory reset forgets them.
import binascii
import json
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
_IO_NO_INPUT_OUTPUT = const(3)
_SECRET_OUR_KEYS = const(1)      # sec_type of bond keys (3 = notification settings)
_SECRET_PEER_KEYS = const(2)

_FLAG_READ = const(0x0002)
_FLAG_WRITE = const(0x0008)
_FLAG_NOTIFY = const(0x0010)
_FLAG_WRITE_ENCRYPTED = const(0x1000)

BONDS_FILE = "ble_bonds.json"
PAIRING_WINDOW_S = const(120)   # after a start the user caused, and after "add a phone"
FIRST_PAIRING_S = const(600)    # the same, while no phone is bonded yet
_ASK_AFTER_MS = const(1500)     # ask for encryption only then: a phone pairing by itself goes first
_SETTLE_MS = const(1000)        # after encryption, wait this long for new bond keys before trusting
_PROVE_MS = const(6000)         # a connected phone must encrypt with its bond within this
_PAIRING_MS = const(60000)      # ... or, in the window, finish pairing (the user taps "Pair")
_BONDS_MAX = const(48)        # stored keys (a few per phone)
_PENDING_MAX = const(8)       # queued writes; more are dropped
_ADV_SWITCH_MS = const(1000)
_ADV_INTERVAL_US = const(100000)
_IDENTIFY_MS = const(3000)


def _service():
    """Writes need the encrypted link of a bonded phone; reads are public."""
    def uuid(short):
        return bluetooth.UUID(protocol.uuid(short))
    w = _FLAG_WRITE | _FLAG_WRITE_ENCRYPTED
    return (bluetooth.UUID(protocol.SERVICE_UUID), (
        (uuid(0x01), _FLAG_READ | _FLAG_NOTIFY),         # Env
        (uuid(0x06), _FLAG_READ),                        # Info
        (uuid(0x07), w),                                 # Command
        (uuid(0x08), _FLAG_READ | w),                    # Name
        (uuid(0x0B), _FLAG_READ | _FLAG_NOTIFY),         # System
        (uuid(0x11), _FLAG_READ | w | _FLAG_NOTIFY),     # Soil
    ))


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
        self.ble.config(gap_name=self.name)
        # Pairing without a code (Just Works), only while the pairing window is open.
        self.ble.config(bond=True, le_secure=True, mitm=False, io=_IO_NO_INPUT_OUTPUT)
        self._bonds = self._load_bonds()
        self._bonds_dirty = False
        self._hidden = set()           # keys NimBLE was told are deleted (see _set_secret)
        self.pairing_until = None      # ticks when the pairing window closes; None = closed
        self.trusted = False           # the connected phone proved its bond (or just bonded in the window)
        self._connected_at = self._encrypted_at = None
        self._security_asked = self._new_bond = self._kick = self._dropping = False
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
        if not app.timer_wake:   # a start the user caused: power-on, EN, BOOT, a deploy
            if self.bonded_phones():
                self.open_pairing(PAIRING_WINDOW_S, "after start-up")
            else:
                self.open_pairing(FIRST_PAIRING_S, "no phone paired yet")
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
        self._check_connection(now)
        while self.pending and (self.connection is None or self.trusted):   # held until the bond is checked
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
                    led.set(False)
            elif led:
                led.set(now // 100 % 2)

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
            packets.append(protocol.app_advertisement(protocol.ADV_PAIRING_OPEN if self.pairing_open() else 0))
        self._adv_index = (self._adv_index + 1) % len(packets)
        packet = packets[self._adv_index]
        try:
            self.ble.gap_advertise(_ADV_INTERVAL_US, adv_data=packet,
                                   resp_data=bthome.ad(0x09, self.name.encode()), connectable=not connected)
        except OSError as e:
            print("Bluetooth: advertising failed:", e)

    # --- pairing ------------------------------------------------------------------

    def bonded_phones(self):
        """How many phones are bonded (each has one set of peer keys)."""
        return sum(1 for t, _ in self._bonds if t == str(_SECRET_PEER_KEYS))

    def pairing_open(self):
        if self.pairing_until is not None and time.ticks_diff(time.ticks_ms(), self.pairing_until) >= 0:
            self.pairing_until = None
            print("Bluetooth: pairing window closed")
        return self.pairing_until is not None

    def open_pairing(self, seconds, why):
        """Lets new phones pair for that long; the app packet goes out meanwhile."""
        self.pairing_until = time.ticks_add(time.ticks_ms(), seconds * 1000)
        self.app_visible = True
        self._next_adv = time.ticks_ms()   # advertise the open window at once
        print("Bluetooth: pairing window open for %d s (%s)" % (seconds, why))

    def checking(self):
        """A phone is connected and its bond isn't checked yet: stay awake."""
        return self.connection is not None and not self.trusted and not self._dropping

    def _check_connection(self, now):
        """Only bonded phones stay: ask the new connection to encrypt with its
        bond, and drop it if it can't (outside the pairing window). Encrypted
        alone isn't enough: a phone pairing anew also ends up encrypted, and
        NimBLE stores its new keys only after the encryption event. So the link
        is trusted _SETTLE_MS later, if no new keys came meanwhile, or if they
        came while the pairing window is open."""
        conn = self.connection
        if conn is None or self._dropping:   # (a disconnect takes a moment: don't repeat it)
            return
        if self._kick:
            self._disconnect(conn)
            return
        if self.trusted:
            return
        if self._encrypted_at is not None and time.ticks_diff(now, self._encrypted_at) >= _SETTLE_MS:
            if self._new_bond and not self.pairing_open():
                self._disconnect(conn)
                return
            self.trusted = True
            print("Bluetooth: %s, link encrypted" % ("new phone paired" if self._new_bond else "paired phone"))
            return
        if not self._security_asked and time.ticks_diff(now, self._connected_at) >= _ASK_AFTER_MS:
            self._security_asked = True
            try:
                self.ble.gap_pair(conn)   # a bonded phone encrypts; a new one pairs (if allowed)
            except OSError as e:
                print("Bluetooth: security request failed:", e)
        limit = _PAIRING_MS if self.pairing_open() else _PROVE_MS
        if time.ticks_diff(now, self._connected_at) > limit:
            self._disconnect(conn)

    def _disconnect(self, conn):
        self._dropping = True
        print("Bluetooth: phone not paired, disconnected%s" % (
            "" if self.pairing_open() else " (press BOOT to pair a new phone: 2 minutes)"))
        self._kick = False
        self.trusted = False
        self.pending = []
        try:
            self.ble.gap_disconnect(conn)
        except OSError:
            pass

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
        new_bond = sec_type in (_SECRET_OUR_KEYS, _SECRET_PEER_KEYS)
        key = (str(sec_type), binascii.hexlify(bytes(key)).decode())
        if value is None:
            # NimBLE deletes a bond before a phone pairs again: only in the window,
            # else copying a phone's address would wipe the bonds. Outside it the
            # keys are kept but hidden from NimBLE until this connection ends.
            # NimBLE deletes in a loop until it hears "not found" (False): so
            # "deleted" once, then "not found"; anything else makes it loop
            # forever (seen on the ESP32 Air board). The new keys that follow are
            # refused, and that disconnects.
            if key in self._hidden:
                return False
            if new_bond and key in self._bonds and not self.pairing_open():
                self._hidden.add(key)
                return True
            if self._bonds.pop(key, None) is None:
                return False
        else:
            value = binascii.hexlify(bytes(value)).decode()
            # New keys, or different keys for an address we know, are a new
            # pairing: only in the window.
            if new_bond and self._bonds.get(key) != value:
                self._new_bond = True
                if not self.pairing_open():
                    self._kick = True
                    return False
            if key not in self._bonds and len(self._bonds) >= _BONDS_MAX:
                return False
            self._bonds[key] = value
        self._bonds_dirty = True   # written to flash in poll(), not in the callback
        return True

    def _get_secret(self, sec_type, index, key):
        if key is None:   # the index-th secret of this type
            i = 0
            for k, value in self._bonds.items():
                if k[0] == str(sec_type) and k not in self._hidden:
                    if i == index:
                        return binascii.unhexlify(value)
                    i += 1
            return None
        key = (str(sec_type), binascii.hexlify(bytes(key)).decode())
        value = None if key in self._hidden else self._bonds.get(key)
        return binascii.unhexlify(value) if value else None

    # --- events -------------------------------------------------------------------

    def _irq(self, event, data):
        # Runs in a callback: keep it short, the real work is done in poll().
        if event == _IRQ_CENTRAL_CONNECT:
            self.connection = data[0]
            self._connected_at = time.ticks_ms()
            self.trusted = self._security_asked = self._new_bond = self._kick = self._dropping = False
            self._encrypted_at = None
            self._next_adv = time.ticks_ms()
            print("Bluetooth: phone connected")
        elif event == _IRQ_CENTRAL_DISCONNECT:
            self.connection = None
            self.trusted = False
            self.pending = []
            self._hidden = set()
            self._next_adv = time.ticks_ms()
            print("Bluetooth: phone disconnected")
        elif event == _IRQ_GATTS_WRITE:
            handle = data[1]
            if len(self.pending) < _PENDING_MAX:
                self.pending.append((handle, bytes(self.ble.gatts_read(handle))))
        elif event == _IRQ_ENCRYPTION_UPDATE:
            _, encrypted, _, bonded, _ = data
            if encrypted and bonded:
                self._encrypted_at = time.ticks_ms()   # trusted in poll(), see _check_connection
            elif not self.pairing_open():   # in the window a failed try may be followed by a pairing
                self._kick = True
        elif event == _IRQ_GET_SECRET:
            return self._get_secret(*data)
        elif event == _IRQ_SET_SECRET:
            return self._set_secret(*data)

    def _handle_write(self, handle, value):
        # MicroPython can't refuse a write over the air: a rejected value is
        # replaced by the old one, and the app sees that when it reads back.
        app = self.app
        if not self.trusted:   # (the flags already need encryption; this is the bond check)
            print("Bluetooth: write from a phone that isn't paired, ignored")
            return
        if handle == self.h_command and value:
            command = value[0]
            print("Bluetooth: command", command)
            if command == protocol.COMMAND_REBOOT:
                app.reboot_soon()
            elif command == protocol.COMMAND_IDENTIFY:
                self.identify_until = time.ticks_add(time.ticks_ms(), _IDENTIFY_MS)
            elif command == protocol.COMMAND_FACTORY_RESET:
                app.factory_reset()
            elif command == protocol.COMMAND_OPEN_PAIRING:
                self.open_pairing(PAIRING_WINDOW_S, "asked by a paired phone")
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
