# Soil Moisture Sensor

An **ESP32 soil moisture board** (DIYMORE "ESP32 18650 Battery Shield", with a
DHT11 and a capacitive probe) running MicroPython. It measures soil moisture,
air temperature and air humidity, and sends them over **Bluetooth LE**:
- to **Home Assistant** as BTHome broadcasts, with no Wi-Fi, broker or pairing;
- to the **BLE Sensor phone app** (`~/git/BLE_Sensor`), which finds it as
  "ESP32 Soil", shows the readings and calibrates the probe.

On battery it spends most of its time in deep sleep: it wakes every 5 minutes,
measures, broadcasts for 4 seconds and sleeps again.

## Features

- **Soil moisture in %,** from the probe's voltage and two calibration points
  (in air and in water), with a median of 15 samples.
- **Air temperature and humidity** from the DHT11, with retries when it misses
  a read.
- **BLE Sensor app:** live readings, charts, calibration (Set dry / Set
  wet), measuring interval, name, reboot and factory reset. Changes need a
  phone paired with the board's 6-digit code ([docs/bluetooth.md](docs/bluetooth.md)).
- **BTHome v2 broadcasts,** not encrypted. Every measurement has a new packet
  id, which survives deep sleep, so Home Assistant never drops a reading as a
  repeat.
- **Easy to work with over USB.** After power-on, the reset button or a
  deploy, it stays awake for 2 minutes and measures every 5 s, so you can
  watch the log, calibrate and reach the REPL.
- **Battery level,** if the board has a battery divider. On the board tested
  so far it doesn't (see [docs/hardware.md](docs/hardware.md)). Readings from
  an unconnected pin are dropped rather than sent.

## Quick start

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
tools/deploy.sh                                  # compile, upload, restart
.venv/bin/mpremote connect /dev/ttyUSB0 repl     # watch the log (Ctrl+] quits)
.venv/bin/python tools/scan.py                   # see the broadcasts from the PC
```

The log shows one line per measurement:

```
Soil sensor: Soil-7D82 (B0:B2:1C:44:7D:82), BLE Sensor app and BTHome
Bluetooth: pairing code 123456
Awake for 120 s, then measuring every 300 s in deep sleep. Ctrl+C for the REPL.
soil 2302 mV (34.5 %)  23 C  60 %RH  battery - (-)
```

The board needs MicroPython 1.25 for ESP32 (`ESP32_GENERIC`). To flash it on a
new board, see [docs/hardware.md](docs/hardware.md#flashing-micropython).

## Home Assistant

1. Make sure Home Assistant has Bluetooth: a Bluetooth adapter on the Home
   Assistant machine, or an ESPHome Bluetooth proxy near the plant.
2. Power the board. Within a minute, Home Assistant shows a new **BTHome**
   device under Settings → Devices & services → Discovered. Confirm it.
3. It has these entities:

| BTHome object | Entity | Resolution |
|---|---|---|
| `0x14` moisture | Moisture | 0.01 % |
| `0x02` temperature | Temperature | 0.01 °C (the DHT11 gives whole degrees) |
| `0x03` humidity | Humidity | 0.01 % (the DHT11 gives whole percent) |
| `0x01` battery | Battery | 1 %, only with a battery divider |
| `0x0C` voltage | Voltage | 0.001 V, battery voltage, only with a battery divider |

The device is named after the scan-response name, `Soil-XXXX` (the last 4 hex
digits of the Bluetooth address).

A reading that fails (e.g. the DHT11 doesn't answer) is left out of that
packet, so Home Assistant keeps the last value.

## Phone app

Press the board's **EN** button, then open the BLE Sensor app within 2 minutes:
the radar shows **ESP32 Soil**. On the first connection Android asks for the
pairing code. The code is on the USB log at start-up (put it on a label on the
board). See [docs/bluetooth.md](docs/bluetooth.md) for the protocol and the
security.

## Calibration

The probe gives a lower voltage the wetter it is. `soil.dry_mv` and
`soil.wet_mv` are the two ends of the scale. In the app: Settings → Soil
calibration, **Set dry** with the probe in air, **Set wet** with it in water.
By hand:

1. Probe in air, dry and clean: note the `soil … mV` value in the log. That's
   `dry_mv`.
2. Probe in a glass of water, up to the line, without wetting the electronics:
   the value is `wet_mv`.
3. Put both in `firmware/config.json` and run `tools/deploy.sh --config`.

Values set from the app are kept in `settings.json` on the board and win over
`config.json`; a factory reset deletes them.

## Configuration

Everything has a default (`DEFAULTS` in `firmware/app.py`). To change
something, copy `firmware/config.example.json` to `firmware/config.json`, keep
the keys you want to change, and deploy with `--config`. `config.json` holds
per-board calibration, so it is git-ignored.

| Key | Default | Meaning |
|---|---|---|
| `pins.soil` | 32 | probe output (ADC) |
| `pins.dht11` | 22 | DHT11 data |
| `pins.battery` | 34 | battery divider (ADC); `null` = none |
| `pins.led` | `null` | an LED for the app's identify command |
| `soil.dry_mv`, `soil.wet_mv` | 2830, 1300 | calibration, see above |
| `battery.divider` | 2.0 | battery voltage / pin voltage |
| `battery.min_v`, `battery.max_v` | 2.8, 4.5 | readings outside are dropped |
| `bluetooth.name` | `""` | `""` = `Soil-XXXX`; a name set from the app wins |
| `bluetooth.passkey` | `""` | fixed 6-digit pairing code; `""` = made on the first start, kept in `ble_passkey` |
| `interval_s` | 300 | deep sleep between measurements; `0` = never sleep (USB power) |
| `advertise_s` | 4 | how long each measurement is broadcast |
| `awake_s` | 120 | awake time after power-on, reset or deploy (a connected phone keeps it awake) |
| `awake_measure_s` | 5 | measurement interval while awake |

## Repository layout

```
firmware/                  everything that runs on the board (MicroPython)
  main.py                  start-up; on a crash, sleeps 60 s and retries
  app.py                   configuration, settings, the measure/broadcast/sleep cycle
  sensors.py               soil probe, DHT11, battery
  ble.py                   Bluetooth: GATT service, pairing, advertising
  protocol.py              BLE Sensor protocol values (pure Python)
  bthome.py                BTHome v2 packets        (pure Python)
  convert.py               moisture %, battery %    (pure Python)
  config.example.json      settings template; your config.json is git-ignored
tools/
  deploy.sh                compile to .mpy, upload, restart
  scan.py                  decode the BTHome broadcasts from the PC (bleak)
tests/                     pytest for the pure-Python modules
docs/hardware.md           the board: pins, what was checked, flashing
docs/bluetooth.md          the app protocol, pairing and security
```

## Tests

```sh
.venv/bin/pytest
.venv/bin/pyflakes firmware tests tools
```

The tests cover the BTHome encoding (byte layout, missing values, the
31-byte packet limit), the BLE Sensor values (advertising, Env, Soil, System,
which Soil writes are accepted) and the moisture and battery conversions.

## Working with a sleeping board

While the board deep-sleeps, it can't hear `mpremote`. `tools/deploy.sh`
resets it first through the USB-serial chip (RTS is wired to EN), and it then
stays awake for `awake_s`. Without USB, press the board's **EN** button.
