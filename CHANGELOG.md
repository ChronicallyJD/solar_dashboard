# Solar Monitor Changelog

Notable changes, newest first.

## 2026-07-12: Release 1.1 "Deep Cycle"

Security hardening, a full test pass, and an offline-capable dashboard.

### Security
- Dashboard HTML now escapes all dynamic values (device names, addresses,
  error strings, state labels) and the embedded history JSON, closing a
  stored XSS path via crafted device names.
- The auto-generated TLS certificate is now a leaf certificate
  (`CA:FALSE`, key usage `digitalSignature`/`keyEncipherment`, EKU
  `serverAuth`) instead of a 10-year CA certificate. If you added the old
  `server.crt` to a trust store, remove it and re-trust the new one.
- `server.key` is created with mode 0600 from the start.
- HTTP requests get one 15s deadline and a 100-header cap, preventing
  slow-header connection holding.
- `HistoryDB.query()` validates `fields` and `order` identifiers itself
  instead of relying on callers.
- State file writes use a unique `mkstemp` temp file in the target
  directory: concurrent workers cannot clobber each other and planted
  symlinks cannot redirect the write.
- The MCP server compares `api_key` with `hmac.compare_digest`.
- Example configs and test fixtures no longer contain real Victron
  advertisement keys or device MACs.

### Offline dashboard
- Chart.js 4.5.1 is vendored at `solar_monitor/vendor/` and inlined into
  the generated HTML; the Google Fonts import was dropped. The dashboard
  now renders fully offline with no CDN references.

### Tests
- Suite grew from 783 to 1107 tests; coverage 85% to 96%. New coverage:
  JBD protocol parsing (100%), Victron advertisement parsers (99%),
  supervisor main (99%), worker entry points, history CLI utilities, and
  security regression tests for every fix above.
- Tests no longer hardcode absolute paths and run from any checkout.

### Documentation
- All five docs rewritten: terse register, no duplicated content
  (MANUAL.md alone shrank 622 lines), dated changelog, corrected install
  instructions, CONFIG.md is the single home for the annotated config.
- Added `.gitignore`; the repo no longer tracks bytecode, runtime output,
  or TLS material.

## 2026-06-02: VE.Bus record type read from wrong nibble

A VE.Bus Smart Dongle broadcasting record type `0x0C` (newer firmware) was
silently dropped: it never appeared in the dashboard or logs despite a valid
advertisement key. `parse_payload()` extracted the record type from the low
nibble of byte[3], but the Victron BLE spec (and the reference `victron-ble`
library) put it in the high nibble, so `0xC0` decoded as unknown type `0x00`
instead of `0x0C`. Fixed to `(mfr_raw[3] & 0xF0) >> 4`, with regression tests
covering all known record types including the exact bytes from the original
HCI dump.

## 2026-06-01: SmartShunt / BMV impossible readings

Battery monitors occasionally logged values like V=142.3, A=-1499, impossible
for any supported battery system. The voltage plausibility ceiling was a
global 150V, so a garbage decryption (for example a foreign VE.Smart payload
decrypted with the correct key but mismatched record type) could still pass.
`read_victron_advertisement` now applies per-type ceilings: 80V for monitor
devices (SmartShunt, BMV, DC bus only), 150V for MPPT, inverter/VE.Bus, and
unknown types. Implausible candidates fall through to the next candidate or
return an error if none pass.

## 2026-05-29: SQLite history, MCP server, HTTPS server, mobile dashboard

### SQLite history (`solar_monitor/history.py`)
- New `HistoryDB` stores every successful `DeviceReading` to a local SQLite
  database after each poll cycle; error readings are never stored, and a
  failed write never crashes a worker
- Single `readings` table covering all `DeviceReading` fields; list fields
  stored as JSON strings; indexed for date-range and per-device queries
- WAL journal mode plus `synchronous=NORMAL` so multiple workers can write
  concurrently
- Retention (default 3 years, 0 = keep forever) enforced at most once per
  hour; automatic `vacuum()` on a configurable interval
- `load_recent_for_dashboard(max_points)` pre-populates chart history on
  worker startup
- New `[history]` config section: `enabled`, `db_path`, `retention_days`,
  `vacuum_interval_days`; see CONFIG.md

### Management utilities (`utils/`)
- `utils/purge_history.py`: delete by date range (`--before`/`--after`),
  device, or type; `--enforce-retention`, `--vacuum`, `--dry-run`, `--stats`,
  `--list-devices`; always requires a filter and confirms before deleting
  (`--yes` to skip)
- `utils/query_history.py`: query and export as table, CSV, or JSON; date
  filters accept `today`/`yesterday`; device/type filters, column projection
  via `--fields`, `--limit`/`--order`, and inspection modes

### MCP server (`mcp_server.py`)
- Model Context Protocol 1.0 over stdio (JSON-RPC 2.0), standard library
  only, no extra dependencies
- Eight read-only tools: system, battery, solar, and inverter status, single
  device lookup, device list, alerts, and data age
- `read_only = True` is hardcoded, not configurable
- New `[mcp]` config section: `enabled`, `api_key`, `allowed_tools`,
  `rate_limit`, `require_local`, `log_requests`; see CONFIG.md
- API key is stripped from arguments before reaching tool functions; rate
  limiting uses a sliding 60-second window; distinct JSON-RPC error codes for
  unauthorized, forbidden, and rate limited
- All logging goes to stderr; stdout is reserved for the protocol

### HTTPS dashboard server (`solar_monitor/server.py`)
- Async HTTPS server running as an asyncio task inside the supervisor, no
  separate process
- Routes: `/` and `/dashboard.html` (HTML), `/state.json` (raw JSON),
  `/health`; all other paths and non-GET methods return 404
- Auto-generates a self-signed RSA-2048 certificate on first run (10-year
  validity, SANs for localhost, 127.0.0.1, and the configured host, key
  written chmod 600); TLS 1.2 minimum; 10-second read timeout per request
- New `[server]` config section: `enabled`, `host`, `port`, `cert_file`,
  `key_file`, `auto_cert`; see CONFIG.md
- `AppConfig` gains `server` and `history` sections

### Mobile-first dashboard redesign (`solar_monitor/dashboard.py`)
- CSS rewritten mobile-first, scaling from a single-column phone layout up to
  multi-column desktop grids
- Five-number totals banner replaced with three aggregate cards (☀ MPPT,
  ⚡ Inverter, 🔋 Battery): side by side on wide screens, stacked on narrow
- Device cards split into labelled sections: MPPT Chargers, Inverters,
  Battery Packs
- Sticky blurred header, fluid type via `clamp()`, PWA meta tags, iPhone
  safe-area inset, 44px touch targets, pull-to-refresh interference disabled
- Removed hard min-widths so no viewport width scrolls horizontally
- Chart rendering tweaks: smaller points, fewer axis ticks, HH:MM timestamps

### Connection fixes
- BMS direct MAC connection: `read_jbd_device()` accepts a MAC string or
  `BLEDevice`, and `_poll_bms` passes the plain MAC to `BleakClient`. This
  fixes `KeyError: 'path'` caused by synthetic `BLEDevice` objects; BlueZ
  builds the D-Bus path from the MAC without a prior scan. `'path'` and
  `keyerror` added to the transient error list as a safety net.
- Victron passive scan: `VictronScanner.scan()` tries passive mode with
  `OrPattern` (bleak 0.21+), then the raw-tuple form (bleak 0.20 and older),
  then falls back to active scanning. The kernel-level filter matches Victron
  company ID `0x02E1`. Any passive failure triggers the fallback, logged once
  at WARNING with the actual exception.

## 2026-05-28: Supervisor, worker split, console dashboard, VE.Bus support

### Supervisor (`solar_monitor.py`)
- Single entry point replaces running two terminal sessions
- `WorkerSpec` registry: add one entry to register a new data source
- `WorkerProcess` launches each worker subprocess, prefixes its output with
  the worker name, restarts with exponential backoff (1s doubling to 60s),
  and abandons a worker after 10 crashes per hour
- Workers auto-selected from `config.ini`: a populated `[bms]` section starts
  the BMS worker, `[victron]` the Victron worker
- Dashboard loop runs as its own task, merging all state sections on a timer
  independent of worker poll cycles
- `--list-workers` shows which workers would start without launching them

### Worker split (`bms_monitor.py`, `victron_monitor.py`)
- Each worker is a standalone script with a common contract:
  `--config FILE --state-file FILE --log-level LEVEL --once`
- State file writes are atomic (write to `.tmp`, then `os.replace`), so
  readers always see a complete file; `load_state` never raises on a
  missing or corrupt file; `save_section` updates only the owning section,
  preserving the other worker's data

### Console dashboard (`console_monitor.py`)
- Live full-screen terminal dashboard using Rich, re-rendered only when the
  state file's mtime changes
- Mirrors the HTML layout: aggregate row (MPPT, Inverter, Battery) plus
  per-device panels with SoC bar, TTE/TTF, temps, faults, and alarms
- Color-coded metrics; terminal restored cleanly on Ctrl-C
- `rich` is optional; exits with a clear message if not installed

### VE.Bus Smart Dongle support
- New `_parse_vebus` parser handles record types `0x07` and `0x0C`: device
  state, VE.Bus error, battery current, voltage, and temperature, active AC
  input, AC in/out power, alarm, and SoC
- `0x0C` removed from `_RECORDS_WITH_STATE` (root cause of earlier
  misparses); `_VALID_STATES` expanded to all known VE.Bus states; `0x0C`
  tried before `0x07` in candidate order
- Inverter-type readings below 9.0V rejected as implausible
- `models.py`: added `ac_in_power_w`, `ac_in_source`, `vebus_error`,
  `temperature_c`
- Inverter card redesigned to match VictronConnect groupings: AC Output L1
  (voltage hardcoded 120V, power from payload, current computed as P/120),
  Battery (voltage, current, temperature), and a status row (state, AC in
  source, alarm)

### Victron parser audit (all record types)
- `_parse_inverter` (0x03): voltage decoded as int16 at 0.01V resolution
  (was uint16 at 0.001V)
- `_parse_bmv` (0x02) and `_parse_dcenergy` (0x08/0x0D): NA sentinel checked
  before sign extension
- `_parse_solar` (0x01): added `load_current_a`
- `_parse_inverter_rs` (0x06): restored missing `def` line
- Per-candidate logging moved to DEBUG level

### Package structure
- Monolithic script refactored into the `solar_monitor/` package: `models`
  (dataclasses), `config`, `jbd`, `victron` (parsers), `scanner`,
  `dashboard`, `state`, `server`, and `history` modules
- Test suite runs without BLE hardware or a browser

### Documentation
- `MANUAL.md`: full manual covering installation, configuration, running as
  a service, both dashboards, Victron and BMS setup, architecture, adding a
  data source, troubleshooting, the HTTPS API, the MCP server, and history
  storage
- `CONFIG.md`: config file reference
- `GUIDE.md`: quick-start guide

## 2026-05-27: Initial release and BMS fault tolerance

### JBD / Vatrer BMS fault tolerance
- 35-second per-device timeout prevents infinite hangs
- Notification buffer cleared on a corrupt length byte; checksum verification
  added (warns rather than raises); payload length capped
- NTC count capped to prevent index overflow
- Permanent errors checked before transient ones, so they are not retried;
  empty string removed from the transient error list
- 1.5-second gap between device connections; read and settle timeouts made
  explicit constants

### Initial release
- BLE polling for JBD BMS packs and Victron devices, HTML dashboard,
  device scanning utility, license, and README
