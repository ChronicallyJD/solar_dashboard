"""
solar_monitor/config.py — Configuration loading and CLI parsing
===============================================================
Handles INI file reading, CLI argument parsing, and the two dataclasses
(AppConfig, DeviceConfig) that carry runtime configuration throughout the
application.

INI file format
---------------
[general]
output        = dashboard.html
interval      = 30
scan_timeout  = 10
max_history   = 60
log_level     = INFO
theme         = dark          # dark | light | business

[bms]
# Friendly Name = MAC_OR_BLE_NAME [ : password ]
House Bank    = AA:BB:CC:DD:EE:01 : 0000
Vatrer Pack   = SP16S020L16S100A

[victron]                     # also accepts [mppt]
# Friendly Name = MAC : 32-hex-advertisement-key
Roof MPPT     = E1:2D:6C:B5:83:76 : 2bbad134f666e8f1f23e510584af3450
Inverter      = E6:2E:31:75:9A:1A : dd15693279172720da3ecb1d2e4e7da1
"""

import argparse
import configparser
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

DEFAULT_INI_PATH = "monitor.ini"

INI_EXAMPLE = """\
# Solar Monitor configuration file
# Lines starting with # are comments.
# All settings are optional; built-in defaults apply when omitted.

[general]
# Path to the generated HTML dashboard
output          = dashboard.html

# How often to poll devices (seconds)
interval        = 30

# Historical data-points to keep per device in the dashboard charts
max_history     = 60

# BLE scan duration each poll cycle (seconds)
scan_timeout    = 10

# Log level: DEBUG | INFO | WARNING | ERROR
log_level       = INFO

# Dashboard colour theme: dark | light | business
theme           = dark


# ── JBD / Vatrer BMS devices ─────────────────────────────────────────────────
# Each entry is:   Friendly Name = <identifier> [ : <password> ]
#
# Supports JBD (Xiaoxiang), Overkill Solar, Daly, and Vatrer batteries.
# Identify a BMS by its MAC address OR by its BLE advertisement name.
# Append  ' : <password>'  (space-colon-space) to include a connection password.
# Factory default password on most JBD/Vatrer units is '0000'.
#
# Examples:
#   House Bank   = AA:BB:CC:DD:EE:01 : 0000    (by MAC with password)
#   Vatrer Bank  = SP16S020L16S100A             (by BLE name, no password)
#
# If this section is absent the script auto-discovers JBD/Vatrer devices
# by BLE name pattern.  No password is sent for auto-discovered devices.

[bms]
# House Bank   = AA:BB:CC:DD:EE:01 : 0000
# Vatrer Bank  = SP16S020L16S100A


# ── Victron devices ──────────────────────────────────────────────────────────
# Accepts [victron] or [mppt] as the section name.
# Each entry is:   Friendly Name = MAC : KEY [ type=TYPE ]
#
# The 32-hex-character AES-128 Advertisement key comes from VictronConnect:
#   Connect to device -> gear icon -> Product info
#   -> scroll to "Instant Readout via Bluetooth" -> tap Show
#   -> copy the "Advertisement key"
#   (NOT the "Encryption key" shown higher up on the same screen)
#
# Optional  type=  declaration pins the device to a specific role.
# Without it, the type is inferred from the BLE record type, which can be
# unreliable when VE.Smart networking is active (devices share each other's
# data on the network, causing record-type confusion).
#
# Valid types: mppt | inverter | monitor | dcdc | lithium | meter
#
# Two separator formats for the MAC:KEY pair:
#   No spaces:   Name = AA:BB:CC:DD:EE:FF:aabbccddeeff00112233445566778899
#   With spaces: Name = AA:BB:CC:DD:EE:FF : aabbccddeeff00112233445566778899
#
# Full examples:
#   (1)-14-130V  = DD:1B:7E:A7:91:83:613f9fd95d3633385cf49d32a9d551e3 type=mppt
#   (3)-12-180V  = E1:2D:6C:B5:83:76:2bbad134f666e8f1f23e510584af3450 type=mppt
#   48V-2400W    = E6:2E:31:75:9A:1A:dd15693279172720da3ecb1d2e4e7da1 type=inverter
#
# If this section is absent the script auto-discovers Victron devices.

[victron]
# (1)-14-130V  = DD:1B:7E:A7:91:83:613f9fd95d3633385cf49d32a9d551e3 type=mppt
# 48V-2400W    = E6:2E:31:75:9A:1A:dd15693279172720da3ecb1d2e4e7da1 type=inverter
"""


@dataclass
class DeviceConfig:
    """Parsed configuration for a single monitored device."""
    name:        str             # friendly label shown in the dashboard
    mac:         Optional[str]   # normalised upper-case MAC or None
    ble_name:    Optional[str]   # BLE advertisement name to match (BMS only)
    enc_key:     Optional[str]   # 32-hex Victron advertisement key
    password:    Optional[str]   # JBD BMS connection password
    device_type: Optional[str]   = None  # explicit type override: "mppt"|"inverter"|"monitor"|"dcdc"


@dataclass
class AppConfig:
    """Runtime configuration assembled from INI file + CLI overrides."""
    output:       str   = "dashboard.html"
    interval:     float = 30.0
    max_history:  int   = 60
    scan_timeout: float = 10.0
    log_level:    str   = "INFO"
    theme:        str   = "dark"         # "dark" | "light" | "business"
    bms_devices:  list  = field(default_factory=list)   # list[DeviceConfig]
    mppt_devices: list  = field(default_factory=list)   # list[DeviceConfig]
    once:         bool  = False
    auto_discover_bms:  bool = True
    auto_discover_mppt: bool = True


# ── MAC / key parsing helpers ─────────────────────────────────────────────────

def normalise_mac(raw: str) -> str:
    """
    Return an upper-case colon-separated MAC address from any common format.

    Accepts: AA:BB:CC:DD:EE:FF, AA-BB-CC-DD-EE-FF, AABBCCDDEEFF.
    """
    clean = raw.upper().replace("-", ":").strip()
    if len(clean) == 12 and ":" not in clean:
        clean = ":".join(clean[i:i+2] for i in range(0, 12, 2))
    return clean


def is_mac(value: str) -> bool:
    """
    Return True if *value* looks like a valid MAC address.

    Accepts colon- or dash-separated hex pairs, or a plain 12-hex-character
    string.
    """
    v = value.strip().upper().replace("-", ":").replace(":", "")
    return len(v) == 12 and all(c in "0123456789ABCDEF" for c in v)


def parse_mac_key(value: str) -> tuple[str, Optional[str], Optional[str]]:
    """
    Parse a Victron INI value into (mac, advertisement_key, device_type).

    Supported formats::

        AA:BB:CC:DD:EE:FF                               # MAC only
        AA:BB:CC:DD:EE:FF : aabbccddeeff0011...         # MAC + key (spaced)
        AA:BB:CC:DD:EE:FF:aabbccddeeff0011...           # MAC + key (no spaces)
        AA:BB:CC:DD:EE:FF:aabbcc... type=inverter       # MAC + key + explicit type
        AA:BB:CC:DD:EE:FF type=mppt                     # MAC + type, no key

    Valid device_type values: mppt, inverter, monitor, dcdc, lithium, meter.
    If omitted, the type is inferred from the BLE record type at runtime.

    Returns:
        (normalised_mac, key_hex_or_None, device_type_or_None)
    """
    value = value.strip()

    # Extract optional type= declaration before further parsing
    device_type: Optional[str] = None
    if " type=" in value:
        value, type_part = value.rsplit(" type=", 1)
        device_type = type_part.strip().lower() or None
        value = value.strip()

    if " : " in value:
        mac_part, key_part = value.split(" : ", 1)
        return normalise_mac(mac_part.strip()), key_part.strip() or None, device_type
    parts = value.split(":")
    if len(parts) == 7:
        return normalise_mac(":".join(parts[:6])), parts[6].strip() or None, device_type
    return normalise_mac(value), None, device_type


def parse_bms_value(value: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Parse a [bms] INI value into (mac, ble_name, password).

    Supported formats::

        AA:BB:CC:DD:EE:FF                # MAC, no password
        AA:BB:CC:DD:EE:FF : 0000         # MAC + password
        JBD-SP04S034                     # BLE name, no password
        JBD-SP04S034 : mysecret          # BLE name + password

    The separator between identifier and password is " : " (space-colon-space).

    Returns:
        (mac_or_None, ble_name_or_None, password_or_None)
    """
    password = None
    if " : " in value:
        ident, password = value.split(" : ", 1)
        ident    = ident.strip()
        password = password.strip() or None
    else:
        ident = value.strip()

    if is_mac(ident):
        return normalise_mac(ident), None, password
    return None, ident, password


# ── INI loader ────────────────────────────────────────────────────────────────

def load_config(ini_path: Optional[str]) -> AppConfig:
    """
    Read an INI configuration file and return a populated AppConfig.

    If *ini_path* is None the default path ("monitor.ini") is used.  A missing
    default-path file is silently ignored; a missing explicitly-supplied file
    produces a warning.

    Section name aliases: [victron] and [mppt] are both accepted.
    """
    cfg = AppConfig()
    if ini_path is None:
        ini_path = DEFAULT_INI_PATH

    ini_file = Path(ini_path)
    if not ini_file.exists():
        if ini_path != DEFAULT_INI_PATH:
            log.warning(f"Config file not found: {ini_path}")
        return cfg

    log.info(f"Reading config: {ini_file.resolve()}")
    parser = configparser.ConfigParser(
        inline_comment_prefixes=("#", ";"),
        empty_lines_in_values=False,
    )
    parser.read(ini_file, encoding="utf-8")

    # [general] ----------------------------------------------------------------
    g = parser["general"] if "general" in parser else {}
    cfg.output       = g.get("output",       cfg.output)
    cfg.interval     = float(g.get("interval",     cfg.interval))
    cfg.max_history  = int(  g.get("max_history",  cfg.max_history))
    cfg.scan_timeout = float(g.get("scan_timeout", cfg.scan_timeout))
    cfg.log_level    = g.get("log_level", cfg.log_level).upper()
    cfg.theme        = g.get("theme",     cfg.theme).lower()

    # [bms] --------------------------------------------------------------------
    if "bms" in parser and parser["bms"]:
        cfg.auto_discover_bms = False
        for name, value in parser["bms"].items():
            mac, ble_name, password = parse_bms_value(value.strip())
            label     = name.title()
            ident_str = f"MAC {mac}" if mac else f"BLE name '{ble_name}'"
            pw_str    = " (password set)" if password else ""
            log.info(f"  BMS  '{label}' -> {ident_str}{pw_str}")
            cfg.bms_devices.append(DeviceConfig(
                name=label, mac=mac, ble_name=ble_name, enc_key=None, password=password
            ))

    # [victron] / [mppt] -------------------------------------------------------
    victron_section = None
    if "victron" in parser and parser["victron"]:
        victron_section = parser["victron"]
    elif "mppt" in parser and parser["mppt"]:
        victron_section = parser["mppt"]

    if victron_section:
        cfg.auto_discover_mppt = False
        for name, value in victron_section.items():
            mac, key, dtype = parse_mac_key(value)
            label    = name.title()
            type_str = f" type={dtype}" if dtype else ""
            log.info(f"  Victron '{label}' -> {mac}"
                     + (" (key set)" if key else " (no key)")
                     + type_str)
            cfg.mppt_devices.append(DeviceConfig(
                name=label, mac=mac, ble_name=None, enc_key=key,
                password=None, device_type=dtype,
            ))

    return cfg


def apply_cli_overrides(cfg: AppConfig, args: argparse.Namespace) -> AppConfig:
    """
    Merge parsed CLI arguments on top of an existing AppConfig.

    CLI arguments that were not supplied (None / False) are ignored, so they
    do not clobber INI values.  --bms and --mppt replace the entire
    corresponding device list when present.
    """
    if args.output:
        cfg.output = args.output
    if args.interval is not None:
        cfg.interval = args.interval
    if args.scan_timeout is not None:
        cfg.scan_timeout = args.scan_timeout
    if args.once:
        cfg.once = True

    if args.bms:
        cfg.auto_discover_bms = False
        cfg.bms_devices = [
            DeviceConfig(name=normalise_mac(m), mac=normalise_mac(m),
                         ble_name=None, enc_key=None, password=None)
            for m in args.bms
        ]

    if args.mppt:
        cfg.auto_discover_mppt = False
        cfg.mppt_devices = []
        for entry in args.mppt:
            mac, key, dtype = parse_mac_key(entry)
            cfg.mppt_devices.append(DeviceConfig(
                name=mac, mac=mac, ble_name=None, enc_key=key,
                password=None, device_type=dtype,
            ))

    return cfg


def write_example_ini(path: str = "monitor.ini.example") -> None:
    """Write the example INI template to *path*."""
    Path(path).write_text(INI_EXAMPLE, encoding="utf-8")
    print(f"Example config written to {path}")
