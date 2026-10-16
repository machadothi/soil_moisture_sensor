import struct

import protocol


def test_app_advertisement_is_what_the_app_scans_for():
    adv = protocol.app_advertisement()
    assert len(adv) <= 31
    # 128-bit service UUID, little-endian on air
    assert bytes((17, 0x07)) + bytes(reversed(bytes.fromhex("a7e400005c2b4f1a9d3e6b8c0f2e1d47"))) in adv
    # manufacturer data: company 0x02FF, board id 0x0D, protocol version 1
    assert adv.endswith(bytes((5, 0xFF, 0xFF, 0x02, 0x0D, 0x01)))


def test_info():
    assert protocol.info() == bytes((1, 0x0D, 0x81, 0))


def test_env_layout():
    value = protocol.env(1234, 22.5, 61)
    assert len(value) == 27
    uptime, valid, t, h = struct.unpack_from("<IHhH", value)
    assert (uptime, valid, t, h) == (1234, 0b11, 2250, 6100)
    assert protocol.env(0, None, None)[4:6] == b"\0\0"


def test_soil_layout():
    value = protocol.soil(5000, 2302, 34.51, 2830, 1300, 300, None, None)
    assert len(value) == protocol.SOIL_SIZE
    assert struct.unpack("<IHHHHHHB", value[:17]) == (5000, 2302, 3451, 2830, 1300, 300, 0, 0xFF)
    missing = protocol.soil(0, None, None, 2830, 1300, 0, 3.9, 72)
    assert struct.unpack("<HH", missing[4:8]) == (0, 0xFFFF)
    assert struct.unpack("<HB", missing[14:17]) == (3900, 72)


def test_system_layout():
    value = protocol.system(65, 140000, 52.3, True, 160, 4, (1, 25, 0), 2)
    assert len(value) == 32
    fields = struct.unpack("<IIhbB4sHBBBBHHIH", value)
    assert fields == (65, 140000, 5230, 0, 4, b"\0\0\0\0", 160, 4, 1, 25, 0, 2, 0, 0, 0)


def test_soil_writes():
    parse = protocol.parse_soil_write
    assert parse(b"\x01", 2830, 1300, 300) == ("dry", None)
    assert parse(b"\x02", 2830, 1300, 300) == ("wet", None)
    assert parse(struct.pack("<HH", 2900, 1200), 2830, 1300, 300) == ("set", (2900, 1200, 300))
    assert parse(struct.pack("<HHH", 2900, 1200, 600), 2830, 1300, 300) == ("set", (2900, 1200, 600))
    assert parse(struct.pack("<HHH", 2900, 1200, 0), 2830, 1300, 300) == ("set", (2900, 1200, 0))


def test_bad_soil_writes_are_ignored():
    parse = protocol.parse_soil_write
    assert parse(b"", 2830, 1300, 300) == (None, None)
    assert parse(b"\x03", 2830, 1300, 300) == (None, None)
    assert parse(struct.pack("<HH", 1300, 2830), 2830, 1300, 300) == (None, None)    # dry below wet
    assert parse(struct.pack("<HH", 2000, 1900), 2830, 1300, 300) == (None, None)    # too close
    assert parse(struct.pack("<HH", 4000, 1300), 2830, 1300, 300) == (None, None)    # out of range
    assert parse(struct.pack("<HHH", 2900, 1200, 30), 2830, 1300, 300) == (None, None)  # interval too short
    assert parse(b"\x01\x02\x03", 2830, 1300, 300) == (None, None)
