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
7. [The Dashboard](#7-the-dashboard)
8. [Victron Device Setup](#8-victron-device-setup)
9. [JBD / Vatrer BMS Setup](#9-jbd--vatrer-bms-setup)
10. [Architecture](#10-architecture)
11. [Adding a New Data Source](#11-adding-a-new-data-source)
12. [Troubleshooting](#12-troubleshooting)
13. [Reference](#13-reference)

---

## 1. Overview

Solar Monitor is a local Bluetooth dashboard for solar power systems. It reads
data directly from Victron Energy devices and JBD/Vatrer BMS battery packs over
BLE, writes a self-contained HTML dashboard, and requires no cloud connection,
no app, and no internet access.

**What it displays**

| Source | Data |
|---|---|
| Victron VE.Bus Smart Dongle (MultiPlus-II) | Battery V/A/W/temp/SoC, AC output watts, AC input source, device state, alarm |
| Victron SmartSolar MPPT | PV power, battery V/A, yield today, charger state |
| Victron SmartShunt / BMV | Battery V/A/SoC, time-to-go |
| JBD / Vatrer BMS | Pack V/A/W/SoC, remaining Ah/Wh, TTE/TTF, per-cell voltages, temperatures, active faults, balance status, FET state |

**Key design decisions**

- **No GATT scanning for BMS** — connects directly by MAC address. BlueZ builds the D-Bus path from the MAC itself; no prior scan required.
- **Passive BLE scanning for Victron** — the adapter listens but never sends scan requests. Falls back to active scanning automatically if the kernel doesn't support passive mode.
- **Supervisor process** — a single `solar_monitor.py` manages all worker subprocesses, restarts crashed workers, and writes the dashboard independently.
- **Shared state file** — workers communicate through an atomic JSON file; no sockets, no shared memory.

---

## 2. Requirements

### Hardware

- **Linux host** with Bluetooth: Raspberry Pi 3B+/4/5, any x86 Linux box with a USB or built-in BT adapter
- **BlueZ** 5.50 or later (check: `bluetoothctl --version`)
- Devices within **BLE range** — roughly 10 m line-of-sight; walls and metal enclosures reduce this significantly

### Python

- **Python 3.11 or later** (check: `python3 --version`)
- **bleak** ≥ 0.20 — BLE library
- **cryptography** — Victron AES-128-CTR decryption

### Supported Victron devices

Any Victron device with Instant Readout enabled in VictronConnect:

| Device | Record type | Notes |
|---|---|---|
| VE.Bus Smart Dongle (MultiPlus-II, etc.) | 0x07 / 0x0C | Richest data; attach dongle to VE.Bus port |
| SmartSolar MPPT | 0x01 | All MPPT models |
| Phoenix Inverter Smart | 0x03 | |
| SmartShunt / BMV-712 | 0x02 | |
| Orion XS DC-DC | 0x0E | |

### Supported BMS

- **JBD** protocol packs — sold under many brands: Vatrer, Overkill Solar, Redodo, Chins, Enjoybot, and generic JBD-based units
- Identification: BLE advertises as `BT-TH-XXXXXXXX`, `JBD-`, or similar

---

## 3. Installation

### 3.1 Extract the archive

```bash
tar -xzf solar_monitor.tar.gz
cd solar_monitor
```

The directory structure:

```
solar_monitor/
├── solar_monitor.py        ← Supervisor (recommended entry point)
├── bms_monitor.py          ← BMS worker (can run standalone)
├── victron_monitor.py      ← Victron worker (can run standalone)
├── jbd_bms_monitor.py      ← Combined legacy launcher
├── config.ini.example      ← Annotated configuration template
├── MANUAL.md               ← This file
├── GUIDE.md                ← Quick-start guide
├── CONFIG.md               ← Config reference
├── solar_monitor/          ← Python package
│   ├── scanner.py
│   ├── jbd.py
│   ├── victron.py
│   ├── dashboard.py
│   ├── state.py
│   ├── config.py
│   └── models.py
└── tests/                  ← 330 unit tests
```

### 3.2 Install Python dependencies

```bash
pip install bleak cryptography
```

If `pip` installs to a system location that requires privileges:

```bash
pip install --user bleak cryptography
# or
python3 -m pip install bleak cryptography
```

### 3.3 Bluetooth permissions

BlueZ requires either root or membership in the `bluetooth` group:

```bash
sudo usermod -aG bluetooth $USER
# Log out and back in, then verify:
id | grep bluetooth
```

To confirm BLE is working before configuring:

```bash
bluetoothctl
[bluetooth]# scan on
# Devices should appear within a few seconds
[bluetooth]# scan off
[bluetooth]# exit
```

### 3.4 Configure

```bash
cp config.ini.example config.ini
nano config.ini    # or your preferred editor
```

The minimum required entries are your device MACs and keys. See [Section 4](#4-configuration) for full details.

### 3.5 Verify the installation

```bash
# Check which workers would start without actually launching them
python3 solar_monitor.py --config config.ini --list-workers

# Run one poll cycle to confirm everything works
python3 solar_monitor.py --config config.ini
# Let it run for one full cycle, then Ctrl-C
```

A successful first run looks like:

```
08:15:01 [INFO]  Solar Monitor supervisor starting — 2 worker(s)  config: config.ini
08:15:01 [INFO]  [Victron] Victron worker starting — state: solar_state.json  interval: 30s
08:15:01 [INFO]  [BMS] BMS worker starting — state: solar_state.json  interval: 120s
08:15:01 [INFO]  [Victron] Listening for Victron advertisements (passive) …
08:15:11 [INFO]  [Victron] 'Multiplus-Ii' (E6:2E:31:75:9A:1A): 3 payload(s) accumulated
08:15:11 [INFO]  [Victron] [VE.Bus] Multiplus-Ii: V=54.0  A=-15.0  ac=755VA  state=Inverting
08:15:11 [INFO]  Dashboard written -> /home/pi/solar_monitor/dashboard.html
```

### 3.6 Run the test suite

```bash
python3 -m unittest discover -s tests -v
# Expected: 330 tests, 0 failures
```

---

## 4. Configuration

All settings live in `config.ini`. The file has four sections.

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
| `scan_timeout` | — | 10 s | Long enough for Victron to cycle through all record types |

For systems with many BMS packs, add `(N_packs × 40s)` to `bms_interval` to account for sequential polling.

### 4.2 `[bms]`

One line per JBD battery pack:

```ini
[bms]
Label = MAC_ADDRESS [ : password ]
```

- **Label** — shown as the card title on the dashboard (e.g. `House Bank`)
- **MAC** — colon-separated Bluetooth address (`A1:B2:C3:D4:E5:F6`)
- **password** — optional 6-digit numeric password; omit entirely if no password is set

```ini
[bms]
House Bank   = A1:B2:C3:D4:E5:F6 : 123456
Spare Pack   = A1:B2:C3:D4:E5:F7
```

Leave this section empty or absent to disable BMS polling entirely.

### 4.3 `[victron]`

One line per Victron device:

```ini
[victron]
Label = MAC : KEY  [ type=mppt|inverter|monitor|dcdc ]
```

- **Label** — card title (e.g. `South Array`, `MultiPlus`)
- **MAC** — Bluetooth address
- **KEY** — 32-character Advertisement Key from VictronConnect (see [Section 8](#8-victron-device-setup))
- **type** — optional; controls dashboard card layout and which record types are accepted

| `type=` | Dashboard card | Accepted devices |
|---|---|---|
| `mppt` | Solar Charger | SmartSolar MPPT |
| `inverter` | Inverter / VE.Bus | MultiPlus (via dongle), Phoenix Inverter |
| `monitor` | Battery Monitor | SmartShunt, BMV-712 |
| `dcdc` | DC-DC Converter | Orion XS |

```ini
[victron]
South Array  = 11:22:33:44:55:01 : aabbccddeeff00112233445566778899  type=mppt
West Array   = 11:22:33:44:55:02 : 00112233445566778899aabbccddeeff  type=mppt
MultiPlus    = C0:FF:EE:12:34:56 : 0123456789abcdef0123456789abcdef  type=inverter
```

Leave this section empty or absent to disable Victron polling.

### 4.4 Complete annotated example

```ini
# ─── solar_monitor/config.ini ─────────────────────────────────────────────────

[general]

# Path where the HTML dashboard is written.
# Use an absolute path when running as a service.
output = /home/pi/solar_monitor/dashboard.html

# Shared state file — all workers read and write to this JSON file.
# Each worker owns one section; writes are atomic (temp file → rename).
state_file = /home/pi/solar_monitor/solar_state.json

# How often each worker polls.  The BMS interval should account for the
# time required to connect to all packs sequentially.
bms_interval     = 120    # 2 min — battery SoC changes slowly
victron_interval = 30     # 30 s  — real-time power data

# How long the Victron worker listens for BLE advertisements each cycle.
# 10 s is usually enough for 1-5 devices.  Increase to 15 s if devices
# are frequently reported as "not seen during scan".
scan_timeout = 10

# How many data points to keep per device for the dashboard chart.
# At 30 s interval: 600 points = 5 hours of history.
max_history = 600

# Console log verbosity.  Use DEBUG to see all BLE traffic and
# decrypted Victron packets.
log_level = INFO

# Dashboard colour theme.  Can be changed without restarting.
theme = business

# ─── JBD / Vatrer BMS packs ───────────────────────────────────────────────────

[bms]
# Format: Label = MAC_ADDRESS [ : password ]
# Password is optional. Most packs default to 123456.
# Find the MAC with: bluetoothctl scan on  (look for BT-TH-* devices)

House Bank = A1:B2:C3:D4:E5:F6 : 123456
# Spare    = A1:B2:C3:D4:E5:F7

# ─── Victron BLE devices ──────────────────────────────────────────────────────

[victron]
# Format: Label = MAC : KEY  [ type=mppt|inverter|monitor|dcdc ]
# Get KEY from VictronConnect → device → gear → Product Info → Instant Readout.

South Array = 11:22:33:44:55:01 : aabbccddeeff00112233445566778899  type=mppt
West Array  = 11:22:33:44:55:02 : 00112233445566778899aabbccddeeff  type=mppt

# VE.Bus Smart Dongle attached to MultiPlus-II 48/5000/70-95 120V.
# The dongle appears as a separate device in VictronConnect.
MultiPlus = C0:FF:EE:12:34:56 : 0123456789abcdef0123456789abcdef  type=inverter
```

---

## 5. Running the Monitor

### 5.1 Supervisor mode (recommended)

`solar_monitor.py` is the recommended entry point. It reads the config,
determines which workers are needed based on populated config sections,
and manages them as subprocesses:

```bash
python3 solar_monitor.py --config config.ini
```

**Options:**

| Flag | Description |
|---|---|
| `--config FILE` | Config file path (default: `config.ini`) |
| `--log-level LEVEL` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `--list-workers` | Show which workers would start, then exit |

**`--list-workers` example:**

```bash
$ python3 solar_monitor.py --config config.ini --list-workers

Workers that would start:
  Victron      victron_monitor.py
  BMS          bms_monitor.py
```

Workers are selected automatically: if `[bms]` has entries, the BMS worker starts; if `[victron]` has entries, the Victron worker starts. If a section is empty or absent, that worker is skipped.

### 5.2 Standalone workers

Each worker can run independently for debugging or if you prefer manual management:

```bash
# Victron only
python3 victron_monitor.py --config config.ini

# BMS only
python3 bms_monitor.py --config config.ini
```

**Worker flags** (both workers accept the same set):

| Flag | Description |
|---|---|
| `--config FILE` | Config file |
| `--state-file FILE` | Override shared state file path |
| `--interval SECS` | Override poll interval from config |
| `--output FILE` | Override dashboard output path |
| `--scan-timeout SECS` | Override BLE scan duration |
| `--once` | Poll once and exit (useful for testing) |
| `--log-level LEVEL` | Log verbosity |
| `--theme THEME` | Dashboard theme |

**Test a single poll cycle:**

```bash
python3 victron_monitor.py --config config.ini --once --log-level DEBUG
python3 bms_monitor.py     --config config.ini --once --log-level DEBUG
```

### 5.3 Combined legacy mode

`jbd_bms_monitor.py` runs everything in a single process — useful for very simple setups with one BMS pack:

```bash
python3 jbd_bms_monitor.py --config config.ini
```

This is less robust than supervisor mode: if either the BMS or Victron polling blocks, the other is delayed. Not recommended for multi-pack systems.

---

## 6. Running as a System Service

### 6.1 Single service unit (supervisor mode)

Create `/etc/systemd/system/solar-monitor.service`:

```ini
[Unit]
Description=Solar Monitor
Documentation=file:///home/pi/solar_monitor/MANUAL.md
After=network.target bluetooth.target
Wants=bluetooth.target

[Service]
Type=simple
User=pi
Group=pi
WorkingDirectory=/home/pi/solar_monitor
ExecStart=/usr/bin/python3 solar_monitor.py --config config.ini --log-level INFO
Restart=on-failure
RestartSec=10
# Give BlueZ time to start after boot
ExecStartPre=/bin/sleep 5
# Ensure BlueZ has the correct adapter state on start
ExecStartPre=/usr/bin/bluetoothctl power on
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable solar-monitor
sudo systemctl start  solar-monitor

# Check status
sudo systemctl status solar-monitor

# Follow logs
journalctl -u solar-monitor -f

# Restart after config changes
sudo systemctl restart solar-monitor
```

### 6.2 Serving the dashboard

The dashboard is a self-contained HTML file. The simplest way to serve it:

**Python's built-in server (development):**

```bash
cd /home/pi/solar_monitor
python3 -m http.server 8080
# Browse to: http://raspberrypi.local:8080/dashboard.html
```

**nginx (production):**

```bash
sudo apt install nginx
```

`/etc/nginx/sites-available/solar`:
```nginx
server {
    listen 80;
    server_name _;
    root /home/pi/solar_monitor;
    location / {
        try_files $uri $uri/ =404;
        add_header Cache-Control "no-cache";
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/solar /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
# Browse to: http://raspberrypi.local/dashboard.html
```

Point `output` in `config.ini` to wherever your web server serves from:

```ini
[general]
output = /home/pi/solar_monitor/dashboard.html
```

### 6.3 Auto-refresh the browser

Add this to a `<head>` tag, or use a browser extension. Alternatively, set the
dashboard page to auto-refresh by opening it with a query parameter:

The dashboard does not auto-refresh itself — reload the browser tab to see
updated readings. On a dedicated display (Raspberry Pi touchscreen, etc.),
a simple kiosk setup with `chromium-browser --kiosk` and an auto-reload
extension works well.

---

## 7. The Dashboard

The dashboard is a single, self-contained HTML file with no external dependencies.
It works in any modern browser and can be opened directly from the filesystem
(`file://`) or served over HTTP.

### 7.1 Themes

Three themes are available, switchable with the button in the top-right corner:

| Theme | Description |
|---|---|
| `dark` | Dark background, high contrast |
| `light` | Light background |
| `business` | Dark with blue accents, Share Tech Mono font |

The selected theme is remembered in `localStorage` across page loads.
Set the default in `config.ini` with `theme = business`.

### 7.2 BMS card

One card per battery pack. Displays:

**Main metrics row:** Pack voltage (V) · Current (A, signed) · Power (W)

**SoC bar:** Colour-coded — green above 50%, yellow 20–50%, red below 20%.
Shows remaining Wh alongside the percentage.

**Capacity row:** `84.0 / 100.0 Ah` · `TTE 5h36m` (time to empty) · `TTF 1h12m` (time to full).
TTE and TTF only appear when current is flowing.

**Pack info:** `16 cells · 8 cycles · CHG ✓ DSG ✓ · fw 6.2`

**Temperatures:** All NTC sensor readings in °C.

**Faults (when active):** Red text listing active protection triggers, e.g.
`⚠ Cell overvoltage, Discharge overcurrent`

**Balance (when active):** `⚡ Balancing cells: 4, 7` — only shown when
at least one cell is actively balancing.

### 7.3 VE.Bus inverter card

For a MultiPlus-II monitored via VE.Bus Smart Dongle. Matches VictronConnect's layout.

**AC Output L1 section:**
- Voltage (V): `120` (hardcoded — not transmitted in BLE record)
- Power (W): Real AC output watts from the dongle
- Current (A): Computed as Power ÷ 120V

**Battery section:**
- Voltage (V): DC bus voltage
- Current (A): Raw signed value — negative = discharging, positive = charging
- Temperature: Dongle's onboard sensor (°C)

**Status row:** `STATE: Inverting · AC In: Not connected · ALARM: None`

### 7.4 Solar charger card (MPPT)

- PV Power (W) · Battery V · Battery A
- Yield Today (Wh)
- Charger state: `Bulk` / `Absorption` / `Float` / `Storage` / `Off`
- Load current (A): shown on models with a load output terminal

### 7.5 Battery Monitor card (SmartShunt / BMV)

- Battery V · Current A · SoC %
- Time to go (minutes)
- Alarm status

### 7.6 System totals banner

Spans the full width of the page:

- **Battery Power** — sum of BMS pack power (V × A). Does not include inverter DC readings to avoid double-counting.
- **PV Power** — sum of MPPT solar charger output watts.
- **Yield Today** — total energy harvested across all MPPTs.

### 7.7 Charts

Each card includes a small sparkline chart showing the voltage, current, or
power history for that device. The chart retains up to `max_history` data
points (default 600, about 5 hours at 30 s interval).

Charts render on page load from data embedded in the HTML. They update
automatically when the page is refreshed.

---

## 8. Victron Device Setup

### 8.1 Enable Instant Readout

Victron Instant Readout must be enabled on each device before the monitor
can read its data.

1. Open **VictronConnect** on your phone or tablet
2. Connect to the device
3. Tap the **⚙️ gear icon** → **Product Info**
4. Scroll to **Instant Readout via Bluetooth**
5. If the toggle is **off**, enable it
6. Tap **Show** to reveal the Advertisement Key
7. Copy the 32-character key
8. Note the **MAC address** shown above the key

> **iOS note:** iOS shows a UUID instead of a MAC address. Use an Android
> device, or scan with a Linux tool (`bluetoothctl scan on`) to find the
> real MAC.

### 8.2 VE.Bus Smart Dongle (MultiPlus-II)

The VE.Bus Smart Dongle plugs into the VE.Bus port on the inverter/charger
and appears as a **separate device** from the inverter's built-in Bluetooth.
In VictronConnect it is listed under its own entry — usually named after the
inverter system.

**Setup:**

1. In VictronConnect, find the dongle entry (separate from the inverter)
2. Connect to it and follow steps in 8.1 above
3. Add to `config.ini` as `type=inverter`

**Data available from the dongle:**

| Field | Notes |
|---|---|
| Battery voltage (V) | DC bus voltage |
| Battery current (A) | Signed — negative = discharging |
| AC output real power (W) | True watts, not apparent |
| AC input source | AC1 / AC2 / Not connected |
| AC input power (W) | Watts from grid/generator |
| Battery temperature (°C) | Dongle onboard sensor |
| Device state | Inverting / Passthrough / Charging / etc. |
| VE.Bus error code | 0 = no error |
| Alarm level | None / Warning / Alarm |
| State of charge (%) | When reported by the VE.Bus system |

**Firmware note:** Older dongle firmware broadcasts record type `0x07` with
the full VE.Bus layout. Newer firmware uses `0x0C`. Both are parsed identically.
To update: VictronConnect → dongle → gear → Product Info → Firmware → Check.

### 8.3 Verifying Victron reception

With `--log-level DEBUG`, each received advertisement candidate is logged:

```
[Victron] Multiplus-Ii: candidate fmt=A rec=0x07 nonce=0xF357 len=21 raw=...
[Victron] Multiplus-Ii: SUCCESS rec=0x07 dec=09006aff18950000981700c2ff
[VE.Bus] Multiplus-Ii: V=54.0  A=-15.0  ac=755VA  state=Inverting
```

If you only see Format B `rec=0x02` candidates (no Format A records), the
device is broadcasting VE.Smart Networking beacons but not Instant Readout.
Enable Instant Readout in VictronConnect and re-try.

---

## 9. JBD / Vatrer BMS Setup

### 9.1 Finding the MAC address

**Method 1 — bluetoothctl:**
```bash
bluetoothctl
[bluetooth]# scan on
# Wait 20 seconds. BMS devices typically appear as "BT-TH-XXXXXXXX"
[bluetooth]# devices
[bluetooth]# scan off
[bluetooth]# exit
```

**Method 2 — nRF Connect (Android):**
Install nRF Connect from Google Play. Scan for devices. BMS packs appear
with names like `BT-TH-A1B2C3D4`, `JBD-BMS`, or similar.

**Method 3 — LightBlue (iOS):**
Same as nRF Connect but for iOS.

### 9.2 Password

Factory default is usually `123456`. If the pack has a custom password,
add it after the MAC with a colon separator. If no password is set, omit
the colon entirely:

```ini
[bms]
House Bank = A1:B2:C3:D4:E5:F6 : 123456   # with password
Spare Pack = A1:B2:C3:D4:E5:F7             # no password
```

Common default passwords: `000000`, `123456`, `888888`. Check your BMS
manufacturer's documentation if none of these work.

### 9.3 BLE range and reliability

JBD packs in metal battery enclosures can have poor BLE range. If you see
frequent timeouts:

- Move the Pi closer to the battery bank
- Use a USB Bluetooth adapter with an external antenna
- Increase `bms_interval` to reduce connection frequency
- Check that no thick metal enclosure is blocking the signal

Each device is polled sequentially with a gap between connections. With
multiple packs, add roughly 40 s per pack when setting `bms_interval`.

### 9.4 Verifying BMS connectivity

```bash
python3 bms_monitor.py --config config.ini --once --log-level DEBUG
```

A successful read looks like:

```
[BMS]  House Bank: JBD service matched: 0000ff00-...
[BMS]  House Bank: 54.32V  0.00A  0.00W  SoC=100%  5455.1Wh
```

A failed read shows the specific error:

```
[BMS]  House Bank: TIMEOUT (35s) — device connected but did not respond
[BMS]  House Bank: ERROR — BMS rejected password — update the password in your config
```

---

## 10. Architecture

### 10.1 Process model

```
solar_monitor.py (supervisor)
│
├── victron_monitor.py  ←→  solar_state.json["victron"]  →  dashboard.html
│     passive BLE scan                                      (written by supervisor)
│     every 30 s
│
└── bms_monitor.py      ←→  solar_state.json["bms"]
      GATT connect by MAC
      every 120 s
```

The supervisor:
1. Reads config and determines which workers to launch
2. Starts each worker as a child subprocess (`asyncio.create_subprocess_exec`)
3. Streams worker stdout/stderr into its own log with `[WorkerName]` prefix
4. Restarts crashed workers with exponential backoff (1 → 2 → 4 → … → 60 s)
5. Abandons a worker after 10 crashes in one hour (logs an error)
6. Writes the dashboard independently on a timer by merging all state sections

Workers are fully isolated — a hung BMS connection cannot delay Victron data
collection, and a Victron scan error does not affect BMS polling.

### 10.2 BLE strategy

| Device type | Strategy | Why |
|---|---|---|
| BMS | `BleakClient(mac_string)` direct GATT | No scan needed; BlueZ constructs the D-Bus path from the MAC |
| Victron | `BleakScanner` passive, MAC-filtered | Advertisement protocol — no GATT; device broadcasts continuously |

**Passive vs active scanning for Victron:**

Passive mode (`scanning_mode="passive"`) means the adapter listens but never
sends scan request packets. This reduces radio activity and avoids interfering
with other devices. It requires BlueZ `or_patterns` to tell the kernel which
AD types to deliver.

If passive mode fails (unsupported kernel or adapter), the monitor automatically
falls back to active scanning. Active scanning works identically for Victron —
their devices broadcast without solicitation, so the adapter receives the same
advertisement data regardless of mode.

### 10.3 State file

The shared state file (`solar_state.json`) is a plain JSON document:

```json
{
  "bms": {
    "updated": "2024-01-01T12:00:00",
    "readings": [ { "name": "House Bank", "voltage_v": 54.32, ... } ]
  },
  "victron": {
    "updated": "2024-01-01T12:00:01",
    "readings": [ { "name": "Multiplus-Ii", "ac_out_power_va": 755, ... } ]
  }
}
```

Each worker updates only its own section using an atomic write (write to
`.tmp` then `os.replace`). The other section is never touched. If the file
is missing or corrupt on startup, the process creates a fresh state.

### 10.4 Victron Instant Readout decryption

Victron devices broadcast encrypted advertisements using AES-128-CTR with
a per-device key (the "Advertisement Key" from VictronConnect). The nonce
is a 16-bit counter that increments with each advertisement. Decryption is
entirely local — no Victron servers involved.

Each advertisement can carry multiple record types (solar charger, battery
monitor, VE.Bus, etc.) in rotation. The monitor accumulates all payloads
seen for a device during the scan window, tries each parser in priority order,
and uses the first that successfully decrypts and passes plausibility checks.

---

## 11. Adding a New Data Source

The supervisor is generic — any script that follows the worker contract can be
added without modifying the core monitor code.

### 11.1 Worker contract

A worker script must:

1. Accept `--config FILE --state-file FILE --log-level LEVEL --once` arguments
2. Write its readings to the shared state file using `save_section(state_file, "section_name", readings)`
3. Loop indefinitely (the supervisor restarts it if it exits unexpectedly)
4. Exit with code 0 on clean shutdown (e.g. `--once`), non-zero on error

### 11.2 Register the worker

In `solar_monitor.py`, add an entry to `WORKER_REGISTRY`:

```python
WORKER_REGISTRY: list[WorkerSpec] = [
    WorkerSpec(
        name             = "Victron",
        script           = "victron_monitor.py",
        state_section    = "victron",
        config_sections  = ["victron", "mppt"],
        interval_cfg_key = "victron_interval",
        min_gap          = 10.0,
    ),
    WorkerSpec(
        name             = "BMS",
        script           = "bms_monitor.py",
        state_section    = "bms",
        config_sections  = ["bms"],
        interval_cfg_key = "bms_interval",
        min_gap          = 30.0,
    ),
    # Add your new worker here:
    WorkerSpec(
        name             = "EcoFlow",
        script           = "ecoflow_monitor.py",
        state_section    = "ecoflow",
        config_sections  = ["ecoflow"],
        interval_cfg_key = "ecoflow_interval",
        min_gap          = 30.0,
    ),
]
```

### 11.3 Add to AppConfig

In `solar_monitor/config.py`, add the interval field to `AppConfig` and
load it in `load_config()`:

```python
# In AppConfig dataclass:
ecoflow_interval: float = 60.0

# In load_config():
cfg.ecoflow_interval = float(g.get("ecoflow_interval", cfg.ecoflow_interval))
```

### 11.4 Add to state.py (if needed)

If the new source has its own state section beyond "bms" and "victron",
add it to `load_state()`:

```python
for section in ("bms", "victron", "ecoflow"):   # add new section here
    ...
```

### 11.5 Add config section

Users add their devices to `config.ini`:

```ini
[ecoflow]
# Power Station = MAC : USER_ID
Delta Pro = AA:BB:CC:DD:EE:FF : 1234567890
```

When `[ecoflow]` has entries, the supervisor automatically starts `ecoflow_monitor.py`.

---

## 12. Troubleshooting

### 12.1 Supervisor / startup issues

**`No workers to start — add devices to [bms] and/or [victron]`**

All config sections are empty or absent. Check that:
- `config.ini` exists and is readable
- `[bms]` and/or `[victron]` sections contain un-commented device lines
- The path passed to `--config` is correct

**Worker shows `SIGTERM` immediately after starting**

Another instance is running. Check: `ps aux | grep monitor.py`

**`bluetooth.service not found` or `bluetoothctl: command not found`**

BlueZ is not installed: `sudo apt install bluetooth bluez`

### 12.2 BMS issues

**`ERROR — 'path'`** *(KeyError: 'path')*

This was a bug in an older version where a synthetic `BLEDevice` with
empty `details` was passed to bleak, causing a D-Bus path lookup failure.
Update to the current version where `BleakClient` is called with the MAC
string directly.

**`TIMEOUT (35s)`**

- Device is out of BLE range or in a metal enclosure
- Another device (phone app) has an open GATT connection — close it
- BlueZ is in a bad state: `sudo hciconfig hci0 reset` then restart

**`BMS rejected password`**

The password in config is wrong. Try `000000`, `123456`, `888888`.

**`BMS checksum mismatch — data may be corrupt`**

The packet was received but failed integrity check. Usually transient
(RF interference). The monitor warns and continues — data is still used.

**Multiple packs: only first pack succeeds, others timeout**

Packs are polled sequentially. If `bms_interval` is too short for all packs
to complete, increase it: `bms_interval = N_packs × 45`.

### 12.3 Victron issues

**`Device not seen during scan`**

- Device is out of BLE range
- Instant Readout is not enabled in VictronConnect
- Wrong MAC address in config
- `scan_timeout` is too short — try `scan_timeout = 15`

**`no candidate payload decrypted successfully`**

The Advertisement Key is wrong. Re-copy it from VictronConnect (Product
Info → Instant Readout → Show). The key is 32 hex characters, no spaces.

**Only Format B `rec=0x02` packets (no Format A)**

These are VE.Smart Networking beacons, not Instant Readout packets.
Enable Instant Readout in VictronConnect for this device.

**`passive scan unavailable — using active scanning`**

Passive scanning failed on this system (kernel/BlueZ version doesn't
support it, or wrong `or_patterns` format). Active scanning is the
automatic fallback and works identically for Victron devices. No action
required — data will still be received correctly.

**Values are wrong (impossible voltages, huge wattage)**

The Advertisement Key decrypts successfully but the parsed values are
physically impossible. Usually means `type=` is wrong or the key is for
a different device. Check `type=mppt|inverter|monitor|dcdc` matches the device.

### 12.4 Dashboard issues

**Dashboard not updating**

Check that the monitor is still running:
```bash
sudo systemctl status solar-monitor
# or: ps aux | grep solar_monitor
```

Check the state file modification time:
```bash
ls -la solar_state.json
```

**Dashboard shows data for BMS but not Victron (or vice versa)**

One worker may have crashed. Check the supervisor log:
```bash
journalctl -u solar-monitor --since "1 hour ago"
```

Look for `[WorkerName] Worker exited` and `Restarting in Ns`.

**`Dashboard write failed`**

Check disk space (`df -h`) and file permissions on the output path.

### 12.5 Running the test suite

All 330 tests run without BLE hardware:

```bash
cd /path/to/solar_monitor
python3 -m unittest discover -s tests -v
```

If a test fails after an update, check the error message — it usually
indicates a structural change that needs a corresponding config or code
update.

---

## 13. Reference

### 13.1 Timing constants

| Constant | Value | Location | Description |
|---|---|---|---|
| `bms_interval` (default) | 120 s | `config.ini` | BMS poll interval |
| `victron_interval` (default) | 30 s | `config.ini` | Victron poll interval |
| `scan_timeout` (default) | 10 s | `config.ini` | Victron BLE scan window |
| `_MIN_BMS_GAP` | 30 s | `bms_monitor.py` | Minimum BMS cycle gap (enforced) |
| `_MIN_VICTRON_GAP` | 10 s | `victron_monitor.py` | Minimum Victron cycle gap (enforced) |
| `PER_DEVICE_TIMEOUT` | 35 s | `jbd.py` | Max time for one BMS read operation |
| `READ_TIMEOUT` | 12 s | `jbd.py` | Max wait for BMS to respond after command |
| `NOTIFY_SETTLE_DELAY` | 1.0 s | `jbd.py` | Wait after GATT notify subscription |
| `BMS_RETRIES` | 3 | `scanner.py` | Attempts per BMS device per cycle |
| `RETRY_DELAY` | 4.0 s | `scanner.py` | Gap between retry attempts |
| `INTER_DEVICE_GAP` | 1.5 s | `scanner.py` | Gap between consecutive BMS connections |
| `MAX_CRASHES_PER_HOUR` | 10 | `solar_monitor.py` | Crashes before supervisor gives up on a worker |
| `MAX_BACKOFF` | 60 s | `solar_monitor.py` | Maximum restart delay after crash |

### 13.2 Log prefixes

| Prefix | Source |
|---|---|
| `[Victron]` | Victron worker |
| `[BMS]` | BMS worker |
| `[VE.Bus]` | Victron VE.Bus device reading |
| `[Solar]` | MPPT device reading |
| `supervisor` | Supervisor process |

### 13.3 File summary

| File | Purpose |
|---|---|
| `solar_monitor.py` | Supervisor — manages workers, writes dashboard |
| `bms_monitor.py` | BMS worker — direct GATT connect by MAC |
| `victron_monitor.py` | Victron worker — passive BLE scan |
| `jbd_bms_monitor.py` | Legacy combined launcher |
| `solar_monitor/scanner.py` | BLE scanning, VictronScanner, _poll_bms |
| `solar_monitor/jbd.py` | JBD protocol: GATT, packet parsing |
| `solar_monitor/victron.py` | Victron protocol: decryption, all parsers |
| `solar_monitor/dashboard.py` | HTML dashboard generation |
| `solar_monitor/state.py` | Atomic JSON state file I/O |
| `solar_monitor/config.py` | AppConfig, INI loading, CLI overrides |
| `solar_monitor/models.py` | DeviceReading dataclass |
| `tests/` | 330 unit tests — run without BLE hardware |

### 13.4 Config quick reference

```ini
[general]
output           = dashboard.html   # HTML output path
state_file       = solar_state.json # Worker IPC file
bms_interval     = 120              # BMS poll interval (s)
victron_interval = 30               # Victron poll interval (s)
scan_timeout     = 10               # BLE scan window (s)
max_history      = 600              # Chart points per device
log_level        = INFO             # DEBUG/INFO/WARNING/ERROR
theme            = business         # dark/light/business

[bms]
Label = MAC [ : password ]

[victron]
Label = MAC : 32-char-key  [ type=mppt|inverter|monitor|dcdc ]
```

### 13.5 Victron record types

| Type | Device | Parser |
|---|---|---|
| 0x01 | Solar Charger (MPPT) | `_parse_solar` |
| 0x02 | Battery Monitor (SmartShunt, BMV) | `_parse_bmv` |
| 0x03 | Inverter (Phoenix) | `_parse_inverter` |
| 0x06 | Inverter RS | `_parse_inverter_rs` |
| 0x07 | VE.Bus Smart Dongle (older firmware) | `_parse_vebus` |
| 0x08 | AC Charger / SmartShunt IP65 | `_parse_dcenergy` |
| 0x0B | Multi RS | `_parse_inverter_rs` |
| 0x0C | VE.Bus Smart Dongle (newer firmware) | `_parse_vebus` |
| 0x0D | DC Energy Meter | `_parse_dcenergy` |
| 0x0E | Orion XS DC-DC | `_parse_bmv` |
