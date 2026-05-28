# Solar Monitor — Changelog

All changes made to the solar monitor codebase, in reverse-chronological order.

---

## [Current] — VE.Bus Smart Dongle full support

### Dashboard
- Inverter card redesigned to match VictronConnect label groupings exactly
- **AC Output L1** section: Voltage (V) hardcoded 120V, Power (W) from payload, Current (A) computed as P÷120
- **Battery** section: Voltage (V), Current (A) raw signed (negative = discharging, positive = charging), Temperature
- Status row: STATE · AC In source · ALARM
- Added `section-lbl` CSS class — thin divider line with uppercase label between metric groups
- Frequency omitted (not transmitted in any VE.Bus record type)

### victron.py — `_parse_vebus` (new)
- New dedicated parser for record types `0x07` and `0x0C` (VE.Bus Smart Dongle)
- Implements the full official Victron spec (2022-12-14), all 10 fields:
  - `device_state` (8-bit) — Inverting / Passthrough / Charging / etc.
  - `vebus_error` (8-bit) — VE.Bus error code
  - `battery_current` (int16, 0.1A) — signed; negative = discharging
  - `battery_voltage` (uint14, 0.01V)
  - `active_ac_in` (2-bit) — AC1 / AC2 / Not connected / Unknown
  - `ac_in_power` (int19, 1W) — real watts from grid/generator; negative = feed-in
  - `ac_out_power` (int19, 1W) — real AC output watts
  - `alarm` (2-bit) — None / Warning / Alarm
  - `battery_temperature` (7-bit, °C, raw−40 offset)
  - `soc` (7-bit, 1%) — NA=0x7F when not available
- All NA sentinels checked on raw unsigned value before sign extension
- `PARSERS[0x07]` and `PARSERS[0x0C]` both wired to `_parse_vebus` — confirmed identical bit layout from live decrypted packet analysis

### victron.py — dispatch fixes
- **Root cause fix**: `0x0C` removed from `_RECORDS_WITH_STATE`
  - MultiPlus-II has 17+ operating states; Passthrough (0x08), Power Assist (0x0A), Charge (0xFD), External Control (0xF7) were absent from `_VALID_STATES`
  - When in Passthrough the 0x0C record was silently rejected; the 0x07 fallback parser ran on 0x0C ciphertext, producing garbage (e.g. 6.4V battery, 3785W, 225V AC)
- **`_VALID_STATES` expanded** to all known VE.Bus states: `{0,1,2,3,4,5,6,7,8,9,10,11,246,247,252,253,255}`
- **`_INVERTER_STATES` expanded** — added Storage, Passthrough, Power Assist, Power Supply, Sustain, External Control, Charge
- **Candidate sort order** — candidates sorted before try-loop; `0x0C` (priority 1) tried before `0x07` (priority 9)
- **Voltage plausibility floor** — inverter-type rejects `voltage_v < 9.0V` (was 0.0V); prevents 6.4V garbage from passing and blocking the correct parser

### models.py
- Added `ac_in_power_w` — AC input real power (W)
- Added `ac_in_source` — "AC1" / "AC2" / "Not connected"
- Added `vebus_error` — VE.Bus error code
- Added `temperature_c` — battery temperature from dongle (°C)
- `ac_out_power_va` carries real watts (W) for VE.Bus records, apparent power (VA) for 0x03 Inverter

---

## VE.Bus Smart Dongle — protocol reverse-engineering

- Confirmed device: VE.Bus Smart Dongle attached to MultiPlus-II 48/5000/70-95 120V
- Dongle broadcasts record type `0x07` (Instant Readout, Format A, incrementing nonces)
- VE.Smart Networking beacons (Format B, `rec=0x02`, fixed nonce `0x047C`) are separate and unrelated to Instant Readout
- All 10 spec fields verified by decrypting 5 sequential live packets and cross-referencing with VictronConnect simultaneously:
  - bit 32 (14-bit) → 53.99V battery ✓
  - bit 16 (int16) → −19.0A discharge ✓
  - bit 67 (int19) → 944W AC output ✓
  - bit 88 (7-bit) → 26°C temperature ✓
  - bit 46 (2-bit) → "Not connected" (inverting from battery) ✓
- Spec bit layout is correct; earlier garbage readings were caused by the wrong parser running, not wrong offsets

---

## Victron spec audit — all record types

### `_parse_inverter` (0x03 — Phoenix Inverter)
- **Bug**: battery voltage read as `uint16 * 0.001V`. Spec says `int16 * 0.01V`. Fixed.

### `_parse_bmv` (0x02 — Battery Monitor / SmartShunt)
- **Bug**: 22-bit battery current NA sentinel (`0x3FFFFF`) checked after sign extension where it becomes `−1` (a valid current). Fixed to check raw unsigned value before sign extension.

### `_parse_dcenergy` (0x08/0x0D — DC Energy Meter)
- Same NA sentinel bug as `_parse_bmv`. Fixed.

### `_parse_solar` (0x01 — Solar Charger)
- Added `load_current_a` (9-bit uint at bit 80, 0.1A, NA=0x1FF) — load output current on MPPT models with a load terminal

### `_parse_inverter_rs` (0x06 — Inverter RS)
- `def` line was missing (orphaned function body). Restored.

### Candidate logging
- Per-candidate dump moved from INFO to DEBUG with `log.isEnabledFor(DEBUG)` guard — was generating hundreds of lines per cycle

---

## JBD / Vatrer BMS fault tolerance

### jbd.py
- `asyncio.timeout(PER_DEVICE_TIMEOUT=35s)` wraps full connect+settle+read — prevents infinite hang
- `_on_notify`: buffer cleared on corrupt length byte (> `MAX_PAYLOAD_LEN=128`)
- `_verify_checksum`: added checksum validation (warns, does not raise)
- NTC count capped: `min(payload[22], (len(payload)−23)//2)` — prevents index overflow on malformed payloads
- Constants: `READ_TIMEOUT=12s`, `NOTIFY_SETTLE_DELAY=1.0s`, `PER_DEVICE_TIMEOUT=35s`, `MAX_PAYLOAD_LEN=128`

### scanner.py
- `_PERMANENT_ERRORS` (bad password, no GATT service) — checked first, no retry
- Empty string `''` removed from `_TRANSIENT_ERRORS` — was matching every error
- `PersistentScanner._device_cache` — retains `BLEDevice` objects across scans; missing-scan devices use cached reference
- `INTER_DEVICE_GAP=1.5s` between successive device connections

---

## Package structure

Refactored from monolithic script into `solar_monitor/` Python package:

| Module | Contents |
|---|---|
| `models.py` | `DeviceReading` dataclass — single source of truth for all field names |
| `config.py` | `AppConfig`, INI/CLI parsing, `parse_mac_key` 3-tuple |
| `jbd.py` | `JBDGattReader`, `read_jbd_device` with fault tolerance |
| `victron.py` | All parsers, `PARSERS` dispatch, `read_victron_advertisement` |
| `scanner.py` | `PersistentScanner` with device cache, `poll_all` with retry |
| `dashboard.py` | `build_html` — dark/light/business themes |
| `__main__.py` | CLI entry, `--log-level` pre-parse fix |

Launcher: `jbd_bms_monitor.py` at project root.

---

## Dashboard

### Themes
- Three themes: dark / light / business, cycled by button
- Business theme: Share Tech Mono + Inter, muted palette

### Cards
- **BMS**: cell voltage grid, NTC temperatures, SoC bar, fault indicators
- **MPPT**: PV power, battery V/A, yield today, charger state, load current (when present)
- **Inverter (VE.Bus)**: AC Output L1 (Voltage/Power/Current) + Battery (Voltage/Current/Temperature) + status row
- **Inverter (standard 0x03)**: DC Batt V / AC Out W / AC Out V + state/alarm row; raw load indicator fallback for uncalibrated 0x07 payloads
- **Battery Monitor**: voltage, current, SoC, TTG, alarm
- **Aggregate**: spans full width — Battery Power (BMS only), PV Power In, Yield Today

### Totals logic
- Battery V/A/W from BMS packs only (no double-counting from inverter DC readings)
- PV Power from MPPT chargers only (not inverter AC output)

---

## CONFIG.md (new file)

- `[general]`, `[bms]`, `[victron]` section formats with all keys documented
- Exact VictronConnect tap sequence to retrieve MAC and Advertisement Key
- VE.Bus Smart Dongle step-by-step setup (5 steps)
- Firmware note: 0x07 vs 0x0C record types, how to update dongle firmware
- `type=` values table and accepted record types per type
- Full annotated example `config.ini` for 48V system with 3 MPPTs and VE.Bus dongle
