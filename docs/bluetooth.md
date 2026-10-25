# Bluetooth

The board is a Bluetooth LE device with two audiences:
- **Phone app.** It speaks the "BLE Sensor" protocol (`~/git/BLE_Sensor`), so the
  BLE Sensor app finds it in its scan as board type **ESP32 Soil**, connects,
  and shows and configures it.
- **Home Assistant.** It broadcasts its readings as **BTHome**, which Home
  Assistant picks up without Wi-Fi or a broker (README.md, "Home Assistant").

The code is `firmware/ble.py` (radio, pairing) and `firmware/protocol.py`
(packet layouts, pure Python so the PC tests cover them).

- [When it can be reached](#when-it-can-be-reached)
- [What it sends](#what-it-sends)
- [GATT service](#gatt-service)
- [Pairing and security](#pairing-and-security)
- [Trying it](#trying-it)
- [Limits](#limits)

## When it can be reached

The board deep-sleeps between measurements, and asleep it's off the air:

| When | For how long | Sends |
|---|---|---|
| Each measurement (every `interval_s`, 60 s) | `advertise_s` (3 s) | BTHome only: the app doesn't see it |
| After the **BOOT** button, power-on, the EN button or a deploy | until a phone connects, at most `awake_s` (300 s) | BTHome and the app packet |
| While a phone is connected | until it disconnects, plus 5 s | BTHome (not connectable) |

So to use the app: press **BOOT**, then connect within 5 minutes. BOOT (GPIO0)
wakes the board from deep sleep (`esp32.wake_on_ext0`); EN resets it, which
works too.

## What it sends

MicroPython has a single advertising slot, so the board alternates two
packets every second:

| Packet | Content | Connectable |
|---|---|---|
| **App packet** | flags · 128-bit service UUID `a7e40000-5c2b-4f1a-9d3e-6b8c0f2e1d47` · manufacturer data: company `0x02FF`, board id `0x0D`, protocol version `1` | yes |
| **BTHome packet** | flags · service data `0xFCD2` (README.md, "Home Assistant") | yes |

The scan response carries the name, by default `Soil-XXXX` (the last 4 hex
digits of the Bluetooth address). While a phone is connected, only the BTHome
packet goes out (not connectable), so Home Assistant keeps getting readings.

## GATT service

Service `a7e40000-5c2b-4f1a-9d3e-6b8c0f2e1d47`; characteristic UUIDs are
`a7e400XX-5c2b-4f1a-9d3e-6b8c0f2e1d47`. All values are packed little-endian,
and the shared ones have the layouts of `ble_protocol.h` in BLE_Sensor.

| XX | Name | Access | Bytes | Content |
|---|---|---|---|---|
| 01 | Env | read, notify | 27 | Thunderboard layout; only temperature and humidity are filled in (`valid` bits 0 and 1) |
| 06 | Info | read | 4 | protocol version `1`, board id `0x0D`, sensors `0x81` (bit 0 temperature/humidity, bit 7 soil moisture), reserved |
| 07 | Command | write | 1 | `0x02` factory reset, `0x03` reboot, `0x04` identify (blinks `pins.led` for 3 s, if set) |
| 08 | Name | read, write | ≤ 20 | UTF-8; stored in `settings.json`, advertised from the next round |
| 0B | System | read, notify | 32 | the ESP32 Air layout: uptime, free RAM, chip temperature, flags (bit 2 Bluetooth connected), CPU MHz, last reset, MicroPython version, sensor errors (failed DHT11 reads). Wi-Fi fields are 0 |
| 11 | **Soil** | read, write, notify | 20 | see below |

Values are updated at every measurement (every 2 s while a phone is
connected) and notified to a subscribed phone.

**Soil** (read):

| Offset | Type | Field |
|---|---|---|
| 0 | u32 | uptime, ms |
| 4 | u16 | probe output, mV (0 = none) |
| 6 | u16 | moisture, % × 100 (`0xFFFF` = none) |
| 8 | u16 | dry end: probe in air, mV |
| 10 | u16 | wet end: probe in water, mV |
| 12 | u16 | deep sleep between measurements, s (0 = never sleeps) |
| 14 | u16 | battery, mV (0 = not measured) |
| 16 | u8 | battery, % (`0xFF` = not measured) |
| 17 | u8 | battery protection level: 0 normal, 1 saving, 2 low, 3 very low, 4 critical, 5 cutoff; `0xFF` = no battery reading ([battery.md](battery.md)) |
| 18 | 2 bytes | reserved |

**Soil** (write):

| Bytes | Meaning |
|---|---|
| `01` | the probe's current reading becomes the dry end |
| `02` | the probe's current reading becomes the wet end |
| u16 dry, u16 wet | both ends |
| u16 dry, u16 wet, u16 interval | both ends and the deep-sleep interval |

Accepted: dry ≤ 3300 mV, wet ≥ 100 mV, dry at least 200 mV above wet, interval
0 or 60–65535 s. Anything else is ignored, and the old value written back.
Changes go to `settings.json`.

## Pairing and security

The same rules as the ESP32 Air board (air_quality_sensor
`docs/bluetooth.md#pairing-and-security`):
- **Readings are public:** Env, Info, System and Soil can be read without
  pairing, and BTHome broadcasts the readings anyway, for Home Assistant.
- **Only bonded phones stay connected, and only they can write.** Command,
  Name and Soil writes need the encrypted link of a bonded phone
  (`_FLAG_WRITE_ENCRYPTED`, plus a bond check in `ble.py`).
- **Pairing needs no code** ("Just Works", LE Secure Connections): Android
  just asks "Pair with Soil-…?". **But only while the pairing window is open:**
  - **after a start you caused** (BOOT, reset, power-on, a deploy): for 2
    minutes, or 10 minutes while no phone is bonded yet. The timed wakes
    every minute never open it;
  - **from a bonded phone:** Command `0x06` ("Add a phone" in the app) opens
    it for 2 minutes, and the board stays awake and findable meanwhile.

  While it's open, the app packet's flags byte has bit 0 set, and the app
  shows **Ready to pair**.
- **Bonded phones connect any time:** after BOOT, and also on the 3-second
  timed wakes. The board stays awake while it checks a phone's bond, and the
  app (*Your boards*) connects to the board's address without scanning.
- **Strangers are dropped.** Every phone that connects is asked to encrypt
  with its bond; one that can't is disconnected after 6 s (a minute in the
  window). A new bond outside the window is refused, and so is re-pairing a
  known address (someone copying a bonded phone's address) without losing
  the real bond. A stranger's connection doesn't change when the board
  sleeps.
- **Paired phones are remembered** in `ble_bonds.json`. A factory reset
  forgets them all (and restarts the board, so the first-setup window opens).

## Trying it

- **BLE Sensor app:** press BOOT on the board; the radar shows "ESP32 Soil".
  - **Live:** soil moisture, temperature, humidity, the probe, the board.
  - **Charts:** includes soil moisture.
  - **Control:** reboot, factory reset.
  - **Settings:** name, soil calibration (Set dry / Set wet), measuring
    interval or stay awake.
- **From a Linux PC** with the BLE Sensor Python client:
  ```sh
  ble-sensor scan
  ble-sensor -n Soil-XXXX read
  ```
  Writes need `bluetoothctl`, then `pair <address>` and the code, first.

## Limits

- **Rejected writes aren't refused over the air.** MicroPython can't answer a
  write with an ATT error; the old value is written back instead.
- **One phone at a time.**
- **Off the air while asleep,** and invisible to the app on the timed wakes.
  Press BOOT to connect.
