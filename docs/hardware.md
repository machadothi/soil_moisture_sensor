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
| 34 | battery divider? (ADC1) | [Tasmota template](https://templates.blakadder.com/diymore_012592.html) lists a second ADC here; on the tested board it floats (see below) |
| 16, 33 | LEDs | Tasmota template; not checked yet |

## Board check (2026-10-16)

| Check | Result |
|---|---|
| MicroPython | 1.25.0, `ESP32_GENERIC` |
| Flash, RAM, filesystem | 4 MB, 148 KB free, 2 MB free |
| Bluetooth | works; address `B0:B2:1C:44:7D:82`, BTHome received on a PC at -61 dBm |
| DHT11 | 22–23 °C, 55–61 %RH. The first read after a long pause can be stale |
| Soil probe | 2.3–2.9 V in air. The value moved between sessions and drifted up within one session; not settled yet |
| Bluetooth vs. ADC | no effect: the probe reads the same with the radio on or off |
| Battery voltage | **not found**: GPIO34 reads 0.14–0.21 V and falls while sampled (a floating pin). No other ADC pin carries a battery voltage either. The firmware drops readings outside 2.8–4.5 V, so nothing wrong is sent |

## Flashing MicroPython

Only for a new or wiped board:

```sh
wget https://micropython.org/resources/firmware/ESP32_GENERIC-20250415-v1.25.0.bin
.venv/bin/esptool.py --port /dev/ttyUSB0 erase_flash
.venv/bin/esptool.py --port /dev/ttyUSB0 --baud 460800 write_flash -z 0x1000 ESP32_GENERIC-20250415-v1.25.0.bin
```

If esptool can't connect, hold **BOOT** while pressing **EN**.
