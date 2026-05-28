# Solar Monitor — Complete Manual

**Version:** 2025-01 · **Hardware tested:** Raspberry Pi 4, Ubuntu 24.04 LTS

---

## Table of Contents

1. [Overview](#1-overview)
2. [Requirements](#2-requirements)
3. [Installation](#3-installation)
4. [Configuration](#4-configuration)
5. [Running the Monitor](#5-running-the-monitor)
6. [Running as a System Service](#6-running-as-a-system-service)
7. [The HTML Dashboard](#7-the-html-dashboard)
8. [The Console Dashboard](#8-the-console-dashboard)
9. [Victron Device Setup](#9-victron-device-setup)
10. [JBD / Vatrer BMS Setup](#10-jbd--vatrer-bms-setup)
11. [Architecture](#11-architecture)
12. [Adding a New Data Source](#12-adding-a-new-data-source)
13. [Troubleshooting](#13-troubleshooting)
14. [Reference](#14-reference)

---

## 1. Overview

Solar Monitor is a local Bluetooth dashboard for solar power systems. It reads
data directly from Victron Energy devices and JBD/Vatrer BMS battery packs over
BLE, and displays it either as a self-contained HTML file in a browser or as a
live Rich terminal dashboard. No cloud, no app, no internet access required.

**What it displays**

| Source | Data |
|---|---|
| Victron VE.Bus Smart Dongle (MultiPlus-II) | Battery V/A/W/temp/SoC, AC output watts, AC input source, device state, alarm |
| Victron SmartSolar MPPT | PV power, battery V/A, yield today, charger state |
| Victron SmartShunt / BMV | Battery V/A/SoC, time-to-go |
| JBD / Vatrer BMS | Pack V/A/W/SoC, remaining Ah/Wh, TTE/TTF, per-cell voltages, temperatures, active faults, balance status, FET state |

**Key design decisions**

- **No GATT scanning for BMS** — connects directly by MAC address. BlueZ builds the D-Bus path from the MAC itself; no prior scan required.
- **Passive BLE scanning for Victron** — the adapter listens but never sends scan requests, with automatic fallback to active scanning.
- **Supervisor process** — a single `solar_monitor.py` manages all worker subprocesses, restarts crashed workers, and writes the dashboard independently.
- **Shared state file** — workers communicate through an atomic JSON file; no sockets, no shared memory.
- **Dual display modes** — HTML dashboard (browser) and Rich console dashboard (terminal).

---

## 2. Requirements

### Hardware

- **Linux host** with Bluetooth: Raspberry Pi 3B+/4/5, any x86 Linux box
- **BlueZ** 5.50 or later (`bluetoothctl --version`)
- Devices within **BLE range** — roughly 10 m line-of-sight

### Python

- **Python 3.11 or later** (`python3 --version`)
- **bleak** ≥ 0.20 — BLE library (required)
- **cryptography** — Victron AES-128-CTR decryption (required)
- **rich** — terminal dashboard (optional; only needed for `console_monitor.py`)

```bash
pip install bleak cryptography       # required
pip install rich                     # optional — console dashboard only
```

### Supported Victron devices

| Device | Record type |
|---|---|
| VE.Bus Smart Dongle (MultiPlus-II) | 0x07 / 0x0C |
| SmartSolar MPPT | 0x01 |
| Phoenix Inverter Smart | 0x03 |
| SmartShunt / BMV-712 | 0x02 |
| Orion XS DC-DC | 0x0E |

### Supported BMS

JBD protocol packs — Vatrer, Overkill Solar, Redodo, Chins, Enjoybot, and generic JBD-based units.

---

## 3. Installation

### 3.1 Extract the archive

```bash
tar -xzf solar_monitor.tar.gz
cd solar_monitor
```

Directory structure:

```
solar_monitor/
├── solar_monitor.py        ← Supervisor (recommended entry point)
├── bms_monitor.py          ← BMS worker (can run standalone)
├── victron_monitor.py      ← Victron worker (can run standalone)
├── console_monitor.py      ← Rich terminal dashboard (read-only)
├── jbd_bms_monitor.py      ← Combined legacy launcher
├── config.ini.example      ← Annotated configuration template
├── MANUAL.md               ← This file
├── solar_monitor/          ← Python package
│   ├── scanner.py
│   ├── jbd.py
│   ├── victron.py
│   ├── dashboard.py
│   ├── state.py
│   ├── config.py
│   └── models.py
└── tests/                  ← 443 unit tests
```

### 3.2 Install Python dependencies

```bash
pip install bleak cryptography       # required for all modes
pip install rich                     # optional — console dashboard only
```

### 3.3 Bluetooth permissions

```bash
sudo usermod -aG bluetooth $USER
# Log out and back in, then:
bluetoothctl scan on   # devices should appear within a few seconds
```

### 3.4 Configure

```bash
cp config.ini.example config.ini
nano config.ini
```

### 3.5 Verify the installation

```bash
# See which workers would start without launching them
python3 solar_monitor.py --config config.ini --list-workers

# One poll cycle to confirm everything works
python3 solar_monitor.py --config config.ini
```

### 3.6 Run the test suite

```bash
python3 -m unittest discover -s tests -v
# Expected: 443 tests, 0 failures (runs without BLE hardware or browser)
```

---

## 4. Configuration

### 4.1 `[general]`

```ini
[general]
output           = dashboard.html      # HTML output path
state_file       = solar_state.json    # Shared state file (worker IPC)
bms_interval     = 120                 # BMS poll interval (seconds)
victron_interval = 30                  # Victron poll interval (seconds)
scan_timeout     = 10                  # BLE scan window (seconds)
max_history      = 600                 # Chart data points per device
log_level        = INFO                # DEBUG / INFO / WARNING / ERROR
theme            = business            # dark / light / business
```

**Interval guidance**

| Setting | Minimum enforced | Recommended | Notes |
|---|---|---|---|
| `bms_interval` | 30 s | 120 s | BMS GATT connection takes 5–35 s per pack |
| `victron_interval` | 10 s | 30 s | Passive scan, very fast |
| `scan_timeout` | — | 10 s | Long enough to catch all Victron record types |

### 4.2 `[bms]`

```ini
[bms]
Label = MAC_ADDRESS [ : password ]

# Examples:
House Bank   = A1:B2:C3:D4:E5:F6 : 123456
Spare Pack   = A1:B2:C3:D4:E5:F7
```

### 4.3 `[victron]`

```ini
[victron]
Label = MAC : KEY  [ type=mppt|inverter|monitor|dcdc ]

# Examples:
South Array  = 11:22:33:44:55:01 : aabbccddeeff00112233445566778899  type=mppt
MultiPlus    = C0:FF:EE:12:34:56 : 0123456789abcdef0123456789abcdef  type=inverter
```

### 4.4 Complete annotated example

```ini
[general]
output           = /var/www/html/solar.html
state_file       = solar_state.json
bms_interval     = 120
victron_interval = 30
scan_timeout     = 10
max_history      = 600
log_level        = INFO
theme            = business

[bms]
House Bank = A1:B2:C3:D4:E5:F6 : 123456

[victron]
South Array = 11:22:33:44:55:01 : aabbccddeeff00112233445566778899  type=mppt
West Array  = 11:22:33:44:55:02 : 00112233445566778899aabbccddeeff  type=mppt
MultiPlus   = C0:FF:EE:12:34:56 : 0123456789abcdef0123456789abcdef  type=inverter
```

---

## 5. Running the Monitor

### 5.1 Supervisor mode (recommended)

```bash
python3 solar_monitor.py --config config.ini
```

Workers are started automatically based on populated config sections. Use
`--list-workers` to see what would start without launching.

| Flag | Description |
|---|---|
| `--config FILE` | Config file (default: `config.ini`) |
| `--log-level LEVEL` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `--list-workers` | List workers that would start and exit |

### 5.2 Standalone workers

```bash
python3 bms_monitor.py     --config config.ini
python3 victron_monitor.py --config config.ini
```

Both accept: `--config`, `--state-file`, `--interval`, `--output`,
`--scan-timeout`, `--once`, `--log-level`, `--theme`.

### 5.3 Console dashboard

```bash
python3 console_monitor.py --config config.ini
```

See [Section 8](#8-the-console-dashboard) for full details.

### 5.4 Combined legacy mode

```bash
python3 jbd_bms_monitor.py --config config.ini
```

Single process for simple single-pack setups.

---

## 6. Running as a System Service

### 6.1 Supervisor systemd unit

`/etc/systemd/system/solar-monitor.service`:

```ini
[Unit]
Description=Solar Monitor
After=network.target bluetooth.target
Wants=bluetooth.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/solar_monitor
ExecStart=/usr/bin/python3 solar_monitor.py --config config.ini
ExecStartPre=/bin/sleep 5
ExecStartPre=/usr/bin/bluetoothctl power on
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable solar-monitor
sudo systemctl start  solar-monitor
journalctl -u solar-monitor -f
```

### 6.2 Serving the HTML dashboard

**nginx:**

```nginx
server {
    listen 80;
    root /home/pi/solar_monitor;
    location / {
        try_files $uri $uri/ =404;
        add_header Cache-Control "no-cache";
    }
}
```

Set `output = /home/pi/solar_monitor/dashboard.html` in config.ini,
then browse to `http://your-pi-ip/dashboard.html`.

### 6.3 Console dashboard as a service

To run the console dashboard in a persistent `tmux` or `screen` session:

```bash
# In a tmux session
tmux new-session -d -s solar-console \
  'python3 /home/pi/solar_monitor/console_monitor.py --config config.ini'

# Attach later
tmux attach -t solar-console
```

---

## 7. The HTML Dashboard

The HTML dashboard is a self-contained file that works in any modern browser.
Refresh the page to see updated readings.

### 7.1 Layout

The dashboard is divided into three zones from top to bottom:

**Zone 1 — System Overview (aggregate cards)**

Three cards side-by-side in a single flex row:

| Card | Shows |
|---|---|
| ☀ MPPT Chargers | Total PV watts, yield today, charger states, N online |
| ⚡ Inverter / VE.Bus | Total AC output watts, device states, alarms, N online |
| 🔋 Battery Bank | Average SoC bar (colour-coded), total Wh/Ah remaining, net amps, N packs online |

On narrow screens the cards wrap automatically; on wide screens they always sit side by side.

**Zone 2 — Individual device cards**

Three grouped sections, each showing one card per configured device:

1. **MPPT Chargers — Individual** — one card per solar charger
2. **Inverters — Individual** — one card per VE.Bus dongle or inverter
3. **Battery Packs — Individual** — one card per JBD/Vatrer BMS pack

**Zone 3 — Historical charts**

Four Chart.js sparklines: Battery Voltage, Battery Current, PV Power, State of Charge.

### 7.2 Themes

Three themes switchable with the button in the top-right corner:

| Theme | Style |
|---|---|
| `dark` | Dark background, neon accent colours, scanline overlay |
| `light` | Light background, muted accents |
| `business` | Off-white, Inter font, clean card layout |

Set the default in `config.ini` with `theme = business`. The chosen theme
is remembered in `localStorage` across page loads.

### 7.3 BMS card

**Main metrics:** Pack voltage (V) · Current (A, signed) · Power (W)

**SoC bar:** Colour-coded — green ≥ 60%, yellow 30–59%, red < 30%.
Shows remaining Wh inline.

**Capacity:** `84.0 / 100.0 Ah` · `TTE 5h36m` (time to empty) · `TTF 1h12m` (time to full)

**Pack info:** `16 cells · 8 cycles · CHG ✓ DSG ✓ · fw 6.2`

**Temperatures:** All NTC readings in °C.

**Faults (when active):** Red text, e.g. `⚠ Cell overvoltage, Discharge overcurrent`

**Balance (when active):** `⚡ Balancing cells: 4, 7`

### 7.4 VE.Bus inverter card

Matches VictronConnect layout:

- **AC Output L1:** Voltage 120 V (hardcoded) · Power W · Current A
- **Battery:** Voltage V · Current A · Temperature °C
- **Status row:** STATE · AC In · ALARM

### 7.5 Solar charger card (MPPT)

PV Power W · Battery V · Battery A · Yield Today Wh · Charger state · Load A (if available)

### 7.6 Battery Monitor card (SmartShunt / BMV)

Battery V · Current A · SoC % (with bar) · Time to go

---

## 8. The Console Dashboard

`console_monitor.py` renders the same data as the HTML dashboard directly in
the terminal, using the Rich library for live in-place updates. It is
**read-only** — it never writes to the state file.

### 8.1 Installation

```bash
pip install rich
```

### 8.2 Usage

```bash
# With a config file (reads state_file path from it)
python3 console_monitor.py --config config.ini

# With an explicit state file path
python3 console_monitor.py --state-file solar_state.json

# Faster polling (checks for new data every second)
python3 console_monitor.py --config config.ini --interval 1

# Press Ctrl-C to exit cleanly
```

| Flag | Default | Description |
|---|---|---|
| `--config FILE` | — | Config file (reads `state_file` path from it) |
| `--state-file FILE` | `solar_state.json` | State file path (overrides config) |
| `--interval SECS` | `2` | How often to check for new data |

### 8.3 Display layout

The console mirrors the HTML dashboard layout:

```
┌─ Solar Monitor ─────────────────── 2 BMS · 2 Victron ── BMS 12:00 · Victron 12:00 ─┐
│                                                                                       │
│ ┌─ ☀ MPPT Chargers ─┐  ┌─ ⚡ Inverter / VE.Bus ─┐  ┌─ 🔋 Battery Bank ───────────┐ │
│ │ PV Power  680.0 W  │  │ AC Output   755 W       │  │ Avg SoC  ████████████░░  84%│ │
│ │ Yield     3200 Wh  │  │ Online      1/1          │  │ Remaining  9000 Wh         │ │
│ │ Online    2/2      │  │ States  1× Inverting     │  │ Ah      168.0 / 200.0      │ │
│ │ States  2× Float   │  │ Alarms  None             │  │ Net amps  -20.0 A          │ │
│ └────────────────────┘  └──────────────────────────┘  │ Online    2/2              │ │
│                                                        └────────────────────────────┘ │
│ ┌─ South Array ─ MPPT ─ ONLINE ─┐  ┌─ West Array ─ MPPT ─ ONLINE ─┐                │
│ │ PV Power   400.0 W             │  │ PV Power   280.0 W            │                │
│ │ Yield      2000 Wh             │  │ Yield      1200 Wh            │                │
│ └────────────────────────────────┘  └───────────────────────────────┘                │
│ ┌─ MultiPlus ─ INVERTER ─ ONLINE ──────────────────────────────────┐                 │
│ │ AC Out    755 W · DC Batt V  54.00 · DC Batt A  -15.00 · 26°C   │                 │
│ │ State  Inverting · AC In  Not connected                           │                 │
│ └───────────────────────────────────────────────────────────────────┘                │
│ ┌─ Batt1 ─ ONLINE ─┐  ┌─ Batt2 ─ ONLINE ─┐                                         │
│ │ 54.32 V / -10.0 A │  │ 53.90 V / -5.0 A │                                         │
│ │ ████████████░ 84% │  │ ██████████░░░ 72% │                                         │
│ └───────────────────┘  └──────────────────┘                                          │
│  12:00:05  ·  Press Ctrl-C to exit                                                   │
└───────────────────────────────────────────────────────────────────────────────────────┘
```

The display updates in-place (full-screen mode) whenever the state file
changes. The terminal is restored cleanly on exit.

### 8.4 How updates work

The console monitor watches the state file's modification time using
`os.path.getmtime()`. When either `bms_monitor.py` or `victron_monitor.py`
writes new data, the mtime changes and the console re-renders immediately
on its next check (up to `--interval` seconds).

### 8.5 Running alongside the supervisor

The console monitor is a passive viewer — it never writes to any file.
Run it in a second terminal while the supervisor is running:

```bash
# Terminal 1: supervisor
python3 solar_monitor.py --config config.ini

# Terminal 2: console view
python3 console_monitor.py --config config.ini
```

### 8.6 Colour coding

| Colour | Meaning |
|---|---|
| Cyan | Voltage values, battery aggregate |
| Green | Current (charging), online status, SoC ≥ 60% |
| Yellow | MPPT / solar data, SoC 30–60% |
| Magenta | Inverter / VE.Bus data, yield |
| Red | Errors, faults, alarms, SoC < 30% |
| Dim | Labels, metadata, muted info |

---

## 9. Victron Device Setup

### 9.1 Enable Instant Readout

1. Open **VictronConnect** → connect to device → ⚙️ gear → **Product Info**
2. Enable **Instant Readout via Bluetooth**
3. Tap **Show** → copy the 32-character Advertisement Key
4. Note the MAC address (iOS shows UUID — use Android or `bluetoothctl` for the real MAC)

### 9.2 VE.Bus Smart Dongle (MultiPlus-II)

The dongle appears as a **separate device** from the inverter's built-in Bluetooth.
Add as `type=inverter`. Data includes battery V/A/temp, AC output real watts,
AC input source/power, device state, alarm level, and SoC when available.

### 9.3 Verifying Victron reception

```bash
python3 victron_monitor.py --config config.ini --once --log-level DEBUG
```

Successful output:
```
[Victron] 'Multiplus-Ii': 3 payload(s) accumulated
[VE.Bus] Multiplus-Ii: V=54.0  A=-15.0  ac=755VA  state=Inverting
```

If you only see Format B `rec=0x02` packets, Instant Readout is not enabled.

---

## 10. JBD / Vatrer BMS Setup

### 10.1 Finding the MAC address

```bash
bluetoothctl
[bluetooth]# scan on    # BMS appears as BT-TH-XXXXXXXX after a few seconds
[bluetooth]# devices
```

Or use nRF Connect (Android) or LightBlue (iOS).

### 10.2 Password

Factory default is usually `123456`. Omit the colon entirely if no password:

```ini
[bms]
House Bank = A1:B2:C3:D4:E5:F6 : 123456
Spare Pack = A1:B2:C3:D4:E5:F7
```

### 10.3 Verifying BMS connectivity

```bash
python3 bms_monitor.py --config config.ini --once --log-level DEBUG
```

Success: `House Bank: 54.32V  0.00A  0.00W  SoC=100%  5455.1Wh`

---

## 11. Architecture

### 11.1 Process model

```
solar_monitor.py (supervisor)
│
├── victron_monitor.py  →  solar_state.json["victron"]  →  dashboard.html
│     passive BLE scan, every 30 s                         (supervisor writes)
│
└── bms_monitor.py      →  solar_state.json["bms"]
      GATT connect by MAC, every 120 s

console_monitor.py (optional, read-only)
      watches solar_state.json, re-renders terminal on change
```

### 11.2 BLE strategy

| Device type | Strategy | Why |
|---|---|---|
| BMS | `BleakClient(mac_string)` — direct GATT | No scan needed; BlueZ constructs D-Bus path from MAC |
| Victron | `BleakScanner` passive + or_patterns | Advertisement protocol; device broadcasts continuously |

Passive scanning requires BlueZ `or_patterns` (AD type 0xFF, Victron company ID 0x02E1).
Falls back to active scanning automatically if passive fails.

### 11.3 State file format

```json
{
  "bms":     { "updated": "2024-01-01T12:00:00", "readings": [...] },
  "victron": { "updated": "2024-01-01T12:00:01", "readings": [...] }
}
```

Each worker updates only its own section with an atomic write (`os.replace`).

---

## 12. Adding a New Data Source

### 12.1 Worker contract

A worker script must accept `--config FILE --state-file FILE --log-level LEVEL --once`,
write to the shared state file with `save_section(state_file, "section_name", readings)`,
loop indefinitely, and exit non-zero on error.

### 12.2 Register in WORKER_REGISTRY

In `solar_monitor.py`:

```python
WorkerSpec(
    name             = "EcoFlow",
    script           = "ecoflow_monitor.py",
    state_section    = "ecoflow",
    config_sections  = ["ecoflow"],
    interval_cfg_key = "ecoflow_interval",
    min_gap          = 30.0,
),
```

### 12.3 Add to AppConfig and state.py

In `config.py`: add `ecoflow_interval: float = 60.0` to `AppConfig` and load it.
In `state.py`: add `"ecoflow"` to the section loop in `load_state()`.

---

## 13. Troubleshooting

### 13.1 Supervisor / startup

**`No workers to start`** — all config sections empty. Check that `[bms]` or `[victron]`
have uncommented device lines.

### 13.2 BMS issues

**`ERROR — 'path'`** — old bug (synthetic BLEDevice with empty details). Update to current version; `BleakClient` is now called with the MAC string directly.

**`TIMEOUT (35s)`** — device out of range, or another app has an open GATT connection.

**`BMS rejected password`** — wrong password. Try `000000`, `123456`, `888888`.

**`BMS checksum mismatch`** — transient RF interference. Data is still used.

### 13.3 Victron issues

**`Device not seen during scan`** — out of range, Instant Readout not enabled, wrong MAC.

**`passive scan unavailable … using active scanning`** — not an error. Active scanning works identically for Victron. The log message tells you exactly why passive failed.

**`no candidate payload decrypted successfully`** — wrong Advertisement Key.

### 13.4 Console dashboard issues

**`ERROR: the 'rich' library is required`** — install with `pip install rich`.

**Display is garbled or too narrow** — make your terminal window wider. The console
monitor renders best at 140+ columns. Resize the window and the display adjusts on the next refresh.

**Data not updating** — check that the supervisor is running and writing to the state file:
```bash
ls -la solar_state.json    # watch modification time
```

### 13.5 Running the test suite

```bash
cd /path/to/solar_monitor
python3 -m unittest discover -s tests -v
# Expected: 443 tests, 0 failures — no BLE hardware or browser needed
```

---

## 14. Reference

### 14.1 Timing constants

| Constant | Value | Description |
|---|---|---|
| `bms_interval` (default) | 120 s | BMS poll interval |
| `victron_interval` (default) | 30 s | Victron poll interval |
| `scan_timeout` (default) | 10 s | Victron BLE scan window |
| `_MIN_BMS_GAP` | 30 s | Minimum BMS cycle gap |
| `_MIN_VICTRON_GAP` | 10 s | Minimum Victron cycle gap |
| `PER_DEVICE_TIMEOUT` | 35 s | Max time for one BMS read |
| `READ_TIMEOUT` | 12 s | Max wait for BMS response |
| `NOTIFY_SETTLE_DELAY` | 1.0 s | Wait after GATT notify subscription |
| `BMS_RETRIES` | 3 | Attempts per BMS device per cycle |
| `RETRY_DELAY` | 4.0 s | Gap between retry attempts |
| `INTER_DEVICE_GAP` | 1.5 s | Gap between BMS connections |
| `MAX_CRASHES_PER_HOUR` | 10 | Crashes before supervisor gives up |
| `MAX_BACKOFF` | 60 s | Maximum restart delay |

### 14.2 Log prefixes

| Prefix | Source |
|---|---|
| `[Victron]` | Victron worker |
| `[BMS]` | BMS worker |
| `[VE.Bus]` | VE.Bus device reading |
| `supervisor` | Supervisor process |

### 14.3 File summary

| File | Purpose |
|---|---|
| `solar_monitor.py` | Supervisor — manages workers, writes dashboard |
| `bms_monitor.py` | BMS worker — direct GATT by MAC |
| `victron_monitor.py` | Victron worker — passive BLE scan |
| `console_monitor.py` | Rich terminal dashboard (read-only) |
| `jbd_bms_monitor.py` | Legacy combined launcher |
| `solar_monitor/scanner.py` | BLE scanning, VictronScanner, _poll_bms |
| `solar_monitor/jbd.py` | JBD protocol: GATT, packet parsing |
| `solar_monitor/victron.py` | Victron protocol: decryption, all parsers |
| `solar_monitor/dashboard.py` | HTML dashboard generation |
| `solar_monitor/state.py` | Atomic JSON state file I/O |
| `solar_monitor/config.py` | AppConfig, INI loading, CLI overrides |
| `solar_monitor/models.py` | DeviceReading dataclass |
| `tests/test_solar_monitor.py` | JBD/Victron protocol + dashboard tests |
| `tests/test_split_process.py` | State file + split-process tests |
| `tests/test_ble_resilience.py` | VictronScanner, BLE resilience tests |
| `tests/test_supervisor.py` | Supervisor: WorkerSpec, WorkerProcess tests |
| `tests/test_console_monitor.py` | Rich console dashboard tests |

### 14.4 Config quick reference

```ini
[general]
output           = dashboard.html   # HTML output path
state_file       = solar_state.json # Worker IPC file
bms_interval     = 120              # BMS poll (s)
victron_interval = 30               # Victron poll (s)
scan_timeout     = 10               # BLE scan window (s)
max_history      = 600              # Chart points per device
log_level        = INFO             # DEBUG/INFO/WARNING/ERROR
theme            = business         # dark/light/business

[bms]
Label = MAC [ : password ]

[victron]
Label = MAC : 32-char-key  [ type=mppt|inverter|monitor|dcdc ]
```

### 14.5 Victron record types

| Type | Device | Parser |
|---|---|---|
| 0x01 | Solar Charger (MPPT) | `_parse_solar` |
| 0x02 | Battery Monitor (SmartShunt, BMV) | `_parse_bmv` |
| 0x03 | Inverter (Phoenix) | `_parse_inverter` |
| 0x06 | Inverter RS | `_parse_inverter_rs` |
| 0x07 | VE.Bus Smart Dongle (older firmware) | `_parse_vebus` |
| 0x0B | Multi RS | `_parse_inverter_rs` |
| 0x0C | VE.Bus Smart Dongle (newer firmware) | `_parse_vebus` |
| 0x0D | DC Energy Meter | `_parse_dcenergy` |
| 0x0E | Orion XS DC-DC | `_parse_bmv` |

### 14.6 Console dashboard colour reference

| Colour | Rich style | Used for |
|---|---|---|
| Cyan | `bright_cyan` | Voltage, battery aggregate |
| Green | `bright_green` | Charging current, online, SoC ≥ 60% |
| Yellow | `yellow` | PV power, MPPT data, SoC 30–59% |
| Magenta | `bright_magenta` | Inverter output, yield today |
| Red | `bright_red` | Errors, faults, alarms, SoC < 30% |
| Dim grey | `bright_black` | Labels, metadata |
