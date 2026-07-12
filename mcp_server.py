"""
mcp_server.py - Solar Monitor MCP Server
=========================================
Exposes solar system data to AI assistants (Claude Desktop, Cursor, etc.)
via the Model Context Protocol (MCP) over stdio transport.

The server implements MCP 1.0 using JSON-RPC 2.0 over stdin/stdout.
No third-party MCP SDK is required - only the Python standard library and
the solar_monitor package.

Usage
-----
    python mcp_server.py [--config FILE] [--state-file FILE]

Claude Desktop configuration (~/.config/claude/claude_desktop_config.json):

    {
      "mcpServers": {
        "solar-monitor": {
          "command": "python3",
          "args": ["/path/to/solar_monitor/mcp_server.py",
                   "--config", "/path/to/config.ini"]
        }
      }
    }

Security
--------
All security settings live in the [mcp] section of config.ini:

    [mcp]
    enabled       = true
    api_key       =             # bearer token required in every request (empty = disabled)
    read_only     = true        # never allow writes (always enforced)
    allowed_tools =             # comma-separated whitelist (empty = all tools)
    rate_limit    = 60          # max requests per minute (0 = unlimited)
    require_local = true        # only accept connections from loopback (stdio: N/A)
    log_requests  = false       # log every tool call to stderr

Available tools
---------------
  get_system_status    - aggregate: total PV W, AC out W, avg SoC, pack count
  get_battery_status   - all BMS packs: V, A, SoC, Wh, TTE/TTF, faults
  get_solar_status     - all MPPT chargers: PV W, yield Wh, charger state
  get_inverter_status  - all inverters: AC out W, state, alarms, battery V/A
  get_device           - single device by name or MAC address
  list_devices         - all configured devices with type and online status
  get_alerts           - active faults, alarms, offline devices system-wide
  get_data_age         - timestamp of last successful poll per section
"""

import argparse
import hmac
import json
import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# ── Logging to stderr only (stdout is reserved for JSON-RPC) ─────────────────
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s [MCP] %(levelname)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("solar_mcp")


# ─────────────────────────────────────────────────────────────────────────────
# MCP Security configuration
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class McpConfig:
    """All MCP server security and behaviour settings."""
    enabled:       bool       = True
    api_key:       str        = ""          # empty = no auth required
    read_only:     bool       = True        # always True; explicit for clarity
    allowed_tools: list[str]  = field(default_factory=list)   # empty = all
    rate_limit:    int        = 60          # requests/minute, 0 = unlimited
    require_local: bool       = True        # N/A for stdio; documents intent
    log_requests:  bool       = False       # log every tool call to stderr

    def tool_allowed(self, name: str) -> bool:
        """Return True if *name* is in the allowed_tools whitelist (or list is empty)."""
        if not self.allowed_tools:
            return True
        return name in self.allowed_tools

    def check_api_key(self, provided: Optional[str]) -> bool:
        """Return True if the provided key matches, or no key is required."""
        if not self.api_key:
            return True
        if not isinstance(provided, str):
            return False
        # Constant-time comparison; avoids leaking key length/prefix via timing
        return hmac.compare_digest(provided, self.api_key)


def _load_mcp_config(ini_path: str) -> McpConfig:
    """Read [mcp] section from the INI file and return a McpConfig."""
    import configparser
    p = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
    p.read(ini_path)

    if "mcp" not in p:
        return McpConfig()

    s = p["mcp"]

    def _bool(key: str, default: bool) -> bool:
        return s.get(key, str(default)).strip().lower() in ("1", "true", "yes", "on")

    def _list(key: str) -> list[str]:
        raw = s.get(key, "").strip()
        return [t.strip() for t in raw.split(",") if t.strip()] if raw else []

    return McpConfig(
        enabled       = _bool("enabled",       True),
        api_key       = s.get("api_key",       "").strip(),
        read_only     = True,  # always enforced regardless of config
        allowed_tools = _list("allowed_tools"),
        rate_limit    = int(s.get("rate_limit", "60")),
        require_local = _bool("require_local",  True),
        log_requests  = _bool("log_requests",   False),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Rate limiter (token bucket, per-process)
# ─────────────────────────────────────────────────────────────────────────────

class RateLimiter:
    """Simple sliding-window rate limiter. Thread-safe for single-process use."""

    def __init__(self, max_per_minute: int) -> None:
        self._max   = max_per_minute
        self._calls: list[float] = []

    def allow(self) -> bool:
        if self._max == 0:
            return True
        now = time.monotonic()
        self._calls = [t for t in self._calls if now - t < 60.0]
        if len(self._calls) >= self._max:
            return False
        self._calls.append(now)
        return True


# ─────────────────────────────────────────────────────────────────────────────
# Data access helpers
# ─────────────────────────────────────────────────────────────────────────────

def _load(state_path: str) -> dict:
    """Load the shared state file. Returns empty dicts on any failure."""
    try:
        text = Path(state_path).read_text(encoding="utf-8")
        return json.loads(text)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"bms": {"updated": None, "readings": []},
                "victron": {"updated": None, "readings": []}}


def _reading_ok(r: dict) -> bool:
    return not r.get("error")


def _fmt_v(v, d=2) -> Optional[str]:
    return f"{v:.{d}f}" if v is not None else None


def _tte(h: Optional[float]) -> Optional[str]:
    if h is None:
        return None
    hrs, mins = divmod(int(h * 60), 60)
    return f"{hrs}h{mins:02d}m"


# ─────────────────────────────────────────────────────────────────────────────
# Tool implementations
# ─────────────────────────────────────────────────────────────────────────────

def tool_get_system_status(state: dict) -> dict:
    """
    High-level system summary - one number per energy flow.

    Returns totals across all online devices so an assistant can answer
    questions like "how much solar power am I generating?" or
    "what's the overall battery state of charge?".
    """
    bms_ok = [r for r in state["bms"]["readings"] if _reading_ok(r)]
    vic_ok = [r for r in state["victron"]["readings"] if _reading_ok(r)]

    mppt_ok = [r for r in vic_ok if r.get("device_type") == "mppt"]
    inv_ok  = [r for r in vic_ok if r.get("device_type") == "inverter"]

    total_pv    = sum(r.get("pv_power_w") or 0 for r in mppt_ok)
    total_yield = sum(r.get("yield_today_wh") or 0 for r in mppt_ok)
    total_ac    = sum(r.get("ac_out_power_va") or 0 for r in inv_ok)

    soc_vals = [r["capacity_pct"] for r in bms_ok if r.get("capacity_pct") is not None]
    avg_soc  = round(sum(soc_vals) / len(soc_vals)) if soc_vals else None
    total_wh = sum(r.get("remain_wh") or 0 for r in bms_ok)
    net_a    = sum(r.get("current_a") or 0 for r in bms_ok
                   if r.get("current_a") is not None)

    alerts = _count_alerts(state)

    return {
        "solar": {
            "total_pv_w":       round(total_pv, 1),
            "yield_today_wh":   round(total_yield, 0),
            "mppt_online":      len(mppt_ok),
            "mppt_total":       len([r for r in state["victron"]["readings"]
                                     if r.get("device_type") == "mppt"]),
        },
        "inverter": {
            "total_ac_out_w":   round(total_ac, 0),
            "inverters_online": len(inv_ok),
            "inverters_total":  len([r for r in state["victron"]["readings"]
                                     if r.get("device_type") == "inverter"]),
        },
        "battery": {
            "avg_soc_pct":      avg_soc,
            "total_remain_wh":  round(total_wh, 0),
            "net_current_a":    round(net_a, 2),
            "packs_online":     len(bms_ok),
            "packs_total":      len(state["bms"]["readings"]),
            "charging":         net_a > 0.5,
            "discharging":      net_a < -0.5,
        },
        "alerts": {
            "active_faults":    alerts["faults"],
            "offline_devices":  alerts["offline"],
            "alarms":           alerts["alarms"],
        },
        "data_age": {
            "bms_updated":     state["bms"].get("updated"),
            "victron_updated": state["victron"].get("updated"),
        },
    }


def tool_get_battery_status(state: dict) -> dict:
    """
    Detailed status of every JBD/Vatrer BMS battery pack.

    Includes state of charge, voltage, current, energy remaining,
    estimated runtime, active faults, and cell balancing activity.
    """
    packs = []
    for r in state["bms"]["readings"]:
        if r.get("error"):
            packs.append({
                "name":    r["name"],
                "address": r["address"],
                "online":  False,
                "error":   r["error"],
            })
            continue

        soc = r.get("capacity_pct")
        pack = {
            "name":              r["name"],
            "address":           r["address"],
            "online":            True,
            "timestamp":         r.get("timestamp"),
            "voltage_v":         _fmt_v(r.get("voltage_v"), 2),
            "current_a":         _fmt_v(r.get("current_a"), 2),
            "power_w":           _fmt_v(r.get("power_w"),   1),
            "soc_pct":           soc,
            "remain_wh":         _fmt_v(r.get("remain_wh"), 0),
            "remain_ah":         _fmt_v(r.get("remain_ah"), 1),
            "nominal_ah":        _fmt_v(r.get("nominal_ah"), 1),
            "time_to_empty":     _tte(r.get("time_to_empty_h")),
            "time_to_full":      _tte(r.get("time_to_full_h")),
            "cell_count":        r.get("cell_count"),
            "cycle_count":       r.get("cycle_count"),
            "temperatures_c":    r.get("temp_c") or [],
            "faults":            r.get("faults") or [],
            "balancing_cells":   (
                [i + 1 for i, b in enumerate(r["balance_cells"]) if b]
                if r.get("balance_cells") else []
            ),
            "charge_fet":        r.get("charge_fet"),
            "discharge_fet":     r.get("discharge_fet"),
            "firmware":          r.get("sw_version"),
        }
        packs.append(pack)

    total_ok = [p for p in packs if p.get("online")]
    return {
        "packs":         packs,
        "summary": {
            "total":     len(packs),
            "online":    len(total_ok),
            "any_faults": any(p.get("faults") for p in total_ok),
        },
    }


def tool_get_solar_status(state: dict) -> dict:
    """
    Status of all Victron SmartSolar MPPT charge controllers.

    Includes PV power input, yield today, battery output, and charger state.
    """
    chargers = []
    for r in state["victron"]["readings"]:
        if r.get("device_type") != "mppt":
            continue
        if r.get("error"):
            chargers.append({
                "name":    r["name"],
                "address": r["address"],
                "online":  False,
                "error":   r["error"],
            })
            continue

        chargers.append({
            "name":           r["name"],
            "address":        r["address"],
            "online":         True,
            "timestamp":      r.get("timestamp"),
            "pv_power_w":     _fmt_v(r.get("pv_power_w"),    1),
            "yield_today_wh": _fmt_v(r.get("yield_today_wh"), 0),
            "battery_v":      _fmt_v(r.get("voltage_v"),      2),
            "battery_a":      _fmt_v(r.get("current_a"),      2),
            "charger_state":  r.get("charger_state"),
            "load_a":         _fmt_v(r.get("load_current_a"), 1),
        })

    total_pv    = sum(
        float(c["pv_power_w"]) for c in chargers
        if c.get("online") and c.get("pv_power_w") is not None
    )
    total_yield = sum(
        float(c["yield_today_wh"]) for c in chargers
        if c.get("online") and c.get("yield_today_wh") is not None
    )

    return {
        "chargers":    chargers,
        "summary": {
            "total":         len(chargers),
            "online":        sum(1 for c in chargers if c.get("online")),
            "total_pv_w":    round(total_pv, 1),
            "total_yield_wh": round(total_yield, 0),
        },
    }


def tool_get_inverter_status(state: dict) -> dict:
    """
    Status of all Victron inverters / VE.Bus Smart Dongles (MultiPlus, etc.).

    Includes AC output power, input source, device state, alarms,
    and battery readings from the dongle.
    """
    inverters = []
    for r in state["victron"]["readings"]:
        if r.get("device_type") != "inverter":
            continue
        if r.get("error"):
            inverters.append({
                "name":    r["name"],
                "address": r["address"],
                "online":  False,
                "error":   r["error"],
            })
            continue

        alarm = r.get("alarm_reason")
        inverters.append({
            "name":           r["name"],
            "address":        r["address"],
            "online":         True,
            "timestamp":      r.get("timestamp"),
            "state":          r.get("inverter_state"),
            "ac_out_w":       _fmt_v(r.get("ac_out_power_va"), 0),
            "ac_in_source":   r.get("ac_in_source"),
            "ac_in_power_w":  _fmt_v(r.get("ac_in_power_w"),  0),
            "battery_v":      _fmt_v(r.get("voltage_v"),       2),
            "battery_a":      _fmt_v(r.get("current_a"),       2),
            "temperature_c":  r.get("temperature_c"),
            "alarm":          alarm if alarm not in (None, 0, "None") else None,
            "vebus_error":    r.get("vebus_error") or None,
        })

    return {
        "inverters": inverters,
        "summary": {
            "total":        len(inverters),
            "online":       sum(1 for i in inverters if i.get("online")),
            "any_alarms":   any(i.get("alarm") for i in inverters),
            "total_ac_out_w": round(sum(
                float(i["ac_out_w"]) for i in inverters
                if i.get("online") and i.get("ac_out_w") is not None
            ), 0),
        },
    }


def tool_get_device(state: dict, name_or_address: str) -> dict:
    """
    Return all available data for a single device identified by name or MAC.

    Name matching is case-insensitive. If multiple devices share a name,
    all matches are returned.
    """
    needle = name_or_address.strip().lower()
    matches = []

    for section in ("bms", "victron"):
        for r in state[section]["readings"]:
            if (r.get("name", "").lower() == needle or
                    r.get("address", "").lower() == needle):
                matches.append({"section": section, **r})

    if not matches:
        return {"found": False, "query": name_or_address, "devices": []}

    return {"found": True, "query": name_or_address, "devices": matches}


def tool_list_devices(state: dict) -> dict:
    """
    List all configured devices with their type, online status, and last reading.

    Useful for discovering what's available before querying specific devices.
    """
    devices = []

    for r in state["bms"]["readings"]:
        devices.append({
            "name":        r["name"],
            "address":     r["address"],
            "type":        "bms",
            "online":      _reading_ok(r),
            "last_updated": state["bms"].get("updated"),
            "soc_pct":     r.get("capacity_pct") if _reading_ok(r) else None,
            "error":       r.get("error"),
        })

    for r in state["victron"]["readings"]:
        entry = {
            "name":        r["name"],
            "address":     r["address"],
            "type":        r.get("device_type", "victron"),
            "online":      _reading_ok(r),
            "last_updated": state["victron"].get("updated"),
            "error":       r.get("error"),
        }
        if _reading_ok(r):
            if r.get("device_type") == "mppt":
                entry["pv_power_w"] = _fmt_v(r.get("pv_power_w"), 1)
            elif r.get("device_type") == "inverter":
                entry["ac_out_w"] = _fmt_v(r.get("ac_out_power_va"), 0)
                entry["state"]    = r.get("inverter_state")
        devices.append(entry)

    return {
        "devices": devices,
        "counts": {
            "total":   len(devices),
            "online":  sum(1 for d in devices if d["online"]),
            "offline": sum(1 for d in devices if not d["online"]),
        },
    }


def _count_alerts(state: dict) -> dict:
    """Count active alerts across the whole system."""
    faults  = []
    offline = []
    alarms  = []

    for r in state["bms"]["readings"]:
        if r.get("error"):
            offline.append({"name": r["name"], "type": "bms", "error": r["error"]})
        elif r.get("faults"):
            for f in r["faults"]:
                faults.append({"name": r["name"], "fault": f})

    for r in state["victron"]["readings"]:
        if r.get("error"):
            offline.append({"name": r["name"], "type": r.get("device_type", "victron"),
                            "error": r["error"]})
        alarm = r.get("alarm_reason")
        if alarm and alarm not in (None, 0, "None"):
            alarms.append({"name": r["name"], "alarm": alarm})

    return {"faults": faults, "offline": offline, "alarms": alarms}


def tool_get_alerts(state: dict) -> dict:
    """
    Active alerts across the entire system.

    Returns a prioritised view of anything requiring attention:
    offline devices, active battery protection faults, and inverter alarms.
    All-clear is explicitly indicated when no alerts are active.
    """
    result = _count_alerts(state)
    all_clear = (not result["faults"] and
                 not result["offline"] and
                 not result["alarms"])

    return {
        "all_clear":     all_clear,
        "offline":       result["offline"],
        "battery_faults": result["faults"],
        "inverter_alarms": result["alarms"],
        "summary": (
            "All systems nominal." if all_clear else
            "; ".join(filter(None, [
                f"{len(result['offline'])} device(s) offline" if result["offline"] else "",
                f"{len(result['faults'])} active fault(s)" if result["faults"] else "",
                f"{len(result['alarms'])} alarm(s)" if result["alarms"] else "",
            ]))
        ),
    }


def tool_get_data_age(state: dict) -> dict:
    """
    When was each data section last updated?

    Use this to assess data freshness before relying on readings.
    If 'updated' is null, no poll has completed since startup.
    """
    import datetime

    def _age(ts: Optional[str]) -> Optional[str]:
        if not ts:
            return None
        try:
            then = datetime.datetime.fromisoformat(ts)
            now  = datetime.datetime.now()
            secs = (now - then).total_seconds()
            if secs < 60:
                return f"{int(secs)}s ago"
            if secs < 3600:
                return f"{int(secs/60)}m ago"
            return f"{secs/3600:.1f}h ago"
        except (ValueError, TypeError):
            return None

    bms_updated = state["bms"].get("updated")
    vic_updated = state["victron"].get("updated")

    return {
        "bms": {
            "last_updated": bms_updated,
            "age":          _age(bms_updated),
            "readings":     len(state["bms"].get("readings", [])),
        },
        "victron": {
            "last_updated": vic_updated,
            "age":          _age(vic_updated),
            "readings":     len(state["victron"].get("readings", [])),
        },
        "stale": {
            "bms":     bms_updated is None,
            "victron": vic_updated is None,
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# Tool registry
# ─────────────────────────────────────────────────────────────────────────────

TOOLS = {
    "get_system_status": {
        "fn": tool_get_system_status,
        "description": "Get a high-level summary of the entire solar system: total PV power, AC output, average battery SoC, and active alerts.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    "get_battery_status": {
        "fn": tool_get_battery_status,
        "description": "Get detailed status of all JBD/Vatrer BMS battery packs: voltage, current, state of charge, energy remaining, time-to-empty/full, active faults, and balancing activity.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    "get_solar_status": {
        "fn": tool_get_solar_status,
        "description": "Get status of all Victron SmartSolar MPPT charge controllers: PV power input, energy harvested today, battery charging output, and charger state.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    "get_inverter_status": {
        "fn": tool_get_inverter_status,
        "description": "Get status of all Victron inverters and VE.Bus Smart Dongles (MultiPlus, etc.): AC output power, AC input source, device state, alarms, and battery readings.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    "get_device": {
        "fn": tool_get_device,
        "description": "Get all available data for a single device, identified by its display name (e.g. 'House Bank', 'South Array') or MAC address. Name matching is case-insensitive.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name_or_address": {
                    "type": "string",
                    "description": "Device display name (e.g. 'House Bank') or Bluetooth MAC address (e.g. 'AA:BB:CC:DD:EE:FF')",
                },
            },
            "required": ["name_or_address"],
        },
    },
    "list_devices": {
        "fn": tool_list_devices,
        "description": "List all configured devices with their type, online/offline status, and a key metric. Use this to discover available devices before querying specific ones.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    "get_alerts": {
        "fn": tool_get_alerts,
        "description": "Get all active alerts across the system: offline devices, battery protection faults (overvoltage, overcurrent, etc.), and inverter alarms. Returns 'all_clear: true' when no issues are detected.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    "get_data_age": {
        "fn": tool_get_data_age,
        "description": "Check how recently each data section was updated. Use before relying on readings to confirm data is fresh. Returns human-readable ages (e.g. '45s ago', '2m ago').",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# JSON-RPC 2.0 helpers
# ─────────────────────────────────────────────────────────────────────────────

def _ok(id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": id, "result": result}


def _err(id: Any, code: int, message: str, data: Any = None) -> dict:
    e: dict = {"code": code, "message": message}
    if data is not None:
        e["data"] = data
    return {"jsonrpc": "2.0", "id": id, "error": e}


# JSON-RPC error codes
_PARSE_ERROR      = -32700
_INVALID_REQUEST  = -32600
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS   = -32602
_INTERNAL_ERROR   = -32603
_RATE_LIMITED     = -32000   # application-defined
_UNAUTHORIZED     = -32001
_FORBIDDEN        = -32002


# ─────────────────────────────────────────────────────────────────────────────
# MCP protocol handlers
# ─────────────────────────────────────────────────────────────────────────────

def _handle_initialize(req: dict, cfg: McpConfig) -> dict:
    return _ok(req.get("id"), {
        "protocolVersion": "2024-11-05",
        "capabilities": {
            "tools": {"listChanged": False},
        },
        "serverInfo": {
            "name":    "solar-monitor",
            "version": "1.0.0",
        },
        "instructions": (
            "Solar Monitor MCP server. Provides real-time data from "
            "JBD/Vatrer BMS battery packs and Victron Energy devices. "
            "Start with get_system_status for an overview, or list_devices "
            "to see all available devices. Use get_alerts to check for "
            "any active faults or alarms."
            + (f" API key required: include 'api_key' in params." if cfg.api_key else "")
        ),
    })


def _handle_tools_list(req: dict, cfg: McpConfig) -> dict:
    tools_out = []
    for name, defn in TOOLS.items():
        if not cfg.tool_allowed(name):
            continue
        tools_out.append({
            "name":        name,
            "description": defn["description"],
            "inputSchema": defn["inputSchema"],
        })
    return _ok(req.get("id"), {"tools": tools_out})


def _handle_tools_call(req: dict, cfg: McpConfig, state_path: str,
                       limiter: RateLimiter) -> dict:
    req_id  = req.get("id")
    params  = req.get("params", {})
    name    = params.get("name", "")
    args    = params.get("arguments", {})

    # ── Security checks ────────────────────────────────────────────────────
    # API key check
    if not cfg.check_api_key(args.get("api_key")):
        log.warning(f"tools/call '{name}' rejected: bad or missing api_key")
        return _err(req_id, _UNAUTHORIZED, "Invalid or missing api_key")

    # Strip api_key from args before passing to tool
    clean_args = {k: v for k, v in args.items() if k != "api_key"}

    # Tool whitelist
    if not cfg.tool_allowed(name):
        log.warning(f"tools/call '{name}' rejected: not in allowed_tools")
        return _err(req_id, _FORBIDDEN,
                    f"Tool '{name}' is not enabled on this server",
                    {"allowed": cfg.allowed_tools})

    # Tool exists?
    if name not in TOOLS:
        return _err(req_id, _METHOD_NOT_FOUND, f"Unknown tool: '{name}'")

    # Rate limit
    if not limiter.allow():
        log.warning(f"tools/call '{name}' rejected: rate limit exceeded")
        return _err(req_id, _RATE_LIMITED,
                    f"Rate limit exceeded ({cfg.rate_limit} req/min). Retry shortly.")

    if cfg.log_requests:
        log.info(f"tools/call {name} args={list(clean_args.keys())}")

    # ── Execute tool ────────────────────────────────────────────────────────
    try:
        state  = _load(state_path)
        fn     = TOOLS[name]["fn"]
        # Tools that take extra args
        if clean_args:
            result = fn(state, **clean_args)
        else:
            result = fn(state)
    except TypeError as exc:
        return _err(req_id, _INVALID_PARAMS, f"Invalid arguments: {exc}")
    except Exception as exc:
        log.exception(f"Tool '{name}' raised an exception")
        return _err(req_id, _INTERNAL_ERROR, f"Tool execution failed: {exc}")

    return _ok(req_id, {
        "content": [{"type": "text", "text": json.dumps(result, indent=2)}]
    })


def _dispatch(raw: str, cfg: McpConfig, state_path: str,
              limiter: RateLimiter) -> Optional[dict]:
    """Parse and dispatch a single JSON-RPC request. Returns None for notifications."""
    try:
        req = json.loads(raw)
    except json.JSONDecodeError as exc:
        return _err(None, _PARSE_ERROR, f"Parse error: {exc}")

    if not isinstance(req, dict):
        return _err(None, _INVALID_REQUEST, "Request must be a JSON object")

    req_id  = req.get("id")   # None for notifications
    method  = req.get("method", "")
    is_notif = "id" not in req

    if method == "initialize":
        return _handle_initialize(req, cfg)
    if method == "notifications/initialized":
        return None   # notification, no response
    if method == "tools/list":
        return _handle_tools_list(req, cfg)
    if method == "tools/call":
        return _handle_tools_call(req, cfg, state_path, limiter)
    if method == "ping":
        return _ok(req_id, {})

    if is_notif:
        return None
    return _err(req_id, _METHOD_NOT_FOUND, f"Method not found: '{method}'")


# ─────────────────────────────────────────────────────────────────────────────
# Main stdio loop
# ─────────────────────────────────────────────────────────────────────────────

def run_stdio(cfg: McpConfig, state_path: str) -> None:
    """
    Read newline-delimited JSON-RPC messages from stdin, write responses
    to stdout. This is the standard MCP stdio transport.

    Each message is a single line of JSON. Responses are a single line of
    JSON followed by a newline. The loop exits when stdin is closed.
    """
    limiter = RateLimiter(cfg.rate_limit)

    log.info(
        f"Solar Monitor MCP server starting - state: {state_path}  "
        f"tools: {list(TOOLS.keys()) if not cfg.allowed_tools else cfg.allowed_tools}  "
        f"api_key: {'set' if cfg.api_key else 'none'}  "
        f"rate_limit: {cfg.rate_limit}/min"
    )

    out = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            response = _dispatch(line, cfg, state_path, limiter)
            if response is not None:
                out.write(json.dumps(response) + "\n")
                out.flush()
        except Exception as exc:
            log.exception(f"Unhandled error processing: {line[:80]}")
            try:
                out.write(json.dumps(
                    _err(None, _INTERNAL_ERROR, f"Internal error: {exc}")
                ) + "\n")
                out.flush()
            except Exception:
                pass

    log.info("stdin closed - MCP server exiting")


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Solar Monitor MCP Server - exposes solar data to AI assistants",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Claude Desktop config (~/.config/claude/claude_desktop_config.json):
  {
    "mcpServers": {
      "solar-monitor": {
        "command": "python3",
        "args": ["/path/to/mcp_server.py", "--config", "/path/to/config.ini"]
      }
    }
  }

config.ini [mcp] section:
  [mcp]
  enabled       = true
  api_key       = your-secret-key-here
  allowed_tools = get_system_status,get_alerts,list_devices
  rate_limit    = 60
  log_requests  = false
""",
    )
    parser.add_argument("--config",     metavar="FILE", default="config.ini",
                        help="Config file (reads [mcp] and state_file)")
    parser.add_argument("--state-file", metavar="FILE",
                        help="Shared state JSON file (overrides config)")
    parser.add_argument("--log-level",  metavar="LEVEL", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    logging.getLogger().setLevel(getattr(logging, args.log_level, logging.INFO))

    # Load config
    cfg        = McpConfig()
    state_path = "solar_state.json"

    config_path = Path(args.config)
    if config_path.exists():
        cfg        = _load_mcp_config(str(config_path))
        # Read state_file from [general] section
        import configparser
        p = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
        p.read(str(config_path))
        if "general" in p:
            state_path = p["general"].get("state_file", state_path)
    else:
        log.warning(f"Config file not found: {config_path} - using defaults")

    if args.state_file:
        state_path = args.state_file

    if not cfg.enabled:
        log.error("MCP server is disabled (set [mcp] enabled = true in config)")
        sys.exit(1)

    run_stdio(cfg, state_path)


if __name__ == "__main__":
    main()
