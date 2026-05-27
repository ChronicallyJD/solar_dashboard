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

# Solar Monitor — Changelog

## Overview

Solar Monitor is a Python application that polls JBD/Vatrer BMS battery packs
over BLE GATT and reads Victron Energy solar charger, inverter, and battery
monitor data from BLE Instant Readout advertisements, then renders a
self-contained HTML dashboard with live charts and historical trend data.

This document records the full development history from initial concept through
production-quality modularised release.

---

## v1.0.0 — Production Release (Modularised Package)

### Architecture — Full Modularisation

The original 2,100-line single-file monolith (`jbd_bms_monitor.py`) was
refactored into a proper Python package (`solar_monitor/`) for easier
debugging, testing, and maintenance.

**Package structure:**

| Module | Lines | Responsibility |
|---|---|---|
| `models.py` | 60 | `DeviceReading` dataclass — shared by all modules |
| `config.py` | 352 | `AppConfig`, INI/CLI parsing, MAC+key helpers |
| `jbd.py` | 398 | JBD/Vatrer BMS GATT protocol |
| `victron.py` | 664 | Victron Instant Readout BLE advertisement parsing |
| `scanner.py` | 417 | BLE scanning, device resolution, poll orchestration |
| `dashboard.py` | 615 | HTML template, card renderers, `build_html()` |
| `__main__.py` | 180 | CLI entry point (`python -m solar_monitor`) |

The original `jbd_bms_monitor.py` launcher is preserved as a thin
backward-compatible wrapper that adds the package directory to `sys.path`
then calls `solar_monitor.__main__:run`.

Distribution is a `tar.gz` archive that extracts to:
```
solar_monitor/
├── jbd_bms_monitor.py        ← backward-compatible launcher
├── setup.py                  ← pip install -e .
├── config.ini.example        ← annotated configuration template
└── solar_monitor/            ← Python package
```

---

## Development History

### Phase 1 — Initial Implementation

**JBD/Vatrer BMS GATT protocol (jbd.py)**

- Implemented basic GATT connection, notify subscription, and command/response
  exchange for the JBD BMS register 0x03 "basic info" request.
- Empirically confirmed the **4-byte header** packet format from Vatrer hardware
  (`DD [reg] [status] [len] [payload] [chk_hi] [chk_lo] 77`). Older JBD
  documentation describes a 3-byte header; Vatrer devices use 4 bytes.
- Implemented dynamic GATT service discovery (`_discover_jbd_chars`) that tries
  UUID candidates in order: standard ff00/ff01/ff02, Vatrer ffe0/ffe1, Nordic
  UART 6e400001. Falls back to heuristic scan for any notify+write pair.
- Added 1-second settle delay after `start_notify` before sending the command.
  Without this, BlueZ completes the subscription asynchronously and the first
  notify arrives before the handler is registered, leaving the response buffer
  permanently empty.
- Implemented packet length-based framing (not 0x77 sentinel). The end marker
  byte 0x77 can appear inside the payload as valid data.
- Added **sequential polling** with `INTER_DEVICE_GAP` between devices. BlueZ
  serialises GATT operations through a single D-Bus socket; concurrent
  connections beyond ~2 devices produce "Operation already in progress" errors.
- Added per-device retry logic (`BMS_RETRIES = 3`) with transient-error
  detection. Permanent errors (bad password, no GATT service) do not retry.
- **Bug fixed:** `_jbd_packet_complete` would wait indefinitely if a corrupt
  length byte (e.g. `0xFF = 255`) arrived, requiring 262 bytes that never come.
  Added `MAX_PAYLOAD_LEN = 128` guard — returns `False` immediately for
  implausibly large lengths.

**Victron Instant Readout protocol (victron.py)**

*Advertisement format discovery (extensive empirical analysis):*

Two distinct advertisement formats were identified in the wild. Bleak strips
the 2-byte Victron company ID (`0x02E1`) from `manufacturer_data` values, so
all byte offsets below are relative to the first application payload byte:

- **Format A** (Product Advertisement, starts with `0x10`, ≥9 bytes):
  ```
  [0]    = 0x10  outer type
  [1-2]  = model ID
  [3]    = readout byte: high nibble = key index, low nibble = record type
  [4]    = counter/flags byte
  [5-6]  = nonce (uint16 LE)
  [7]    = key-index byte (NOT key[0] — multiple failed verification attempts)
  [8+]   = AES-128-CTR encrypted payload
  ```
- **Format B** (Extra Manufacturer Data, direct record, ≥5 bytes):
  ```
  [0]    = record type
  [1-2]  = nonce (uint16 LE)
  [3]    = key-index byte
  [4+]   = AES-128-CTR encrypted payload
  ```

*Key byte verification — investigated and abandoned:*

Early versions attempted to verify the key by comparing the byte at position
`[7]` (Format A) or `[3]` (Format B) against `key[0]`. This was rejected after
extensive byte-level analysis:
- The byte at that position is NOT `key[0]` — it is a key-index field that
  tells the device which of multiple stored keys to use.
- The byte changes between Format A and Format B for the same device, confirming
  it has no stable relationship to the key value.
- Coincidental equality (when the field happened to equal `key[0]`) caused the
  check to pass intermittently, making failures appear random.
- **Resolution:** key-byte verification removed entirely. Decryption validity
  is checked via post-decrypt sanity checks instead.

*Decryption:*

- AES-128-CTR with 16-byte nonce = 2-byte LE counter + 14 zero bytes.
- Requires `pip install cryptography`.

*State-byte sanity check:*

Post-decryption validation checks that `decrypted[0]` is a known
charger/inverter state code (`{0, 1, 2, 3, 4, 5, 7, 9, 252, 255}`) for record
types that have a state byte at position 0 (`0x01, 0x03, 0x06, 0x07, 0x0B,
0x0C`). Random bytes from a wrong key are unlikely to hit any of these 9 values.

- **Bug fixed:** The check was initially applied to ALL record types. For Battery
  Monitor (`0x02`), byte `[0]` is the low byte of time-to-go (TTG), not a state
  code. Values like `0x12` (18 minutes TTG) and `0x43` (67 minutes TTG) were
  being falsely rejected. Fixed by scoping the check to `_RECORDS_WITH_STATE`.

*Record type support:*

| Type | Device | Parser |
|---|---|---|
| 0x01 | Solar Charger (MPPT) | `_parse_solar` |
| 0x02 | Battery Monitor (SmartShunt/BMV) | `_parse_bmv` |
| 0x03 | Inverter (Phoenix) | `_parse_inverter` |
| 0x04 | DC/DC Converter | `_parse_bmv` |
| 0x05 | SmartLithium | `_parse_bmv` |
| 0x06 | Inverter RS | `_parse_inverter` |
| 0x07 | DC Energy Meter / Inverter | `_parse_inverter` |
| 0x08 | SmartShunt IP65 | `_parse_dcenergy` |
| 0x09 | DC-DC Charger | `_parse_bmv` |
| 0x0B | Multi RS | `_parse_inverter` |
| 0x0C | VE.Bus | `_parse_inverter` |
| 0x0D | Orion XS | `_parse_bmv` |

*Inverter parser — battery voltage field investigation:*

The Victron spec states battery voltage is `int16` at `[3:5]` with `0.01V`
scale. Live packet analysis of a 48V system produced `0xAEFF` at that position:
- As `int16 * 0.01`: `-20737 * 0.01 = -207.37V` — impossible.
- As `uint16 * 0.001`: `44799 * 0.001 = 44.799V` — correct for a
  slightly-discharged 48V battery.
- **Resolution:** battery voltage field is `uint16` in millivolts (mV), not
  `int16 * 0.01V` as documented.

*Inverter parser — AC watts field investigation (multiple iterations):*

The field at `[5:7]` was initially interpreted as AC apparent power (148 VA).
The bit-packed `uint32` at `[7:11]` provided `ac_voltage` (bits 0–14, `0.01V`)
and `ac_current` (bits 15–25, `0.1A`).

User testing revealed AC Volts and AC Watts were displaying reversed values:
- `[5:7] = 148` — initially labelled "AC Out W", actually the AC voltage in 1V
  units (coarse measurement).
- `bits[0-14] = 14336 → 143.36V` — actually the AC voltage at 0.01V resolution.
- Neither field contains a dedicated watts value.
- **Resolution:** `ac_out_voltage_v` = bits 0–14 × 0.01 (fine resolution,
  preferred); fall back to `[5:7]` × 1V if fine field is N/A. AC apparent
  power = **computed** as `ac_voltage × ac_current = 143.36V × 2.6A = 372.7W`.

*BMV current N/A sentinel:*

- **Bug fixed:** `_parse_bmv` always returned a current value even when the BMS
  reported unavailable (22-bit sentinel `0x1FFFFF`). Fixed to return `None`,
  which also prevents `batt_w` being computed from a garbage current.

---

### Phase 2 — BLE Scanner Architecture

**PersistentScanner (scanner.py)**

The initial implementation used `BleakScanner.discover()`, which stops the BLE
radio after its timeout. BlueZ then evicts cached device objects; subsequent
`BleakClient(device)` calls raise "device was not found / removed from BlueZ
when scanning stopped".

**Resolution:** Replaced with a persistent scanner that calls
`BleakScanner.start()` once and keeps the radio active throughout the entire
poll cycle. The scanner is only stopped after all GATT connections complete.

**Stale nonce problem (major bug):**

Victron devices increment a 16-bit nonce counter with every advertisement
beacon (~1 per second). The original code took a snapshot of advertisement data
at the end of the scan window. BMS polling then ran for several minutes (9
devices × retry delays). By the time Victron devices were processed, the
snapshot's nonce was many minutes stale — the device had already moved to a
nonce thousands of positions ahead. AES-CTR with the wrong nonce produces
random-looking plaintext that passes no sanity checks.

**Resolution:** The scanner continues running during BMS polling. Victron
devices are read using `scanner.latest_adv()` — the most recently received
advertisement — immediately before decryption. The nonce is always fresh.

**Multi-payload accumulation (VE.Smart networking):**

Victron devices rotate through advertising **multiple record types** in a single
advertisement period. For example, the `48V-2400W` inverter broadcasts both
a generic Solar Charger record (`0x01`) and an Inverter record (`0x07`). Bleak
fires the callback once per received advertisement; `latest_adv()` returns
whichever record arrived most recently — non-deterministically alternating
between record types each poll cycle.

**Resolution:** `PersistentScanner` accumulates every distinct payload seen for
each MAC throughout the scan window into a per-device list. `poll_all` passes
the full accumulated list to `read_victron_advertisement`, which tries each
candidate in order.

**Payload sort order — VE.Smart interference:**

When VE.Smart networking is active, each device on the network re-broadcasts
neighbour data as additional BLE manufacturer payloads. An MPPT solar charger
broadcasts its own `0x01` record AND the battery monitor's `0x02` record from
the VE.Smart network. The BMV parser applied to MPPT data produced impossible
currents (865A, 310A).

Multiple sort orders were tried:
- Higher record type numbers first (`-rt`): `0x02` sorted before `0x01`,
  causing the BMV parser to win over the MPPT's own data. ✗
- Lower record type numbers first (`+rt`) among non-`0x01` records: `0x01` MPPT
  record sorts before `0x02` BMV network data. ✓

**Final candidate selection algorithm:**
1. Merge `all_payloads` (accumulated during scan) with snapshot from current adv.
2. Filter: discard short VE.Smart beacons (Format A < 9 bytes) and payloads
   with unknown record types.
3. Sort: non-`0x01` records by ascending record type number first; generic
   `0x01` beacon last.
4. If `type=` declared in config: skip records not in the allowed set for that
   type.
5. For each candidate: decrypt → state-byte check → voltage range check
   (0–150V) → current range check (±2000A) → accept first that passes all.

**Voltage/current range checks:**

Added post-parse voltage (0–150V) and current (±2000A) checks for all record
types. A wrong parser applied to correctly-decrypted but mismatched data (e.g.
BMV parser on Solar Charger data) produces impossible values caught by these
checks.

---

### Phase 3 — Configuration

**INI file parsing (config.py)**

- `[bms]` section: each entry is `Name = identifier [ : password ]` where
  identifier is a MAC address or BLE advertisement name.
- `[victron]` section: accepted as `[victron]` or `[mppt]` (alias).
- MAC+key format: accepts both no-space (`MAC:KEY`) and spaced (`MAC : KEY`)
  separator styles. The no-space form is detected by counting 7 colon-separated
  segments.
- `configparser` lowercases all keys and values by default — no impact since
  hex keys are case-insensitive, but verified explicitly.
- **Bug fixed:** `INI_EXAMPLE` string was missing its closing `"""`, causing
  Python to treat everything up to the next triple-quote as a string literal.
  All function definitions following the constant were invisible to the parser,
  producing cascading `SyntaxError` failures.
- **Bug fixed:** Smart quotes (`'` U+2019) and Unicode arrows (`→`) in
  docstrings caused `SyntaxError: unterminated string literal` on Python 3.12.
  Replaced with straight quotes and ASCII throughout.
- **Bug fixed:** Duplicate `_normalise_mac` function definition (remnant of
  earlier edits) — removed.

**`type=` device type declaration:**

Added optional `type=TYPE` field to `[victron]` entries. When declared:
- Only advertisement payloads matching the declared type's record types are
  attempted. All other payloads (including VE.Smart network data) are skipped.
- The final `DeviceReading.device_type` is set to the declared type regardless
  of which record type decoded successfully.

```ini
[victron]
(3)-12-180V = E1:2D:6C:B5:83:76:2bbad134... type=mppt
48V-2400W   = E6:2E:31:75:9A:1A:dd156932... type=inverter
```

Valid values: `mppt`, `inverter`, `monitor`, `dcdc`, `lithium`, `meter`.

**Key location clarification:**

VictronConnect shows two different keys for each device:
- **Encryption key** (under Product info, top section) — used for VE.Smart
  networking between Victron devices. **Wrong key for Instant Readout.**
- **Advertisement key** (Product info → scroll to "Instant Readout via
  Bluetooth" → Show) — the correct key for BLE advertisement decryption.

This distinction is documented in `config.ini.example`, all error messages, and
the module docstring.

---

### Phase 4 — Dashboard

**HTML dashboard (dashboard.py)**

- Three switchable colour themes: **dark** (default), **light**, **business**.
  Theme preference persisted in `localStorage`. Button cycles Dark → Light →
  Business → Dark.
- **Dark theme:** monospace digital aesthetic with cyan/green/amber glows.
- **Light theme:** clean white, muted colours, no glow effects.
- **Business theme:** Inter typeface, DM Serif Display for branding, warm
  off-white background, card-based layout with 3px colour-coded top borders,
  no scanlines or glows.
- Totals banner: average battery V, net A, battery W, PV power, yield today.
- Aggregate BMS card: large SoC percentage with colour bar and online count.
- Per-device BMS cards: V / A / W metrics, SoC bar, cell count, temperatures.
- Per-device Victron cards:
  - MPPT: PV power, yield today, battery V/A, charger state.
  - Inverter: DC input V, AC output W (computed), AC V, state, AC A.
  - Monitor: battery V/A/W, SoC, TTG.
- Chart.js historical graphs: battery voltage, current, PV power, SoC.

**Bug fixed — totals double-counting battery power:**

`total_w` was summing `power_w` from all devices including Victron. The MPPT
charger and battery bank sit at the same voltage; their watts represent the same
energy flow from different measurement points. Fixed to sum **BMS readings
only** for voltage, current, and power totals.

**Bug fixed — PV power including inverter:**

PV Power In was summing `pv_power_w` across all Victron devices. Inverters have
no PV power field; including them could pull in AC output values incorrectly.
Fixed to filter to `device_type == "mppt"` (solar chargers) only.

**Business theme layout fixes (multiple iterations):**

- Cards switching from CSS grid to flexbox broke `grid-column: 1 / -1` on the
  aggregate card — reverted to grid.
- Base `.soc-track` used `rgba(255,255,255,0.06)` (invisible on white) — fixed
  to `rgba(0,0,0,0.08)` for business theme.
- Card names were `text-transform:uppercase` in the base CSS with a wide
  condensed font — overridden with `text-transform:none` and Inter.
- Metric values font reduced from `1.4rem` to `1.2rem`; Inter is
  proportional-width and wider than monospace at the same size.
- Added `min-width:0` to cards to allow grid items to shrink below content size.
- Added `white-space:nowrap; overflow:hidden; text-overflow:ellipsis` to card
  names and addresses.
- Added `flex-shrink:0` to status badges so they don't compress against names.
- Grid minimum column reduced from 300px to 280px.

---

### Phase 5 — Production Quality

**Documentation pass:**

- Module-level docstring expanded to full architecture overview with protocol
  notes for both JBD and Victron (byte layouts for both advertisement formats,
  key location instructions, supported record types).
- All public functions and classes have `Args`, `Returns`, `Raises` docstrings.
- All wire-protocol byte layouts are documented inline at the point of use.

**Bugs fixed in final audit:**

| # | Location | Bug | Fix |
|---|---|---|---|
| 1 | `dashboard.py` | Battery power total included Victron AC watts (double-counting) | Sum BMS readings only |
| 2 | `dashboard.py` | PV power total included inverter devices | Filter to `device_type == "mppt"` |
| 3 | `jbd.py` | Corrupt length byte caused indefinite packet wait | Cap at `MAX_PAYLOAD_LEN = 128` |
| 4 | `victron.py` | `0x07` missing from `_RECORDS_WITH_STATE` | Added |
| 5 | `dashboard.py` | `ac_out_power_va` not stored in history | Added to history dict |
| 6 | `dashboard.py` | Inverter card labels "DC In V" / "AC Out W" swapped from values | Corrected labels |
| 7 | `victron.py` | BMV `_parse_bmv` ignored N/A sentinel `0x1FFFFF` for current | Return `None` |
| 8 | `victron.py` | `batt_w` computed when `batt_a` is `None` | Guard `batt_a is not None` |

---

## Supported Hardware

### BMS (JBD/Vatrer protocol)

- JBD BMS modules (Jiabaida / Xiaoxiang)
- Vatrer Power battery packs (SP16S020L16S100A and similar)
- Overkill Solar boards
- Any BMS exposing the ff00/ffe0/Nordic UART GATT profile

Tested with 9 concurrent Vatrer packs.

### Victron Energy (Instant Readout via BLE)

All devices with "Instant Readout via Bluetooth" enabled in VictronConnect:

- SmartSolar / BlueSolar MPPT charge controllers
- SmartShunt / BMV-712 / BMV-702 battery monitors
- Phoenix / MultiPlus / Quattro inverters
- Orion DC-DC converters
- SmartLithium batteries
- DC Energy Meters

Tested with 3× SmartSolar MPPT (12V/180W), 1× Phoenix Inverter (48V/2400W),
VE.Smart networking active.

---

## Dependencies

```
bleak>=0.21        # BLE scanning and GATT
cryptography>=41   # AES-128-CTR for Victron decryption
```

Python 3.10+ required (uses `match`-free but uses `X | Y` union type hints).

---

## Configuration Reference

```ini
[general]
output          = dashboard.html    # output HTML path
interval        = 30                # poll interval (seconds)
scan_timeout    = 10                # BLE scan duration per cycle
max_history     = 60                # chart data points to retain
log_level       = INFO              # DEBUG|INFO|WARNING|ERROR
theme           = dark              # dark|light|business

[bms]
Name = MAC_OR_BLE_NAME [ : password ]

[victron]
Name = MAC:KEY [ type=mppt|inverter|monitor|dcdc|lithium|meter ]
# or
Name = MAC : KEY [ type=... ]
```

## CLI Reference

```
python jbd_bms_monitor.py [options]
python -m solar_monitor   [options]

--config FILE        INI file (default: monitor.ini)
--interval SECS      Poll interval
--output FILE        Dashboard output path
--scan-timeout SECS  BLE scan duration
--once               Single poll then exit
--bms MAC [MAC ...]  Explicit BMS addresses
--mppt MAC:KEY ...   Explicit Victron MAC+key pairs
--theme THEME        dark|light|business
--log-level LEVEL    DEBUG|INFO|WARNING|ERROR
--write-example-config [FILE]
```
