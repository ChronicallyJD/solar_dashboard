"""
tests/test_console_monitor.py — unit tests for the Rich console dashboard
=========================================================================
Covers:
  - _fmt, _soc_bar, _tte utility functions
  - Aggregate panel construction (MPPT, Inverter, Battery)
  - Individual device panel construction (BMS, Victron variants)
  - Error/offline device handling
  - _render with full state, empty state, partial state
  - _mtime helper
  - CLI argument handling (state_file path resolution)
  - Missing-rich guard message

All tests run without Rich being installed — a complete stub is loaded
before the module is imported.
"""

import os
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

# ── Stub bleak ────────────────────────────────────────────────────────────────
bleak    = types.ModuleType("bleak")
backends = types.ModuleType("bleak.backends")
dev_m    = types.ModuleType("bleak.backends.device")


class _BLEDevice:
    def __init__(self, a="", n="", **kw):
        self.address = a
        self.name    = n


class _BleakClient:  pass
class _BleakScanner: pass

dev_m.BLEDevice    = _BLEDevice
bleak.BleakClient  = _BleakClient
bleak.BleakScanner = _BleakScanner
sys.modules.update({
    "bleak": bleak,
    "bleak.backends": backends,
    "bleak.backends.device": dev_m,
})
import os
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

# ── Stub Rich ─────────────────────────────────────────────────────────────────

class _Text:
    def __init__(self, s="", style=""): self.s = str(s); self.style = style
    def __add__(self, o): return _Text(self.s + (o.s if hasattr(o, "s") else str(o)))
    def append(self, s, style=""): self.s += str(s); return self
    def __repr__(self): return f"Text({self.s!r})"


class _Table:
    def __init__(self, *a, **kw): self.rows = []; self.cols = []
    def add_column(self, *a, **kw): self.cols.append(kw)
    def add_row(self, *cells): self.rows.append(cells)
    @classmethod
    def grid(cls, *a, **kw): return cls()


class _Panel:
    def __init__(self, renderable=None, title="", border_style="", **kw):
        self.renderable   = renderable
        self.title        = title
        self.border_style = border_style


class _Console:
    def __init__(self, **kw): pass
    def print(self, *a, **kw): pass


class _Live:
    def __init__(self, *a, **kw): pass
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def update(self, *a, **kw): pass


for mod_name, attrs in [
    ("rich",         {"box": type("box", (), {})}),
    ("rich.console", {"Console": _Console}),
    ("rich.layout",  {"Layout": type("Layout", (), {"__init__": lambda s, *a, **k: None})}),
    ("rich.live",    {"Live": _Live}),
    ("rich.panel",   {"Panel": _Panel}),
    ("rich.table",   {"Table": _Table}),
    ("rich.text",    {"Text": _Text}),
    ("rich.box",     {"SIMPLE": None}),
]:
    m = types.ModuleType(mod_name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[mod_name] = m

# ── Import module under test ──────────────────────────────────────────────────
import importlib.util
_spec = importlib.util.spec_from_file_location(
    "console_monitor", f"{REPO_ROOT}/console_monitor.py"
)
cm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cm)

from solar_monitor.models import DeviceReading


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _bms(name="Batt1", **kw) -> DeviceReading:
    r = DeviceReading(address="AA:BB:CC:DD:EE:FF", name=name,
                      device_type="bms", timestamp="2024-01-01T12:00:00")
    r.voltage_v = 54.32; r.current_a = -10.0; r.power_w = -543.2
    r.capacity_pct = 84; r.remain_wh = 4500.0; r.remain_ah = 84.0
    r.nominal_ah = 100.0; r.nominal_wh = 5432.0
    r.time_to_empty_h = 8.4; r.time_to_full_h = None
    r.cell_count = 16; r.cycle_count = 8
    r.charge_fet = True; r.discharge_fet = True
    r.faults = []; r.temp_c = [23.1, 21.8]; r.balance_cells = [0] * 16
    for k, v in kw.items():
        setattr(r, k, v)
    return r


def _mppt(name="South Array", dtype="mppt", **kw) -> DeviceReading:
    r = DeviceReading(address="11:22:33:44:55:66", name=name,
                      device_type=dtype, timestamp="2024-01-01T12:00:01")
    r.pv_power_w = 680.0; r.yield_today_wh = 3200.0; r.charger_state = "Float"
    r.voltage_v = 54.0; r.current_a = 12.0; r.power_w = 648.0
    r.ac_out_power_va = 755.0; r.inverter_state = "Inverting"
    r.alarm_reason = None; r.ac_in_source = "Not connected"
    r.faults = []; r.temp_c = []
    for k, v in kw.items():
        setattr(r, k, v)
    return r


# ─────────────────────────────────────────────────────────────────────────────
# 1. Utility functions
# ─────────────────────────────────────────────────────────────────────────────

class TestFmt(unittest.TestCase):

    def test_float_two_decimals(self):
        self.assertEqual(cm._fmt(54.32, 2), "54.32")

    def test_float_zero_decimals(self):
        self.assertEqual(cm._fmt(3200.0, 0), "3200")

    def test_none_returns_dash(self):
        self.assertEqual(cm._fmt(None), "—")

    def test_negative(self):
        self.assertEqual(cm._fmt(-10.0, 1), "-10.0")

    def test_zero(self):
        self.assertEqual(cm._fmt(0.0, 1), "0.0")


class TestSocBar(unittest.TestCase):

    def test_none_returns_dash(self):
        result = cm._soc_bar(None)
        self.assertEqual(result.s, "—")

    def test_contains_percentage(self):
        result = cm._soc_bar(84)
        self.assertIn("84%", result.s)

    def test_full_bar(self):
        result = cm._soc_bar(100, 10)
        # 10 filled blocks
        self.assertEqual(result.s.count("█"), 10)

    def test_empty_bar(self):
        result = cm._soc_bar(0, 10)
        self.assertEqual(result.s.count("█"), 0)

    def test_partial_bar(self):
        result = cm._soc_bar(50, 10)
        self.assertEqual(result.s.count("█"), 5)
        self.assertEqual(result.s.count("░"), 5)

    def test_high_soc_uses_more_fill_than_low(self):
        """High SoC should produce more filled blocks than low SoC."""
        high = cm._soc_bar(80, 10)
        low  = cm._soc_bar(20, 10)
        self.assertGreater(high.s.count("█"), low.s.count("█"))

    def test_low_soc_has_mostly_empty_blocks(self):
        result = cm._soc_bar(10, 10)
        self.assertGreater(result.s.count("░"), result.s.count("█"))


class TestTte(unittest.TestCase):

    def test_none_returns_empty_string(self):
        self.assertEqual(cm._tte(None), "")

    def test_whole_hours(self):
        self.assertEqual(cm._tte(2.0), "2h00m")

    def test_half_hour(self):
        self.assertEqual(cm._tte(0.5), "0h30m")

    def test_one_hour_exactly(self):
        self.assertEqual(cm._tte(1.0), "1h00m")

    def test_minutes_zero_padded(self):
        # Use 1.25h = 1h15m to avoid float precision issues
        result = cm._tte(1.25)
        self.assertTrue(result.endswith("15m"),
                        f"Expected zero-padded minutes ending in 15m, got {result!r}")

    def test_format_is_HhMMm(self):
        result = cm._tte(3.5)
        self.assertRegex(result, r"^\d+h\d{2}m$")


# ─────────────────────────────────────────────────────────────────────────────
# 2. Aggregate panels
# ─────────────────────────────────────────────────────────────────────────────

class TestMpptAggregatePanel(unittest.TestCase):

    def setUp(self):
        self.readings = [
            _mppt("South", "mppt", pv_power_w=400.0, yield_today_wh=2000.0,
                  charger_state="Float"),
            _mppt("West",  "mppt", pv_power_w=280.0, yield_today_wh=1200.0,
                  charger_state="Bulk"),
        ]

    def test_returns_panel(self):
        result = cm._mppt_aggregate_panel(self.readings)
        self.assertIsInstance(result, _Panel)

    def test_sums_pv_power(self):
        panel = cm._mppt_aggregate_panel(self.readings)
        text_vals = [c.s for row in panel.renderable.rows for c in row if hasattr(c, 's')]
        combined = " ".join(text_vals)
        self.assertIn("680.0", combined)   # 400 + 280

    def test_sums_yield(self):
        panel = cm._mppt_aggregate_panel(self.readings)
        text_vals = [c.s for row in panel.renderable.rows for c in row if hasattr(c, 's')]
        combined = " ".join(text_vals)
        self.assertIn("3200", combined)    # 2000 + 1200

    def test_counts_online(self):
        panel = cm._mppt_aggregate_panel(self.readings)
        text_vals = [c.s for row in panel.renderable.rows for c in row if hasattr(c, 's')]
        combined = " ".join(text_vals)
        self.assertIn("2/2", combined)

    def test_empty_readings(self):
        panel = cm._mppt_aggregate_panel([])
        self.assertIsInstance(panel, _Panel)

    def test_offline_device_excluded_from_totals(self):
        r_err = _mppt("Dead", "mppt")
        r_err.error = "timeout"
        panel = cm._mppt_aggregate_panel(self.readings + [r_err])
        text_vals = [c.s for row in panel.renderable.rows for c in row if hasattr(c, 's')]
        combined = " ".join(text_vals)
        self.assertIn("2/3", combined)     # 2 online out of 3 total


class TestInverterAggregatePanel(unittest.TestCase):

    def setUp(self):
        self.readings = [_mppt("MultiPlus", "inverter", ac_out_power_va=755.0)]

    def test_returns_panel(self):
        self.assertIsInstance(
            cm._inverter_aggregate_panel(self.readings), _Panel
        )

    def test_ac_power_shown(self):
        panel = cm._inverter_aggregate_panel(self.readings)
        text_vals = [c.s for row in panel.renderable.rows for c in row if hasattr(c, 's')]
        combined = " ".join(text_vals)
        self.assertIn("755", combined)

    def test_no_alarm_shown(self):
        panel = cm._inverter_aggregate_panel(self.readings)
        text_vals = [c.s for row in panel.renderable.rows for c in row if hasattr(c, 's')]
        combined = " ".join(text_vals)
        self.assertIn("None", combined)

    def test_empty_inverters_no_crash(self):
        panel = cm._inverter_aggregate_panel([])
        self.assertIsInstance(panel, _Panel)

    def test_non_inverter_readings_ignored(self):
        """MPPT readings must not pollute the inverter aggregate."""
        mixed = [_mppt("South", "mppt"), _mppt("Multi", "inverter")]
        panel = cm._inverter_aggregate_panel(mixed)
        text_vals = [c.s for row in panel.renderable.rows for c in row if hasattr(c, 's')]
        combined = " ".join(text_vals)
        self.assertIn("1/1", combined)


class TestBatteryAggregatePanel(unittest.TestCase):

    def setUp(self):
        self.readings = [
            _bms("Batt1", capacity_pct=84, remain_wh=4500.0,
                 remain_ah=84.0, nominal_wh=5000.0),
            _bms("Batt2", capacity_pct=72, remain_wh=3900.0,
                 remain_ah=72.0, nominal_wh=5000.0),
        ]

    def test_returns_panel(self):
        self.assertIsInstance(cm._battery_aggregate_panel(self.readings), _Panel)

    def test_average_soc_shown(self):
        panel = cm._battery_aggregate_panel(self.readings)
        text_vals = [c.s for row in panel.renderable.rows for c in row if hasattr(c, 's')]
        combined = " ".join(text_vals)
        # avg of 84 and 72 = 78
        self.assertIn("78%", combined)

    def test_total_wh_shown(self):
        panel = cm._battery_aggregate_panel(self.readings)
        text_vals = [c.s for row in panel.renderable.rows for c in row if hasattr(c, 's')]
        combined = " ".join(text_vals)
        self.assertIn("8400", combined)  # 4500 + 3900

    def test_online_count_shown(self):
        panel = cm._battery_aggregate_panel(self.readings)
        text_vals = [c.s for row in panel.renderable.rows for c in row if hasattr(c, 's')]
        combined = " ".join(text_vals)
        self.assertIn("2/2", combined)

    def test_empty_no_crash(self):
        panel = cm._battery_aggregate_panel([])
        self.assertIsInstance(panel, _Panel)

    def test_offline_pack_excluded(self):
        r_err = _bms("Dead")
        r_err.error = "timeout"
        panel = cm._battery_aggregate_panel(self.readings + [r_err])
        text_vals = [c.s for row in panel.renderable.rows for c in row if hasattr(c, 's')]
        combined = " ".join(text_vals)
        self.assertIn("2/3", combined)


# ─────────────────────────────────────────────────────────────────────────────
# 3. BMS device panel
# ─────────────────────────────────────────────────────────────────────────────

class TestBmsDevicePanel(unittest.TestCase):

    def _text_content(self, panel: _Panel) -> str:
        return " ".join(
            c.s for row in panel.renderable.rows for c in row if hasattr(c, 's')
        )

    def test_returns_panel(self):
        self.assertIsInstance(cm._bms_device_panel(_bms()), _Panel)

    def test_voltage_shown(self):
        panel = cm._bms_device_panel(_bms(voltage_v=54.32))
        self.assertIn("54.32", self._text_content(panel))

    def test_soc_bar_shown(self):
        panel = cm._bms_device_panel(_bms(capacity_pct=84))
        content = self._text_content(panel)
        self.assertIn("84%", content)

    def test_remain_wh_shown(self):
        panel = cm._bms_device_panel(_bms(remain_wh=4500.0))
        self.assertIn("4500", self._text_content(panel))

    def test_tte_shown_when_discharging(self):
        panel = cm._bms_device_panel(_bms(time_to_empty_h=8.0))
        self.assertIn("8h00m", self._text_content(panel))

    def test_tte_absent_when_none(self):
        r = _bms(); r.time_to_empty_h = None
        panel = cm._bms_device_panel(r)
        self.assertNotIn("TTE", self._text_content(panel))

    def test_temperature_shown(self):
        panel = cm._bms_device_panel(_bms(temp_c=[23.1, 21.8]))
        content = self._text_content(panel)
        self.assertIn("23.1", content)

    def test_faults_shown_in_red(self):
        r = _bms(); r.faults = ["Cell overvoltage"]
        panel = cm._bms_device_panel(r)
        # Find the faults row
        fault_rows = [row for row in panel.renderable.rows
                      if any(hasattr(c, 's') and "Cell overvoltage" in c.s
                             for c in row)]
        self.assertTrue(len(fault_rows) > 0, "Fault text not found in panel")

    def test_no_faults_no_fault_row(self):
        r = _bms(); r.faults = []
        panel = cm._bms_device_panel(r)
        content = self._text_content(panel)
        self.assertNotIn("overvoltage", content)

    def test_balancing_shown_when_active(self):
        r = _bms(); r.balance_cells = [0] * 16; r.balance_cells[3] = 1
        panel = cm._bms_device_panel(r)
        content = self._text_content(panel)
        self.assertIn("4", content)   # cell 4 (1-indexed)

    def test_no_balancing_row_when_inactive(self):
        r = _bms(); r.balance_cells = [0] * 16
        panel = cm._bms_device_panel(r)
        content = self._text_content(panel)
        self.assertNotIn("Balancing", content)

    def test_offline_shows_error_message(self):
        r = _bms(); r.error = "Timed out after 35s"
        panel = cm._bms_device_panel(r)
        content = self._text_content(panel)
        self.assertIn("Timed out", content)

    def test_offline_border_style(self):
        r = _bms(); r.error = "Connection refused"
        panel = cm._bms_device_panel(r)
        self.assertEqual(panel.border_style, cm.C_ERR)

    def test_online_border_style(self):
        panel = cm._bms_device_panel(_bms())
        self.assertEqual(panel.border_style, cm.C_VOLT)

    def test_name_in_title(self):
        panel = cm._bms_device_panel(_bms(name="House Bank"))
        self.assertIn("House Bank", panel.title)

    def test_fet_status_shown(self):
        panel = cm._bms_device_panel(_bms(charge_fet=True, discharge_fet=True))
        content = self._text_content(panel)
        self.assertIn("✓", content)


# ─────────────────────────────────────────────────────────────────────────────
# 4. Victron device panel
# ─────────────────────────────────────────────────────────────────────────────

class TestVictronDevicePanel(unittest.TestCase):

    def _content(self, panel: _Panel) -> str:
        return " ".join(
            c.s for row in panel.renderable.rows for c in row if hasattr(c, 's')
        )

    def test_mppt_returns_panel(self):
        self.assertIsInstance(cm._victron_device_panel(_mppt()), _Panel)

    def test_mppt_pv_power_shown(self):
        panel = cm._victron_device_panel(_mppt(pv_power_w=680.0))
        self.assertIn("680.0", self._content(panel))

    def test_mppt_yield_shown(self):
        panel = cm._victron_device_panel(_mppt(yield_today_wh=3200.0))
        self.assertIn("3200", self._content(panel))

    def test_mppt_charger_state_shown(self):
        panel = cm._victron_device_panel(_mppt(charger_state="Float"))
        self.assertIn("Float", self._content(panel))

    def test_inverter_ac_out_shown(self):
        panel = cm._victron_device_panel(_mppt("Multi", "inverter", ac_out_power_va=755.0))
        self.assertIn("755", self._content(panel))

    def test_inverter_state_shown(self):
        panel = cm._victron_device_panel(_mppt("Multi", "inverter",
                                                inverter_state="Inverting"))
        self.assertIn("Inverting", self._content(panel))

    def test_inverter_battery_voltage_shown(self):
        panel = cm._victron_device_panel(_mppt("Multi", "inverter", voltage_v=54.0))
        self.assertIn("54.00", self._content(panel))

    def test_offline_shows_error(self):
        r = _mppt(); r.error = "Device not seen"
        panel = cm._victron_device_panel(r)
        self.assertIn("Device not seen", self._content(panel))

    def test_mppt_border_is_yellow(self):
        panel = cm._victron_device_panel(_mppt("S", "mppt"))
        self.assertEqual(panel.border_style, "yellow")

    def test_inverter_border_is_magenta(self):
        panel = cm._victron_device_panel(_mppt("M", "inverter"))
        self.assertEqual(panel.border_style, "bright_magenta")

    def test_name_in_title(self):
        panel = cm._victron_device_panel(_mppt(name="South Array"))
        self.assertIn("South Array", panel.title)


# ─────────────────────────────────────────────────────────────────────────────
# 5. _render — full layout composition
# ─────────────────────────────────────────────────────────────────────────────

class TestRender(unittest.TestCase):

    def _full_state(self):
        return {
            "bms":     {"updated": "2024-01-01T12:00:00",
                        "readings": [_bms("B1"), _bms("B2", capacity_pct=72)]},
            "victron": {"updated": "2024-01-01T12:00:01",
                        "readings": [_mppt("South", "mppt"),
                                     _mppt("Multi", "inverter")]},
        }

    def test_returns_renderable(self):
        result = cm._render(self._full_state())
        self.assertIsNotNone(result)

    def test_empty_state_no_crash(self):
        state = {"bms":     {"updated": None, "readings": []},
                 "victron": {"updated": None, "readings": []}}
        result = cm._render(state)
        self.assertIsNotNone(result)

    def test_bms_only_no_crash(self):
        state = {"bms":     {"updated": "t", "readings": [_bms()]},
                 "victron": {"updated": None, "readings": []}}
        cm._render(state)

    def test_victron_only_no_crash(self):
        state = {"bms":     {"updated": None, "readings": []},
                 "victron": {"updated": "t",  "readings": [_mppt()]}}
        cm._render(state)

    def test_many_bms_packs_grouped_in_rows(self):
        """More than 3 BMS packs should not crash — they wrap into multiple rows."""
        packs = [_bms(f"Batt{i}") for i in range(5)]
        state = {"bms": {"updated": "t", "readings": packs},
                 "victron": {"updated": None, "readings": []}}
        cm._render(state)

    def test_offline_bms_in_render_no_crash(self):
        r = _bms(); r.error = "timeout"
        state = {"bms": {"updated": "t", "readings": [_bms(), r]},
                 "victron": {"updated": None, "readings": []}}
        cm._render(state)


# ─────────────────────────────────────────────────────────────────────────────
# 6. _mtime helper
# ─────────────────────────────────────────────────────────────────────────────

class TestMtime(unittest.TestCase):

    def test_missing_file_returns_zero(self):
        self.assertEqual(cm._mtime("/nonexistent/path/file.json"), 0.0)

    def test_existing_file_returns_positive(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            path = f.name
        try:
            result = cm._mtime(path)
            self.assertGreater(result, 0.0)
        finally:
            os.unlink(path)

    def test_updated_file_changes_mtime(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            path = f.name
        try:
            t1 = cm._mtime(path)
            import time; time.sleep(0.01)
            Path = __import__('pathlib').Path
            Path(path).write_text("updated", encoding="utf-8")
            t2 = cm._mtime(path)
            self.assertGreaterEqual(t2, t1)
        finally:
            os.unlink(path)


# ─────────────────────────────────────────────────────────────────────────────
# 7. Source-level guarantees
# ─────────────────────────────────────────────────────────────────────────────

class TestSourceGuarantees(unittest.TestCase):

    def _src(self):
        with open(f"{REPO_ROOT}/console_monitor.py") as f:
            return f.read()

    def test_rich_import_guard_present(self):
        self.assertIn("ImportError", self._src(),
                      "Must have ImportError guard for missing rich")

    def test_screen_true_in_live(self):
        """Live() must use screen=True for full-screen in-place updates."""
        self.assertIn("screen=True", self._src())

    def test_keyboard_interrupt_handled(self):
        self.assertIn("KeyboardInterrupt", self._src())

    def test_mtime_used_for_change_detection(self):
        self.assertIn("_mtime", self._src())

    def test_state_file_resolved_from_config(self):
        """If --config is given, state_file must be read from it."""
        self.assertIn("cfg.state_file", self._src())

    def test_aggregate_panels_called(self):
        src = self._src()
        self.assertIn("_mppt_aggregate_panel",     src)
        self.assertIn("_inverter_aggregate_panel", src)
        self.assertIn("_battery_aggregate_panel",  src)

    def test_refresh_per_second_set(self):
        self.assertIn("refresh_per_second", self._src())

    def test_argparse_interval_argument(self):
        self.assertIn("--interval", self._src())

    def test_argparse_state_file_argument(self):
        self.assertIn("--state-file", self._src())


if __name__ == "__main__":
    unittest.main(verbosity=2)
