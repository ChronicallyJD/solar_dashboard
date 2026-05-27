# solar_monitor — config.ini reference

## Quick-start example

```ini
[general]
output     = dashboard.html
interval   = 30
theme      = business

[bms]
House Bank = AA:BB:CC:DD:EE:FF : 123456

[victron]
South Array  = 11:22:33:44:55:66 : aabbccddeeff00112233445566778899  type=mppt
West Array   = 11:22:33:44:55:77 : 00112233445566778899aabbccddeeff  type=mppt
MultiPlus    = 11:22:33:44:55:88 : ffeeddccbbaa99887766554433221100  type=inverter
```

---

## [general]

| Key          | Default          | Description |
|---|---|---|
| `output`     | `dashboard.html` | Path for the generated HTML dashboard |
| `interval`   | `30`             | Seconds between full poll cycles |
| `scan_timeout` | `10`           | BLE scan duration per cycle (seconds) |
| `max_history`  | `600`          | Chart data-points kept per device |
| `log_level`    | `INFO`         | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `theme`        | `business`     | `dark` / `light` / `business` |

---

## [bms]

One line per JBD / Vatrer BMS cell pack:

```ini
[bms]
Name = MAC_ADDRESS_OR_BLE_NAME [ : password ]
```

- **MAC** (recommended): `AA:BB:CC:DD:EE:FF`
- **BLE name**: the advertised name shown in nRF Connect / BLE scanner apps.
  Less reliable — use MAC when possible.
- **Password**: optional 6-digit numeric BMS password (default `123456`).
  Omit the colon entirely if no password is set.

```ini
[bms]
House Bank  = A1:B2:C3:D4:E5:F6 : 123456
Spare Pack  = A1:B2:C3:D4:E5:F7
```

---

## [victron]

One line per Victron BLE device:

```ini
[victron]
Name = MAC:KEY [ type=mppt|inverter|monitor|dcdc ]
```

- **MAC**: 12-hex-digit address, colon-separated — `AA:BB:CC:DD:EE:FF`
- **KEY**: 32-hex-digit advertisement key (16 bytes) — no spaces, no colons
- **type**: optional; controls which dashboard card is shown and which record
  types are accepted (see table below)

### Finding MAC and KEY in VictronConnect

1. Open VictronConnect and connect to the device.
2. Tap the **⚙️ gear icon** → **Product Info**.
3. Under **Instant Readout via Bluetooth**, tap **Show**.
4. Copy the **Advertisement Key** (32 hex digits).
5. The **MAC address** is shown above it, or find it in your OS BLE scanner.

> **Note**: On iOS, Victron shows a UUID instead of a MAC. Use a Linux or
> Android host to run the monitor, or use nRF Connect on Android to find the
> real MAC.

### type= values

| `type=`    | Dashboard card | Accepted records               |
|---|---|---|
| `mppt`     | Solar Charger  | `0x01` Solar Charger           |
| `inverter` | Inverter       | `0x07` VE.Bus dongle (old fw)  |
|            |                | `0x0C` VE.Bus dongle (new fw)  |
|            |                | `0x03` Phoenix Inverter        |
|            |                | `0x06` Inverter RS             |
|            |                | `0x0B` Multi RS                |
| `monitor`  | Battery Monitor| `0x02` SmartShunt / BMV        |
|            |                | `0x05` SmartLithium            |
| `dcdc`     | DC-DC          | `0x04` DC/DC Converter         |
|            |                | `0x0E` Orion XS                |

If `type=` is omitted the monitor tries every known parser and uses the first
that succeeds.

---

## VE.Bus Smart Dongle — step-by-step

The VE.Bus Smart Dongle plugged into a MultiPlus-II (or any VE.Bus
inverter/charger) is the richest Victron BLE source. When it broadcasts
record type **`0x0C`** it provides:

| Field                | Units | Notes |
|---|---|---|
| Battery voltage      | V     | DC bus voltage |
| Battery current      | A     | + = charging, − = discharging |
| Battery temperature  | °C    | Dongle's onboard sensor |
| State of charge      | %     | As reported by VE.Bus |
| AC in source         | —     | AC1 / AC2 / Not connected |
| AC in power (real)   | W     | Power from grid/generator |
| AC out power (real)  | W     | Power delivered to loads |
| Device state         | —     | Inverting / Charging / Passthrough / … |
| VE.Bus error code    | —     | 0 = no error |
| Alarm                | —     | None / Warning / Alarm |

### Setup

**Step 1 — Find the dongle in VictronConnect.**

The dongle appears as a *separate device entry* from the MultiPlus-II. It is
typically named after whatever label you gave the inverter system (e.g.
`48V-2400W`). If you have never opened it in VC, connect to it once — it
will appear on the device list page alongside the inverter, MPPT, and BMS
devices.

**Step 2 — Enable Instant Readout.**

Tap the dongle in VC → gear icon → Product Info → scroll to
**Instant Readout via Bluetooth** → enable it if it is off → tap **Show**.

**Step 3 — Copy the key.**

```
Advertisement key:  aabbccddeeff00112233445566778899
```

Copy the 32-character hex key exactly as shown. Also note the **MAC address**
shown just above (e.g. `C0:FF:EE:12:34:56`).

**Step 4 — Add to config.ini.**

```ini
[victron]
MultiPlus = C0:FF:EE:12:34:56 : aabbccddeeff00112233445566778899  type=inverter
```

> **Tip**: Give it a meaningful name — it shows as the card title on the
> dashboard. `MultiPlus`, `Inverter`, or the inverter model all work well.

**Step 5 — (Optional) Remove the built-in BT entry.**

If you previously had a line for the MultiPlus-II's built-in Bluetooth
(record type `0x07`, limited data), you can remove or keep it. The dongle
(`0x0C`) will always provide more data, so there is no advantage to
polling both. If you keep both, the dashboard shows two inverter cards.

**Step 6 — Restart the monitor and verify.**

```
python jbd_bms_monitor.py --config config.ini
```

In the log you should see a line like:

```
[Victron] MultiPlus: VE.Bus 0x0C state=0x09 batt=52.1V/-6.0A ac_out=306W soc=84%
```

If you see `no candidate payload decrypted successfully`, the key or MAC is
wrong. Re-check step 3.

---

## Firmware note — 0x07 vs 0x0C

Older VE.Bus Smart Dongle firmware broadcasts record type **`0x07`** with a
limited payload (state, battery voltage, a load indicator byte). Newer
firmware broadcasts **`0x0C`** with the full VE.Bus data set.

The monitor handles both automatically when `type=inverter` is set:

- **`0x0C` present** → uses `_parse_vebus`: all fields populated.
- **`0x07` present** → uses `_parse_inverter_0x07`: battery voltage and
  device state confirmed; AC out power shown as `~N (raw)` until you
  calibrate `_SCALE_WATTS` in `victron.py`.

To check which record your dongle broadcasts, run with `--log-level DEBUG`
and look for lines containing `rec=0x07` or `rec=0x0C`.

To update the dongle firmware: connect to it in VictronConnect → gear icon
→ Product Info → Firmware → check for updates. After an update it may switch
from `0x07` to `0x0C` and all fields will populate automatically.

---

## Full example config.ini

```ini
# ─── solar_monitor/config.ini ────────────────────────────────────────────────

[general]
output       = /var/www/html/solar.html
interval     = 30
scan_timeout = 10
max_history  = 600
log_level    = INFO
theme        = business

# ─── JBD / Vatrer BMS packs ──────────────────────────────────────────────────

[bms]
# House Bank = AA:BB:CC:DD:EE:FF : 123456
House Bank = A1:B2:C3:D4:E5:F6 : 123456

# ─── Victron devices ─────────────────────────────────────────────────────────
# Format:  Name = MAC : KEY  [ type=mppt|inverter|monitor|dcdc ]
# KEY is the 32-character Advertisement Key from VictronConnect.

[victron]
# Three SmartSolar MPPTs on South / West / East arrays:
South Array  = 11:22:33:44:55:01 : aabbccddeeff00112233445566778899  type=mppt
West Array   = 11:22:33:44:55:02 : 00112233445566778899aabbccddeeff  type=mppt
East Array   = 11:22:33:44:55:03 : ffeeddccbbaa99887766554433221100  type=mppt

# VE.Bus Smart Dongle for MultiPlus-II 48/5000/70-95 120V.
# This single entry provides battery V/A/W/temp/SoC + AC-in power + AC-out
# real power, all from the verified 0x0C VE.Bus record type.
MultiPlus = C0:FF:EE:12:34:56 : 0123456789abcdef0123456789abcdef  type=inverter
```
