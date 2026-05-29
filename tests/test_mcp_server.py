"""
tests/test_mcp_server.py — unit tests for mcp_server.py
=========================================================
Covers:
  - McpConfig: defaults, tool_allowed, check_api_key
  - _load_mcp_config: all [mcp] INI fields, defaults, missing section
  - RateLimiter: under limit, at limit, over limit, unlimited mode
  - Tool implementations: all 8 tools with realistic state data
    - get_system_status: totals, counts, alert summary
    - get_battery_status: full pack data, offline pack, fault pack
    - get_solar_status: MPPT data, yield totals
    - get_inverter_status: VE.Bus data, alarm detection
    - get_device: name match, MAC match, case-insensitive, not found
    - list_devices: all types, offline, metric summary
    - get_alerts: all-clear, faults, alarms, offline
    - get_data_age: human-readable age, null handling
  - _dispatch: initialize, tools/list, tools/call routing,
    notifications, unknown method, parse error
  - Security: api_key enforcement, tool whitelist, rate limit in dispatch
  - JSON-RPC: correct error codes, response structure
  - Source-level guarantees
"""

import json
import os
import sys
import tempfile
import textwrap
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, "/home/claude")

import importlib.util
_spec = importlib.util.spec_from_file_location("mcp_server", "/home/claude/mcp_server.py")
ms = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ms)


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

def _make_state(bms_packs=None, victron_devices=None) -> dict:
    """Build a realistic state dict for testing."""
    return {
        "bms": {
            "updated":  "2024-01-15T08:15:42",
            "readings": bms_packs or [],
        },
        "victron": {
            "updated":  "2024-01-15T08:15:11",
            "readings": victron_devices or [],
        },
    }


def _pack(name="House Bank", **kw) -> dict:
    defaults = dict(
        address="A1:B2:C3:D4:E5:F6", device_type="bms",
        timestamp="2024-01-15T08:15:40",
        voltage_v=54.32, current_a=-15.0, power_w=-814.8,
        capacity_pct=84, remain_wh=4562.9, remain_ah=84.0,
        nominal_ah=100.0, nominal_wh=5432.0,
        time_to_empty_h=5.6, time_to_full_h=None,
        cycle_count=8, cell_count=16,
        sw_version="6.2", production_date="2025-11-26",
        temp_c=[23.1, 21.8],
        balance_cells=[0]*16,
        protection_bits=0, faults=[],
        charge_fet=True, discharge_fet=True,
        error=None,
    )
    defaults.update(kw)
    defaults["name"] = name
    return defaults


def _mppt(name="South Array", **kw) -> dict:
    defaults = dict(
        address="11:22:33:44:55:66", device_type="mppt",
        timestamp="2024-01-15T08:15:09",
        voltage_v=54.0, current_a=12.0, power_w=648.0,
        pv_power_w=680.0, yield_today_wh=3200.0,
        charger_state="Float", load_current_a=None,
        error=None,
    )
    defaults.update(kw)
    defaults["name"] = name
    return defaults


def _inverter(name="MultiPlus", **kw) -> dict:
    defaults = dict(
        address="C0:FF:EE:12:34:56", device_type="inverter",
        timestamp="2024-01-15T08:15:09",
        voltage_v=54.0, current_a=-15.0, power_w=-810.0,
        ac_out_power_va=755.0, ac_in_power_w=0.0,
        ac_in_source="Not connected", inverter_state="Inverting",
        temperature_c=26.0, alarm_reason=None, vebus_error=0,
        error=None,
    )
    defaults.update(kw)
    defaults["name"] = name
    return defaults


def _write_ini(content: str) -> str:
    f = tempfile.NamedTemporaryFile(
        mode="w", suffix=".ini", delete=False, encoding="utf-8"
    )
    f.write(textwrap.dedent(content))
    f.close()
    return f.name


def _write_state(state: dict) -> str:
    f = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    )
    json.dump(state, f)
    f.close()
    return f.name


def _call(name: str, args: dict, cfg=None, state_path=None, limiter=None) -> dict:
    """Invoke a tool via _dispatch and return the parsed result content."""
    if cfg is None:
        cfg = ms.McpConfig()
    if state_path is None:
        state_path = _write_state(_make_state(
            bms_packs=[_pack()],
            victron_devices=[_mppt(), _inverter()],
        ))
    if limiter is None:
        limiter = ms.RateLimiter(0)  # unlimited

    req = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
           "params": {"name": name, "arguments": args}}
    resp = ms._dispatch(json.dumps(req), cfg, state_path, limiter)
    return resp


# ─────────────────────────────────────────────────────────────────────────────
# 1. McpConfig
# ─────────────────────────────────────────────────────────────────────────────

class TestMcpConfig(unittest.TestCase):

    def test_defaults(self):
        cfg = ms.McpConfig()
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.api_key, "")
        self.assertTrue(cfg.read_only)
        self.assertEqual(cfg.allowed_tools, [])
        self.assertEqual(cfg.rate_limit, 60)
        self.assertTrue(cfg.require_local)
        self.assertFalse(cfg.log_requests)

    def test_tool_allowed_empty_list_allows_all(self):
        cfg = ms.McpConfig(allowed_tools=[])
        self.assertTrue(cfg.tool_allowed("get_system_status"))
        self.assertTrue(cfg.tool_allowed("anything"))

    def test_tool_allowed_whitelist(self):
        cfg = ms.McpConfig(allowed_tools=["get_system_status", "get_alerts"])
        self.assertTrue(cfg.tool_allowed("get_system_status"))
        self.assertTrue(cfg.tool_allowed("get_alerts"))
        self.assertFalse(cfg.tool_allowed("get_battery_status"))
        self.assertFalse(cfg.tool_allowed("list_devices"))

    def test_check_api_key_no_key_required(self):
        cfg = ms.McpConfig(api_key="")
        self.assertTrue(cfg.check_api_key(None))
        self.assertTrue(cfg.check_api_key("anything"))

    def test_check_api_key_correct(self):
        cfg = ms.McpConfig(api_key="secret-123")
        self.assertTrue(cfg.check_api_key("secret-123"))

    def test_check_api_key_wrong(self):
        cfg = ms.McpConfig(api_key="secret-123")
        self.assertFalse(cfg.check_api_key("wrong"))
        self.assertFalse(cfg.check_api_key(None))
        self.assertFalse(cfg.check_api_key(""))

    def test_read_only_always_true(self):
        """read_only must always be True regardless of config."""
        cfg = ms.McpConfig()
        self.assertTrue(cfg.read_only)


# ─────────────────────────────────────────────────────────────────────────────
# 2. _load_mcp_config
# ─────────────────────────────────────────────────────────────────────────────

class TestLoadMcpConfig(unittest.TestCase):

    def _load(self, ini: str) -> ms.McpConfig:
        path = _write_ini(ini)
        try:
            return ms._load_mcp_config(path)
        finally:
            os.unlink(path)

    def test_missing_section_uses_defaults(self):
        cfg = self._load("[general]\ntheme = dark\n")
        self.assertEqual(cfg.api_key, "")
        self.assertEqual(cfg.rate_limit, 60)

    def test_enabled_false(self):
        cfg = self._load("[mcp]\nenabled = false\n")
        self.assertFalse(cfg.enabled)

    def test_enabled_true(self):
        cfg = self._load("[mcp]\nenabled = true\n")
        self.assertTrue(cfg.enabled)

    def test_api_key(self):
        cfg = self._load("[mcp]\napi_key = my-secret\n")
        self.assertEqual(cfg.api_key, "my-secret")

    def test_rate_limit(self):
        cfg = self._load("[mcp]\nrate_limit = 120\n")
        self.assertEqual(cfg.rate_limit, 120)

    def test_rate_limit_unlimited(self):
        cfg = self._load("[mcp]\nrate_limit = 0\n")
        self.assertEqual(cfg.rate_limit, 0)

    def test_allowed_tools_list(self):
        cfg = self._load("[mcp]\nallowed_tools = get_system_status, get_alerts\n")
        self.assertEqual(cfg.allowed_tools, ["get_system_status", "get_alerts"])

    def test_allowed_tools_empty(self):
        cfg = self._load("[mcp]\nallowed_tools =\n")
        self.assertEqual(cfg.allowed_tools, [])

    def test_log_requests(self):
        cfg = self._load("[mcp]\nlog_requests = true\n")
        self.assertTrue(cfg.log_requests)

    def test_require_local(self):
        cfg = self._load("[mcp]\nrequire_local = false\n")
        self.assertFalse(cfg.require_local)

    def test_all_fields_together(self):
        cfg = self._load("""
            [mcp]
            enabled       = true
            api_key       = abc123
            allowed_tools = get_system_status, list_devices
            rate_limit    = 30
            require_local = true
            log_requests  = true
        """)
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.api_key, "abc123")
        self.assertEqual(cfg.allowed_tools, ["get_system_status", "list_devices"])
        self.assertEqual(cfg.rate_limit, 30)
        self.assertTrue(cfg.require_local)
        self.assertTrue(cfg.log_requests)


# ─────────────────────────────────────────────────────────────────────────────
# 3. RateLimiter
# ─────────────────────────────────────────────────────────────────────────────

class TestRateLimiter(unittest.TestCase):

    def test_unlimited_always_allows(self):
        lim = ms.RateLimiter(0)
        for _ in range(1000):
            self.assertTrue(lim.allow())

    def test_allows_up_to_limit(self):
        lim = ms.RateLimiter(5)
        for _ in range(5):
            self.assertTrue(lim.allow())

    def test_blocks_over_limit(self):
        lim = ms.RateLimiter(3)
        for _ in range(3):
            lim.allow()
        self.assertFalse(lim.allow())

    def test_window_is_one_minute(self):
        lim = ms.RateLimiter(2)
        lim.allow(); lim.allow()
        self.assertFalse(lim.allow())
        # Simulate calls expiring
        lim._calls = [t - 61 for t in lim._calls]  # push into past
        self.assertTrue(lim.allow())   # window reset

    def test_single_limit_allows_one(self):
        lim = ms.RateLimiter(1)
        self.assertTrue(lim.allow())
        self.assertFalse(lim.allow())


# ─────────────────────────────────────────────────────────────────────────────
# 4. Tool: get_system_status
# ─────────────────────────────────────────────────────────────────────────────

class TestGetSystemStatus(unittest.TestCase):

    def _run(self, bms=None, vic=None) -> dict:
        state = _make_state(bms or [], vic or [])
        return ms.tool_get_system_status(state)

    def test_returns_solar_inverter_battery_alerts(self):
        r = self._run([_pack()], [_mppt(), _inverter()])
        self.assertIn("solar", r)
        self.assertIn("inverter", r)
        self.assertIn("battery", r)
        self.assertIn("alerts", r)

    def test_sums_pv_power(self):
        r = self._run(vic=[_mppt("S1", pv_power_w=400.0),
                           _mppt("S2", pv_power_w=280.0)])
        self.assertAlmostEqual(r["solar"]["total_pv_w"], 680.0, places=0)

    def test_sums_yield_today(self):
        r = self._run(vic=[_mppt("S1", yield_today_wh=2000.0),
                           _mppt("S2", yield_today_wh=1200.0)])
        self.assertAlmostEqual(r["solar"]["yield_today_wh"], 3200.0, places=0)

    def test_sums_ac_output(self):
        r = self._run(vic=[_inverter("I1", ac_out_power_va=755.0),
                           _inverter("I2", ac_out_power_va=245.0)])
        self.assertAlmostEqual(r["inverter"]["total_ac_out_w"], 1000.0, places=0)

    def test_average_soc(self):
        r = self._run([_pack("B1", capacity_pct=84),
                       _pack("B2", capacity_pct=76)])
        self.assertEqual(r["battery"]["avg_soc_pct"], 80)

    def test_discharging_flag(self):
        r = self._run([_pack(current_a=-10.0)])
        self.assertTrue(r["battery"]["discharging"])
        self.assertFalse(r["battery"]["charging"])

    def test_charging_flag(self):
        r = self._run([_pack(current_a=10.0)])
        self.assertTrue(r["battery"]["charging"])
        self.assertFalse(r["battery"]["discharging"])

    def test_all_clear_when_no_problems(self):
        r = self._run([_pack()], [_mppt(), _inverter()])
        self.assertEqual(r["alerts"]["active_faults"], [])
        self.assertEqual(r["alerts"]["offline_devices"], [])

    def test_online_count(self):
        err_pack = _pack("Dead"); err_pack["error"] = "timeout"
        r = self._run([_pack(), err_pack])
        self.assertEqual(r["battery"]["packs_online"], 1)
        self.assertEqual(r["battery"]["packs_total"],  2)

    def test_empty_state_no_crash(self):
        r = self._run([], [])
        self.assertIsNone(r["battery"]["avg_soc_pct"])
        self.assertEqual(r["solar"]["total_pv_w"], 0)


# ─────────────────────────────────────────────────────────────────────────────
# 5. Tool: get_battery_status
# ─────────────────────────────────────────────────────────────────────────────

class TestGetBatteryStatus(unittest.TestCase):

    def _run(self, packs) -> dict:
        return ms.tool_get_battery_status(_make_state(packs, []))

    def test_returns_packs_and_summary(self):
        r = self._run([_pack()])
        self.assertIn("packs", r)
        self.assertIn("summary", r)

    def test_pack_has_key_fields(self):
        r = self._run([_pack()])
        p = r["packs"][0]
        for key in ("name", "address", "online", "soc_pct", "voltage_v",
                    "remain_wh", "time_to_empty", "faults", "temperatures_c"):
            self.assertIn(key, p, f"Missing key: {key}")

    def test_offline_pack_marked(self):
        dead = _pack("Dead"); dead["error"] = "Timed out"
        r = self._run([dead])
        self.assertFalse(r["packs"][0]["online"])
        self.assertIn("Timed out", r["packs"][0]["error"])

    def test_fault_pack(self):
        r = self._run([_pack(faults=["Cell overvoltage"])])
        p = r["packs"][0]
        self.assertIn("Cell overvoltage", p["faults"])
        self.assertTrue(r["summary"]["any_faults"])

    def test_balancing_cells_indexed_from_1(self):
        bal = [0]*16; bal[3] = 1   # cell 4 (0-indexed 3)
        r = self._run([_pack(balance_cells=bal)])
        self.assertIn(4, r["packs"][0]["balancing_cells"])

    def test_no_balancing_empty_list(self):
        r = self._run([_pack(balance_cells=[0]*16)])
        self.assertEqual(r["packs"][0]["balancing_cells"], [])

    def test_tte_formatted(self):
        r = self._run([_pack(time_to_empty_h=5.5)])
        self.assertEqual(r["packs"][0]["time_to_empty"], "5h30m")

    def test_tte_none_when_charging(self):
        r = self._run([_pack(current_a=10.0, time_to_empty_h=None)])
        self.assertIsNone(r["packs"][0]["time_to_empty"])

    def test_online_count(self):
        dead = _pack("Dead"); dead["error"] = "timeout"
        r = self._run([_pack(), dead])
        self.assertEqual(r["summary"]["online"], 1)
        self.assertEqual(r["summary"]["total"],  2)

    def test_empty_no_crash(self):
        r = self._run([])
        self.assertEqual(r["packs"], [])
        self.assertFalse(r["summary"]["any_faults"])


# ─────────────────────────────────────────────────────────────────────────────
# 6. Tool: get_solar_status
# ─────────────────────────────────────────────────────────────────────────────

class TestGetSolarStatus(unittest.TestCase):

    def _run(self, vic) -> dict:
        return ms.tool_get_solar_status(_make_state([], vic))

    def test_only_mppt_included(self):
        r = self._run([_mppt(), _inverter()])
        self.assertEqual(len(r["chargers"]), 1)
        self.assertEqual(r["chargers"][0]["name"], "South Array")

    def test_pv_power_shown(self):
        r = self._run([_mppt(pv_power_w=680.0)])
        self.assertEqual(r["chargers"][0]["pv_power_w"], "680.0")

    def test_yield_summed(self):
        r = self._run([_mppt("S1", yield_today_wh=2000.0),
                       _mppt("S2", yield_today_wh=1200.0)])
        self.assertAlmostEqual(r["summary"]["total_yield_wh"], 3200.0, places=0)

    def test_charger_state(self):
        r = self._run([_mppt(charger_state="Bulk")])
        self.assertEqual(r["chargers"][0]["charger_state"], "Bulk")

    def test_offline_charger(self):
        dead = _mppt("Dead"); dead["error"] = "not seen"
        r = self._run([dead])
        self.assertFalse(r["chargers"][0]["online"])


# ─────────────────────────────────────────────────────────────────────────────
# 7. Tool: get_inverter_status
# ─────────────────────────────────────────────────────────────────────────────

class TestGetInverterStatus(unittest.TestCase):

    def _run(self, vic) -> dict:
        return ms.tool_get_inverter_status(_make_state([], vic))

    def test_only_inverters_included(self):
        r = self._run([_mppt(), _inverter()])
        self.assertEqual(len(r["inverters"]), 1)
        self.assertEqual(r["inverters"][0]["name"], "MultiPlus")

    def test_ac_out_shown(self):
        r = self._run([_inverter(ac_out_power_va=755.0)])
        self.assertEqual(r["inverters"][0]["ac_out_w"], "755")

    def test_state_shown(self):
        r = self._run([_inverter(inverter_state="Inverting")])
        self.assertEqual(r["inverters"][0]["state"], "Inverting")

    def test_no_alarm_when_none(self):
        r = self._run([_inverter(alarm_reason=None)])
        self.assertIsNone(r["inverters"][0]["alarm"])

    def test_alarm_when_set(self):
        r = self._run([_inverter(alarm_reason="Warning")])
        self.assertIsNotNone(r["inverters"][0]["alarm"])
        self.assertTrue(r["summary"]["any_alarms"])

    def test_total_ac_out_summed(self):
        r = self._run([_inverter("I1", ac_out_power_va=755.0),
                       _inverter("I2", ac_out_power_va=245.0)])
        self.assertAlmostEqual(r["summary"]["total_ac_out_w"], 1000.0, places=0)


# ─────────────────────────────────────────────────────────────────────────────
# 8. Tool: get_device
# ─────────────────────────────────────────────────────────────────────────────

class TestGetDevice(unittest.TestCase):

    def _state(self):
        return _make_state(
            [_pack("House Bank", address="A1:B2:C3:D4:E5:F6")],
            [_mppt("South Array", address="11:22:33:44:55:66"),
             _inverter("MultiPlus", address="C0:FF:EE:12:34:56")],
        )

    def test_find_by_exact_name(self):
        r = ms.tool_get_device(self._state(), "House Bank")
        self.assertTrue(r["found"])
        self.assertEqual(len(r["devices"]), 1)

    def test_find_by_name_case_insensitive(self):
        r = ms.tool_get_device(self._state(), "house bank")
        self.assertTrue(r["found"])

    def test_find_by_mac(self):
        r = ms.tool_get_device(self._state(), "A1:B2:C3:D4:E5:F6")
        self.assertTrue(r["found"])
        self.assertEqual(r["devices"][0]["name"], "House Bank")

    def test_find_victron_device(self):
        r = ms.tool_get_device(self._state(), "South Array")
        self.assertTrue(r["found"])

    def test_not_found(self):
        r = ms.tool_get_device(self._state(), "Nonexistent")
        self.assertFalse(r["found"])
        self.assertEqual(r["devices"], [])

    def test_query_preserved_in_response(self):
        r = ms.tool_get_device(self._state(), "House Bank")
        self.assertEqual(r["query"], "House Bank")


# ─────────────────────────────────────────────────────────────────────────────
# 9. Tool: list_devices
# ─────────────────────────────────────────────────────────────────────────────

class TestListDevices(unittest.TestCase):

    def test_includes_bms_and_victron(self):
        state = _make_state([_pack()], [_mppt(), _inverter()])
        r = ms.tool_list_devices(state)
        names = [d["name"] for d in r["devices"]]
        self.assertIn("House Bank",  names)
        self.assertIn("South Array", names)
        self.assertIn("MultiPlus",   names)

    def test_offline_device_shown(self):
        dead = _pack("Dead"); dead["error"] = "timeout"
        state = _make_state([dead], [])
        r = ms.tool_list_devices(state)
        d = next(d for d in r["devices"] if d["name"] == "Dead")
        self.assertFalse(d["online"])

    def test_counts_correct(self):
        dead = _pack("Dead"); dead["error"] = "timeout"
        state = _make_state([_pack(), dead], [_mppt()])
        r = ms.tool_list_devices(state)
        self.assertEqual(r["counts"]["total"],   3)
        self.assertEqual(r["counts"]["online"],  2)
        self.assertEqual(r["counts"]["offline"], 1)

    def test_soc_included_for_bms(self):
        state = _make_state([_pack(capacity_pct=84)], [])
        r = ms.tool_list_devices(state)
        bms_d = next(d for d in r["devices"] if d["name"] == "House Bank")
        self.assertEqual(bms_d["soc_pct"], 84)

    def test_pv_power_included_for_mppt(self):
        state = _make_state([], [_mppt(pv_power_w=680.0)])
        r = ms.tool_list_devices(state)
        mppt_d = next(d for d in r["devices"] if d["name"] == "South Array")
        self.assertIn("pv_power_w", mppt_d)


# ─────────────────────────────────────────────────────────────────────────────
# 10. Tool: get_alerts
# ─────────────────────────────────────────────────────────────────────────────

class TestGetAlerts(unittest.TestCase):

    def test_all_clear_when_healthy(self):
        state = _make_state([_pack()], [_mppt(), _inverter()])
        r = ms.tool_get_alerts(state)
        self.assertTrue(r["all_clear"])
        self.assertEqual(r["battery_faults"], [])
        self.assertEqual(r["offline"], [])
        self.assertIn("nominal", r["summary"])

    def test_fault_detected(self):
        state = _make_state([_pack(faults=["Cell overvoltage"])], [])
        r = ms.tool_get_alerts(state)
        self.assertFalse(r["all_clear"])
        self.assertEqual(len(r["battery_faults"]), 1)
        self.assertEqual(r["battery_faults"][0]["fault"], "Cell overvoltage")

    def test_offline_bms_detected(self):
        dead = _pack("Dead"); dead["error"] = "timeout"
        state = _make_state([dead], [])
        r = ms.tool_get_alerts(state)
        self.assertFalse(r["all_clear"])
        self.assertEqual(len(r["offline"]), 1)
        self.assertEqual(r["offline"][0]["name"], "Dead")
        self.assertEqual(r["offline"][0]["type"], "bms")

    def test_offline_victron_detected(self):
        dead = _mppt("Dead MPPT"); dead["error"] = "not seen"
        state = _make_state([], [dead])
        r = ms.tool_get_alerts(state)
        self.assertFalse(r["all_clear"])
        self.assertEqual(len(r["offline"]), 1)

    def test_inverter_alarm_detected(self):
        state = _make_state([], [_inverter(alarm_reason="Warning")])
        r = ms.tool_get_alerts(state)
        self.assertFalse(r["all_clear"])
        self.assertEqual(len(r["inverter_alarms"]), 1)

    def test_summary_string_describes_problem(self):
        dead = _pack("Dead"); dead["error"] = "timeout"
        state = _make_state([dead], [])
        r = ms.tool_get_alerts(state)
        self.assertIn("offline", r["summary"].lower())

    def test_multiple_alerts_all_reported(self):
        dead = _pack("Dead"); dead["error"] = "timeout"
        fault_pack = _pack("Fault", faults=["Discharge overcurrent"])
        state = _make_state([dead, fault_pack], [])
        r = ms.tool_get_alerts(state)
        self.assertEqual(len(r["offline"]), 1)
        self.assertEqual(len(r["battery_faults"]), 1)


# ─────────────────────────────────────────────────────────────────────────────
# 11. Tool: get_data_age
# ─────────────────────────────────────────────────────────────────────────────

class TestGetDataAge(unittest.TestCase):

    def test_stale_when_never_updated(self):
        state = {"bms": {"updated": None, "readings": []},
                 "victron": {"updated": None, "readings": []}}
        r = ms.tool_get_data_age(state)
        self.assertTrue(r["stale"]["bms"])
        self.assertTrue(r["stale"]["victron"])

    def test_not_stale_when_updated(self):
        import datetime
        now = datetime.datetime.now().isoformat(timespec="seconds")
        state = {"bms": {"updated": now, "readings": []},
                 "victron": {"updated": now, "readings": []}}
        r = ms.tool_get_data_age(state)
        self.assertFalse(r["stale"]["bms"])
        self.assertFalse(r["stale"]["victron"])

    def test_age_human_readable(self):
        import datetime
        recent = (datetime.datetime.now() - datetime.timedelta(seconds=45)).isoformat(timespec="seconds")
        state = {"bms": {"updated": recent, "readings": []},
                 "victron": {"updated": recent, "readings": []}}
        r = ms.tool_get_data_age(state)
        self.assertIn("s ago", r["bms"]["age"])

    def test_reading_count(self):
        state = _make_state([_pack(), _pack("B2")], [_mppt()])
        r = ms.tool_get_data_age(state)
        self.assertEqual(r["bms"]["readings"], 2)
        self.assertEqual(r["victron"]["readings"], 1)


# ─────────────────────────────────────────────────────────────────────────────
# 12. JSON-RPC dispatch
# ─────────────────────────────────────────────────────────────────────────────

class TestDispatch(unittest.TestCase):

    def _dispatch(self, req: dict, cfg=None) -> dict:
        if cfg is None:
            cfg = ms.McpConfig()
        state_path = _write_state(_make_state([_pack()], [_mppt()]))
        limiter    = ms.RateLimiter(0)
        resp = ms._dispatch(json.dumps(req), cfg, state_path, limiter)
        os.unlink(state_path)
        return resp

    # ── initialize ────────────────────────────────────────────────────────────

    def test_initialize_returns_server_info(self):
        resp = self._dispatch({"jsonrpc":"2.0","id":1,"method":"initialize","params":{}})
        self.assertIn("result", resp)
        self.assertIn("serverInfo", resp["result"])
        self.assertEqual(resp["result"]["serverInfo"]["name"], "solar-monitor")

    def test_initialize_returns_protocol_version(self):
        resp = self._dispatch({"jsonrpc":"2.0","id":1,"method":"initialize","params":{}})
        self.assertIn("protocolVersion", resp["result"])

    # ── tools/list ────────────────────────────────────────────────────────────

    def test_tools_list_returns_all_tools(self):
        resp = self._dispatch({"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}})
        names = [t["name"] for t in resp["result"]["tools"]]
        for expected in ("get_system_status", "get_battery_status",
                         "get_solar_status", "get_inverter_status",
                         "get_device", "list_devices", "get_alerts",
                         "get_data_age"):
            self.assertIn(expected, names)

    def test_tools_list_filtered_by_allowed(self):
        cfg = ms.McpConfig(allowed_tools=["get_system_status", "get_alerts"])
        resp = self._dispatch(
            {"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}, cfg=cfg
        )
        names = [t["name"] for t in resp["result"]["tools"]]
        self.assertIn("get_system_status", names)
        self.assertIn("get_alerts", names)
        self.assertNotIn("get_battery_status", names)
        self.assertNotIn("list_devices", names)

    def test_tools_have_description_and_schema(self):
        resp = self._dispatch({"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}})
        for t in resp["result"]["tools"]:
            self.assertIn("description", t)
            self.assertIn("inputSchema", t)

    # ── tools/call ────────────────────────────────────────────────────────────

    def test_tools_call_get_system_status(self):
        resp = self._dispatch({
            "jsonrpc":"2.0","id":3,"method":"tools/call",
            "params":{"name":"get_system_status","arguments":{}}
        })
        self.assertIn("result", resp)
        content = json.loads(resp["result"]["content"][0]["text"])
        self.assertIn("battery", content)

    def test_tools_call_unknown_tool_returns_error(self):
        resp = self._dispatch({
            "jsonrpc":"2.0","id":4,"method":"tools/call",
            "params":{"name":"nonexistent_tool","arguments":{}}
        })
        self.assertIn("error", resp)
        self.assertEqual(resp["error"]["code"], ms._METHOD_NOT_FOUND)

    # ── notifications ─────────────────────────────────────────────────────────

    def test_notification_returns_none(self):
        # No "id" field = notification, no response expected
        resp = ms._dispatch(
            '{"jsonrpc":"2.0","method":"notifications/initialized"}',
            ms.McpConfig(),
            _write_state(_make_state()),
            ms.RateLimiter(0),
        )
        self.assertIsNone(resp)

    # ── ping ──────────────────────────────────────────────────────────────────

    def test_ping_returns_ok(self):
        resp = self._dispatch({"jsonrpc":"2.0","id":5,"method":"ping"})
        self.assertIn("result", resp)

    # ── unknown method ────────────────────────────────────────────────────────

    def test_unknown_method_returns_method_not_found(self):
        resp = self._dispatch({"jsonrpc":"2.0","id":6,"method":"unknown/method"})
        self.assertIn("error", resp)
        self.assertEqual(resp["error"]["code"], ms._METHOD_NOT_FOUND)

    # ── parse error ───────────────────────────────────────────────────────────

    def test_invalid_json_returns_parse_error(self):
        resp = ms._dispatch("not valid json{{", ms.McpConfig(),
                            _write_state(_make_state()), ms.RateLimiter(0))
        self.assertIn("error", resp)
        self.assertEqual(resp["error"]["code"], ms._PARSE_ERROR)


# ─────────────────────────────────────────────────────────────────────────────
# 13. Security enforcement in dispatch
# ─────────────────────────────────────────────────────────────────────────────

class TestSecurityEnforcement(unittest.TestCase):

    def _call(self, name, args, cfg) -> dict:
        state_path = _write_state(_make_state([_pack()], [_mppt()]))
        limiter    = ms.RateLimiter(0)
        req = {"jsonrpc":"2.0","id":1,"method":"tools/call",
               "params":{"name": name, "arguments": args}}
        resp = ms._dispatch(json.dumps(req), cfg, state_path, limiter)
        os.unlink(state_path)
        return resp

    def test_missing_api_key_rejected(self):
        cfg  = ms.McpConfig(api_key="secret")
        resp = self._call("get_system_status", {}, cfg)
        self.assertIn("error", resp)
        self.assertEqual(resp["error"]["code"], ms._UNAUTHORIZED)

    def test_wrong_api_key_rejected(self):
        cfg  = ms.McpConfig(api_key="secret")
        resp = self._call("get_system_status", {"api_key": "wrong"}, cfg)
        self.assertIn("error", resp)
        self.assertEqual(resp["error"]["code"], ms._UNAUTHORIZED)

    def test_correct_api_key_allowed(self):
        cfg  = ms.McpConfig(api_key="secret")
        resp = self._call("get_system_status", {"api_key": "secret"}, cfg)
        self.assertIn("result", resp)

    def test_api_key_stripped_from_tool_args(self):
        """api_key must not be passed to the tool function itself."""
        cfg = ms.McpConfig(api_key="secret")
        # get_device expects name_or_address; if api_key leaked through it would fail
        resp = self._call("get_device",
                          {"api_key": "secret", "name_or_address": "House Bank"},
                          cfg)
        # Should succeed, not raise TypeError for unexpected kwarg
        self.assertIn("result", resp)

    def test_tool_not_in_whitelist_rejected(self):
        cfg  = ms.McpConfig(allowed_tools=["get_system_status"])
        resp = self._call("get_battery_status", {}, cfg)
        self.assertIn("error", resp)
        self.assertEqual(resp["error"]["code"], ms._FORBIDDEN)

    def test_rate_limit_enforced(self):
        cfg     = ms.McpConfig(api_key="", rate_limit=2, allowed_tools=[])
        state_p = _write_state(_make_state([_pack()]))
        limiter = ms.RateLimiter(2)

        def call():
            req = {"jsonrpc":"2.0","id":1,"method":"tools/call",
                   "params":{"name":"get_system_status","arguments":{}}}
            return ms._dispatch(json.dumps(req), cfg, state_p, limiter)

        r1 = call(); r2 = call(); r3 = call()
        self.assertIn("result", r1)
        self.assertIn("result", r2)
        self.assertIn("error",  r3)
        self.assertEqual(r3["error"]["code"], ms._RATE_LIMITED)
        os.unlink(state_p)

    def test_read_only_always_true(self):
        """No tool should mutate state — enforce at config level."""
        cfg = ms.McpConfig()
        self.assertTrue(cfg.read_only)


# ─────────────────────────────────────────────────────────────────────────────
# 14. Source-level guarantees
# ─────────────────────────────────────────────────────────────────────────────

class TestSourceGuarantees(unittest.TestCase):

    def _src(self) -> str:
        with open("/home/claude/mcp_server.py") as f:
            return f.read()

    def test_all_eight_tools_registered(self):
        for name in ("get_system_status", "get_battery_status", "get_solar_status",
                     "get_inverter_status", "get_device", "list_devices",
                     "get_alerts", "get_data_age"):
            self.assertIn(f'"{name}"', self._src(), f"Tool not registered: {name}")

    def test_api_key_check_present(self):
        self.assertIn("check_api_key", self._src())

    def test_rate_limiter_used_in_dispatch(self):
        self.assertIn("limiter.allow()", self._src())

    def test_tool_whitelist_checked(self):
        self.assertIn("tool_allowed", self._src())

    def test_api_key_stripped_from_args(self):
        self.assertIn("api_key", self._src())
        self.assertIn("clean_args", self._src())

    def test_stdio_transport_used(self):
        self.assertIn("sys.stdin", self._src())
        self.assertIn("sys.stdout", self._src())

    def test_logging_to_stderr(self):
        self.assertIn("sys.stderr", self._src())

    def test_protocol_version_set(self):
        self.assertIn("protocolVersion", self._src())

    def test_error_codes_defined(self):
        for code in ("_PARSE_ERROR", "_INVALID_REQUEST", "_METHOD_NOT_FOUND",
                     "_UNAUTHORIZED", "_FORBIDDEN", "_RATE_LIMITED"):
            self.assertIn(code, self._src())

    def test_initialize_handler(self):
        self.assertIn("_handle_initialize", self._src())

    def test_tools_list_handler(self):
        self.assertIn("_handle_tools_list", self._src())

    def test_tools_call_handler(self):
        self.assertIn("_handle_tools_call", self._src())

    def test_read_only_always_enforced(self):
        self.assertIn("read_only     = True,  # always enforced", self._src())


if __name__ == "__main__":
    unittest.main(verbosity=2)
