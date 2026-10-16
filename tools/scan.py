#!/usr/bin/env python3
"""Listen for BTHome broadcasts from the PC and decode them, to check what
Home Assistant will receive.

    .venv/bin/python tools/scan.py              # every BTHome device, 30 s
    .venv/bin/python tools/scan.py Soil-7D82 60 # one name (or address), 60 s
"""
import asyncio
import struct
import sys

from bleak import BleakScanner

BTHOME_UUID = "0000fcd2-0000-1000-8000-00805f9b34fb"
# id: (name, format, factor, unit), the subset the firmware sends (firmware/bthome.py)
OBJECTS = {
    0x00: ("packet", "<B", 1, ""),
    0x01: ("battery", "<B", 1, " %"),
    0x02: ("temperature", "<h", 0.01, " C"),
    0x03: ("humidity", "<H", 0.01, " %"),
    0x0C: ("voltage", "<H", 0.001, " V"),
    0x14: ("moisture", "<H", 0.01, " %"),
}


def decode(data):
    info, data = data[0], data[1:]
    fields = ["v%d%s" % (info >> 5, " encrypted" if info & 1 else "")]
    while data:
        object_id = data[0]
        if object_id not in OBJECTS:
            fields.append("unknown object 0x%02X" % object_id)
            break
        name, fmt, factor, unit = OBJECTS[object_id]
        size = struct.calcsize(fmt)
        value = struct.unpack(fmt, data[1:1 + size])[0] * factor
        fields.append("%s %s%s" % (name, round(value, 3), unit))
        data = data[1 + size:]
    return "  ".join(fields)


async def main(wanted, seconds):
    last = {}

    def seen(device, adv):
        data = adv.service_data.get(BTHOME_UUID)
        if data is None:
            return
        name = adv.local_name or device.name or ""
        if wanted and wanted not in (name, device.address):
            return
        if last.get(device.address) == data:
            return   # the same packet again
        last[device.address] = data
        print("%s %-12s %4d dBm  %s" % (device.address, name, adv.rssi, decode(data)), flush=True)

    async with BleakScanner(seen, scanning_mode="active"):
        await asyncio.sleep(seconds)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else None,
                     float(sys.argv[2]) if len(sys.argv) > 2 else 30))
