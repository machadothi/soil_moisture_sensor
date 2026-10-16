# The BLE Sensor protocol (~/git/BLE_Sensor, docs/capabilities.md), as this
# board speaks it: board id 0x0D "ESP32 Soil". The phone app finds the board
# by the service UUID and the manufacturer data in its advertising, then reads
# these packed little-endian values over GATT. Layouts: docs/bluetooth.md.
#
# Pure Python (struct only): the PC tests import it as it is.
import binascii
import struct

from bthome import ad

VERSION = 1
BOARD_ID = 0x0D            # "ESP32 Soil" (Thunderboards 0x0A/0x0B, ESP32 Air 0x0C)
COMPANY_ID = 0x02FF
SERVICE_UUID = "a7e40000-5c2b-4f1a-9d3e-6b8c0f2e1d47"

SENSOR_RHT = 1 << 0
SENSOR_SOIL = 1 << 7

COMMAND_FACTORY_RESET = 0x02
COMMAND_REBOOT = 0x03
COMMAND_IDENTIFY = 0x04

NAME_MAX = 20
SOIL_SIZE = 20

# Allowed Soil writes; anything else is ignored and the old value written back.
MV_MIN, MV_MAX = 100, 3300
MV_GAP_MIN = 200            # dry must be at least this far above wet
INTERVAL_MIN_S, INTERVAL_MAX_S = 60, 65535   # or 0: never sleep

_NONE_U16 = 0xFFFF
_NONE_I16 = 0x7FFF
_ENV_VALID_TEMPERATURE = 1 << 0
_ENV_VALID_HUMIDITY = 1 << 1


def uuid(short):
    return "a7e400%02x-5c2b-4f1a-9d3e-6b8c0f2e1d47" % short


def uuid_bytes(text):
    """A 128-bit UUID as it goes on air: little-endian."""
    return bytes(reversed(binascii.unhexlify(text.replace("-", ""))))


def app_advertisement():
    """Flags, the service UUID and manufacturer data: what the app scans for."""
    return (ad(0x01, b"\x06") + ad(0x07, uuid_bytes(SERVICE_UUID))
            + ad(0xFF, struct.pack("<HBB", COMPANY_ID, BOARD_ID, VERSION)))


def info():
    return struct.pack("<BBBB", VERSION, BOARD_ID, SENSOR_RHT | SENSOR_SOIL, 0)


def env(uptime_ms, temperature, humidity):
    """The Thunderboard's 27-byte Env layout; only temperature and humidity are filled in."""
    valid = 0
    t = h = 0
    if temperature is not None:
        valid |= _ENV_VALID_TEMPERATURE
        t = round(temperature * 100)
    if humidity is not None:
        valid |= _ENV_VALID_HUMIDITY
        h = round(humidity * 100)
    return struct.pack("<IHhHIHiBhHh", uptime_ms & 0xFFFFFFFF, valid, t, h, 0, 0, 0, 0, 0, 0, 0)


def soil(uptime_ms, soil_mv, moisture, dry_mv, wet_mv, interval_s, battery_v, battery_pct):
    """Soil, 20 bytes:
    u32 uptime ms, u16 probe mV (0 = none), u16 moisture % x 100 (0xFFFF = none),
    u16 dry mV, u16 wet mV, u16 deep sleep between measurements s (0 = never),
    u16 battery mV (0 = none), u8 battery % (0xFF = none), 3 bytes reserved."""
    return struct.pack(
        "<IHHHHHHB3s", uptime_ms & 0xFFFFFFFF,
        0 if soil_mv is None else round(soil_mv),
        _NONE_U16 if moisture is None else round(moisture * 100),
        dry_mv, wet_mv, interval_s,
        0 if battery_v is None else round(battery_v * 1000),
        0xFF if battery_pct is None else battery_pct, b"\0\0\0")


def system(uptime_s, free_ram, chip_c, bluetooth, cpu_mhz, reset_cause, micropython, sensor_errors):
    """The ESP32 Air board's 32-byte System layout. No Wi-Fi on this board:
    RSSI, IP and the Wi-Fi/MQTT flags stay 0."""
    return struct.pack(
        "<IIhbB4sHBBBBHHIH", uptime_s, free_ram,
        _NONE_I16 if chip_c is None else round(chip_c * 100), 0, 4 if bluetooth else 0, b"\0\0\0\0",
        cpu_mhz, reset_cause, micropython[0], micropython[1], micropython[2],
        min(sensor_errors, 0xFFFF), 0, 0, 0)


def parse_soil_write(value, dry_mv, wet_mv, interval_s):
    """A write to Soil:
      1 byte:  1 = the probe's current reading is "dry", 2 = it is "wet";
      4 bytes: u16 dry mV, u16 wet mV;
      6 bytes: u16 dry mV, u16 wet mV, u16 interval s.
    Returns ("dry" | "wet", None) for the 1-byte form, ("set", (dry, wet, interval))
    for a valid 4/6-byte write, (None, None) for anything else."""
    if len(value) == 1 and value[0] in (1, 2):
        return ("dry" if value[0] == 1 else "wet"), None
    if len(value) in (4, 6):
        dry, wet = struct.unpack("<HH", value[:4])
        interval = struct.unpack("<H", value[4:6])[0] if len(value) == 6 else interval_s
        if valid_calibration(dry, wet) and valid_interval(interval):
            return "set", (dry, wet, interval)
    return None, None


def valid_calibration(dry_mv, wet_mv):
    return MV_MIN <= wet_mv and dry_mv <= MV_MAX and dry_mv - wet_mv >= MV_GAP_MIN


def valid_interval(interval_s):
    return interval_s == 0 or INTERVAL_MIN_S <= interval_s <= INTERVAL_MAX_S
