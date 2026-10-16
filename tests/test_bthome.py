import struct

import bthome


def test_header_and_order():
    data = bthome.service_data({bthome.MOISTURE: 42.5, bthome.PACKET_ID: 7, bthome.TEMPERATURE: -1.25})
    assert data[:3] == b"\xd2\xfc\x40"
    # Objects in id order: packet id, temperature, moisture.
    assert data[3:] == bytes((0x00, 7)) + b"\x02" + struct.pack("<h", -125) + b"\x14" + struct.pack("<H", 4250)


def test_missing_values_are_left_out():
    data = bthome.service_data({bthome.PACKET_ID: 1, bthome.TEMPERATURE: None, bthome.HUMIDITY: 55})
    assert data[3:] == bytes((0x00, 1, 0x03)) + struct.pack("<H", 5500)


def test_battery_and_voltage():
    data = bthome.service_data({bthome.BATTERY: 80, bthome.VOLTAGE: 3.912})
    assert data[3:] == bytes((0x01, 80, 0x0C)) + struct.pack("<H", 3912)


def test_advertisement_fits():
    # Every value present must still fit a legacy 31-byte advertising packet.
    adv = bthome.advertisement({bthome.PACKET_ID: 255, bthome.BATTERY: 100, bthome.TEMPERATURE: 25.0,
                                bthome.HUMIDITY: 60, bthome.VOLTAGE: 4.2, bthome.MOISTURE: 100})
    assert adv[:3] == b"\x02\x01\x06"
    assert adv[3] == len(adv) - 4 and adv[4] == 0x16
    assert len(adv) <= 31
