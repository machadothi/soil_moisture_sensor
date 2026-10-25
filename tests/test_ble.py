"""ble.py's pairing rules, with a fake of MicroPython's bluetooth module: only
bonded phones stay; pairing (no code) only while the window is open, which a
start the user caused opens (never a timed wake)."""

import json
import sys
import time
import types

import pytest

CLOCK = [0]


class FakeBLE:
    def __init__(self):
        self.settings, self.values, self.disconnects, self.security_requests = {}, {}, [], []
        self.irq_handler = None
        FakeBLE.last = self

    def active(self, *a):
        return True

    def config(self, *args, **kwargs):
        if args:
            return {"mac": (0, b"\xb0\xb2\x1c\x44\x7d\x82")}.get(args[0])
        self.settings.update(kwargs)

    def irq(self, handler):
        self.irq_handler = handler

    def gatts_register_services(self, services):
        return [tuple(range(1, 1 + len(services[0][1])))]

    def gatts_set_buffer(self, *a):
        pass

    def gatts_write(self, handle, value):
        self.values[handle] = bytes(value)

    def gatts_read(self, handle):
        return self.values.get(handle, b"")

    def gatts_notify(self, *a):
        pass

    def gap_advertise(self, *a, **k):
        self.advert = k.get("adv_data")

    def gap_pair(self, conn):
        self.security_requests.append(conn)

    def gap_disconnect(self, conn):
        self.disconnects.append(conn)
        self.irq_handler(2, (conn, 0, b"\0" * 6))

    # what a phone does
    def connects(self, bond=None, pair_as=None):
        self.irq_handler(1, (0, 0, b"\x11" * 6))
        if pair_as is not None:   # like NimBLE: encrypted first, new keys after
            self.irq_handler(28, (0, True, False, True, 16))
            self.irq_handler(30, (2, pair_as, b"peer-keys"))
            self.irq_handler(30, (1, pair_as, b"our-keys"))
        elif bond == "known":
            self.irq_handler(28, (0, True, False, True, 16))

    def writes(self, handle, value):
        self.values[handle] = bytes(value)
        self.irq_handler(3, (0, handle))


@pytest.fixture
def ble(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    CLOCK[0] = 0
    monkeypatch.setattr(time, "ticks_ms", lambda: CLOCK[0], raising=False)
    monkeypatch.setattr(time, "ticks_add", lambda a, b: a + b, raising=False)
    monkeypatch.setattr(time, "ticks_diff", lambda a, b: a - b, raising=False)
    bluetooth = types.ModuleType("bluetooth")
    bluetooth.BLE = FakeBLE
    bluetooth.UUID = lambda value: value
    micropython = types.ModuleType("micropython")
    micropython.const = lambda x: x
    monkeypatch.setitem(sys.modules, "bluetooth", bluetooth)
    monkeypatch.setitem(sys.modules, "micropython", micropython)
    sys.modules.pop("ble", None)
    import ble as module
    return module


class App:
    def __init__(self, timer_wake=False):
        self.cfg = {"bluetooth": {"name": ""}}
        self.timer_wake = timer_wake
        self.led = None
        self.reboots = 0

    def reboot_soon(self):
        self.reboots += 1


def bonded_before():
    json.dump({"2:" + b"phone-1".hex(): "aa", "1:" + b"phone-1".hex(): "bb"}, open("ble_bonds.json", "w"))


def run(b, seconds):
    for _ in range(int(seconds * 10)):
        CLOCK[0] += 100
        b.poll()


def test_writes_need_encryption_and_no_code(ble):
    ble.Ble(App())
    s = FakeBLE.last.settings
    assert s["bond"] and s["le_secure"] and not s["mitm"] and s["io"] == 3


def test_user_start_opens_the_window_timed_wake_doesnt(ble):
    bonded_before()
    assert ble.Ble(App()).pairing_open()
    assert not ble.Ble(App(timer_wake=True)).pairing_open()


def test_window_is_2_minutes_and_10_on_first_setup(ble):
    bonded_before()
    b = ble.Ble(App())
    CLOCK[0] += 121 * 1000
    assert not b.pairing_open()
    import os
    os.remove("ble_bonds.json")
    first = ble.Ble(App())
    CLOCK[0] += 590 * 1000
    assert first.pairing_open()


def test_window_is_advertised(ble):
    b = ble.Ble(App())
    b._advertise()
    b._advertise()   # the app packet alternates with BTHome
    packets = [FakeBLE.last.advert]
    b._advertise()
    packets.append(FakeBLE.last.advert)
    app_packet = [p for p in packets if b"\xff\xff\x02\x0d" in p][0]
    assert app_packet.endswith(b"\x0d\x01\x01")


def test_stranger_is_dropped_on_a_timed_wake(ble):
    bonded_before()
    b = ble.Ble(App(timer_wake=True))
    FakeBLE.last.connects()
    run(b, 3)
    assert b.checking()                      # app.py stays awake meanwhile
    run(b, 5)
    assert FakeBLE.last.disconnects == [0] and not b.checking()


def test_bonded_phone_is_trusted_on_a_timed_wake(ble):
    bonded_before()
    b = ble.Ble(App(timer_wake=True))
    FakeBLE.last.connects(bond="known")
    run(b, 2)
    assert b.trusted and not FakeBLE.last.disconnects


def test_new_pairing_outside_the_window_is_refused(ble):
    bonded_before()
    b = ble.Ble(App(timer_wake=True))
    FakeBLE.last.connects(pair_as=b"stranger")
    FakeBLE.last.writes(b.h_command, b"\x03")   # reboot: must not happen
    run(b, 3)
    assert not b.trusted and FakeBLE.last.disconnects == [0] and b.app.reboots == 0


def test_re_pairing_a_known_address_keeps_the_bond(ble):
    bonded_before()
    b = ble.Ble(App(timer_wake=True))
    FakeBLE.last.connects()
    h = FakeBLE.last.irq_handler
    assert h(30, (2, b"phone-1", None)) is True      # NimBLE's delete: "done" once
    assert h(30, (2, b"phone-1", None)) is False     # then "not found" (it loops until it hears that)
    assert h(29, (2, 0, b"phone-1")) is None         # hidden meanwhile
    run(b, 1)
    FakeBLE.last.irq_handler(2, (0, 0, b""))         # disconnect: back
    assert h(29, (2, 0, b"phone-1")) == b"\xaa" and b.bonded_phones() == 1


def test_new_phone_pairs_in_the_window(ble):
    bonded_before()
    b = ble.Ble(App())
    FakeBLE.last.connects(pair_as=b"phone-2")
    run(b, 2)
    assert b.trusted and b.bonded_phones() == 2


def test_add_a_phone_opens_the_window_and_shows_the_board(ble):
    bonded_before()
    b = ble.Ble(App(timer_wake=True))
    b.app_visible = False
    FakeBLE.last.connects(bond="known")
    FakeBLE.last.writes(b.h_command, b"\x06")
    run(b, 2)
    assert b.pairing_open() and b.app_visible
