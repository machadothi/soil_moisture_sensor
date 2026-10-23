# Battery protection

The board runs from one 18650 Li-ion cell. It needs the battery divider on
GPIO34 (see [hardware.md](hardware.md#battery-sensing)); without a battery
reading the firmware can't protect anything and always runs at "normal".

The code is `firmware/power.py` (the policy, tested on the PC in
`tests/test_power.py`) and `firmware/app.py` (applying it).

- [Levels](#levels)
- [How the voltage is read](#how-the-voltage-is-read)
- [The LED](#the-led)
- [Home Assistant and the app](#home-assistant-and-the-app)
- [Safety: what is covered, and by what](#safety-what-is-covered-and-by-what)

## Levels

At every wake the battery's resting voltage sets a level:

| Level | Resting voltage | ≈ charge | Wakes every | Radio | App after BOOT | LED |
|---|---|---|---|---|---|---|
| normal | ≥ 3.75 V | > 50 % | `interval_s` (60 s) | BTHome (+ app) | `awake_s` (5 min) | off |
| saving | 3.65–3.75 V | 30–50 % | 2 × | BTHome (+ app) | `awake_s` | off |
| low | 3.55–3.65 V | 15–30 % | 5 × | BTHome (+ app) | 2 min | flash every 2 s |
| very low | 3.45–3.55 V | 5–15 % | 15 × | BTHome (+ app) | 1 min | every 1 s |
| critical | 3.35–3.45 V | < 5 % | 1 h | 1 s of BTHome | none | every 0.5 s |
| cutoff | < 3.35 V | empty | 6 h | **none** | none | every 0.25 s, for 2 s after BOOT |

- **Hysteresis.** Falling, a level applies at once. Rising, the voltage must
  clear the edge by 0.05 V, so a cell near an edge doesn't flip back and
  forth. **Leaving cutoff takes 3.60 V**, which in practice means charging.
- **"Never sleep"** (`interval_s` 0, for USB power) is honoured only at normal
  and saving. From low down the board sleeps anyway (60 s × the factor), so a
  board left awake can't drain its cell.
- **Long app sessions.** While connected, the voltage under load is watched:
  below 3.25 V the session ends, the level becomes critical, and the board
  sleeps an hour.
- The previous level is kept in RTC memory across deep sleep, for the
  hysteresis.

## How the voltage is read

- **Before the radio starts.** Bluetooth draws current spikes that make the
  voltage sag, so the level comes from a reading taken first thing at each
  wake. The values sent while awake are taken under load and read a little
  lower.
- **Median of 15 samples**, through the divider (`battery.divider`, 2.0).
- **Charging over USB** reads high (up to 4.2 V): the board then counts as
  normal, which is right.
- **Readings outside 2.8–4.5 V are dropped** (an unconnected pin reads about
  0 V). Below 2.8 V the board can't run anyway.

## The LED

From low down, the LED gives one short flash (30 ms) per period: every 2 s at
low, 1 s at very low, 0.5 s at critical, 0.25 s at cutoff. **The faster it
blinks, the emptier the battery.** It flashes whenever the board is awake: for
a moment at each timed wake, and the whole time after a BOOT press. In cutoff
the radio stays off, and a BOOT press only gets 2 s of fast flashing, so you
can tell the board isn't broken, just empty. While the battery is fine the LED
stays off to save power.

The LED pin is `pins.led` in the configuration (README.md).

## Home Assistant and the app

BTHome sends, besides battery % and voltage:

| Object | Home Assistant | On when |
|---|---|---|
| `0x15` battery | "Battery" low/normal | level low or below |
| `0x1D` heat | "Heat" hot/normal | the DHT11 reads 45 °C or more |
| `0x26` problem | "Problem" | the battery reads above 4.30 V |

The app gets the level in the Soil characteristic, byte 17 (0 normal … 5
cutoff, `0xFF` = no battery reading); see [bluetooth.md](bluetooth.md).

## Safety: what is covered, and by what

Firmware can only decide how much the board draws. The cell's own protection
circuit and the charger chip are what actually stop a dangerous current, so
they stay the last line of defence. Concern by concern:

| Concern | Covered by | Notes |
|---|---|---|
| **Over-discharge** (cell damaged below ~3.0 V) | firmware levels and cutoff; then the cell's protection circuit | The firmware stops the radio at 3.35 V and wakes only every 6 h, so the board draws very little. The cell's protection circuit (it has one: it cut off when empty on 2026-10-16) disconnects at about 2.5–3.0 V. A cell left in cutoff for weeks still drains slowly (deep sleep, regulator and divider): charge it. |
| **Brownout loops** (radio start pulls a weak cell under the ESP32's minimum, restart, repeat) | firmware | Critical allows only 1 s of radio; cutoff none. |
| **Overcharge** | the board's charger chip (4.2 V, constant current, then constant voltage) | The firmware can't control charging. It flags "problem" above 4.30 V. |
| **Charging too hot or too cold** (Li-ion: 0–45 °C) | not controllable | The board has no charge-enable pin and no cell thermistor. The DHT11 measures the air next to the cell and flags "heat" at 45 °C; it can't read below 0 °C. Don't charge it in direct sun or frost. |
| **Short circuit, over-current** | the cell's protection circuit | Nothing in firmware. |
| **Reverse polarity** | care when connecting | Checked on 2026-10-16: red wire to +. The board isn't protected against a reversed cell. |
| **Physical damage, swelling** | you | Replace a swollen, dented or hot cell. Don't leave the board charging unattended in a closed box. |
| **Storage** | you | Store the cell at about 50–60 % (3.7–3.8 V), not full and not empty. |
| **Divider drain** | design | 2 × 100 kΩ draws about 20 µA, always: about 0.5 mAh a day, negligible against 2200 mAh. |
| **Measurement error** | calibration | Resistor tolerance and the ADC shift the voltage by a few hundredths of a volt; check against a multimeter on battery alone and adjust `battery.divider`. The thresholds have margin for it. |
