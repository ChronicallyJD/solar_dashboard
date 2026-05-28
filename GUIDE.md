# Solar Monitor — Installation and Operations Guide

A Bluetooth dashboard for JBD/Vatrer BMS battery packs and Victron
Energy devices (SmartSolar MPPT, MultiPlus-II via VE.Bus Smart Dongle).
No cloud, no app, no internet connection required.

---

## Table of Contents

1. [Requirements](#1-requirements)
2. [Installation](#2-installation)
3. [Configuration](#3-configuration)
4. [Running the Monitor](#4-running-the-monitor)
5. [Split-Process Mode (recommended)](#5-split-process-mode-recommended)
6. [Running as a Service](#6-running-as-a-service)
7. [Dashboard](#7-dashboard)
8. [Victron Device Setup](#8-victron-device-setup)
9. [BMS Device Setup](#9-bms-device-setup)
10. [Troubleshooting](#10-troubleshooting)
11. [Configuration Reference](#11-configuration-reference)

---

## 1. Requirements

**Hardware**
- Linux host with Bluetooth (Raspberry Pi 4/5, any x86 Linux box with BT adapter)
- BlueZ 5.50 or later (`bluetoothctl --version`)
- Devices within BLE range (~10 m line-of-sight; walls reduce this significantly)

**Software**
- Python 3.11 or later
- `bleak` BLE library
- `cryptography` library (Victron AES-128-CTR decryption)

**Supported BMS**
- JBD / Vatrer BMS packs (any cell count, any voltage)
- Protocol: GATT (active connection, register 0x03 + 0x04)

**Supported Victron devices**
- SmartSolar MPPT (any model with Instant Readout)
- MultiPlus-II via VE.Bus Smart Dongle
- Phoenix Inverter Smart
- SmartShunt / BMV-712 Battery Monitor
- DC-DC Converter (Orion XS)

---

## 2. Installation

```bash
# Extract the archive
tar -xzf solar_monitor.tar.gz
cd solar_monitor

# Install Python dependencies
pip install bleak cryptography

# Copy and edit the example config
cp config.ini.example config.ini
nano config.ini

# Verify BLE is working
bluetoothctl scan on   # should show nearby BLE devices after a few seconds
```

**Permissions** — BlueZ requires either root or membership in the `bluetooth`
group to open BLE sockets:

```bash
sudo usermod -aG bluetooth $USER
# Log out and back in, then verify:
python3 -c "import bleak; print(bleak.__version__)"
```

---

## 3. Configuration

All settings live in `config.ini`. A fully-annotated example is in
`config.ini.example`; copy it and edit as needed.

### Minimal working example

```ini
[general]
output           = /var/www/html/solar.html
bms_interval     = 120
victron_interval = 30
theme            = business

[bms]
House Bank = A1:B2:C3:D4:E5:F6 : 123456

[victron]
South Array = 11:22:33:44:55:01 : aabbccddeeff00112233445566778899  type=mppt
West Array  = 11:22:33:44:55:02 : 00112233445566778899aabbccddeeff  type=mppt
MultiPlus   = C0:FF:EE:12:34:56 : 0123456789abcdef0123456789abcdef  type=inverter
```

### `[general]` keys

| Key | Default | Description |
|---|---|---|
| `output` | `dashboard.html` | Path where the HTML dashboard is written |
| `state_file` | `solar_state.json` | Shared state file (split-process mode) |
| `bms_interval` | `120` | Seconds between BMS polls (split mode) |
| `victron_interval` | `30` | Seconds between Victron polls (split mode) |
| `interval` | `30` | Combined mode poll interval |
| `scan_timeout` | `10` | BLE scan duration per cycle (seconds) |
| `max_history` | `600` | Chart data-points kept per device |
| `log_level` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `theme` | `dark` | `dark` / `light` / `business` |

### `[bms]` section

One line per JBD/Vatrer pack:

```ini
[bms]
Label = MAC_ADDRESS [ : password ]
```

- **Label** — shown as the card title on the dashboard
- **MAC** — colon-separated Bluetooth MAC (`A1:B2:C3:D4:E5:F6`)
- **password** — optional 6-digit BMS password; omit if not set (default factory password is usually `123456`)

```ini
[bms]
House Bank  = A1:B2:C3:D4:E5:F6 : 123456
Spare Pack  = A1:B2:C3:D4:E5:F7
```

### `[victron]` section

One line per Victron device:

```ini
[victron]
Label = MAC : KEY  [ type=mppt|inverter|monitor|dcdc ]
```

- **Label** — card title on the dashboard
- **MAC** — Bluetooth MAC address
- **KEY** — 32-character advertisement key (see [Victron Device Setup](#8-victron-device-setup))
- **type** — controls dashboard card and accepted record types (see below)

| `type=` | Dashboard card | Devices |
|---|---|---|
| `mppt` | Solar Charger | SmartSolar MPPT |
| `inverter` | Inverter / VE.Bus | MultiPlus-II (via dongle), Phoenix Inverter |
| `monitor` | Battery Monitor | SmartShunt, BMV-712 |
| `dcdc` | DC-DC | Orion XS |

---

## 4. Running the Monitor

### Combined mode (single process)

The simplest way to run — one process polls everything:

```bash
python jbd_bms_monitor.py --config config.ini
```

This works well for a single BMS pack. With multiple BMS packs the
total cycle time grows quickly (each pack can take up to 35 s to read),
which delays Victron data updates. Use split mode instead.

### Command-line options

All three launchers accept the same flags:

| Flag | Description |
|---|---|
| `--config FILE` | Config file path (default: `config.ini`) |
| `--interval SECS` | Override poll interval from INI |
| `--output FILE` | Override dashboard output path |
| `--scan-timeout SECS` | Override BLE scan duration |
| `--once` | Poll once and exit (useful for testing / cron) |
| `--log-level LEVEL` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `--theme THEME` | `dark` / `light` / `business` |

---

## 5. Split-Process Mode (recommended)

**Run two processes simultaneously** — one for BMS, one for Victron.
They communicate through a shared JSON state file (`solar_state.json`).
Either process renders the full dashboard after each of its own polls.

```
bms_monitor.py          victron_monitor.py
      │                        │
      │  polls BMS every 120s  │  polls Victron every 30s
      │                        │
      └──── solar_state.json ──┘
                  │
           dashboard.html  (written by whichever process polled last)
```

### Why split?

| Problem (combined mode) | Solution (split mode) |
|---|---|
| BMS GATT connection takes 5–35 s per pack | BMS runs independently; Victron doesn't wait |
| Hung BMS blocks Victron refresh | Each process has its own event loop |
| Battery SoC changes slowly — no need to poll often | BMS on 2-min interval, Victron on 30-s interval |
| BMS failure crashes the whole monitor | Processes restart independently |

### Starting split mode

```bash
# Terminal 1 or screen/tmux pane:
python bms_monitor.py --config config.ini

# Terminal 2 or separate pane:
python victron_monitor.py --config config.ini
```

Both processes log independently. The dashboard updates every time
*either* process completes a cycle, so Victron data refreshes every
30 s even if the BMS is mid-connection.

### Interval recommendations

```ini
[general]
bms_interval     = 120   # 2 minutes — battery SoC changes slowly
victron_interval = 30    # 30 seconds — real-time power monitoring
scan_timeout     = 10    # BLE scan window per cycle
```

For very slow or unreliable BMS packs, raise `bms_interval` to 300
(5 minutes). The BMS process enforces a minimum gap of 30 s regardless
of your setting, to give BlueZ time to fully release GATT connections.

### State file

The shared state file is a JSON document written atomically by each
process (write to `.tmp`, then `os.replace`):

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

Each process updates only its own section and never touches the
other's. If the file is missing or corrupt on startup, the process
starts with an empty section and populates it on the first successful
poll.

---

## 6. Running as a Service

### systemd (recommended for Raspberry Pi / server)

Create two service files so each process starts on boot, restarts on
failure, and logs to journald.

**`/etc/systemd/system/solar-victron.service`**

```ini
[Unit]
Description=Solar Monitor — Victron (MPPT + Inverter)
After=network.target bluetooth.target
Wants=bluetooth.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/solar_monitor
ExecStart=/usr/bin/python3 victron_monitor.py --config config.ini
Restart=on-failure
RestartSec=15
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

**`/etc/systemd/system/solar-bms.service`**

```ini
[Unit]
Description=Solar Monitor — BMS (JBD/Vatrer)
After=network.target bluetooth.target
Wants=bluetooth.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/solar_monitor
ExecStart=/usr/bin/python3 bms_monitor.py --config config.ini
Restart=on-failure
RestartSec=30
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable solar-victron solar-bms
sudo systemctl start  solar-victron solar-bms

# Check status
sudo systemctl status solar-victron
sudo systemctl status solar-bms

# View logs
journalctl -u solar-victron -f
journalctl -u solar-bms -f
```

### Serving the dashboard

The dashboard is a single self-contained HTML file. Any web server can
serve it. The simplest option on a Pi:

```bash
# Python built-in server (development / local network only)
cd /home/pi/solar_monitor
python3 -m http.server 8080
# then browse to http://raspberrypi.local:8080/dashboard.html
```

For a permanent setup, set `output` in `config.ini` to a path inside
your web root:

```ini
[general]
output = /var/www/html/solar.html
```

Then browse to `http://your-pi-ip/solar.html`.

### cron (alternative — one-shot polling)

If you prefer cron over a long-running daemon:

```cron
# Poll Victron every minute
* * * * * pi cd /home/pi/solar_monitor && python3 victron_monitor.py --config config.ini --once >> /var/log/solar.log 2>&1

# Poll BMS every 5 minutes
*/5 * * * * pi cd /home/pi/solar_monitor && python3 bms_monitor.py --config config.ini --once >> /var/log/solar.log 2>&1
```

---

## 7. Dashboard

The dashboard is a single HTML file that opens in any browser. It
updates in-place — refresh the page to see new readings.

### Themes

Three themes are available: `dark`, `light`, and `business`. Switch
with the button in the top-right corner; the preference is remembered
across page loads.

Set the default in `config.ini`:

```ini
[general]
theme = business
```

### BMS card

Displays per battery pack:
- **Volts / Amps / Watts** — pack voltage, signed current (−=discharge, +=charge), DC power
- **SoC bar** — colour-coded (green → yellow → red as charge drops)
- **Remaining Wh** — energy remaining at current voltage
- **Ah remaining / total** — e.g. `84.0 / 100.0 Ah`
- **TTE** — time to empty at current discharge rate (e.g. `TTE 5h36m`)
- **TTF** — time to full at current charge rate (e.g. `TTF 1h12m`)
- **Cell count · Cycle count · FET status** — `16 cells · 8 cycles · CHG ✓ DSG ✓`
- **Temperatures** — all NTC sensor readings
- **Faults** — active protection faults in red (e.g. `⚠ Cell overvoltage`)
- **Balancing** — cells currently being balanced (e.g. `⚡ Balancing cells: 4, 7`)

### Victron inverter card (VE.Bus Smart Dongle)

Matches the layout shown in VictronConnect:

**AC Output L1**
- Voltage (V): `120` (hardcoded — not transmitted in BLE record)
- Power (W): real AC output watts from dongle
- Current (A): computed as Power ÷ 120V

**Battery**
- Voltage (V): DC bus voltage
- Current (A): raw signed value (−=discharging, +=charging)
- Temperature: dongle onboard sensor

**Status row**: State · AC In source · Alarm

### MPPT solar charger card

- PV Power, Battery Voltage, Battery Current
- Yield Today (Wh)
- Charger state: Bulk / Absorption / Float / Off
- Load current (models with a load terminal)

### System totals banner

Spans the full width:
- **Battery Power** — sum of BMS pack power only (no double-counting from inverter)
- **PV Power** — sum of MPPT solar input
- **Yield Today** — total energy harvested across all MPPTs

---

## 8. Victron Device Setup

### Finding the advertisement key

Every Victron device has a unique 32-character advertisement key that
must be added to `config.ini` before the monitor can decrypt its data.

1. Open **VictronConnect** on your phone or tablet
2. Connect to the device
3. Tap the **⚙️ gear icon** → **Product Info**
4. Scroll to **Instant Readout via Bluetooth**
5. If the toggle is **off**, enable it
6. Tap **Show**
7. Copy the **Advertisement Key** (32 hex characters)
8. Note the **MAC address** shown above it

> **iOS note**: iOS shows a UUID instead of a MAC. Use an Android device
> or a Linux BLE scanner (`bluetoothctl scan on`) to find the real MAC.

### Adding to config.ini

```ini
[victron]
South Array = 11:22:33:44:55:01 : aabbccddeeff00112233445566778899  type=mppt
MultiPlus   = C0:FF:EE:12:34:56 : 0123456789abcdef0123456789abcdef  type=inverter
```

### VE.Bus Smart Dongle (MultiPlus-II)

The dongle appears as a **separate device** from the inverter's built-in
Bluetooth. In VictronConnect it is listed under its own entry — usually
named after the inverter system (e.g. `48V-2400W`).

The dongle broadcasts record type `0x07` with the full VE.Bus data set:
battery voltage, current, temperature, SoC (when available), AC input
source and power, AC output real watts, device state, and alarm status.

If the dongle shows `ac=N (raw)` instead of a watt value, the dongle
firmware may be an older version. Update via VictronConnect → gear → 
Product Info → Firmware → Check for updates.

---

## 9. BMS Device Setup

### Finding the MAC address

```bash
# Start a BLE scan
bluetoothctl
[bluetooth]# scan on
# Wait 10-20 seconds — BMS devices typically advertise as "BT-TH-XXXXXXXX" or similar
[bluetooth]# scan off
[bluetooth]# exit
```

Alternatively, scan from nRF Connect (Android/iOS) or LightBlue (iOS)
which show device names alongside MAC addresses.

### Password

JBD BMS packs may have a 6-digit numeric password. The factory default
is usually `123456`. If no password is set, omit the colon entirely:

```ini
[bms]
House Bank = A1:B2:C3:D4:E5:F6           # no password
Spare Pack = A1:B2:C3:D4:E5:F7 : 123456  # with password
```

If you see `BMS rejected password` in the log, try common defaults:
`000000`, `123456`, `888888`. Check your BMS manufacturer's documentation
if none work.

### BLE range and reliability

BMS packs are often tucked into battery enclosures that attenuate the
BLE signal. If you see frequent timeouts:

- Move the Pi closer to the battery bank, or add a Bluetooth antenna extension
- Increase `bms_interval` to reduce how often connections are attempted
- Check that no thick metal enclosure is between the Pi and the BMS

The BMS monitor retries each device up to 2 times on transient failures
(timeout, disconnection) before marking it as offline for that cycle.
Permanent failures (wrong password, unsupported GATT service) are not
retried.

---

## 10. Troubleshooting

### Nothing appears on the dashboard

Run with `--once --log-level DEBUG` and check the output:

```bash
python victron_monitor.py --config config.ini --once --log-level DEBUG
python bms_monitor.py    --config config.ini --once --log-level DEBUG
```

Common causes:
- Device not in BLE range — move closer
- Wrong MAC address in `config.ini`
- Wrong or missing advertisement key (Victron)
- Instant Readout not enabled in VictronConnect
- BlueZ not running (`sudo systemctl start bluetooth`)

### Victron: `no candidate payload decrypted successfully`

The advertisement key is wrong. Re-copy it from VictronConnect (Product
Info → Instant Readout → Show). Keys are 32 hex characters with no
spaces, colons, or other separators.

### Victron: candidate lines show only Format B `rec=0x02`

These are VE.Smart Networking beacons, not Instant Readout packets. The
device's Instant Readout is not enabled. In VictronConnect: gear → Product
Info → Instant Readout via Bluetooth → enable the toggle.

### BMS: `TIMEOUT (35s)` on every cycle

- BMS is out of BLE range
- Another device (phone app) has an open GATT connection — close it
- BMS firmware bug: try power-cycling the BMS
- BlueZ interference: `sudo hciconfig hci0 reset` then restart the monitor

### BMS: `BMS rejected password`

The password in `config.ini` is wrong. Try `000000`, `123456`, `888888`.

### BMS: `BMS checksum mismatch`

The packet was received but data integrity check failed. Usually transient
(RF interference, partial packet). The monitor warns and continues with
the data rather than discarding it.

### Dashboard not updating

Check that the process is still running and has write permission to the
output file:

```bash
ls -la dashboard.html        # check modification time
ps aux | grep monitor.py     # check process is alive
```

If running in split mode, check both processes:

```bash
sudo systemctl status solar-victron
sudo systemctl status solar-bms
```

### `Operation already in progress` at startup

BlueZ is still holding a BLE scan from the previous run. Wait 10-15 s
then restart. The BMS monitor enforces a 30 s minimum between cycles to
prevent this; if it occurs at startup it usually resolves on the second
attempt.

---

## 11. Configuration Reference

### Complete annotated config.ini

```ini
# ─── solar_monitor/config.ini ────────────────────────────────────────────────

[general]

# Path where the HTML dashboard is written.
output = /var/www/html/solar.html

# Shared state file used when running bms_monitor.py and victron_monitor.py
# as separate processes. Both processes read and write to this file.
# Default: solar_state.json (in the working directory)
state_file = solar_state.json

# ── Poll intervals ────────────────────────────────────────────────────────────

# BMS poll interval in seconds (bms_monitor.py).
# BMS GATT connections are slow; 120s is a good balance between freshness
# and reliability. Minimum enforced by the process: 30s.
bms_interval = 120

# Victron poll interval in seconds (victron_monitor.py).
# BLE advertisement scanning is fast and passive. 30s gives real-time
# power monitoring. Minimum enforced by the process: 10s.
victron_interval = 30

# Legacy combined-mode interval (jbd_bms_monitor.py).
# Used when running a single process for everything.
interval = 30

# ── Scanner settings ──────────────────────────────────────────────────────────

# How long to scan for BLE devices each cycle (seconds).
# Must be long enough to receive multiple advertisement packets from each
# Victron device (each device cycles through record types). 10s is reliable
# for 1-5 devices; increase to 15s if devices are frequently missed.
scan_timeout = 10

# How many history data-points to retain per device for the dashboard charts.
# At 30s interval, 600 points = 5 hours of history.
max_history = 600

# ── Display ───────────────────────────────────────────────────────────────────

# Console log verbosity: DEBUG, INFO, WARNING, ERROR
log_level = INFO

# Dashboard colour theme: dark, light, business
theme = business

# ─── JBD / Vatrer BMS packs ──────────────────────────────────────────────────

[bms]
# Format: Label = MAC [ : password ]
# Leave this section empty or absent to skip BMS polling.

House Bank = A1:B2:C3:D4:E5:F6 : 123456
# Spare Pack = A1:B2:C3:D4:E5:F7

# ─── Victron BLE devices ─────────────────────────────────────────────────────

[victron]
# Format: Label = MAC : KEY  [ type=mppt|inverter|monitor|dcdc ]
# Advertisement Key from VictronConnect → gear → Product Info → Instant Readout.
# Leave this section empty or absent to skip Victron polling.

# SmartSolar MPPTs:
South Array = 11:22:33:44:55:01 : aabbccddeeff00112233445566778899  type=mppt
West Array  = 11:22:33:44:55:02 : 00112233445566778899aabbccddeeff  type=mppt

# VE.Bus Smart Dongle (MultiPlus-II 48/5000/70-95 120V):
# Provides battery V/A/temp, SoC, AC-in source/power, AC-out real watts.
MultiPlus = C0:FF:EE:12:34:56 : 0123456789abcdef0123456789abcdef  type=inverter
```

### Timing reference

| Constant | Value | Description |
|---|---|---|
| `PER_DEVICE_TIMEOUT` | 35 s | Max time allowed for one full BMS read cycle |
| `READ_TIMEOUT` | 12 s | Max wait for BMS to respond after command sent |
| `NOTIFY_SETTLE_DELAY` | 1.0 s | Wait after GATT notify subscription before sending command |
| `BMS_RETRIES` | 3 | Total attempts per BMS device per poll cycle |
| `INTER_DEVICE_GAP` | 1.5 s | Pause between polling consecutive BMS devices |
| Min BMS cycle gap | 30 s | Minimum time between BMS poll cycles |
| Min Victron cycle gap | 10 s | Minimum time between Victron poll cycles |

### Running the test suite

```bash
cd solar_monitor
python3 -m unittest discover -s tests -v
```

203 tests covering BMS protocol parsing, Victron BLE decryption, state
file atomicity, split-process isolation, config loading, and dashboard
rendering. All tests run without BLE hardware.
