"""
tests/test_split_process.py — Unit tests for split-process architecture
=======================================================================
Covers:
  - state.py: save_section, load_state, atomicity, cross-process isolation,
    error handling, serialisation round-trips for all field types
  - config.py: new bms_interval, victron_interval, state_file fields,
    INI parsing, CLI override, defaults
  - scanner.py: _poll_bms and _poll_victron are importable and separate
"""

import json
import os
import sys
import tempfile
import textwrap
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# ── Stub bleak ────────────────────────────────────────────────────────────────
bleak = types.ModuleType("bleak")
backends = types.ModuleType("bleak.backends")
device_mod = types.ModuleType("bleak.backends.device")


class BLEDevice:
    def __init__(self, address="AA:BB:CC:DD:EE:FF", name="test"):
        self.address = address
        self.name = name


class BleakClient:
    def __init__(self, *a, **kw): pass


class BleakScanner:
    pass


device_mod.BLEDevice = BLEDevice
bleak.BleakClient = BleakClient
bleak.BleakScanner = BleakScanner
sys.modules.update({
    "bleak": bleak,
    "bleak.backends": backends,
    "bleak.backends.device": device_mod,
})
sys.path.insert(0, "/home/claude")

from solar_monitor.models import DeviceReading
from solar_monitor.state import (
    load_state, save_section, _reading_to_dict, _dict_to_reading, _LIST_FIELDS
)
from solar_monitor.config import (
    AppConfig, load_config, parse_mac_key, parse_bms_value, normalise_mac,
)
from solar_monitor.dashboard import _soc_color, _no_card


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _tmp() -> str:
    """Return a path to a temp file that does not yet exist."""
    f = tempfile.NamedTemporaryFile(suffix=".json", delete=True)
    path = f.name
    f.close()
    # File is deleted by close; we just want the path
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    return path


def _bms_reading(**kwargs) -> DeviceReading:
    defaults = dict(
        address="AA:BB:CC:DD:EE:FF", name="House Bank",
        device_type="bms", timestamp="2024-01-01T12:00:00",
        voltage_v=54.32, current_a=-15.0, power_w=-814.8,
        capacity_pct=84, remain_ah=84.0, nominal_ah=100.0,
        remain_wh=4562.9, nominal_wh=5432.0,
        time_to_empty_h=5.6, time_to_full_h=None,
        cycle_count=8, cell_count=16,
        sw_version="6.2", production_date="2025-11-26",
        balance_cells=[0]*16, protection_bits=0, faults=[],
        charge_fet=True, discharge_fet=True,
        temp_c=[23.1, 21.8, 21.9],
    )
    defaults.update(kwargs)
    r = DeviceReading(
        address=defaults.pop("address"), name=defaults.pop("name"),
        device_type=defaults.pop("device_type"), timestamp=defaults.pop("timestamp"),
    )
    for k, v in defaults.items():
        if hasattr(r, k):
            setattr(r, k, v)
    return r


def _victron_reading(**kwargs) -> DeviceReading:
    defaults = dict(
        address="E6:2E:31:75:9A:1A", name="Multiplus-Ii",
        device_type="inverter", timestamp="2024-01-01T12:00:00",
        voltage_v=54.0, current_a=-15.0, power_w=-810.0,
        ac_out_power_va=755.0, ac_in_power_w=0.0,
        ac_in_source="Not connected", inverter_state="Inverting",
        temperature_c=26.0, alarm_reason=None, vebus_error=0,
        faults=[], temp_c=[],
    )
    defaults.update(kwargs)
    r = DeviceReading(
        address=defaults.pop("address"), name=defaults.pop("name"),
        device_type=defaults.pop("device_type"), timestamp=defaults.pop("timestamp"),
    )
    for k, v in defaults.items():
        if hasattr(r, k):
            setattr(r, k, v)
    return r


# ─────────────────────────────────────────────────────────────────────────────
# 1. _reading_to_dict / _dict_to_reading round-trip
# ─────────────────────────────────────────────────────────────────────────────

class TestReadingSerialisation(unittest.TestCase):

    def test_bms_roundtrip_scalars(self):
        r = _bms_reading()
        d = _reading_to_dict(r)
        r2 = _dict_to_reading(d)
        self.assertAlmostEqual(r2.voltage_v,     r.voltage_v,     places=3)
        self.assertAlmostEqual(r2.current_a,     r.current_a,     places=3)
        self.assertEqual(r2.capacity_pct,        r.capacity_pct)
        self.assertAlmostEqual(r2.remain_ah,     r.remain_ah,     places=2)
        self.assertAlmostEqual(r2.remain_wh,     r.remain_wh,     places=1)
        self.assertEqual(r2.cycle_count,         r.cycle_count)
        self.assertEqual(r2.sw_version,          r.sw_version)
        self.assertEqual(r2.production_date,     r.production_date)
        self.assertTrue(r2.charge_fet)
        self.assertTrue(r2.discharge_fet)

    def test_bms_roundtrip_lists(self):
        r = _bms_reading()
        r2 = _dict_to_reading(_reading_to_dict(r))
        self.assertEqual(r2.temp_c,        r.temp_c)
        self.assertEqual(r2.faults,        r.faults)
        self.assertEqual(r2.balance_cells, r.balance_cells)

    def test_list_fields_none_converted_to_empty_list(self):
        """Stored None for list fields must be returned as [] not None."""
        d = _reading_to_dict(_bms_reading())
        for lf in _LIST_FIELDS:
            d[lf] = None          # simulate corrupt/old state file
        r = _dict_to_reading(d)
        for lf in _LIST_FIELDS:
            self.assertEqual(getattr(r, lf), [], msg=f"{lf} should be []")

    def test_victron_roundtrip(self):
        r = _victron_reading()
        r2 = _dict_to_reading(_reading_to_dict(r))
        self.assertAlmostEqual(r2.voltage_v,       r.voltage_v,      places=2)
        self.assertEqual(r2.ac_out_power_va,        r.ac_out_power_va)
        self.assertEqual(r2.inverter_state,         r.inverter_state)
        self.assertEqual(r2.ac_in_source,           r.ac_in_source)
        self.assertAlmostEqual(r2.temperature_c,    r.temperature_c,  places=1)

    def test_unknown_keys_ignored(self):
        """Extra keys in the dict (from future schema) must not raise."""
        d = _reading_to_dict(_bms_reading())
        d["future_field_xyz"] = "something"
        r = _dict_to_reading(d)            # must not raise
        self.assertIsInstance(r, DeviceReading)

    def test_error_field_preserved(self):
        r = _bms_reading()
        r.error = "Connection refused"
        r2 = _dict_to_reading(_reading_to_dict(r))
        self.assertEqual(r2.error, "Connection refused")

    def test_none_optional_fields(self):
        """None optionals must survive the round-trip as None."""
        r = _bms_reading(time_to_full_h=None, capacity_pct=None)
        r2 = _dict_to_reading(_reading_to_dict(r))
        self.assertIsNone(r2.time_to_full_h)
        self.assertIsNone(r2.capacity_pct)

    def test_negative_current_preserved(self):
        r = _bms_reading(current_a=-19.5)
        r2 = _dict_to_reading(_reading_to_dict(r))
        self.assertAlmostEqual(r2.current_a, -19.5, places=2)

    def test_faults_list_preserved(self):
        r = _bms_reading(faults=["Cell overvoltage", "Short circuit"])
        r2 = _dict_to_reading(_reading_to_dict(r))
        self.assertEqual(r2.faults, ["Cell overvoltage", "Short circuit"])

    def test_balance_cells_with_active_cell(self):
        bal = [0]*16
        bal[3] = 1
        r = _bms_reading(balance_cells=bal)
        r2 = _dict_to_reading(_reading_to_dict(r))
        self.assertEqual(r2.balance_cells[3], 1)
        self.assertEqual(sum(r2.balance_cells), 1)


# ─────────────────────────────────────────────────────────────────────────────
# 2. save_section
# ─────────────────────────────────────────────────────────────────────────────

class TestSaveSection(unittest.TestCase):

    def setUp(self):
        self.path = _tmp()

    def tearDown(self):
        for p in (self.path, self.path + ".tmp"):
            try:
                os.unlink(p)
            except FileNotFoundError:
                pass

    def test_creates_file_when_missing(self):
        self.assertFalse(Path(self.path).exists())
        save_section(self.path, "bms", [_bms_reading()])
        self.assertTrue(Path(self.path).exists())

    def test_file_is_valid_json(self):
        save_section(self.path, "bms", [_bms_reading()])
        text = Path(self.path).read_text()
        raw  = json.loads(text)       # must not raise
        self.assertIn("bms", raw)

    def test_updated_timestamp_written(self):
        save_section(self.path, "bms", [_bms_reading()])
        raw = json.loads(Path(self.path).read_text())
        self.assertIsNotNone(raw["bms"]["updated"])
        self.assertRegex(raw["bms"]["updated"], r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")

    def test_empty_readings_list(self):
        save_section(self.path, "bms", [])
        raw = json.loads(Path(self.path).read_text())
        self.assertEqual(raw["bms"]["readings"], [])

    def test_multiple_readings(self):
        readings = [_bms_reading(name="Batt1"), _bms_reading(name="Batt2")]
        save_section(self.path, "bms", readings)
        raw = json.loads(Path(self.path).read_text())
        self.assertEqual(len(raw["bms"]["readings"]), 2)

    def test_cross_section_isolation_bms_does_not_clobber_victron(self):
        """Writing BMS must not erase a previously written Victron section."""
        save_section(self.path, "victron", [_victron_reading()])
        save_section(self.path, "bms",     [_bms_reading()])
        raw = json.loads(Path(self.path).read_text())
        self.assertIn("bms",     raw)
        self.assertIn("victron", raw)
        self.assertEqual(len(raw["victron"]["readings"]), 1)
        self.assertEqual(len(raw["bms"]["readings"]),     1)

    def test_cross_section_isolation_victron_does_not_clobber_bms(self):
        save_section(self.path, "bms",     [_bms_reading()])
        save_section(self.path, "victron", [_victron_reading()])
        raw = json.loads(Path(self.path).read_text())
        self.assertEqual(len(raw["bms"]["readings"]),     1)
        self.assertEqual(len(raw["victron"]["readings"]), 1)

    def test_overwrite_own_section(self):
        """Second write to same section must replace, not append."""
        save_section(self.path, "bms", [_bms_reading(name="Batt1")])
        save_section(self.path, "bms", [_bms_reading(name="Batt2"),
                                         _bms_reading(name="Batt3")])
        raw = json.loads(Path(self.path).read_text())
        self.assertEqual(len(raw["bms"]["readings"]), 2)
        names = [r["name"] for r in raw["bms"]["readings"]]
        self.assertNotIn("Batt1", names)

    def test_tmp_file_removed_after_success(self):
        save_section(self.path, "bms", [_bms_reading()])
        self.assertFalse(Path(self.path + ".tmp").exists())

    def test_invalid_section_raises(self):
        with self.assertRaises(AssertionError):
            save_section(self.path, "invalid_section", [])

    def test_corrupt_existing_file_is_overwritten(self):
        """Corrupt state file must not prevent a write — start fresh."""
        Path(self.path).write_text("NOT VALID JSON", encoding="utf-8")
        save_section(self.path, "bms", [_bms_reading()])
        raw = json.loads(Path(self.path).read_text())
        self.assertIn("bms", raw)

    def test_reading_data_values_correct(self):
        r = _bms_reading(voltage_v=54.32, capacity_pct=84)
        save_section(self.path, "bms", [r])
        raw = json.loads(Path(self.path).read_text())
        stored = raw["bms"]["readings"][0]
        self.assertAlmostEqual(stored["voltage_v"],  54.32, places=2)
        self.assertEqual(stored["capacity_pct"], 84)


# ─────────────────────────────────────────────────────────────────────────────
# 3. load_state
# ─────────────────────────────────────────────────────────────────────────────

class TestLoadState(unittest.TestCase):

    def setUp(self):
        self.path = _tmp()

    def tearDown(self):
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass

    def test_missing_file_returns_empty(self):
        state = load_state(self.path)
        self.assertEqual(state["bms"]["readings"],     [])
        self.assertEqual(state["victron"]["readings"], [])
        self.assertIsNone(state["bms"]["updated"])
        self.assertIsNone(state["victron"]["updated"])

    def test_corrupt_file_returns_empty(self):
        Path(self.path).write_text("GARBAGE", encoding="utf-8")
        state = load_state(self.path)
        self.assertEqual(state["bms"]["readings"],     [])
        self.assertEqual(state["victron"]["readings"], [])

    def test_empty_json_object_returns_empty(self):
        Path(self.path).write_text("{}", encoding="utf-8")
        state = load_state(self.path)
        self.assertEqual(state["bms"]["readings"],     [])
        self.assertEqual(state["victron"]["readings"], [])

    def test_bms_readings_deserialised(self):
        save_section(self.path, "bms", [_bms_reading(voltage_v=54.32)])
        state = load_state(self.path)
        self.assertEqual(len(state["bms"]["readings"]), 1)
        r = state["bms"]["readings"][0]
        self.assertIsInstance(r, DeviceReading)
        self.assertAlmostEqual(r.voltage_v, 54.32, places=2)

    def test_victron_readings_deserialised(self):
        save_section(self.path, "victron", [_victron_reading()])
        state = load_state(self.path)
        self.assertEqual(len(state["victron"]["readings"]), 1)
        r = state["victron"]["readings"][0]
        self.assertEqual(r.device_type, "inverter")

    def test_updated_timestamp_returned(self):
        save_section(self.path, "bms", [_bms_reading()])
        state = load_state(self.path)
        self.assertIsNotNone(state["bms"]["updated"])

    def test_both_sections_loaded(self):
        save_section(self.path, "bms",     [_bms_reading()])
        save_section(self.path, "victron", [_victron_reading()])
        state = load_state(self.path)
        self.assertEqual(len(state["bms"]["readings"]),     1)
        self.assertEqual(len(state["victron"]["readings"]), 1)

    def test_corrupt_single_reading_skipped(self):
        """One corrupt reading must not discard the whole section."""
        # Write a valid file then corrupt one reading manually
        save_section(self.path, "bms", [_bms_reading(name="Good")])
        raw = json.loads(Path(self.path).read_text())
        raw["bms"]["readings"].insert(0, {"not_valid": True})  # corrupt entry
        Path(self.path).write_text(json.dumps(raw), encoding="utf-8")
        state = load_state(self.path)
        # Only the good reading should survive
        self.assertEqual(len(state["bms"]["readings"]), 1)
        self.assertEqual(state["bms"]["readings"][0].name, "Good")

    def test_multiple_readings_all_deserialised(self):
        readings = [_bms_reading(name=f"Batt{i}") for i in range(3)]
        save_section(self.path, "bms", readings)
        state = load_state(self.path)
        self.assertEqual(len(state["bms"]["readings"]), 3)
        names = {r.name for r in state["bms"]["readings"]}
        self.assertEqual(names, {"Batt0", "Batt1", "Batt2"})

    def test_list_fields_never_none_after_load(self):
        """Even if stored as null in JSON, list fields must return []."""
        save_section(self.path, "bms", [_bms_reading()])
        raw = json.loads(Path(self.path).read_text())
        for lf in _LIST_FIELDS:
            raw["bms"]["readings"][0][lf] = None
        Path(self.path).write_text(json.dumps(raw), encoding="utf-8")
        state = load_state(self.path)
        r = state["bms"]["readings"][0]
        for lf in _LIST_FIELDS:
            self.assertEqual(getattr(r, lf), [], msg=f"{lf} should be []")

    def test_error_reading_preserved(self):
        r = _bms_reading()
        r.error = "Timed out after 35s"
        save_section(self.path, "bms", [r])
        state = load_state(self.path)
        self.assertEqual(state["bms"]["readings"][0].error, "Timed out after 35s")


# ─────────────────────────────────────────────────────────────────────────────
# 4. Atomicity — cross-process isolation
# ─────────────────────────────────────────────────────────────────────────────

class TestAtomicity(unittest.TestCase):
    """
    Verifies that concurrent alternating writes from two simulated
    processes do not corrupt each other's data.
    """

    def setUp(self):
        self.path = _tmp()

    def tearDown(self):
        for p in (self.path, self.path + ".tmp"):
            try:
                os.unlink(p)
            except FileNotFoundError:
                pass

    def test_alternating_writes_preserve_both_sections(self):
        """Simulate 10 alternating writes from two processes."""
        for i in range(10):
            save_section(self.path, "bms",
                         [_bms_reading(voltage_v=50.0 + i * 0.1)])
            save_section(self.path, "victron",
                         [_victron_reading(ac_out_power_va=700.0 + i * 5)])

        state = load_state(self.path)
        self.assertEqual(len(state["bms"]["readings"]),     1)
        self.assertEqual(len(state["victron"]["readings"]), 1)
        # Last write wins for each section
        self.assertAlmostEqual(state["bms"]["readings"][0].voltage_v,
                                50.0 + 9 * 0.1, places=2)
        self.assertAlmostEqual(state["victron"]["readings"][0].ac_out_power_va,
                                700.0 + 9 * 5, places=1)

    def test_no_tmp_file_leaks(self):
        """Temp file must not exist after successful writes."""
        for _ in range(5):
            save_section(self.path, "bms",     [_bms_reading()])
            save_section(self.path, "victron", [_victron_reading()])
        self.assertFalse(Path(self.path + ".tmp").exists())

    def test_file_always_valid_json(self):
        """State file must always be parseable between writes."""
        for i in range(20):
            section = "bms" if i % 2 == 0 else "victron"
            reading = _bms_reading() if section == "bms" else _victron_reading()
            save_section(self.path, section, [reading])
            text = Path(self.path).read_text()
            json.loads(text)    # must not raise


# ─────────────────────────────────────────────────────────────────────────────
# 5. AppConfig — new fields and defaults
# ─────────────────────────────────────────────────────────────────────────────

class TestApplyCliOverrides(unittest.TestCase):
    """apply_cli_overrides must work with any Namespace — not just the combined launcher's."""

    def _minimal_namespace(self, **kwargs):
        """Simulate the Namespace produced by bms_monitor.py or victron_monitor.py
        — does NOT include --bms or --mppt flags."""
        import argparse
        ns = argparse.Namespace(
            output=None,
            interval=None,
            scan_timeout=None,
            once=False,
            log_level=None,
            theme=None,
        )
        for k, v in kwargs.items():
            setattr(ns, k, v)
        return ns

    def test_split_launcher_namespace_does_not_crash(self):
        """Namespace without bms/mppt attrs must not raise AttributeError."""
        from solar_monitor.config import apply_cli_overrides
        cfg = AppConfig()
        ns  = self._minimal_namespace()       # no .bms, no .mppt
        result = apply_cli_overrides(cfg, ns)  # must not raise
        self.assertIsInstance(result, AppConfig)

    def test_output_override(self):
        from solar_monitor.config import apply_cli_overrides
        cfg = AppConfig()
        ns  = self._minimal_namespace(output="/tmp/test.html")
        apply_cli_overrides(cfg, ns)
        self.assertEqual(cfg.output, "/tmp/test.html")

    def test_interval_override(self):
        from solar_monitor.config import apply_cli_overrides
        cfg = AppConfig()
        ns  = self._minimal_namespace(interval=60.0)
        apply_cli_overrides(cfg, ns)
        self.assertEqual(cfg.interval, 60.0)

    def test_none_interval_does_not_clobber(self):
        from solar_monitor.config import apply_cli_overrides
        cfg = AppConfig()
        cfg.interval = 45.0
        ns  = self._minimal_namespace(interval=None)
        apply_cli_overrides(cfg, ns)
        self.assertEqual(cfg.interval, 45.0)

    def test_once_flag(self):
        from solar_monitor.config import apply_cli_overrides
        cfg = AppConfig()
        ns  = self._minimal_namespace(once=True)
        apply_cli_overrides(cfg, ns)
        self.assertTrue(cfg.once)

    def test_combined_namespace_with_bms_still_works(self):
        """Full combined-launcher Namespace (with --bms) must still work."""
        import argparse
        from solar_monitor.config import apply_cli_overrides
        cfg = AppConfig()
        ns = argparse.Namespace(
            output=None, interval=None, scan_timeout=None, once=False,
            log_level=None, theme=None,
            bms=["AA:BB:CC:DD:EE:FF"],
            mppt=None,
        )
        apply_cli_overrides(cfg, ns)
        self.assertEqual(len(cfg.bms_devices), 1)

    def test_bms_interval_default(self):
        cfg = AppConfig()
        self.assertEqual(cfg.bms_interval, 120.0)

    def test_victron_interval_default(self):
        cfg = AppConfig()
        self.assertEqual(cfg.victron_interval, 30.0)

    def test_state_file_default(self):
        cfg = AppConfig()
        self.assertEqual(cfg.state_file, "solar_state.json")

    def test_interval_default(self):
        """Legacy combined interval unchanged."""
        cfg = AppConfig()
        self.assertEqual(cfg.interval, 30.0)

    def test_all_new_fields_present(self):
        cfg = AppConfig()
        self.assertTrue(hasattr(cfg, "bms_interval"))
        self.assertTrue(hasattr(cfg, "victron_interval"))
        self.assertTrue(hasattr(cfg, "state_file"))


class TestAppConfigIniLoading(unittest.TestCase):
    """Test that new fields are correctly loaded from INI file."""

    def _write_ini(self, content: str) -> str:
        f = tempfile.NamedTemporaryFile(
            mode="w", suffix=".ini", delete=False, encoding="utf-8"
        )
        f.write(textwrap.dedent(content))
        f.close()
        return f.name

    def tearDown(self):
        pass  # individual tests clean up their own files

    def test_bms_interval_from_ini(self):
        path = self._write_ini("""
            [general]
            bms_interval = 180
        """)
        try:
            cfg = load_config(path)
            self.assertEqual(cfg.bms_interval, 180.0)
        finally:
            os.unlink(path)

    def test_victron_interval_from_ini(self):
        path = self._write_ini("""
            [general]
            victron_interval = 15
        """)
        try:
            cfg = load_config(path)
            self.assertEqual(cfg.victron_interval, 15.0)
        finally:
            os.unlink(path)

    def test_state_file_from_ini(self):
        path = self._write_ini("""
            [general]
            state_file = /tmp/my_solar_state.json
        """)
        try:
            cfg = load_config(path)
            self.assertEqual(cfg.state_file, "/tmp/my_solar_state.json")
        finally:
            os.unlink(path)

    def test_all_three_new_fields_together(self):
        path = self._write_ini("""
            [general]
            bms_interval     = 240
            victron_interval = 20
            state_file       = /run/solar.json
        """)
        try:
            cfg = load_config(path)
            self.assertEqual(cfg.bms_interval,     240.0)
            self.assertEqual(cfg.victron_interval,  20.0)
            self.assertEqual(cfg.state_file,        "/run/solar.json")
        finally:
            os.unlink(path)

    def test_missing_new_fields_use_defaults(self):
        """An INI without the new keys must fall back to defaults."""
        path = self._write_ini("""
            [general]
            interval = 60
        """)
        try:
            cfg = load_config(path)
            self.assertEqual(cfg.bms_interval,     120.0)
            self.assertEqual(cfg.victron_interval,  30.0)
            self.assertEqual(cfg.state_file, "solar_state.json")
        finally:
            os.unlink(path)

    def test_legacy_interval_still_loaded(self):
        path = self._write_ini("""
            [general]
            interval = 45
        """)
        try:
            cfg = load_config(path)
            self.assertEqual(cfg.interval, 45.0)
        finally:
            os.unlink(path)

    def test_bms_interval_float(self):
        path = self._write_ini("""
            [general]
            bms_interval = 90.5
        """)
        try:
            cfg = load_config(path)
            self.assertAlmostEqual(cfg.bms_interval, 90.5, places=1)
        finally:
            os.unlink(path)

    def test_empty_ini_all_defaults(self):
        path = self._write_ini("[general]\n")
        try:
            cfg = load_config(path)
            self.assertEqual(cfg.bms_interval,     120.0)
            self.assertEqual(cfg.victron_interval,  30.0)
            self.assertEqual(cfg.state_file, "solar_state.json")
        finally:
            os.unlink(path)


# ─────────────────────────────────────────────────────────────────────────────
# 6. _poll_bms and _poll_victron importable and separate
# ─────────────────────────────────────────────────────────────────────────────

class TestPollFunctionsImportable(unittest.TestCase):

    def test_poll_bms_importable(self):
        from solar_monitor.scanner import _poll_bms
        self.assertTrue(callable(_poll_bms))

    def test_poll_victron_importable(self):
        from solar_monitor.scanner import _poll_victron
        self.assertTrue(callable(_poll_victron))

    def test_poll_all_still_importable(self):
        from solar_monitor.scanner import poll_all
        self.assertTrue(callable(poll_all))

    def test_poll_bms_is_coroutine(self):
        """_poll_bms must be async (awaitable)."""
        import asyncio
        from solar_monitor.scanner import _poll_bms
        self.assertTrue(asyncio.iscoroutinefunction(_poll_bms))

    def test_poll_victron_is_sync(self):
        """_poll_victron must be a plain function (no GATT, no waiting)."""
        import asyncio
        from solar_monitor.scanner import _poll_victron
        self.assertFalse(asyncio.iscoroutinefunction(_poll_victron))

    def test_poll_all_is_coroutine(self):
        import asyncio
        from solar_monitor.scanner import poll_all
        self.assertTrue(asyncio.iscoroutinefunction(poll_all))


# ─────────────────────────────────────────────────────────────────────────────
# 7. _poll_victron with mocked scanner — no hardware required
# ─────────────────────────────────────────────────────────────────────────────

class TestPollVictronMocked(unittest.TestCase):
    """
    _poll_victron is synchronous and just reads from a VictronScanner,
    so we can test it without any BLE hardware.
    """

    def _make_scanner(self):
        from solar_monitor.scanner import VictronScanner
        vs = VictronScanner([])
        return vs

    def test_missing_device_returns_error_reading(self):
        from solar_monitor.scanner import _poll_victron, VictronScanner
        from solar_monitor.config import DeviceConfig
        dc = DeviceConfig(name="Multiplus-Ii", mac="E6:2E:31:75:9A:1A",
                          ble_name=None, enc_key=None, password=None)
        scanner = VictronScanner(["E6:2E:31:75:9A:1A"])
        # Scanner has no data — device was not seen
        results = _poll_victron([dc], scanner)
        self.assertEqual(len(results), 1)
        r = results[0]
        self.assertIsNotNone(r.error)
        self.assertIn("not seen", r.error.lower())

    def test_empty_triples_returns_empty(self):
        from solar_monitor.scanner import _poll_victron, VictronScanner
        scanner = VictronScanner([])
        results = _poll_victron([], scanner)
        self.assertEqual(results, [])

    def test_returns_list_of_device_readings(self):
        from solar_monitor.scanner import _poll_victron, VictronScanner
        from solar_monitor.config import DeviceConfig
        dc = DeviceConfig(name="Test", mac="AA:BB:CC:DD:EE:FF",
                          ble_name=None, enc_key=None, password=None)
        scanner = VictronScanner(["AA:BB:CC:DD:EE:FF"])
        results = _poll_victron([dc], scanner)
        self.assertIsInstance(results, list)
        for r in results:
            self.assertIsInstance(r, DeviceReading)


# ─────────────────────────────────────────────────────────────────────────────
# 8. Full state file workflow — simulate two-process cycle
# ─────────────────────────────────────────────────────────────────────────────

class TestTwoProcessWorkflow(unittest.TestCase):
    """
    Simulate what bms_monitor.py and victron_monitor.py do each cycle,
    end to end, using the state file.
    """

    def setUp(self):
        self.path = _tmp()

    def tearDown(self):
        for p in (self.path, self.path + ".tmp"):
            try:
                os.unlink(p)
            except FileNotFoundError:
                pass

    def _simulate_bms_cycle(self, readings):
        """What bms_monitor.py does after polling."""
        save_section(self.path, "bms", readings)
        state = load_state(self.path)
        return state["victron"]["readings"]   # returns what Victron last wrote

    def _simulate_victron_cycle(self, readings):
        """What victron_monitor.py does after polling."""
        save_section(self.path, "victron", readings)
        state = load_state(self.path)
        return state["bms"]["readings"]       # returns what BMS last wrote

    def test_victron_reads_bms_data(self):
        bms = [_bms_reading(voltage_v=54.32, capacity_pct=84)]
        # BMS goes first
        save_section(self.path, "bms", bms)
        # Victron then polls and merges
        bms_for_dashboard = self._simulate_victron_cycle([_victron_reading()])
        self.assertEqual(len(bms_for_dashboard), 1)
        self.assertAlmostEqual(bms_for_dashboard[0].voltage_v, 54.32, places=2)

    def test_bms_reads_victron_data(self):
        victron = [_victron_reading(ac_out_power_va=755.0)]
        save_section(self.path, "victron", victron)
        victron_for_dashboard = self._simulate_bms_cycle([_bms_reading()])
        self.assertEqual(len(victron_for_dashboard), 1)
        self.assertEqual(victron_for_dashboard[0].ac_out_power_va, 755.0)

    def test_multiple_bms_packs(self):
        packs = [
            _bms_reading(name="Batt1", voltage_v=54.0),
            _bms_reading(name="Batt2", voltage_v=53.8),
        ]
        save_section(self.path, "bms", packs)
        state = load_state(self.path)
        names = {r.name for r in state["bms"]["readings"]}
        self.assertEqual(names, {"Batt1", "Batt2"})

    def test_bms_offline_error_round_trips(self):
        """Error readings must survive the state file round-trip."""
        r = _bms_reading()
        r.error = "Timed out after 35s — device connected but did not respond"
        save_section(self.path, "bms", [r])
        state = load_state(self.path)
        restored = state["bms"]["readings"][0]
        self.assertIn("Timed out", restored.error)

    def test_victron_state_survives_multiple_bms_updates(self):
        """BMS writing many times must not overwrite Victron data."""
        save_section(self.path, "victron", [_victron_reading()])
        for i in range(5):
            save_section(self.path, "bms",
                         [_bms_reading(voltage_v=54.0 - i * 0.1)])
        state = load_state(self.path)
        self.assertEqual(len(state["victron"]["readings"]), 1)
        self.assertEqual(state["victron"]["readings"][0].device_type, "inverter")

    def test_dashboard_merge_produces_correct_html(self):
        """build_html on merged state must not crash and must contain key values."""
        from solar_monitor.dashboard import build_html
        bms     = [_bms_reading(voltage_v=54.32, capacity_pct=84)]
        victron = [_victron_reading(ac_out_power_va=755.0)]
        save_section(self.path, "bms",     bms)
        save_section(self.path, "victron", victron)
        state   = load_state(self.path)
        html = build_html(
            state["bms"]["readings"],
            state["victron"]["readings"],
            {},
            theme="business",
        )
        self.assertIn("54.32", html)    # BMS voltage
        self.assertIn("84",    html)    # SoC
        self.assertIn("755",   html)    # AC out power
        self.assertIn("AC Output L1", html)
        self.assertIn("Battery",      html)


# ─────────────────────────────────────────────────────────────────────────────
# Config parsing helpers — normalise_mac, parse_bms_value, parse_mac_key
# ─────────────────────────────────────────────────────────────────────────────

class TestNormaliseMac(unittest.TestCase):
    """normalise_mac — accepts all common MAC address formats."""

    def test_colon_separated_uppercase_unchanged(self):
        self.assertEqual(normalise_mac("AA:BB:CC:DD:EE:FF"), "AA:BB:CC:DD:EE:FF")

    def test_colon_separated_lowercase_uppercased(self):
        self.assertEqual(normalise_mac("aa:bb:cc:dd:ee:ff"), "AA:BB:CC:DD:EE:FF")

    def test_dash_separated_converted(self):
        self.assertEqual(normalise_mac("AA-BB-CC-DD-EE-FF"), "AA:BB:CC:DD:EE:FF")

    def test_raw_12_hex_digits_converted(self):
        self.assertEqual(normalise_mac("AABBCCDDEEFF"), "AA:BB:CC:DD:EE:FF")

    def test_raw_lowercase_12_digits_converted(self):
        self.assertEqual(normalise_mac("aabbccddeeff"), "AA:BB:CC:DD:EE:FF")

    def test_whitespace_stripped(self):
        self.assertEqual(normalise_mac("  AA:BB:CC:DD:EE:FF  "), "AA:BB:CC:DD:EE:FF")


class TestParseBmsValue(unittest.TestCase):
    """parse_bms_value — parse BMS config line into (mac, ble_name, password)."""

    def test_mac_with_password(self):
        mac, name, pw = parse_bms_value("AA:BB:CC:DD:EE:FF : 123456")
        self.assertEqual(mac, "AA:BB:CC:DD:EE:FF")
        self.assertIsNone(name)
        self.assertEqual(pw, "123456")

    def test_mac_without_password(self):
        mac, name, pw = parse_bms_value("AA:BB:CC:DD:EE:FF")
        self.assertEqual(mac, "AA:BB:CC:DD:EE:FF")
        self.assertIsNone(name)
        self.assertIsNone(pw)

    def test_ble_name_fallback(self):
        mac, name, pw = parse_bms_value("BT-TH-AABBCC")
        self.assertIsNone(mac)
        self.assertEqual(name, "BT-TH-AABBCC")
        self.assertIsNone(pw)

    def test_whitespace_stripped(self):
        mac, name, pw = parse_bms_value("  A1:B2:C3:D4:E5:F6 : 000000  ")
        self.assertEqual(mac, "A1:B2:C3:D4:E5:F6")
        self.assertEqual(pw, "000000")

    def test_password_is_string(self):
        _, _, pw = parse_bms_value("AA:BB:CC:DD:EE:FF : 000000")
        self.assertIsInstance(pw, str)


class TestParseMacKey(unittest.TestCase):
    """parse_mac_key — parse Victron config line into (mac, key, device_type)."""

    def test_mac_key_and_type(self):
        mac, key, dtype = parse_mac_key(
            "AA:BB:CC:DD:EE:FF : aabbccddeeff00112233445566778899  type=mppt"
        )
        self.assertEqual(mac,   "AA:BB:CC:DD:EE:FF")
        self.assertEqual(key,   "aabbccddeeff00112233445566778899")
        self.assertEqual(dtype, "mppt")

    def test_mac_and_key_no_type(self):
        mac, key, dtype = parse_mac_key(
            "AA:BB:CC:DD:EE:FF : aabbccddeeff00112233445566778899"
        )
        self.assertEqual(mac, "AA:BB:CC:DD:EE:FF")
        self.assertIsNotNone(key)
        self.assertIsNone(dtype)

    def test_mac_and_type_no_key(self):
        mac, key, dtype = parse_mac_key("AA:BB:CC:DD:EE:FF type=inverter")
        self.assertEqual(mac,   "AA:BB:CC:DD:EE:FF")
        self.assertIsNone(key)
        self.assertEqual(dtype, "inverter")

    def test_mac_only(self):
        mac, key, dtype = parse_mac_key("AA:BB:CC:DD:EE:FF")
        self.assertEqual(mac, "AA:BB:CC:DD:EE:FF")
        self.assertIsNone(key)
        self.assertIsNone(dtype)

    def test_all_type_values_accepted(self):
        for t in ("mppt", "inverter", "monitor", "dcdc"):
            _, _, dtype = parse_mac_key(f"AA:BB:CC:DD:EE:FF type={t}")
            self.assertEqual(dtype, t, f"type={t} not parsed correctly")

    def test_type_lowercased(self):
        _, _, dtype = parse_mac_key("AA:BB:CC:DD:EE:FF type=MPPT")
        self.assertEqual(dtype, "mppt")


class TestMaxHistoryConfig(unittest.TestCase):
    """max_history is loaded correctly from [general] section."""

    def test_max_history_loaded_from_ini(self):
        import tempfile, os
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".ini",
                                        delete=False, encoding="utf-8")
        f.write("[general]\nmax_history = 1200\n")
        f.close()
        try:
            cfg = load_config(f.name)
            self.assertEqual(cfg.max_history, 1200)
        finally:
            os.unlink(f.name)

    def test_max_history_default_when_absent(self):
        import tempfile, os
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".ini",
                                        delete=False, encoding="utf-8")
        f.write("[general]\n")
        f.close()
        try:
            cfg = load_config(f.name)
            self.assertIsInstance(cfg.max_history, int)
            self.assertGreater(cfg.max_history, 0)
        finally:
            os.unlink(f.name)


# ─────────────────────────────────────────────────────────────────────────────
# Dashboard helpers — _soc_color, _no_card
# ─────────────────────────────────────────────────────────────────────────────

class TestSocColor(unittest.TestCase):
    """_soc_color — returns correct CSS variable string for each SoC band."""

    def test_full_charge_is_green(self):
        self.assertEqual(_soc_color(100), "var(--green)")

    def test_boundary_60_is_green(self):
        self.assertEqual(_soc_color(60), "var(--green)")

    def test_boundary_59_is_amber(self):
        self.assertEqual(_soc_color(59), "var(--amber)")

    def test_boundary_30_is_amber(self):
        self.assertEqual(_soc_color(30), "var(--amber)")

    def test_boundary_29_is_red(self):
        self.assertEqual(_soc_color(29), "var(--red)")

    def test_zero_is_red(self):
        self.assertEqual(_soc_color(0), "var(--red)")

    def test_returns_string(self):
        self.assertIsInstance(_soc_color(50), str)


class TestNoCard(unittest.TestCase):
    """_no_card — returns a styled placeholder div."""

    def test_contains_message(self):
        html = _no_card("No devices found")
        self.assertIn("No devices found", html)

    def test_is_valid_html_fragment(self):
        html = _no_card("test")
        self.assertIn("<div", html)
        self.assertIn("</div>", html)

    def test_uses_no_card_class(self):
        html = _no_card("test")
        self.assertIn("no-card", html)


# ─────────────────────────────────────────────────────────────────────────────
# Query utility helpers — _resolve_date, _print_table
# ─────────────────────────────────────────────────────────────────────────────

class TestResolveDate(unittest.TestCase):
    """_resolve_date — resolve date shortcuts to ISO date strings."""

    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "query_history", "/home/claude/utils/query_history.py"
        )
        self.qh = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.qh)

    def test_today_resolves_to_todays_date(self):
        from datetime import date
        result = self.qh._resolve_date("today")
        self.assertEqual(result, date.today().isoformat())

    def test_yesterday_resolves_to_yesterdays_date(self):
        from datetime import date, timedelta
        result = self.qh._resolve_date("yesterday")
        self.assertEqual(result, (date.today() - timedelta(days=1)).isoformat())

    def test_iso_date_passed_through(self):
        self.assertEqual(self.qh._resolve_date("2024-01-15"), "2024-01-15")

    def test_iso_datetime_passed_through(self):
        self.assertEqual(self.qh._resolve_date("2024-01-15T08:30:00"),
                         "2024-01-15T08:30:00")

    def test_case_insensitive_today(self):
        from datetime import date
        self.assertEqual(self.qh._resolve_date("TODAY"), date.today().isoformat())

    def test_case_insensitive_yesterday(self):
        from datetime import date, timedelta
        self.assertEqual(self.qh._resolve_date("YESTERDAY"),
                         (date.today() - timedelta(days=1)).isoformat())


class TestPrintTable(unittest.TestCase):
    """_print_table — renders a human-readable table to stdout."""

    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "query_history", "/home/claude/utils/query_history.py"
        )
        self.qh = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.qh)
        self.rows = [
            {"recorded_at": "2024-01-15T08:00:00", "device_name": "House Bank",
             "device_type": "bms", "voltage_v": 54.32, "capacity_pct": 84,
             "current_a": -10.0, "power_w": -543.2, "pv_power_w": None,
             "error": None},
        ]

    def _capture(self, rows, max_rows=40):
        import io
        from contextlib import redirect_stdout
        f = io.StringIO()
        with redirect_stdout(f):
            self.qh._print_table(rows, max_rows)
        return f.getvalue()

    def test_contains_device_name(self):
        out = self._capture(self.rows)
        self.assertIn("House Bank", out)

    def test_contains_voltage(self):
        out = self._capture(self.rows)
        self.assertIn("54.32", out)

    def test_empty_rows_no_crash(self):
        out = self._capture([])
        self.assertIn("no results", out.lower())

    def test_max_rows_limit_respected(self):
        many_rows = self.rows * 50
        out = self._capture(many_rows, max_rows=5)
        # Should mention truncation
        self.assertIn("more rows", out.lower())

    def test_outputs_header_row(self):
        out = self._capture(self.rows)
        lines = [l for l in out.splitlines() if l.strip()]
        # First line should be column headers
        self.assertIn("device_name", lines[0])


# ─────────────────────────────────────────────────────────────────────────────
# BLE scanner — OrPattern import path
# ─────────────────────────────────────────────────────────────────────────────

class TestOrPatternImport(unittest.TestCase):
    """scan() must attempt the OrPattern import path for bleak >= 0.21."""

    def _scanner_src(self):
        with open("/home/claude/solar_monitor/scanner.py") as f:
            return f.read()

    def test_or_pattern_import_attempted(self):
        """scan() source must try to import OrPattern for bleak >= 0.21."""
        src = self._scanner_src()
        self.assertIn("OrPattern", src,
                      "scan() must attempt OrPattern import for bleak >= 0.21")

    def test_or_pattern_import_inside_try_block(self):
        """OrPattern import must be in a try/except so older bleak still works."""
        src = self._scanner_src()
        # Find the try block containing OrPattern
        try_idx     = src.find("try:")
        orpat_idx   = src.find("OrPattern")
        except_idx  = src.find("except ImportError", try_idx)
        self.assertGreater(orpat_idx, try_idx,
                           "OrPattern must be inside a try block")
        self.assertGreater(except_idx, orpat_idx,
                           "ImportError except must follow OrPattern import")

    def test_fallback_tuple_format_present(self):
        """Tuple-format or_patterns must be present as fallback for bleak <= 0.20."""
        src = self._scanner_src()
        self.assertIn("(0, 0xFF,", src,
                      "Tuple-format or_patterns must be present for bleak <= 0.20")


if __name__ == "__main__":
    unittest.main(verbosity=2)
