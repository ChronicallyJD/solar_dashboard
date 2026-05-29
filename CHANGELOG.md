# Solar Monitor — Changelog

All notable changes to this project, in reverse-chronological order.

---

## [Current] — SQLite history, MCP server, HTTPS server, mobile dashboard, supervisor

### SQLite persistent history (`solar_monitor/history.py`)
- New `HistoryDB` class — stores every successful `DeviceReading` to a local
  SQLite database after each poll cycle
- Schema: single `readings` table with 43 columns covering all `DeviceReading`
  fields; list fields (`temp_c`, `faults`, `balance_cells`) stored as JSON strings
- Four indexes: `recorded_at`, `device_name`, `device_type`, composite
  `(device_name, recorded_at)` — fast date-range and device queries
- WAL journal mode + `PRAGMA synchronous=NORMAL` — concurrent writes from
  multiple worker processes never block each other
- New `HistoryConfig` dataclass — six fields: `enabled`, `db_path`,
  `retention_days` (default 1095 = 3 years), `vacuum_interval_days` (default 7)
- `retention_days = 0` keeps data forever; automatic retention runs at most
  once per hour per worker, tracked in `_meta` table
- `vacuum()` compacts the database file; runs automatically per
  `vacuum_interval_days`
- `load_recent_for_dashboard(max_points)` — returns `{name: [entry, ...]}` dict
  that pre-populates chart history on worker startup from persistent storage
- Error readings (`r.error` set) are never stored
- `write_readings()` returns count of rows inserted; never raises — a failed
  write cannot crash a worker

### New `[history]` config section
```ini
[history]
enabled              = true
db_path              = solar_history.db
retention_days       = 1095        # 0 = keep forever
vacuum_interval_days = 7
```

### Management utilities (`utils/`)
New `utils/` directory for server-side management scripts.

**`utils/purge_history.py`** — delete historical data:
- `--before DATE` / `--after DATE` — date range filters
- `--device NAME` / `--type TYPE` — scope deletion to one device or type
- `--enforce-retention` — apply the configured policy immediately
- `--vacuum` — compact the database after deletion
- `--dry-run` — show row count without deleting
- `--yes` / `-y` — skip confirmation for scripted use
- `--stats` / `--list-devices` — inspection modes
- Always requires at least one filter; prompts for confirmation before deletion

**`utils/query_history.py`** — query and export:
- `--format table|csv|json` — output formats; CSV pipes cleanly to files
- `--start DATE` / `--end DATE` — accepts `today` and `yesterday` shortcuts
- `--device NAME` / `--type TYPE` — device filters
- `--fields col1,col2,...` — column projection for smaller exports
- `--limit N` / `--order asc|desc` — result controls
- `--list-devices` / `--list-fields` / `--stats` — inspection modes

---

### MCP server (`mcp_server.py`)
- Implements Model Context Protocol 1.0 over stdio transport (JSON-RPC 2.0)
- No third-party MCP SDK — standard library only, zero extra dependencies
- Eight read-only tools:
  - `get_system_status` — total PV W, AC out W, avg SoC, pack count, alerts
  - `get_battery_status` — all BMS packs: SoC, V, A, Wh, TTE/TTF, faults, balance
  - `get_solar_status` — all MPPT chargers: PV W, yield Wh, charger state
  - `get_inverter_status` — all inverters: AC out W, state, alarms, battery V/A
  - `get_device` — single device by name or MAC, case-insensitive
  - `list_devices` — all devices with type, online status, key metric
  - `get_alerts` — active faults/alarms/offline devices; `all_clear: true` when healthy
  - `get_data_age` — human-readable data freshness per section
- `read_only = True` hardcoded — never writes to any file, not configurable
- Security via new `[mcp]` config section:
  - `api_key` — bearer token required in every `tools/call`; stripped from args
    before reaching tool functions
  - `allowed_tools` — comma-separated whitelist; filters `tools/list` too
  - `rate_limit` — sliding 60-second window (token bucket); `0` = unlimited
  - `require_local = true` — documents intent (stdio is always local)
  - `log_requests` — log every tool call to stderr
- JSON-RPC error codes: `-32001` Unauthorized, `-32002` Forbidden,
  `-32000` Rate Limited
- All logging to stderr; stdout reserved for JSON-RPC protocol

### New `[mcp]` config section
```ini
[mcp]
enabled       = true
api_key       =
allowed_tools =
rate_limit    = 60
require_local = true
log_requests  = false
```

---

### HTTPS dashboard server (`solar_monitor/server.py`)
- Async HTTPS server running as an `asyncio.Task` inside the supervisor —
  no separate process
- Routes: `GET /` and `/dashboard.html` → HTML, `GET /state.json` → raw JSON,
  `GET /health` → `200 OK` plaintext; all other paths and all non-GET methods
  → 404
- Auto-generates a self-signed RSA-2048 / SHA-256 certificate on first run:
  10-year validity, SAN entries for localhost + 127.0.0.1 + configured host,
  key written `chmod 600`
- `ssl.TLSVersion.TLSv1_2` minimum enforced
- 10-second read timeout per request; always closes connection after response
- `ServerConfig` dataclass; all settings in `[server]` config section

### New `[server]` config section
```ini
[server]
enabled   = false
host      = 0.0.0.0
port      = 4443
cert_file = server.crt
key_file  = server.key
auto_cert = true
```

---

### Supervisor architecture (`solar_monitor.py`)
- New `solar_monitor.py` replaces the need to run two terminal sessions
- `WorkerSpec` dataclass describes a worker: name, script, state section,
  config sections (for auto-enable), interval key, min gap
- `WORKER_REGISTRY` list — add one `WorkerSpec` to register a new data source
- `WorkerProcess` manages one subprocess: launches with
  `asyncio.create_subprocess_exec`, streams stdout/stderr with `[WorkerName]`
  prefix, exponential backoff restarts (1 s → 2 s → 4 s → … → 60 s max),
  abandons after `MAX_CRASHES_PER_HOUR = 10`
- Workers auto-selected by scanning `config.ini` — `[bms]` section populated
  → BMS worker starts; `[victron]` populated → Victron worker starts
- `_dashboard_loop` runs as a separate `asyncio.Task` — writes HTML by merging
  all state sections on a timer, independent of individual worker poll cycles
- HTTPS server task started when `cfg.server.enabled`
- `--list-workers` flag shows which workers would start without launching

### Worker split (`bms_monitor.py`, `victron_monitor.py`)
- Each worker is a standalone script satisfying the worker contract:
  `--config FILE --state-file FILE --log-level LEVEL --once`
- `--state-file` flag allows supervisor to set a shared path centrally
- Both workers open `HistoryDB` on startup when history is enabled and call
  `db.write_readings()` after each poll

---

### Rich console dashboard (`console_monitor.py`)
- Live terminal dashboard using Rich `Live` + `asyncio` — full-screen,
  in-place update on every state file change
- Mirrors HTML dashboard layout: aggregate row (MPPT | Inverter | Battery)
  then individual device panels
- Three aggregate panels: `_mppt_aggregate_panel`, `_inverter_aggregate_panel`,
  `_battery_aggregate_panel` — totals, online counts, state summaries
- Per-device panels: BMS (SoC bar, TTE/TTF, temps, faults, balance),
  MPPT (PV W, yield, state), Inverter (AC out, state, alarms, battery V/A)
- Colour coding: cyan (voltage), green (charging/online), yellow (PV/MPPT),
  magenta (inverter), red (faults/offline), dim (labels)
- File-watch via `os.path.getmtime()` — re-renders only when state file changes
- `screen=True` in `Live` — terminal restored cleanly on Ctrl-C
- Optional dependency — exits with clear message if `rich` not installed

---

### Mobile-first dashboard redesign (`solar_monitor/dashboard.py`)
- Full CSS rewrite — mobile-first with five breakpoints:
  - Base: single column, stacked aggregate cards, full-width everything
  - 480 px: individual cards go 2-column; header meta text appears
  - 600 px: aggregate cards go side-by-side; charts go 2-column
  - 768 px: header/section padding increases; agg cards get full padding
  - 900 px: individual cards switch to `auto-fill` grid
  - 1200 px: charts go 4-column
- Sticky header with `backdrop-filter:blur(12px)` — visible while scrolling
- Fluid type with `clamp()` — aggregate numbers scale to viewport width
- PWA meta tags: `apple-mobile-web-app-capable`,
  `apple-mobile-web-app-status-bar-style`, `theme-color` (updated on theme
  switch)
- `safe-area-inset-bottom` — content above iPhone home indicator
- `overscroll-behavior-y:contain` — no pull-to-refresh interference on Android
- `min-height:44px` on theme button — Apple HIG touch target minimum
- No horizontal scroll at any viewport width — removed all hard `min-width`
  values causing overflow; replaced with `flex:1 1 220px` and `clamp()`
- Chart improvements: `pointRadius:1`, `maxTicksLimit:6`, `maxRotation:0`,
  timestamps trimmed to `HH:MM`

### New aggregate card layout
- Replaced five-number totals banner with three rich aggregate cards:
  `agg-mppt` (☀ MPPT), `agg-inv` (⚡ Inverter), `agg-bat` (🔋 Battery)
- All three in a single `flex` container — side-by-side on wide screens,
  stacked on narrow screens (`flex-direction:column` → `flex-direction:row`
  at 600 px)
- Individual device cards split into three labelled sections:
  MPPT Chargers, Inverters, Battery Packs

---

### BMS direct MAC connection fix
- `read_jbd_device()` now accepts either a MAC address string or `BLEDevice`
- `_poll_bms` passes `dc.mac` (plain string) directly to `BleakClient` —
  eliminates `KeyError: 'path'` caused by synthetic `BLEDevice(mac, name, details={})`
- BlueZ constructs the D-Bus path `/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF`
  from the MAC string without requiring a prior scan
- `"'path'"` and `"keyerror"` added to `_TRANSIENT_ERRORS` as a safety net

### Victron passive scan improvements
- `VictronScanner.scan()` now tries three modes in sequence before giving up:
  1. `scanning_mode="passive"` + `OrPattern` objects (bleak ≥ 0.21)
  2. `scanning_mode="passive"` + raw tuple `(0, 0xFF, b'\xe1\x02')` (bleak ≤ 0.20)
  3. `scanning_mode="active"` — universal fallback, works on all platforms
- `or_patterns` use Victron company ID `0x02E1` (AD type `0xFF`, LE bytes
  `\xe1\x02`) — kernel-level filter so only Victron traffic is delivered
- Any passive failure (not just specific error strings) triggers active fallback
- Fallback logs at `WARNING` with the actual exception type and message;
  silences after first cycle

---

### Package additions to `AppConfig`
- `server: ServerConfig` — HTTPS server settings
- `history: HistoryConfig` — SQLite history settings
- `[server]` and `[history]` sections parsed in `load_config()`

### State file (`solar_monitor/state.py`)
- Atomic write: `write to .tmp → os.replace` — readers always see a complete file
- `load_state` returns empty dicts on missing/corrupt file, never raises
- `save_section` updates only the owning section, preserving the other worker's data

---

### Test suite
- **765 tests**, all passing, no BLE hardware or browser required
- Tests added / extended this audit:
  - `normalise_mac`, `parse_bms_value`, `parse_mac_key` — config parsing helpers
  - `_soc_color`, `_no_card` — dashboard utility functions
  - `_resolve_date`, `_print_table` — query utility helpers
  - `max_history` INI key loading
  - `OrPattern` import path and fallback in `VictronScanner.scan()`
- New test files:
  - `tests/test_supervisor.py` — WorkerSpec, WorkerProcess, crash policy, dashboard loop, config detection (59 tests)
  - `tests/test_console_monitor.py` — utility functions, all panels, `_render`, `_mtime` (80 tests)
  - `tests/test_https_server.py` — cert generation, SSL context, routing, config, live bind (73 tests)
  - `tests/test_mcp_server.py` — all 8 tools, security enforcement, dispatch, JSON-RPC (107 tests)
  - `tests/test_history.py` — HistoryDB, config, schema, purge, retention, utilities (99 tests)

---

## VE.Bus Smart Dongle full support

### Dashboard
- Inverter card redesigned to match VictronConnect label groupings exactly
- **AC Output L1** section: Voltage (V) hardcoded 120V, Power (W) from payload,
  Current (A) computed as P÷120
- **Battery** section: Voltage (V), Current (A) raw signed, Temperature
- Status row: STATE · AC In source · ALARM
- Added `section-lbl` CSS class — thin divider line with uppercase label

### victron.py — `_parse_vebus` (new)
- New dedicated parser for record types `0x07` and `0x0C`
- All 10 fields: device_state, vebus_error, battery_current (int16, 0.1A),
  battery_voltage (uint14, 0.01V), active_ac_in (2-bit), ac_in_power (int19, 1W),
  ac_out_power (int19, 1W), alarm (2-bit), battery_temperature (7-bit, raw−40),
  soc (7-bit, NA=0x7F)
- `PARSERS[0x07]` and `PARSERS[0x0C]` both wired to `_parse_vebus`
- Root cause fix: `0x0C` removed from `_RECORDS_WITH_STATE`
- `_VALID_STATES` expanded to all known VE.Bus states
- Candidate sort order: `0x0C` (priority 1) tried before `0x07` (priority 9)
- Voltage plausibility floor: inverter-type rejects `voltage_v < 9.0V`

### models.py additions
- `ac_in_power_w`, `ac_in_source`, `vebus_error`, `temperature_c`

---

## Victron spec audit — all record types

- `_parse_inverter` (0x03): voltage bug fixed (uint16 × 0.001V → int16 × 0.01V)
- `_parse_bmv` (0x02): NA sentinel checked before sign extension
- `_parse_dcenergy` (0x08/0x0D): same NA fix
- `_parse_solar` (0x01): added `load_current_a` (9-bit, 0.1A, NA=0x1FF)
- `_parse_inverter_rs` (0x06): restored missing `def` line
- Per-candidate logging moved to DEBUG level

---

## JBD / Vatrer BMS fault tolerance

- `asyncio.timeout(PER_DEVICE_TIMEOUT=35s)` prevents infinite hang
- Buffer cleared on corrupt length byte in `_on_notify`
- `_verify_checksum` added (warns, does not raise)
- NTC count capped to prevent index overflow
- `_PERMANENT_ERRORS` checked first, no retry
- Empty string removed from `_TRANSIENT_ERRORS`
- `INTER_DEVICE_GAP=1.5s` between device connections
- Constants: `READ_TIMEOUT=12s`, `NOTIFY_SETTLE_DELAY=1.0s`,
  `PER_DEVICE_TIMEOUT=35s`, `MAX_PAYLOAD_LEN=128`

---

## Package structure

Refactored from monolithic script to `solar_monitor/` Python package:

| Module | Contents |
|---|---|
| `models.py` | `DeviceReading` dataclass |
| `config.py` | `AppConfig`, INI/CLI parsing |
| `jbd.py` | `JBDGattReader`, `read_jbd_device` |
| `victron.py` | All parsers, `PARSERS` dispatch |
| `scanner.py` | `VictronScanner`, `_poll_bms`, `poll_all` |
| `dashboard.py` | `build_html`, all card renderers |
| `state.py` | Atomic JSON state file I/O |
| `server.py` | HTTPS server, cert generation, SSL context |
| `history.py` | SQLite history store, `HistoryDB` |

---

## Documentation

- `MANUAL.md` — 2,321-line comprehensive manual, 17 sections:
  Overview, Requirements, Installation, Configuration, Running, Service,
  HTML Dashboard, Console Dashboard, Victron Setup, BMS Setup, Architecture,
  Adding a Data Source, Troubleshooting, Reference, HTTPS Server & API,
  MCP Server, Historical Data & SQLite Storage
- `CONFIG.md` — config file reference
- `GUIDE.md` — quick-start guide
