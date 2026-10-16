# BTHome v2 advertising data (https://bthome.io/format/), which Home Assistant's
# BTHome integration picks up with no Wi-Fi, broker or pairing.
#
# A packet is the service data AD structure for UUID 0xFCD2:
#   device info byte (0x40: BTHome v2, not encrypted, sent at regular intervals)
#   then objects, each an object id followed by its value, little-endian,
#   in increasing id order.
# A reading that is None is left out, so Home Assistant keeps its last value.
#
# Pure Python (struct only): the PC tests import it as it is.
import struct

SERVICE_UUID = 0xFCD2
DEVICE_INFO = 0x40

# Object ids and how each value is packed (format, factor). The factor turns
# the reading into the integer BTHome expects, e.g. 0.01 °C steps.
PACKET_ID = 0x00      # u8, changes per measurement so Home Assistant ignores repeats
BATTERY = 0x01        # u8, %
TEMPERATURE = 0x02    # s16, 0.01 °C
HUMIDITY = 0x03       # u16, 0.01 %
VOLTAGE = 0x0C        # u16, 0.001 V
MOISTURE = 0x14       # u16, 0.01 %

_FORMATS = {
    PACKET_ID: ("<B", 1),
    BATTERY: ("<B", 1),
    TEMPERATURE: ("<h", 100),
    HUMIDITY: ("<H", 100),
    VOLTAGE: ("<H", 1000),
    MOISTURE: ("<H", 100),
}


def service_data(values):
    """values: {object id: reading or None}. Returns the service data payload:
    UUID, device info and the objects in id order."""
    data = bytearray(struct.pack("<HB", SERVICE_UUID, DEVICE_INFO))
    for object_id in sorted(values):
        value = values[object_id]
        if value is None:
            continue
        fmt, factor = _FORMATS[object_id]
        data += bytes((object_id,)) + struct.pack(fmt, round(value * factor))
    return bytes(data)


def ad(ad_type, payload):
    """One advertising data structure: length, type, payload."""
    return bytes((len(payload) + 1, ad_type)) + payload


def advertisement(values):
    """Flags (LE General Discoverable, no BR/EDR) + the BTHome service data."""
    return ad(0x01, b"\x06") + ad(0x16, service_data(values))
