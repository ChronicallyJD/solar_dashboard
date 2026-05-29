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
15. [HTTPS Dashboard Server](#15-https-dashboard-server)
16. [MCP Server](#16-mcp-server)
17. [Historical Data & SQLite Storage](#17-historical-data--sqlite-storage)

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
- **No extra dependencies for MCP** — `mcp_server.py` uses only the standard library

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
# Expected: 765 tests, 0 failures (runs without BLE hardware or browser)
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
# Expected: 765 tests, 0 failures — no BLE hardware or browser needed
```

**Test files and what they cover:**

| File | Tests | Coverage |
|---|---|---|
| `test_solar_monitor.py` | ~150 | JBD/Victron protocol, dashboard cards, aggregate cards, layout |
| `test_split_process.py` | ~113 | State file I/O, config parsing, MAC/key helpers, `_soc_color`, `_no_card`, query utils |
| `test_ble_resilience.py` | ~100 | VictronScanner, passive/active fallback, or_patterns, BMS retry |
| `test_supervisor.py` | ~59 | WorkerSpec, WorkerProcess, crash policy, dashboard loop |
| `test_console_monitor.py` | ~80 | Rich panels, aggregate display, `_render`, mtime watcher |
| `test_https_server.py` | ~73 | Cert generation, SSL context, HTTP routing, config |
| `test_mcp_server.py` | ~107 | All 8 MCP tools, security enforcement, JSON-RPC dispatch |
| `test_history.py` | ~99 | HistoryDB, schema, purge, retention, dashboard seeding |

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
| `solar_monitor/server.py` | HTTPS server, cert generation, SSL context |
| `tests/test_solar_monitor.py` | JBD/Victron protocol + dashboard tests |
| `tests/test_split_process.py` | State file + split-process tests |
| `tests/test_ble_resilience.py` | VictronScanner, BLE resilience tests |
| `tests/test_supervisor.py` | Supervisor: WorkerSpec, WorkerProcess tests |
| `tests/test_console_monitor.py` | Rich console dashboard tests |
| `tests/test_https_server.py` | HTTPS server, cert, routing, config tests |
| `mcp_server.py` | MCP server — AI assistant integration, 8 tools |
| `solar_monitor/history.py` | SQLite history store, HistoryDB, retention |
| `utils/purge_history.py` | CLI utility: purge history by date/device |
| `utils/query_history.py` | CLI utility: query and export history as CSV/JSON |
| `tests/test_history.py` | History DB, config, retention, purge, utils tests |
| `tests/test_mcp_server.py` | MCP server: tools, security, dispatch tests |

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

[history]
enabled              = false           # enable SQLite history
db_path              = solar_history.db  # database file path
retention_days       = 1095              # 3 years (0 = keep forever)
vacuum_interval_days = 7                 # VACUUM frequency

[mcp]
enabled       = true            # start MCP server
api_key       =                 # bearer token (empty = no auth)
allowed_tools =                 # comma-separated whitelist (empty = all)
rate_limit    = 60              # requests/minute (0 = unlimited)
require_local = true            # loopback only (stdio: informational)
log_requests  = false           # log every tool call to stderr
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

---

## 15. HTTPS Dashboard Server & API

Solar Monitor includes a built-in HTTPS server. It serves the live HTML
dashboard and a JSON API over TLS — no nginx, no separate web server
required. The server runs as an `asyncio` task inside the supervisor process.

---

### 15.1 Quick start

```ini
[server]
enabled   = true
port      = 4443
auto_cert = true
```

```bash
python3 solar_monitor.py --config config.ini
# → HTTPS server listening on https://0.0.0.0:4443/
# →   Dashboard:  https://localhost:4443/dashboard.html
# →   State API:  https://localhost:4443/state.json
```

Your browser will warn about the self-signed certificate on first visit.
See Section 15.5 to trust it permanently.

---

### 15.2 Configuration reference

All settings live in the `[server]` section of `config.ini`.

| Key | Type | Default | Description |
|---|---|---|---|
| `enabled` | bool | `false` | Start the HTTPS server (`true`/`false`/`yes`/`1`/`on`) |
| `host` | string | `0.0.0.0` | Address to bind. `0.0.0.0` = all interfaces |
| `port` | integer | `4443` | Port to listen on. Ports < 1024 require root |
| `cert_file` | path | `server.crt` | TLS certificate (PEM). Auto-generated if missing and `auto_cert = true` |
| `key_file` | path | `server.key` | TLS private key (PEM). Auto-generated if missing and `auto_cert = true` |
| `auto_cert` | bool | `true` | Generate a self-signed certificate when `cert_file` does not exist |

**Complete annotated example:**

```ini
[server]

# Enable the HTTPS server (disabled by default)
enabled   = true

# Bind address — 0.0.0.0 listens on all network interfaces
host      = 0.0.0.0

# Port — 4443 requires no special privileges
port      = 4443

# TLS certificate and private key (PEM format)
# Auto-generated on first run when auto_cert = true
cert_file = server.crt
key_file  = server.key

# Automatically generate a self-signed certificate if cert_file is missing.
# Set to false when using a real certificate (e.g. from Let's Encrypt).
auto_cert = true
```

---

### 15.3 API reference

All endpoints require HTTPS. All responses include `Connection: close` and
`Content-Length`. Only `GET` requests are accepted; all other methods return
`404 Not Found`.

---

#### `GET /` or `GET /dashboard.html`

Returns the live HTML dashboard.

**Response**

| Header | Value |
|---|---|
| Status | `200 OK` |
| `Content-Type` | `text/html; charset=utf-8` |

**Body:** The contents of the `output` file configured in `[general]` —
the same file the dashboard writer updates after every poll cycle. The
dashboard is a self-contained HTML file with embedded CSS, JavaScript,
and chart data.

**If the dashboard has not been generated yet** (workers have not completed
their first poll), a plain HTML placeholder is returned:

```html
<html><body>Dashboard not yet generated. Check monitor logs.</body></html>
```

**Example:**

```bash
curl -k https://localhost:4443/
curl -k https://localhost:4443/dashboard.html
```

---

#### `GET /state.json`

Returns the raw shared state as JSON. This is the primary machine-readable
API endpoint — it contains all readings from all monitored devices.

**Response**

| Header | Value |
|---|---|
| Status | `200 OK` |
| `Content-Type` | `application/json` |

**Body:** The shared state file verbatim. If the state file does not exist
yet, an empty JSON object `{}` is returned rather than an error.

**Top-level structure:**

```json
{
  "bms": {
    "updated": "2024-01-15T08:15:42",
    "readings": [ ...DeviceReading objects... ]
  },
  "victron": {
    "updated": "2024-01-15T08:15:11",
    "readings": [ ...DeviceReading objects... ]
  }
}
```

| Field | Type | Description |
|---|---|---|
| `bms.updated` | ISO 8601 string or `null` | Timestamp of the last successful BMS poll |
| `bms.readings` | array | One object per configured BMS device |
| `victron.updated` | ISO 8601 string or `null` | Timestamp of the last successful Victron poll |
| `victron.readings` | array | One object per configured Victron device |

**DeviceReading object — common fields (all device types):**

| Field | Type | Description |
|---|---|---|
| `address` | string | Bluetooth MAC address (`AA:BB:CC:DD:EE:FF`) |
| `name` | string | Label from `config.ini` |
| `device_type` | string | `"bms"`, `"mppt"`, `"inverter"`, `"monitor"`, `"dcdc"`, `"meter"` |
| `timestamp` | string | ISO 8601 timestamp of this reading |
| `voltage_v` | float or `null` | DC battery/pack voltage (V) |
| `current_a` | float or `null` | DC current (A). Positive = charging, negative = discharging |
| `power_w` | float or `null` | DC power (W). For VE.Bus inverters = AC apparent power |
| `error` | string or `null` | Human-readable error message. `null` on success |

**DeviceReading object — BMS fields** (`device_type = "bms"`):

| Field | Type | Description |
|---|---|---|
| `capacity_pct` | integer or `null` | State of charge, 0–100% |
| `remain_ah` | float or `null` | Remaining capacity (Ah) |
| `nominal_ah` | float or `null` | Design capacity (Ah) |
| `remain_wh` | float or `null` | Remaining energy (Wh) at current voltage |
| `nominal_wh` | float or `null` | Design energy (Wh) |
| `time_to_empty_h` | float or `null` | Hours until empty at current discharge rate |
| `time_to_full_h` | float or `null` | Hours until full at current charge rate |
| `cycle_count` | integer or `null` | Full charge cycles completed |
| `cell_count` | integer or `null` | Number of cells in series |
| `sw_version` | string or `null` | BMS firmware version (e.g. `"6.2"`) |
| `production_date` | string or `null` | Pack production date (`"YYYY-MM-DD"`) |
| `temp_c` | array of float | NTC sensor readings (°C). Empty array if none |
| `balance_cells` | array of integer or `null` | Per-cell balance flags. `1` = actively balancing, `0` = idle. Indexed from 0 (cell 1) |
| `protection_bits` | integer or `null` | Raw 16-bit protection status register |
| `faults` | array of string | Active fault names. Empty when healthy. See fault table below |
| `charge_fet` | boolean or `null` | Charge MOSFET enabled |
| `discharge_fet` | boolean or `null` | Discharge MOSFET enabled |

**BMS fault names** (appear in the `faults` array when active):

| Fault string | Bit | Trigger condition |
|---|---|---|
| `"Cell overvoltage"` | 0 | Individual cell voltage too high |
| `"Cell undervoltage"` | 1 | Individual cell voltage too low |
| `"Pack overvoltage"` | 2 | Total pack voltage too high |
| `"Pack undervoltage"` | 3 | Total pack voltage too low |
| `"Charge overtemp"` | 4 | Temperature too high during charging |
| `"Charge undertemp"` | 5 | Temperature too low during charging |
| `"Discharge overtemp"` | 6 | Temperature too high during discharge |
| `"Discharge undertemp"` | 7 | Temperature too low during discharge |
| `"Charge overcurrent"` | 8 | Charge current exceeded limit |
| `"Discharge overcurrent"` | 9 | Discharge current exceeded limit |
| `"Short circuit"` | 10 | Short circuit detected |
| `"IC error"` | 11 | BMS internal IC failure |
| `"MOS lock"` | 12 | MOSFET locked by protection |

**DeviceReading object — MPPT solar charger fields** (`device_type = "mppt"`):

| Field | Type | Description |
|---|---|---|
| `pv_power_w` | float or `null` | PV panel input power (W) |
| `yield_today_wh` | float or `null` | Energy harvested since midnight (Wh) |
| `load_current_a` | float or `null` | Load output current (A). `null` on models without load terminal |
| `charger_state` | string or `null` | Charger state. See states table below |

**MPPT charger states:**

| Value | Meaning |
|---|---|
| `"Off"` | Not charging |
| `"Low Power"` | Reduced power output |
| `"Fault"` | Fault condition |
| `"Bulk"` | Bulk charging phase |
| `"Absorption"` | Absorption phase (constant voltage) |
| `"Float"` | Float maintenance charge |
| `"Storage"` | Storage mode |
| `"Equalize (manual)"` | Manual equalisation |
| `"Inverting"` | Inverter mode (combined units) |
| `"Power Supply"` | Power supply mode |
| `"Starting Up"` | Startup sequence |
| `"Repeated Absorption"` | Repeated absorption |
| `"Auto Equalize"` | Automatic equalisation |
| `"Battery Safe"` | Battery-safe mode |
| `"External Control"` | Externally controlled |

**DeviceReading object — Inverter / VE.Bus fields** (`device_type = "inverter"`):

| Field | Type | Description |
|---|---|---|
| `ac_out_power_va` | float or `null` | AC output power. Real watts for VE.Bus; apparent VA for others |
| `ac_out_voltage_v` | float or `null` | AC output voltage (V) |
| `ac_out_current_a` | float or `null` | AC output current (A) |
| `inverter_state` | string or `null` | Device state. See states table below |
| `ac_in_power_w` | float or `null` | AC input real power (W). Positive = from grid, negative = feed-in |
| `ac_in_source` | string or `null` | AC input source: `"AC1"`, `"AC2"`, `"Not connected"` |
| `vebus_error` | integer or `null` | VE.Bus error code. `0` = no error |
| `temperature_c` | float or `null` | Battery temperature measured by dongle (°C) |
| `alarm_reason` | string or `null` | Alarm level: `"Warning"`, `"Alarm"`, or `null` for none |

**Inverter / VE.Bus states:**

| Value | Meaning |
|---|---|
| `"Off"` | Inverter off |
| `"Low Power"` | Standby / low-power mode |
| `"Fault"` | Fault condition |
| `"Bulk"` | Bulk charging |
| `"Absorption"` | Absorption charging |
| `"Float"` | Float charging |
| `"Storage"` | Storage mode |
| `"Equalize"` | Equalisation |
| `"Passthrough"` | Passing AC through from grid |
| `"Inverting"` | Inverting (generating AC from battery) |
| `"Power Assist"` | Assisting grid with battery power |
| `"Power Supply"` | Power supply mode |
| `"Charge"` | Charging from AC input |
| `"External Control"` | VE.Bus external control active |

**DeviceReading object — Battery Monitor fields** (`device_type = "monitor"`):

| Field | Type | Description |
|---|---|---|
| `capacity_pct` | integer or `null` | State of charge 0–100% |
| `ttg_minutes` | integer or `null` | Time to go in minutes |
| `alarm_reason` | integer or `null` | Alarm bitmask (SmartShunt raw alarm register) |

**Full annotated JSON example** — BMS reading with all fields populated:

```json
{
  "bms": {
    "updated": "2024-01-15T08:15:42",
    "readings": [
      {
        "address":        "A1:B2:C3:D4:E5:F6",
        "name":           "House Bank",
        "device_type":    "bms",
        "timestamp":      "2024-01-15T08:15:40",
        "voltage_v":      54.32,
        "current_a":      -15.0,
        "power_w":        -814.8,
        "capacity_pct":   84,
        "remain_ah":      84.0,
        "nominal_ah":     100.0,
        "remain_wh":      4562.9,
        "nominal_wh":     5432.0,
        "time_to_empty_h": 5.6,
        "time_to_full_h": null,
        "cycle_count":    8,
        "cell_count":     16,
        "sw_version":     "6.2",
        "production_date":"2025-11-26",
        "temp_c":         [23.1, 21.8, 21.9],
        "balance_cells":  [0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
        "protection_bits": 0,
        "faults":         [],
        "charge_fet":     true,
        "discharge_fet":  true,
        "error":          null
      }
    ]
  },
  "victron": {
    "updated": "2024-01-15T08:15:11",
    "readings": [
      {
        "address":         "C0:FF:EE:12:34:56",
        "name":            "Multiplus",
        "device_type":     "inverter",
        "timestamp":       "2024-01-15T08:15:09",
        "voltage_v":       54.0,
        "current_a":       -15.0,
        "power_w":         -810.0,
        "ac_out_power_va": 755.0,
        "ac_in_power_w":   0.0,
        "ac_in_source":    "Not connected",
        "inverter_state":  "Inverting",
        "temperature_c":   26.0,
        "alarm_reason":    null,
        "vebus_error":     0,
        "error":           null
      },
      {
        "address":        "11:22:33:44:55:66",
        "name":           "South Array",
        "device_type":    "mppt",
        "timestamp":      "2024-01-15T08:15:09",
        "voltage_v":      54.0,
        "current_a":      12.0,
        "power_w":        648.0,
        "pv_power_w":     680.0,
        "yield_today_wh": 3200.0,
        "charger_state":  "Float",
        "load_current_a": null,
        "error":          null
      }
    ]
  }
}
```

**Example — offline device** (`error` field populated, electrical fields `null`):

```json
{
  "address":     "A1:B2:C3:D4:E5:F6",
  "name":        "House Bank",
  "device_type": "bms",
  "timestamp":   "2024-01-15T08:15:40",
  "voltage_v":   null,
  "current_a":   null,
  "power_w":     null,
  "error":       "TIMEOUT (35s) — device connected but did not respond"
}
```

**Example requests:**

```bash
# Fetch raw state (ignore self-signed cert warning)
curl -sk https://localhost:4443/state.json | python3 -m json.tool

# Extract BMS SoC values for all packs
curl -sk https://localhost:4443/state.json | \
  python3 -c "import json,sys; s=json.load(sys.stdin); \
  [print(r['name'], r['capacity_pct']) for r in s['bms']['readings']]"

# Check when data was last updated
curl -sk https://localhost:4443/state.json | \
  python3 -c "import json,sys; s=json.load(sys.stdin); \
  print('BMS:', s['bms']['updated']); print('Victron:', s['victron']['updated'])"

# Using Python requests library (with cert verification disabled for self-signed)
import requests
state = requests.get("https://192.168.1.10:4443/state.json", verify=False).json()
house_bank = next(r for r in state["bms"]["readings"] if r["name"] == "House Bank")
print(f"SoC: {house_bank['capacity_pct']}%  Remaining: {house_bank['remain_wh']:.0f} Wh")
```

---

#### `GET /health`

Lightweight health check endpoint for monitoring systems and load balancers.

**Response**

| Header | Value |
|---|---|
| Status | `200 OK` |
| `Content-Type` | `text/plain` |

**Body:** `OK`

**Example:**

```bash
curl -sk https://localhost:4443/health
# OK
```

---

#### All other paths

**Response:** `404 Not Found` with body `Not Found`

Non-`GET` methods (POST, PUT, DELETE, etc.) also return `404 Not Found`.

---

### 15.4 Response headers

All responses from the server include these headers:

| Header | Value | Notes |
|---|---|---|
| `Content-Type` | Varies by route | `text/html; charset=utf-8`, `application/json`, or `text/plain` |
| `Content-Length` | Byte count | Always set; no chunked encoding |
| `Connection` | `close` | Connection is closed after every response |

The server does not set `Cache-Control`, `ETag`, `Last-Modified`, or CORS
headers. If you need caching control or cross-origin access, put nginx
in front (Section 15.10).

---

### 15.5 TLS and certificate details

**Auto-generated self-signed certificates** are created with:

- **Algorithm:** RSA-2048 with SHA-256 signature
- **Validity:** 10 years from generation date
- **Common Name:** `Solar Monitor`
- **Subject Alternative Names:**
  - `DNS: localhost`
  - `IP: 127.0.0.1`
  - `IP: <host>` if `host` is an IP address other than `0.0.0.0`
  - `DNS: <host>` if `host` is a hostname
- **Key file permissions:** `600` (owner-readable only)
- **Minimum TLS version:** 1.2

**Trusting the certificate in your browser/OS:**

*macOS:*
```bash
sudo security add-trusted-cert -d -r trustRoot \
  -k /Library/Keychains/System.keychain server.crt
```

*Linux (system-wide):*
```bash
sudo cp server.crt /usr/local/share/ca-certificates/solar-monitor.crt
sudo update-ca-certificates
```

*Windows:* Right-click `server.crt` → Install Certificate →
Local Machine → Place all certificates in: Trusted Root Certification Authorities.

*Chrome / Chromium:* Settings → Privacy and Security → Security →
Manage certificates → Authorities → Import → select `server.crt`.

*Firefox:* Settings → Privacy & Security → View Certificates →
Authorities → Import → select `server.crt`.

---

### 15.6 Using a real certificate (Let's Encrypt)

```ini
[server]
enabled   = true
port      = 443
cert_file = /etc/letsencrypt/live/solar.example.com/fullchain.pem
key_file  = /etc/letsencrypt/live/solar.example.com/privkey.pem
auto_cert = false
```

For port 443 without root:

```bash
sudo setcap 'cap_net_bind_service=+ep' $(which python3)
```

---

### 15.7 nginx reverse proxy

Recommended for production. nginx handles TLS from the internet; solar
monitor listens on `127.0.0.1` only.

```ini
[server]
enabled   = true
host      = 127.0.0.1
port      = 4443
auto_cert = true
```

```nginx
server {
    listen 443 ssl;
    server_name solar.example.com;

    ssl_certificate     /etc/letsencrypt/live/solar.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/solar.example.com/privkey.pem;

    location / {
        proxy_pass https://127.0.0.1:4443;
        proxy_ssl_verify off;
        add_header Cache-Control "no-cache";
    }
}
```

---

### 15.8 Security notes

- **Only `GET` is accepted.** `POST`, `PUT`, `DELETE`, and all other methods
  return `404 Not Found`.
- **No authentication.** The API is open to anyone who can reach the port.
  Use firewall rules (`ufw allow from 192.168.1.0/24 to any port 4443`),
  VPN, or nginx `auth_basic` to restrict access.
- **No directory traversal.** Only the four named routes are served.
  All other paths return `404`.
- **TLS 1.2 minimum.** TLS 1.0 and 1.1 are disabled.
- **Read timeout: 10 seconds** per request line to prevent slow-client attacks.
- **No response body for 404.** The 404 response body is the literal string
  `Not Found` — no path or file information is disclosed.

---

### 15.9 Troubleshooting

**`NET::ERR_CERT_AUTHORITY_INVALID` in browser**

Expected with a self-signed cert. Trust it (Section 15.5) or use Let's
Encrypt (Section 15.6).

**`FileNotFoundError: HTTPS server: certificate file(s) not found`**

`auto_cert = false` but the cert/key files do not exist. Either set
`auto_cert = true` or supply the files.

**`OSError: [Errno 98] Address already in use`**

Port already occupied:

```bash
sudo lsof -i :4443
sudo fuser -k 4443/tcp   # force-release the port
```

**`PermissionError: [Errno 13] Permission denied`**

Port < 1024 requires root or `CAP_NET_BIND_SERVICE`. Use port ≥ 1024.

**`curl: (60) SSL certificate problem: self-signed certificate`**

Use `curl -k` (or `--insecure`) to skip verification, or add the cert to
your system trust store.

**State file returns `{}`**

Workers have not written their first poll yet. Wait one `bms_interval`
(default 120 s) and retry.

---

### 15.10 Home automation integration examples

The `/state.json` endpoint is designed for integration with Home Assistant,
Node-RED, or any script that wants live solar data.

**Home Assistant (REST sensor):**

```yaml
# configuration.yaml
sensor:
  - platform: rest
    name: "Solar House Bank SoC"
    resource: "https://192.168.1.10:4443/state.json"
    verify_ssl: false
    value_template: >
      {{ value_json.bms.readings[0].capacity_pct }}
    unit_of_measurement: "%"
    scan_interval: 60

  - platform: rest
    name: "Solar PV Power"
    resource: "https://192.168.1.10:4443/state.json"
    verify_ssl: false
    value_template: >
      {{ value_json.victron.readings
         | selectattr('device_type', 'eq', 'mppt')
         | map(attribute='pv_power_w') | sum | round(1) }}
    unit_of_measurement: "W"
    scan_interval: 30
```

**Node-RED (HTTP Request node):**

Configure an HTTP Request node with:
- Method: `GET`
- URL: `https://192.168.1.10:4443/state.json`
- TLS: disable certificate verification for self-signed certs

Parse with a Function node:

```javascript
const state   = JSON.parse(msg.payload);
const pack    = state.bms.readings[0];
const inverter= state.victron.readings.find(r => r.device_type === 'inverter');

msg.payload = {
    soc:      pack?.capacity_pct,
    remain_wh: pack?.remain_wh,
    ac_out_w:  inverter?.ac_out_power_va,
    pv_total_w: state.victron.readings
                  .filter(r => r.device_type === 'mppt')
                  .reduce((s, r) => s + (r.pv_power_w ?? 0), 0),
};
return msg;
```

**Python polling script:**

```python
import requests
import time

URL = "https://192.168.1.10:4443/state.json"

while True:
    try:
        state = requests.get(URL, verify=False, timeout=5).json()

        for pack in state["bms"]["readings"]:
            if pack.get("error"):
                print(f"{pack['name']}: OFFLINE — {pack['error']}")
            else:
                print(f"{pack['name']}: "
                      f"{pack['capacity_pct']}% SoC  "
                      f"{pack['remain_wh']:.0f} Wh  "
                      f"{pack['current_a']:+.1f} A")

        pv_total = sum(
            r.get("pv_power_w") or 0
            for r in state["victron"]["readings"]
            if r["device_type"] == "mppt"
        )
        print(f"Total PV: {pv_total:.0f} W")
        print(f"BMS last updated: {state['bms']['updated']}")

    except Exception as e:
        print(f"Error: {e}")

    time.sleep(30)
```

---

## 16. MCP Server

Solar Monitor includes a Model Context Protocol (MCP) server that exposes
live solar data directly to AI assistants — Claude Desktop, Cursor, and any
other MCP-compatible client. Ask natural-language questions about your system
without leaving the AI interface.

**Example conversations:**

> *"What's my total PV power right now?"*
> *"Are there any battery faults I should know about?"*
> *"How long until House Bank runs out at the current draw?"*
> *"Which charger has produced the most yield today?"*

---

### 16.1 How it works

The MCP server uses the **stdio transport** — it runs as a subprocess launched
directly by the AI client, communicating over stdin/stdout using
[JSON-RPC 2.0](https://www.jsonrpc.org/specification). No port, no TLS, no
network socket. The server reads the shared state file (`solar_state.json`)
on every tool call, so responses always reflect the latest poll data.

**No additional Python packages required.** The server uses only the
standard library plus the existing `solar_monitor` package.

---

### 16.2 Installation — Claude Desktop

1. Locate or create Claude Desktop's config file:

   | Platform | Path |
   |---|---|
   | macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |
   | Windows | `%APPDATA%\Claude\claude_desktop_config.json` |
   | Linux | `~/.config/claude/claude_desktop_config.json` |

2. Add the `solar-monitor` entry:

```json
{
  "mcpServers": {
    "solar-monitor": {
      "command": "python3",
      "args": [
        "/home/pi/solar_monitor/mcp_server.py",
        "--config", "/home/pi/solar_monitor/config.ini"
      ]
    }
  }
}
```

3. Restart Claude Desktop. A solar panel icon appears in the tool bar when
   the server connects successfully.

**Verifying the connection:**

```bash
# Test the server manually — type a request and press Enter
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' | \
  python3 mcp_server.py --config config.ini
```

You should see a JSON response containing `"name": "solar-monitor"`.

---

### 16.3 Configuration

All settings live in the `[mcp]` section of `config.ini`.

| Key | Type | Default | Description |
|---|---|---|---|
| `enabled` | bool | `true` | Enable the MCP server. Set `false` to prevent startup. |
| `api_key` | string | `""` | Shared secret required in every `tools/call`. Empty = no auth. |
| `allowed_tools` | list | `""` | Comma-separated tool whitelist. Empty = all tools exposed. |
| `rate_limit` | integer | `60` | Maximum `tools/call` requests per minute. `0` = unlimited. |
| `require_local` | bool | `true` | Documents intent (stdio is inherently local; no enforcement needed). |
| `log_requests` | bool | `false` | Log every tool call to stderr for auditing. |

**`read_only` is always `true`** — the server never writes to the state file
or any other file. This is a guarantee, not a configuration option.

**Example `[mcp]` section:**

```ini
[mcp]

# Enable the MCP server (Claude Desktop starts it as a subprocess)
enabled = true

# Require a shared secret in every tool call.
# In Claude Desktop, include this in a system prompt or set in the MCP client config.
# Leave empty to disable authentication (fine for local-only use).
api_key =

# Restrict which tools are visible and callable.
# Empty means all 8 tools are available.
# Useful if you want to limit an assistant to read-only aggregate views only.
allowed_tools =

# Maximum tool calls per minute. Prevents runaway loops in automated agents.
rate_limit = 60

# Informational — stdio is always local; no network enforcement possible.
require_local = true

# Set to true to log every tool call name and arguments to stderr.
# Captured by Claude Desktop in its diagnostic logs.
log_requests = false
```

---

### 16.4 Available tools

The MCP server exposes eight read-only tools. All return structured JSON.

---

#### `get_system_status`

High-level system summary — one number per energy flow. Best starting point
for an assistant that needs a quick overview.

**No arguments.**

**Returns:**

```json
{
  "solar": {
    "total_pv_w":     680.0,
    "yield_today_wh": 3200.0,
    "mppt_online":    2,
    "mppt_total":     2
  },
  "inverter": {
    "total_ac_out_w":   755.0,
    "inverters_online": 1,
    "inverters_total":  1
  },
  "battery": {
    "avg_soc_pct":     84,
    "total_remain_wh": 9125.8,
    "net_current_a":   -15.0,
    "packs_online":    2,
    "packs_total":     2,
    "charging":        false,
    "discharging":     true
  },
  "alerts": {
    "active_faults":   [],
    "offline_devices": [],
    "alarms":          []
  },
  "data_age": {
    "bms_updated":     "2024-01-15T08:15:42",
    "victron_updated": "2024-01-15T08:15:11"
  }
}
```

---

#### `get_battery_status`

Detailed status of every JBD/Vatrer BMS battery pack. Includes SoC, energy
remaining, estimated runtime, active faults, and cell balancing.

**No arguments.**

**Returns:** `packs` array (one object per pack) + `summary`.

Each pack object:

| Field | Type | Description |
|---|---|---|
| `name` | string | Display label from config |
| `address` | string | Bluetooth MAC |
| `online` | boolean | `false` if last poll failed |
| `soc_pct` | integer | State of charge 0–100% |
| `voltage_v` | string | Pack voltage (e.g. `"54.32"`) |
| `current_a` | string | Signed current — negative = discharging |
| `power_w` | string | DC power |
| `remain_wh` | string | Energy remaining |
| `remain_ah` | string | Capacity remaining |
| `nominal_ah` | string | Design capacity |
| `time_to_empty` | string or null | e.g. `"5h36m"`, null if charging |
| `time_to_full` | string or null | e.g. `"1h12m"`, null if discharging |
| `cell_count` | integer | Cells in series |
| `cycle_count` | integer | Full charge cycles |
| `temperatures_c` | array | NTC sensor readings |
| `faults` | array | Active fault names (empty = healthy) |
| `balancing_cells` | array | 1-indexed cell numbers actively balancing |
| `charge_fet` | boolean | Charge MOSFET enabled |
| `discharge_fet` | boolean | Discharge MOSFET enabled |
| `firmware` | string | BMS firmware version |
| `error` | string | Error message when `online: false` |

---

#### `get_solar_status`

Status of all Victron SmartSolar MPPT charge controllers.

**No arguments.**

**Returns:** `chargers` array + `summary` with `total_pv_w` and `total_yield_wh`.

Each charger object:

| Field | Type | Description |
|---|---|---|
| `name` | string | Display label |
| `pv_power_w` | string | Current PV input power |
| `yield_today_wh` | string | Energy harvested since midnight |
| `battery_v` | string | Battery output voltage |
| `battery_a` | string | Battery output current |
| `charger_state` | string | `"Off"`, `"Bulk"`, `"Absorption"`, `"Float"`, etc. |
| `load_a` | string or null | Load terminal current (if present) |

---

#### `get_inverter_status`

Status of all Victron inverters and VE.Bus Smart Dongles (MultiPlus, etc.).

**No arguments.**

**Returns:** `inverters` array + `summary` with `any_alarms` and `total_ac_out_w`.

Each inverter object:

| Field | Type | Description |
|---|---|---|
| `name` | string | Display label |
| `state` | string | `"Inverting"`, `"Passthrough"`, `"Charging"`, etc. |
| `ac_out_w` | string | AC output real power (W) |
| `ac_in_source` | string | `"AC1"`, `"AC2"`, `"Not connected"` |
| `ac_in_power_w` | string | AC input power from grid |
| `battery_v` | string | DC bus voltage |
| `battery_a` | string | DC current (negative = discharging) |
| `temperature_c` | float | Dongle temperature sensor |
| `alarm` | string or null | Alarm level, null when none |
| `vebus_error` | integer or null | VE.Bus error code (null or 0 = ok) |

---

#### `get_device`

All available data for a single device, identified by name or MAC address.

**Arguments:**

| Argument | Type | Description |
|---|---|---|
| `name_or_address` | string | Device name (e.g. `"House Bank"`) or MAC (`"AA:BB:CC:DD:EE:FF"`). Case-insensitive. |

**Returns:**

```json
{
  "found":   true,
  "query":   "House Bank",
  "devices": [ { ...full DeviceReading object... } ]
}
```

`found: false` when no device matches. `devices` may contain multiple
entries if devices share a name — prefer MAC for precision.

---

#### `list_devices`

All configured devices with type, online status, and a key metric.
Use this to discover available devices before querying specific ones.

**No arguments.**

**Returns:**

```json
{
  "devices": [
    {
      "name":         "House Bank",
      "address":      "A1:B2:C3:D4:E5:F6",
      "type":         "bms",
      "online":       true,
      "soc_pct":      84,
      "last_updated": "2024-01-15T08:15:42"
    },
    {
      "name":         "South Array",
      "type":         "mppt",
      "online":       true,
      "pv_power_w":   "680.0"
    },
    {
      "name":         "MultiPlus",
      "type":         "inverter",
      "online":       true,
      "ac_out_w":     "755",
      "state":        "Inverting"
    }
  ],
  "counts": {
    "total":   3,
    "online":  3,
    "offline": 0
  }
}
```

---

#### `get_alerts`

Active alerts across the entire system. Returns `all_clear: true` when
everything is healthy — useful for polling.

**No arguments.**

**Returns:**

```json
{
  "all_clear": false,
  "offline": [
    { "name": "West Array", "type": "mppt", "error": "Device not seen during scan" }
  ],
  "battery_faults": [
    { "name": "House Bank", "fault": "Cell overvoltage" }
  ],
  "inverter_alarms": [],
  "summary": "1 device(s) offline; 1 active fault(s)"
}
```

When healthy:
```json
{ "all_clear": true, "offline": [], "battery_faults": [], "inverter_alarms": [],
  "summary": "All systems nominal." }
```

**Fault names** that can appear in `battery_faults`:

`Cell overvoltage`, `Cell undervoltage`, `Pack overvoltage`, `Pack undervoltage`,
`Charge overtemp`, `Charge undertemp`, `Discharge overtemp`, `Discharge undertemp`,
`Charge overcurrent`, `Discharge overcurrent`, `Short circuit`, `IC error`, `MOS lock`

---

#### `get_data_age`

How recently each data section was last updated. Use before relying on
readings to confirm they are fresh.

**No arguments.**

**Returns:**

```json
{
  "bms": {
    "last_updated": "2024-01-15T08:15:42",
    "age":          "45s ago",
    "readings":     2
  },
  "victron": {
    "last_updated": "2024-01-15T08:15:11",
    "age":          "2m ago",
    "readings":     2
  },
  "stale": {
    "bms":     false,
    "victron": false
  }
}
```

`stale: true` when `last_updated` is `null` (no poll has completed).
`age` format: `"45s ago"`, `"3m ago"`, `"1.5h ago"`.

---

### 16.5 Security model

**Authentication — `api_key`**

When `api_key` is set, every `tools/call` must include it as an argument:

```json
{
  "name": "get_system_status",
  "arguments": { "api_key": "your-secret-key" }
}
```

Wrong or missing key returns JSON-RPC error `-32001` (Unauthorized).
The key is stripped from arguments before reaching any tool function —
tools never see it.

With Claude Desktop, inject the key via a system prompt:
*"When using solar-monitor tools, always include `api_key: your-secret` in arguments."*

**Tool whitelisting — `allowed_tools`**

When set, only listed tools appear in `tools/list` and can be called via
`tools/call`. Attempting to call an unlisted tool returns error `-32002` (Forbidden).
This lets you restrict a general-purpose assistant to only summary views:

```ini
allowed_tools = get_system_status, get_alerts, list_devices
```

**Rate limiting — `rate_limit`**

Sliding 60-second window. When exceeded, returns error `-32000` with a
message advising the caller to retry. Protects against runaway loops in
automated agents. Set `rate_limit = 0` to disable.

**Read-only guarantee**

The MCP server never writes to the state file, dashboard, certificates, or
any other file on disk. This is hardcoded and not configurable.

**Transport security**

The stdio transport is inherently local — only a process on the same machine
running as the same user can access it. There is no network socket to firewall.

---

### 16.6 Error codes

All errors follow JSON-RPC 2.0. Custom codes in the `-32000` to `-32099` range:

| Code | Name | Meaning |
|---|---|---|
| `-32700` | Parse Error | Request is not valid JSON |
| `-32600` | Invalid Request | Not a valid JSON-RPC object |
| `-32601` | Method Not Found | Unknown method or tool name |
| `-32602` | Invalid Params | Wrong or missing arguments for a tool |
| `-32603` | Internal Error | Tool raised an unhandled exception |
| `-32000` | Rate Limited | Too many requests per minute |
| `-32001` | Unauthorized | Missing or wrong `api_key` |
| `-32002` | Forbidden | Tool not in `allowed_tools` whitelist |

---

### 16.7 Troubleshooting

**Server doesn't appear in Claude Desktop**

- Check the path in `claude_desktop_config.json` is absolute and correct
- Verify Python is at the path specified (`which python3`)
- Run the server manually and check stderr:
  ```bash
  python3 mcp_server.py --config config.ini --log-level DEBUG
  ```
  Then type `{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}` and press Enter.

**`MCP server is disabled`**

Set `enabled = true` in `[mcp]` section of `config.ini`.

**Tools return stale data**

Check `get_data_age` — if `stale: true`, the supervisor workers haven't completed
a poll. Check that `solar_monitor.py` is running and the state file is being updated:
```bash
ls -la solar_state.json    # watch mtime
```

**`Unauthorized` error on every call**

`api_key` is set in config but not being passed in arguments. Either clear `api_key`
for local use, or ensure your MCP client includes it in every request.

**`Rate Limited` on every call**

Reduce polling frequency or increase `rate_limit`. For interactive use,
`rate_limit = 0` (unlimited) is safe since a human cannot saturate 60/min manually.

**Config file not found warning at startup**

The server falls back to defaults (no auth, all tools, 60/min) and continues.
Specify the correct path with `--config`:
```bash
python3 mcp_server.py --config /absolute/path/to/config.ini
```

---

### 16.8 Command-line reference

```
python3 mcp_server.py [OPTIONS]

Options:
  --config FILE       Config file path (reads [mcp] and state_file from [general])
                      Default: config.ini in the current directory
  --state-file FILE   Override state file path from config
  --log-level LEVEL   DEBUG / INFO / WARNING / ERROR  (default: INFO)
  -h, --help          Show help and Claude Desktop config example
```

All log output goes to **stderr**. Stdout is reserved for JSON-RPC messages.

---

## 17. Historical Data & SQLite Storage

Solar Monitor can persist every reading to a local SQLite database, providing
long-term history independent of the in-memory rolling window used for
dashboard charts. The database stores all device readings with full field
fidelity, indexed for fast date-range queries, and automatically enforces a
configurable retention policy.

By default history storage is **disabled** — enable it by adding a `[history]`
section to `config.ini`.

---

### 17.1 How it works

Each worker (`bms_monitor.py`, `victron_monitor.py`) writes readings to
the SQLite database immediately after every successful poll cycle, in addition
to updating the shared state file. On startup, workers load the most recent
readings from the database to pre-populate the in-memory history used by
dashboard charts — so charts show real history across restarts.

Unsuccessful readings (where `error` is set) are never stored. The database
is shared between workers; SQLite WAL mode ensures concurrent writes never
block each other or the dashboard reader.

---

### 17.2 Configuration

```ini
[history]

# Enable SQLite history storage (disabled by default)
enabled = true

# Database file path. Relative paths are resolved from the working directory.
# The parent directories are created automatically if they don't exist.
db_path = solar_history.db

# How many days of history to retain. Older rows are deleted automatically
# during each write cycle (at most once per hour).
# 0 = keep forever (no automatic deletion).
# Default: 1095 (3 years).
retention_days = 1095

# How often to run VACUUM to compact the database file and reclaim disk space.
# VACUUM runs automatically when the configured number of days has passed
# since the last VACUUM.
vacuum_interval_days = 7
```

**Retention guidance:**

| Interval | Days |
|---|---|
| 1 year | 365 |
| 2 years | 730 |
| 3 years (default) | 1095 |
| 5 years | 1825 |
| Keep forever | 0 |

**Disk space estimate:** at 30-second Victron poll and 120-second BMS poll
with 4 devices, expect roughly 3–5 MB per month, or 40–60 MB per year.
The database compresses well; a 3-year store with 4 devices typically fits
under 200 MB.

---

### 17.3 Database schema

One table: `readings`. Every `DeviceReading` field has its own column.

**Identity columns (indexed):**

| Column | Type | Description |
|---|---|---|
| `recorded_at` | TEXT | UTC timestamp at time of storage (ISO 8601) |
| `device_name` | TEXT | Display label from config |
| `device_type` | TEXT | `"bms"`, `"mppt"`, `"inverter"`, `"monitor"`, etc. |
| `address` | TEXT | Bluetooth MAC address |

**Electrical fundamentals:** `voltage_v`, `current_a`, `power_w`

**BMS-specific:** `capacity_pct`, `cell_count`, `remain_ah`, `nominal_ah`,
`remain_wh`, `nominal_wh`, `time_to_empty_h`, `time_to_full_h`,
`cycle_count`, `sw_version`, `production_date`, `protection_bits`,
`charge_fet`, `discharge_fet`, `temp_c` (JSON), `faults` (JSON),
`balance_cells` (JSON)

**MPPT-specific:** `pv_power_w`, `yield_today_wh`, `load_current_a`,
`charger_state`

**Inverter/VE.Bus-specific:** `ac_out_power_va`, `ac_out_voltage_v`,
`ac_out_current_a`, `inverter_state`, `ac_in_power_w`, `ac_in_source`,
`vebus_error`, `temperature_c`

List fields (`temp_c`, `faults`, `balance_cells`) are stored as JSON strings.
Indexes exist on `recorded_at`, `device_name`, `device_type`, and the
composite `(device_name, recorded_at)`.

---

### 17.4 Management utilities

Two command-line utilities live in the `utils/` directory. Both accept a
`--config` argument and can be run from the repository root without
installation.

---

#### `utils/purge_history.py` — delete historical data

```
python utils/purge_history.py [OPTIONS]

Filters (AND logic — combine freely):
  --before DATE         Delete rows with recorded_at < DATE
  --after  DATE         Delete rows with recorded_at > DATE
  --device NAME         Restrict to this device name (exact match)
  --type   TYPE         Restrict to device type: bms, mppt, inverter, ...

Actions:
  --enforce-retention   Delete all rows older than retention_days
  --vacuum              Run VACUUM after deletion to reclaim space
  --stats               Show database statistics and exit
  --list-devices        List all devices with row counts and exit

Safety:
  --dry-run             Count matching rows without deleting
  --yes / -y            Skip the confirmation prompt
```

**Examples:**

```bash
# See database statistics first
python utils/purge_history.py --config config.ini --stats

# Dry run — see what would be deleted before a given date
python utils/purge_history.py --config config.ini \
    --before 2023-01-01 --dry-run

# Delete everything before 1 January 2023
python utils/purge_history.py --config config.ini \
    --before 2023-01-01

# Delete a specific bad-data window (e.g. sensor was misconfigured)
python utils/purge_history.py --config config.ini \
    --after 2024-03-01 --before 2024-03-05

# Remove all data for a decommissioned device
python utils/purge_history.py --config config.ini \
    --device "Old Pack"

# Delete old BMS data but keep Victron data
python utils/purge_history.py --config config.ini \
    --type bms --before 2023-06-01

# Apply the configured retention policy right now, then compact
python utils/purge_history.py --config config.ini \
    --enforce-retention --vacuum

# Non-interactive (scripted/cron use)
python utils/purge_history.py --config config.ini \
    --before 2023-01-01 --yes
```

---

#### `utils/query_history.py` — query and export data

```
python utils/query_history.py [OPTIONS]

Filters:
  --device NAME         Filter by device name (exact match)
  --type   TYPE         Filter by device type
  --start  DATE         Earliest recorded_at (e.g. 2024-01-01, or 'today', 'yesterday')
  --end    DATE         Latest  recorded_at (inclusive)
  --limit  N            Maximum number of rows

Output:
  --format table|csv|json   Output format (default: table)
  --fields col1,col2,...    Comma-separated column list

Info:
  --stats               Show database statistics
  --list-devices        List all devices with row counts
  --list-fields         List all available column names
```

**Examples:**

```bash
# Show recent readings for all devices (terminal table)
python utils/query_history.py --config config.ini

# Export a device's full history as CSV
python utils/query_history.py --config config.ini \
    --device "House Bank" --format csv > house_bank.csv

# Export a date range as JSON
python utils/query_history.py --config config.ini \
    --start 2024-01-01 --end 2024-01-31 --format json

# Export only key fields — useful for smaller CSV files
python utils/query_history.py --config config.ini \
    --device "House Bank" \
    --fields recorded_at,voltage_v,current_a,capacity_pct,remain_wh \
    --format csv > house_bank_soc.csv

# Today's MPPT yield
python utils/query_history.py --config config.ini \
    --type mppt --start today \
    --fields recorded_at,device_name,pv_power_w,yield_today_wh

# Most recent 20 readings
python utils/query_history.py --config config.ini \
    --limit 20 --order desc

# All available column names
python utils/query_history.py --config config.ini --list-fields
```

---

### 17.5 Automatic retention enforcement

Retention is enforced automatically — no cron job required. After each
write batch, the worker checks whether retention enforcement was run in the
last hour. If not, it deletes all rows where `recorded_at` is older than
`retention_days` days ago.

This means:

- Retention runs at most once per hour per worker process.
- Old data is removed gradually as new data arrives.
- No separate cleanup process or scheduled task is needed.

To apply retention immediately (e.g. after changing `retention_days` to a
smaller value), run:

```bash
python utils/purge_history.py --config config.ini --enforce-retention
```

To disable automatic retention and manage it manually, set:

```ini
[history]
retention_days = 0
```

Then schedule `purge_history.py` via cron:

```cron
# Purge data older than 1 year, every Sunday at 03:00
0 3 * * 0 cd /home/pi/solar_monitor && python utils/purge_history.py \
    --config config.ini --before $(date -d '1 year ago' +\%Y-\%m-\%d) --yes
```

---

### 17.6 Dashboard chart integration

When history is enabled, workers load the most recent readings from SQLite
on startup to pre-populate the in-memory chart history. This means:

- Charts show real data immediately after a restart, not just data since the
  last restart.
- The in-memory rolling window (`max_history`, default 600 points) is
  seeded from SQLite, then extended in memory as new polls arrive.
- The SQLite database is not queried on every dashboard render — only on
  worker startup. Live chart updates continue to use the in-memory dict.

---

### 17.7 Troubleshooting

**`FileNotFoundError: database not found`** (utils)

The database is created automatically when the first reading is written.
Make sure the monitor has run at least one successful poll cycle with
`[history] enabled = true` before using the utilities.

**Database grows faster than expected**

Check `vacuum_interval_days`. After large deletions the file size does not
shrink until VACUUM runs. Run it manually:

```bash
python utils/purge_history.py --config config.ini --vacuum
```

Or reduce the vacuum interval:

```ini
[history]
vacuum_interval_days = 1
```

**Charts are empty after enabling history**

The in-memory history dict is populated on startup from the most recent
`max_history` readings. If the database is new and no polls have completed,
the dict will be empty until the first poll. This is expected — restart the
monitor after the first poll cycle.

**History writes are slow**

SQLite WAL mode (used by default) typically handles hundreds of inserts per
second. If writes are slow, check that the database is on a local filesystem
(not NFS or a network share) and that the disk is not full.
