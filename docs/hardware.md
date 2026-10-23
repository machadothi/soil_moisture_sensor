# Hardware

Sold as "ESP32 DHT11 CP2104 WIFI Bluetooth Temperature Humidity Soil Moisture
Sensor Detection Module 18650 Battery Shield", silkscreen **DIY MORE**.

- **MCU:** ESP32-WROOM module, 4 MB flash, no PSRAM.
- **USB:** micro USB, CP210x USB-serial chip (the listing says CP2104; this
  board reports a CP2102, `10c4:ea60`).
- **Power:** 18650 holder with a charger. The ON/OFF slide switch is next to
  the USB port.
- **Sensors:** DHT11 (air temperature and humidity) and a capacitive soil
  probe, part of the same PCB.

## Pins

| GPIO | Use | Source |
|---|---|---|
| 32 | soil probe output (ADC1) | measured: ~2.3–2.9 V in air, follows the probe |
| 22 | DHT11 data | measured: answers as a DHT11 |
| 34 | battery divider, **added by hand** | the [Tasmota template](https://templates.blakadder.com/diymore_012592.html) lists a second ADC here, but out of the box it reads 0 V (see below) |
| 0 | BOOT button | wakes the board from deep sleep (ext0, active low); tested |
| 16 | blue LED | tested (2026-10-23): the only LED a pin controls. The Tasmota template also lists 33; nothing there, nor on 2, 4, 5, 12–15, 17–19, 21, 23, 25–27. The board's other LED (power/charging) is wired to the hardware |

## Board check (2026-10-16)

| Check | Result |
|---|---|
| MicroPython | 1.25.0, `ESP32_GENERIC` |
| Flash, RAM, filesystem | 4 MB, 148 KB free, 2 MB free |
| Bluetooth | works; address `B0:B2:1C:44:7D:82`, BTHome received on a PC at -61 dBm |
| DHT11 | 22–23 °C, 55–61 %RH. The first read after a long pause can be stale |
| Soil probe | 2.3–2.9 V in air. The value moved between sessions and drifted up within one session; not settled yet |
| Bluetooth vs. ADC | no effect: the probe reads the same with the radio on or off |
| Battery voltage | **not wired to any pin** out of the box; a divider was added, see below |

## Battery sensing

Out of the box the board has no battery measurement (checked 2026-10-22). With a charged ~4 V battery, all 14
analog-capable pins (ADC1: 33–36, 39; ADC2: 2, 4, 12–15, 25–27) were read:
- with USB, over the cable;
- on battery alone, over the air (a temporary diagnostic packet), so USB
  couldn't hide it.

GPIO34, 35, 36 and 39 sit at 0 V (the ADC's 128–142 mV floor) either way; the
others float. A divider would show a steady ~2 V. The board does run on the
battery: it woke on its timer and on the BOOT button with the cable out.

To add one: two 100 kΩ resistors in series from BAT+ to GND, the middle point
to GPIO34, and 100 nF from GPIO34 to GND. The firmware already expects that
(`pins.battery` 34, `battery.divider` 2.0). It draws about 20 µA all the time.
Without it, the firmware sends no battery value (it drops anything outside
2.8–4.5 V).

**Added on this board (2026-10-23):** 2 × 100 kΩ and 100 nF as above. It reads
3.91–3.98 V while charging over USB (~1.96 V at GPIO34), and the battery %
and voltage go out over BTHome and to the app.

## Flashing MicroPython

Only for a new or wiped board:

```sh
wget https://micropython.org/resources/firmware/ESP32_GENERIC-20250415-v1.25.0.bin
.venv/bin/esptool.py --port /dev/ttyUSB0 erase_flash
.venv/bin/esptool.py --port /dev/ttyUSB0 --baud 460800 write_flash -z 0x1000 ESP32_GENERIC-20250415-v1.25.0.bin
```

If esptool can't connect, hold **BOOT** while pressing **EN**.
