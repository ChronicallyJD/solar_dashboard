# Solar Monitor: config.ini reference

Complete reference for every configuration key. All settings live in a single
`config.ini` file. Section order does not matter. Unknown keys are ignored.

---

## Quick-start example

```ini
[general]
output           = dashboard.html
state_file       = solar_state.json
bms_interval     = 120
victron_interval = 30
theme            = business

[bms]
House Bank = AA:BB:CC:DD:EE:FF : 123456

[victron]
South Array = 11:22:33:44:55:66 : aabbccddeeff00112233445566778899  type=mppt
MultiPlus   = C0:FF:EE:12:34:56 : 0123456789abcdef0123456789abcdef  type=inverter
```

---

## [general]

Core runtime settings. All keys are optional; the defaults shown are used
when a key is absent.

| Key | Default | Description |
|---|---|---|
| `output` | `dashboard.html` | Path for the generated HTML dashboard file |
| `state_file` | `solar_state.json` | Shared state file used for inter-process communication between workers |
| `bms_interval` | `120` | Seconds between BMS poll cycles (minimum enforced: 30) |
| `victron_interval` | `30` | Seconds between Victron BLE scan cycles (minimum enforced: 10) |
| `interval` | `30` | Legacy combined poll interval, only used by `jbd_bms_monitor.py` |
| `scan_timeout` | `10` | Seconds to listen for BLE advertisements per Victron cycle |
| `max_history` | `600` | Number of data points retained per device for dashboard charts |
| `log_level` | `INFO` | Log verbosity: `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `theme` | `dark` | Default dashboard theme: `dark` / `light` / `business` |

**Interval guidance:**

| Setting | Minimum | Recommended | Notes |
|---|---|---|---|
| `bms_interval` | 30 s | 120 s | GATT connections take 5–35 s per pack; add 40 s per pack for multi-pack systems |
| `victron_interval` | 10 s | 30 s | Passive BLE scan, very fast |
| `scan_timeout` | none | 10 s | Must be long enough for all Victron record types to rotate through |

---

## [bms]

One line per JBD / Vatrer BMS battery pack. The section may be omitted or left
empty to disable BMS polling entirely.

```ini
[bms]
Label = MAC_ADDRESS [ : password ]
```

- **Label**: display name shown on the dashboard card and in logs
- **MAC**: Bluetooth address in any standard format: `AA:BB:CC:DD:EE:FF`,
  `AA-BB-CC-DD-EE-FF`, or `AABBCCDDEEFF`
- **password**: optional 6-digit numeric BMS password; omit the colon
  entirely if no password is set. Common defaults: `123456`, `000000`, `888888`

```ini
[bms]
House Bank  = A1:B2:C3:D4:E5:F6 : 123456
Spare Pack  = A1:B2:C3:D4:E5:F7          # no password
```

**Finding the MAC address:**

```bash
bluetoothctl scan on
# BMS packs appear as: BT-TH-XXXXXXXX, JBD-BMS, or similar
```

Or use nRF Connect (Android) / LightBlue (iOS).

---

## [victron]

One line per Victron BLE device. Also accepted as `[mppt]` for legacy
compatibility. The section may be omitted to disable Victron polling.

```ini
[victron]
Label = MAC : KEY  [ type=mppt|inverter|monitor|dcdc ]
```

- **Label**: display name
- **MAC**: Bluetooth address (`AA:BB:CC:DD:EE:FF`)
- **KEY**: 32-character Advertisement Key from VictronConnect (see below)
- **type**: optional; controls dashboard card layout and accepted record
  types. Inferred from the first successful parse if omitted.

### Getting MAC and KEY from VictronConnect

1. Open VictronConnect and connect to the device
2. Tap the **⚙️ gear icon** → **Product Info**
3. Scroll to **Instant Readout via Bluetooth** → enable it if off
4. Tap **Show** to reveal the Advertisement Key
5. Copy the 32-character hex key; note the MAC shown above it

> **iOS note:** iOS shows a UUID, not a MAC. Use nRF Connect on Android or
> `bluetoothctl scan on` on Linux to find the real Bluetooth MAC address.

### `type=` values

| `type=` | Dashboard card | Accepted record types |
|---|---|---|
| `mppt` | MPPT Solar Charger | `0x01` SmartSolar |
| `inverter` | Inverter / VE.Bus | `0x07` VE.Bus dongle (older firmware) |
| | | `0x0C` VE.Bus dongle (newer firmware) |
| | | `0x03` Phoenix Inverter Smart |
| | | `0x06` Inverter RS |
| | | `0x0B` Multi RS |
| `monitor` | Battery Monitor | `0x02` SmartShunt / BMV-712 |
| | | `0x05` SmartLithium |
| `dcdc` | DC-DC Converter | `0x04` DC-DC Converter |
| | | `0x0E` Orion XS |

If `type=` is omitted all parsers are tried; the first successful parse wins.

```ini
[victron]
South Array  = 11:22:33:44:55:01 : aabbccddeeff00112233445566778899  type=mppt
West Array   = 11:22:33:44:55:02 : 00112233445566778899aabbccddeeff  type=mppt
MultiPlus    = C0:FF:EE:12:34:56 : 0123456789abcdef0123456789abcdef  type=inverter
SmartShunt   = 11:22:33:44:55:03 : ffeeddccbbaa99887766554433221100  type=monitor
```

### VE.Bus Smart Dongle setup

The dongle plugs into the VE.Bus port of a MultiPlus-II (or similar) and
appears as a **separate device** in VictronConnect, usually named after the
inverter system. Add it as `type=inverter`.

Data provided (record `0x0C`): battery V/A/W/temperature/SoC, AC input
source and real watts, AC output real watts, device state, VE.Bus error code,
alarm level.

**Firmware note:** older dongle firmware broadcasts `0x07` (limited data);
newer firmware broadcasts `0x0C` (full data). Both are handled automatically
with `type=inverter`. To update: VictronConnect → device → gear → Firmware.

---

## [server]

Built-in HTTPS server. Disabled by default. When enabled, runs as an
`asyncio` task inside the supervisor process.

| Key | Default | Description |
|---|---|---|
| `enabled` | `false` | Start the HTTPS server |
| `host` | `0.0.0.0` | Bind address. `0.0.0.0` listens on all interfaces |
| `port` | `4443` | TCP port. Ports < 1024 require root or `CAP_NET_BIND_SERVICE` |
| `cert_file` | `server.crt` | TLS certificate (PEM format) |
| `key_file` | `server.key` | TLS private key (PEM format) |
| `auto_cert` | `true` | Auto-generate a self-signed cert when `cert_file` is missing |

All boolean keys (in any section) accept `true` / `false` / `yes` / `no` /
`1` / `0` / `on` / `off`, case-insensitive; surrounding whitespace is stripped.

**Routes served:**

| Route | Response |
|---|---|
| `GET /` | Dashboard HTML |
| `GET /dashboard.html` | Dashboard HTML (same as `/`) |
| `GET /state.json` | Raw state JSON (all device readings) |
| `GET /health` | `200 OK` plaintext |
| Everything else | `404 Not Found` |

Only `GET` requests are accepted. TLS 1.2 minimum enforced. Private key
written with `chmod 600` when auto-generated.

**Auto-generated certificate spec:**
RSA-2048 · SHA-256 · 10-year validity · SAN entries: `localhost`,
`127.0.0.1`, and the configured `host` if it is a non-wildcard IP or hostname.

```ini
[server]
enabled   = true
host      = 0.0.0.0
port      = 4443
cert_file = server.crt
key_file  = server.key
auto_cert = true
```

To use a real certificate (e.g. from Let's Encrypt):

```ini
[server]
enabled   = true
port      = 443
cert_file = /etc/letsencrypt/live/solar.example.com/fullchain.pem
key_file  = /etc/letsencrypt/live/solar.example.com/privkey.pem
auto_cert = false
```

---

## [history]

SQLite persistent history storage. Disabled by default. When enabled, workers
write every successful reading to the database after each poll cycle.

| Key | Default | Description |
|---|---|---|
| `enabled` | `false` | Enable SQLite history storage |
| `db_path` | `solar_history.db` | Database file path. Parent directories created automatically |
| `retention_days` | `1095` | Days of history to keep. `0` = keep forever (no automatic deletion) |
| `vacuum_interval_days` | `7` | Run `VACUUM` every N days to compact the database file |

**Retention policy** is enforced automatically, at most once per hour per
worker process. Rows older than `retention_days` are deleted during normal
write cycles; no cron job is needed.

**Disk space:** roughly 3–5 MB/month with 4 devices at default poll rates.
A 3-year store with 4 devices typically fits under 200 MB.

```ini
[history]
enabled              = true
db_path              = solar_history.db
retention_days       = 1095
vacuum_interval_days = 7
```

**Management utilities** (in `utils/`):

```bash
# Show database statistics
python utils/purge_history.py --config config.ini --stats

# Dry-run: see what would be deleted before 2023
python utils/purge_history.py --config config.ini --before 2023-01-01 --dry-run

# Apply configured retention policy now
python utils/purge_history.py --config config.ini --enforce-retention

# Export a device's history as CSV
python utils/query_history.py --config config.ini \
    --device "House Bank" --format csv > house_bank.csv
```

---

## [mcp]

MCP (Model Context Protocol) server for AI assistant integration. Runs over
stdio: Claude Desktop launches it as a subprocess; no port or TLS needed.
Enabled by default when the section is present; all security features are
off by default.

| Key | Default | Description |
|---|---|---|
| `enabled` | `true` | Enable the MCP server (set `false` to prevent startup) |
| `api_key` | `""` | Shared secret required in every `tools/call`. Empty = no authentication |
| `allowed_tools` | `""` | Comma-separated tool whitelist. Empty = all 8 tools exposed |
| `rate_limit` | `60` | Maximum `tools/call` requests per minute. `0` = unlimited |
| `require_local` | `true` | Documents intent (stdio is always local; no network enforcement) |
| `log_requests` | `false` | Log every tool call name to stderr for auditing |

**`read_only` is always `true`**: the MCP server never writes to any file,
regardless of configuration.

**Available tools:** `get_system_status`, `get_battery_status`,
`get_solar_status`, `get_inverter_status`, `get_device`, `list_devices`,
`get_alerts`, `get_data_age`.

**Claude Desktop configuration** (`~/.config/claude/claude_desktop_config.json`):

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

```ini
[mcp]
enabled       = true
api_key       = your-secret-key-here
allowed_tools =
rate_limit    = 60
require_local = true
log_requests  = false
```

To restrict an assistant to read-only summary views only:

```ini
[mcp]
allowed_tools = get_system_status, get_alerts, list_devices
```

---

## Complete annotated example

```ini
# ─── solar_monitor/config.ini ────────────────────────────────────────────────

[general]

# Path where the HTML dashboard file is written.
# Use an absolute path when running as a systemd service.
output = /home/pi/solar_monitor/dashboard.html

# Shared state file: workers communicate through this JSON file.
# Each worker owns one section; writes are atomic (temp file → rename).
state_file = solar_state.json

# BMS poll interval. GATT connections are slow (5–35 s per pack).
# For 4 packs: 4 × 40 s ≈ 160 s minimum; 120 s is fine for 1–2 packs.
bms_interval = 120

# Victron poll interval. Passive BLE scan, fast.
victron_interval = 30

# How long to listen for Victron BLE advertisements each cycle.
# Increase to 15 s if devices are reported as "not seen during scan".
scan_timeout = 10

# Chart data points retained in memory per device.
# At 30 s interval: 600 points ≈ 5 hours of history.
max_history = 600

# Log verbosity. Use DEBUG to see all BLE traffic and decrypted packets.
log_level = INFO

# Default dashboard theme (user choice is stored in localStorage).
theme = business


# ─── JBD / Vatrer BMS packs ──────────────────────────────────────────────────
# Format: Label = MAC [ : password ]

[bms]
House Bank = A1:B2:C3:D4:E5:F6 : 123456
# Spare    = A1:B2:C3:D4:E5:F7


# ─── Victron BLE devices ─────────────────────────────────────────────────────
# Format: Label = MAC : KEY  [ type=mppt|inverter|monitor|dcdc ]
# KEY = 32-character Advertisement Key from VictronConnect.

[victron]
South Array  = 11:22:33:44:55:01 : aabbccddeeff00112233445566778899  type=mppt
West Array   = 11:22:33:44:55:02 : 00112233445566778899aabbccddeeff  type=mppt

# VE.Bus Smart Dongle attached to MultiPlus-II 48/5000/70-95 120V.
MultiPlus    = C0:FF:EE:12:34:56 : 0123456789abcdef0123456789abcdef  type=inverter


# ─── HTTPS server ─────────────────────────────────────────────────────────────
# Serves the dashboard and /state.json over TLS. Disabled by default.
# Auto-generates a self-signed cert on first run when auto_cert = true.

[server]
enabled   = true
host      = 0.0.0.0
port      = 4443
cert_file = server.crt
key_file  = server.key
auto_cert = true


# ─── SQLite history ───────────────────────────────────────────────────────────
# Persists every reading to a local database for long-term trend analysis.
# Default retention: 3 years (1095 days). Set retention_days = 0 to keep all.

[history]
enabled              = true
db_path              = solar_history.db
retention_days       = 1095
vacuum_interval_days = 7


# ─── MCP server ───────────────────────────────────────────────────────────────
# Exposes solar data to AI assistants (Claude Desktop, Cursor, etc.)
# via the Model Context Protocol over stdio. No port or TLS required.

[mcp]
enabled       = true
api_key       =
allowed_tools =
rate_limit    = 60
require_local = true
log_requests  = false
```

---

## Inline comments

Both `#` and `;` are supported as inline comment characters:

```ini
bms_interval = 120    # seconds
retention_days = 1095 ; 3 years
```

---

## File location

The config file defaults to `config.ini` in the current working directory.
Override with `--config` on any command:

```bash
python solar_monitor.py  --config /etc/solar/config.ini
python bms_monitor.py    --config /etc/solar/config.ini
python victron_monitor.py --config /etc/solar/config.ini
python mcp_server.py     --config /etc/solar/config.ini
python utils/purge_history.py --config /etc/solar/config.ini
python utils/query_history.py --config /etc/solar/config.ini
```
