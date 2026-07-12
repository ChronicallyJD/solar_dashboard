"""
tests/test_worker_entrypoints.py — Tests for the worker entry-point modules
===========================================================================
Covers the three previously untested entry points:
  - bms_monitor.py          — JBD/Vatrer BMS worker process
  - victron_monitor.py      — Victron BLE worker process
  - solar_monitor/__main__.py — combined package entry point

For each:
  - argument parsing (--config, --state-file, --log-level, --once, bad flags)
  - config loading and CLI-override precedence
  - single-poll (--once) flow with stubbed BLE readers writing the expected
    state-file section and dashboard HTML
  - error handling: recoverable poll failures vs unrecoverable config errors
  - __main__.py mode dispatch (--write-example-config vs polling loop)

No real BLE, no sleeps — every test drives exactly one --once cycle.
"""

import asyncio
import importlib
import importlib.util
import json
import logging
import os
import sys
import tempfile
import textwrap
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# ── Stub bleak so imports work without BLE hardware ───────────────────────────
bleak    = types.ModuleType("bleak")
backends = types.ModuleType("bleak.backends")
dev_m    = types.ModuleType("bleak.backends.device")


class _BLEDevice:
    def __init__(self, a="", n="", **kw):
        self.address = a; self.name = n


class _BleakClient:
    def __init__(self, *a, **kw): pass


class _BleakScanner:
    def __init__(self, *a, **kw): pass
    async def start(self): pass
    async def stop(self):  pass


dev_m.BLEDevice    = _BLEDevice
bleak.BleakClient  = _BleakClient
bleak.BleakScanner = _BleakScanner
sys.modules.update({
    "bleak": bleak,
    "bleak.backends": backends,
    "bleak.backends.device": dev_m,
})

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

# ── Load the two standalone worker scripts as modules ─────────────────────────
_bms_spec = importlib.util.spec_from_file_location(
    "bms_monitor_module", f"{REPO_ROOT}/bms_monitor.py"
)
bms_mod = importlib.util.module_from_spec(_bms_spec)
_bms_spec.loader.exec_module(bms_mod)

_vic_spec = importlib.util.spec_from_file_location(
    "victron_monitor_module", f"{REPO_ROOT}/victron_monitor.py"
)
vic_mod = importlib.util.module_from_spec(_vic_spec)
_vic_spec.loader.exec_module(vic_mod)

# ── Load the package entry point (relative imports need the package) ──────────
main_mod = importlib.import_module("solar_monitor.__main__")

from solar_monitor.models import DeviceReading
from solar_monitor.state import load_state


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _bms_reading(**kwargs) -> DeviceReading:
    r = DeviceReading(
        address=kwargs.pop("address", "AA:BB:CC:DD:EE:01"),
        name=kwargs.pop("name", "House Bank"),
        device_type=kwargs.pop("device_type", "bms"),
        timestamp=kwargs.pop("timestamp", "2024-01-01T12:00:00"),
    )
    defaults = dict(
        voltage_v=54.32, current_a=-15.0, power_w=-814.8,
        capacity_pct=84, temp_c=[23.1], faults=[], balance_cells=[0] * 16,
    )
    defaults.update(kwargs)
    for k, v in defaults.items():
        setattr(r, k, v)
    return r


def _victron_reading(**kwargs) -> DeviceReading:
    r = DeviceReading(
        address=kwargs.pop("address", "AA:BB:CC:DD:EE:F1"),
        name=kwargs.pop("name", "Roof Mppt"),
        device_type=kwargs.pop("device_type", "mppt"),
        timestamp=kwargs.pop("timestamp", "2024-01-01T12:00:00"),
    )
    defaults = dict(
        voltage_v=54.1, current_a=10.0, power_w=541.0,
        pv_power_w=600.0, yield_today_wh=1234.0,
        faults=[], temp_c=[],
    )
    defaults.update(kwargs)
    for k, v in defaults.items():
        setattr(r, k, v)
    return r


class _FakeVictronScanner:
    """Stand-in for VictronScanner — records constructor args and scans."""
    instances: list = []

    def __init__(self, macs):
        self.macs = list(macs)
        self.scan_calls = []
        _FakeVictronScanner.instances.append(self)

    async def scan(self, duration):
        self.scan_calls.append(duration)


class _EntryPointCase(unittest.TestCase):
    """Shared fixture: tmp dir with config/state/output paths, logging guard."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir        = self._tmp.name
        self.ini_path   = os.path.join(self.dir, "monitor.ini")
        self.state_path = os.path.join(self.dir, "state.json")
        self.out_path   = os.path.join(self.dir, "dash.html")
        self._root_level = logging.getLogger().level
        _FakeVictronScanner.instances = []

    def tearDown(self):
        logging.getLogger().setLevel(self._root_level)
        self._tmp.cleanup()

    def write_ini(self, content: str) -> str:
        Path(self.ini_path).write_text(textwrap.dedent(content), encoding="utf-8")
        return self.ini_path

    def run_main(self, module, argv: list):
        with patch.object(sys, "argv", [module.__name__ + ".py"] + argv):
            asyncio.run(module.main())

    def base_args(self, *extra) -> list:
        return ["--config", self.ini_path,
                "--state-file", self.state_path,
                "--output", self.out_path,
                "--once", *extra]


# ─────────────────────────────────────────────────────────────────────────────
# 1. bms_monitor.py — argument parsing
# ─────────────────────────────────────────────────────────────────────────────

class TestBmsArgParsing(_EntryPointCase):

    def test_invalid_log_level_exits_code_2(self):
        with self.assertRaises(SystemExit) as ctx:
            self.run_main(bms_mod, ["--log-level", "BOGUS"])
        self.assertEqual(ctx.exception.code, 2)

    def test_invalid_theme_exits_code_2(self):
        with self.assertRaises(SystemExit) as ctx:
            self.run_main(bms_mod, ["--theme", "neon"])
        self.assertEqual(ctx.exception.code, 2)

    def test_unknown_flag_exits_code_2(self):
        with self.assertRaises(SystemExit) as ctx:
            self.run_main(bms_mod, ["--no-such-flag"])
        self.assertEqual(ctx.exception.code, 2)

    def test_all_worker_contract_flags_accepted(self):
        """--config / --state-file / --log-level / --once (+ extras) must parse."""
        self.write_ini("[general]\n")
        with patch.object(bms_mod, "_poll_bms", AsyncMock(return_value=[])):
            self.run_main(bms_mod, self.base_args(
                "--log-level", "WARNING", "--theme", "light",
                "--interval", "300", "--scan-timeout", "5",
            ))  # must not raise
        self.assertTrue(Path(self.state_path).exists())


# ─────────────────────────────────────────────────────────────────────────────
# 2. bms_monitor.py — --once flow
# ─────────────────────────────────────────────────────────────────────────────

class TestBmsOnceFlow(_EntryPointCase):

    BMS_INI = """
        [general]
        log_level = ERROR
        [bms]
        House Bank = AA:BB:CC:DD:EE:01 : 0000
    """

    def test_once_writes_bms_section_to_state_file(self):
        self.write_ini(self.BMS_INI)
        stub = AsyncMock(return_value=[_bms_reading(voltage_v=54.32)])
        with patch.object(bms_mod, "_poll_bms", stub):
            self.run_main(bms_mod, self.base_args())

        raw = json.loads(Path(self.state_path).read_text())
        self.assertIn("bms", raw)
        self.assertEqual(len(raw["bms"]["readings"]), 1)
        self.assertEqual(raw["bms"]["readings"][0]["name"], "House Bank")
        self.assertAlmostEqual(raw["bms"]["readings"][0]["voltage_v"],
                               54.32, places=2)
        self.assertIsNotNone(raw["bms"]["updated"])

    def test_poller_receives_configured_devices(self):
        self.write_ini(self.BMS_INI)
        stub = AsyncMock(return_value=[])
        with patch.object(bms_mod, "_poll_bms", stub):
            self.run_main(bms_mod, self.base_args())

        stub.assert_awaited_once()
        devices = stub.await_args.args[0]
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0].mac,      "AA:BB:CC:DD:EE:01")
        self.assertEqual(devices[0].password, "0000")

    def test_state_file_cli_overrides_ini(self):
        ini_state = os.path.join(self.dir, "from_ini.json")
        self.write_ini(f"""
            [general]
            state_file = {ini_state}
            [bms]
            House Bank = AA:BB:CC:DD:EE:01
        """)
        with patch.object(bms_mod, "_poll_bms",
                          AsyncMock(return_value=[_bms_reading()])):
            self.run_main(bms_mod, self.base_args())
        self.assertTrue(Path(self.state_path).exists(),
                        "--state-file path must be used")
        self.assertFalse(Path(ini_state).exists(),
                         "INI state_file must be overridden by --state-file")

    def test_no_devices_configured_writes_empty_section(self):
        self.write_ini("[general]\n")
        stub = AsyncMock(return_value=[])
        with patch.object(bms_mod, "_poll_bms", stub):
            self.run_main(bms_mod, self.base_args())
        stub.assert_not_awaited()
        raw = json.loads(Path(self.state_path).read_text())
        self.assertEqual(raw["bms"]["readings"], [])

    def test_poll_exception_is_recoverable_and_writes_empty_state(self):
        """A failed poll must not abort the cycle — empty section written."""
        self.write_ini(self.BMS_INI)
        stub = AsyncMock(side_effect=RuntimeError("BLE adapter gone"))
        with patch.object(bms_mod, "_poll_bms", stub):
            self.run_main(bms_mod, self.base_args())   # must not raise
        raw = json.loads(Path(self.state_path).read_text())
        self.assertEqual(raw["bms"]["readings"], [])

    def test_dashboard_html_written(self):
        self.write_ini(self.BMS_INI)
        with patch.object(bms_mod, "_poll_bms",
                          AsyncMock(return_value=[_bms_reading()])):
            self.run_main(bms_mod, self.base_args())
        self.assertTrue(Path(self.out_path).exists())
        self.assertIn("<html", Path(self.out_path).read_text().lower())

    def test_existing_victron_section_preserved(self):
        """BMS worker must never clobber the Victron worker's section."""
        from solar_monitor.state import save_section
        save_section(self.state_path, "victron", [_victron_reading()])
        self.write_ini(self.BMS_INI)
        with patch.object(bms_mod, "_poll_bms",
                          AsyncMock(return_value=[_bms_reading()])):
            self.run_main(bms_mod, self.base_args())
        state = load_state(self.state_path)
        self.assertEqual(len(state["victron"]["readings"]), 1)
        self.assertEqual(len(state["bms"]["readings"]),     1)

    def test_log_level_cli_overrides_ini(self):
        self.write_ini("[general]\nlog_level = ERROR\n")
        with patch.object(bms_mod, "_poll_bms", AsyncMock(return_value=[])):
            self.run_main(bms_mod, self.base_args("--log-level", "DEBUG"))
        self.assertEqual(logging.getLogger().level, logging.DEBUG)

    def test_log_level_from_ini_applied(self):
        self.write_ini("[general]\nlog_level = WARNING\n")
        with patch.object(bms_mod, "_poll_bms", AsyncMock(return_value=[])):
            self.run_main(bms_mod, self.base_args())
        self.assertEqual(logging.getLogger().level, logging.WARNING)

    def test_run_entry_point_once(self):
        """Synchronous run() wrapper (incl. --log-level= pre-parse) works."""
        self.write_ini(self.BMS_INI)
        argv = ["bms_monitor.py", "--config", self.ini_path,
                "--state-file", self.state_path, "--output", self.out_path,
                "--once", "--log-level=ERROR"]
        with patch.object(sys, "argv", argv), \
             patch.object(bms_mod, "_poll_bms",
                          AsyncMock(return_value=[_bms_reading()])):
            bms_mod.run()
        raw = json.loads(Path(self.state_path).read_text())
        self.assertEqual(len(raw["bms"]["readings"]), 1)


# ─────────────────────────────────────────────────────────────────────────────
# 3. bms_monitor.py — unrecoverable errors propagate (non-zero exit contract)
# ─────────────────────────────────────────────────────────────────────────────

class TestBmsUnrecoverableError(_EntryPointCase):

    def test_bad_interval_value_in_ini_raises(self):
        """A config the worker cannot parse must escape main() so the
        process exits non-zero and the supervisor restarts it."""
        self.write_ini("[general]\ninterval = notanumber\n")
        with self.assertRaises(ValueError):
            self.run_main(bms_mod, self.base_args())


# ─────────────────────────────────────────────────────────────────────────────
# 4. victron_monitor.py — argument parsing
# ─────────────────────────────────────────────────────────────────────────────

class TestVictronArgParsing(_EntryPointCase):

    def test_invalid_log_level_exits_code_2(self):
        with self.assertRaises(SystemExit) as ctx:
            self.run_main(vic_mod, ["--log-level", "trace"])
        self.assertEqual(ctx.exception.code, 2)

    def test_unknown_flag_exits_code_2(self):
        with self.assertRaises(SystemExit) as ctx:
            self.run_main(vic_mod, ["--frequency", "10"])
        self.assertEqual(ctx.exception.code, 2)

    def test_all_worker_contract_flags_accepted(self):
        self.write_ini("[general]\n")
        with patch.object(vic_mod, "VictronScanner", _FakeVictronScanner), \
             patch.object(vic_mod, "_poll_victron", MagicMock(return_value=[])):
            self.run_main(vic_mod, self.base_args(
                "--log-level", "ERROR", "--theme", "business",
                "--interval", "15", "--scan-timeout", "2",
            ))
        self.assertTrue(Path(self.state_path).exists())


# ─────────────────────────────────────────────────────────────────────────────
# 5. victron_monitor.py — --once flow
# ─────────────────────────────────────────────────────────────────────────────

class TestVictronOnceFlow(_EntryPointCase):

    VIC_INI = """
        [general]
        log_level = ERROR
        [victron]
        Roof MPPT = AA:BB:CC:DD:EE:F1 : 00112233445566778899aabbccddeeff
    """

    def _run_once(self, poll_return=None, poll_side_effect=None, extra=()):
        poll = MagicMock(return_value=poll_return or [],
                         side_effect=poll_side_effect)
        with patch.object(vic_mod, "VictronScanner", _FakeVictronScanner), \
             patch.object(vic_mod, "_poll_victron", poll):
            self.run_main(vic_mod, self.base_args(*extra))
        return poll

    def test_once_writes_victron_section_to_state_file(self):
        self.write_ini(self.VIC_INI)
        self._run_once(poll_return=[_victron_reading(pv_power_w=600.0)])
        raw = json.loads(Path(self.state_path).read_text())
        self.assertIn("victron", raw)
        self.assertEqual(len(raw["victron"]["readings"]), 1)
        self.assertEqual(raw["victron"]["readings"][0]["name"], "Roof Mppt")
        self.assertAlmostEqual(raw["victron"]["readings"][0]["pv_power_w"],
                               600.0, places=1)
        self.assertIsNotNone(raw["victron"]["updated"])

    def test_scanner_constructed_with_configured_macs(self):
        self.write_ini(self.VIC_INI)
        self._run_once()
        self.assertEqual(len(_FakeVictronScanner.instances), 1)
        self.assertEqual(_FakeVictronScanner.instances[0].macs,
                         ["AA:BB:CC:DD:EE:F1"])

    def test_scan_called_with_cli_scan_timeout(self):
        self.write_ini(self.VIC_INI)
        self._run_once(extra=("--scan-timeout", "3.5"))
        scanner = _FakeVictronScanner.instances[0]
        self.assertEqual(scanner.scan_calls, [3.5])

    def test_poller_receives_devices_and_scanner(self):
        self.write_ini(self.VIC_INI)
        poll = self._run_once()
        poll.assert_called_once()
        devices, scanner = poll.call_args.args
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0].mac,     "AA:BB:CC:DD:EE:F1")
        self.assertEqual(devices[0].enc_key, "00112233445566778899aabbccddeeff")
        self.assertIs(scanner, _FakeVictronScanner.instances[0])

    def test_no_devices_no_scan_and_empty_section(self):
        self.write_ini("[general]\n")
        poll = self._run_once()
        poll.assert_not_called()
        self.assertEqual(_FakeVictronScanner.instances[0].scan_calls, [])
        raw = json.loads(Path(self.state_path).read_text())
        self.assertEqual(raw["victron"]["readings"], [])

    def test_poll_exception_is_recoverable_and_writes_empty_state(self):
        self.write_ini(self.VIC_INI)
        self._run_once(poll_side_effect=RuntimeError("decrypt failed"))
        raw = json.loads(Path(self.state_path).read_text())
        self.assertEqual(raw["victron"]["readings"], [])

    def test_existing_bms_section_preserved(self):
        """Victron worker must never clobber the BMS worker's section."""
        from solar_monitor.state import save_section
        save_section(self.state_path, "bms", [_bms_reading()])
        self.write_ini(self.VIC_INI)
        self._run_once(poll_return=[_victron_reading()])
        state = load_state(self.state_path)
        self.assertEqual(len(state["bms"]["readings"]),     1)
        self.assertEqual(len(state["victron"]["readings"]), 1)

    def test_dashboard_html_written(self):
        self.write_ini(self.VIC_INI)
        self._run_once(poll_return=[_victron_reading()])
        self.assertTrue(Path(self.out_path).exists())
        self.assertIn("<html", Path(self.out_path).read_text().lower())

    def test_bad_interval_value_in_ini_raises(self):
        self.write_ini("[general]\nvictron_interval = fast\n")
        with self.assertRaises(ValueError):
            self._run_once()


# ─────────────────────────────────────────────────────────────────────────────
# 6. solar_monitor/__main__.py — mode dispatch
# ─────────────────────────────────────────────────────────────────────────────

class TestMainModuleDispatch(_EntryPointCase):

    def test_write_example_config_writes_file_and_skips_polling(self):
        target = os.path.join(self.dir, "example.ini")
        poll = AsyncMock()
        with patch.object(main_mod, "poll_all", poll):
            self.run_main(main_mod,
                          ["--write-example-config", target])
        self.assertTrue(Path(target).exists())
        content = Path(target).read_text()
        self.assertIn("[general]", content)
        self.assertIn("[bms]",     content)
        self.assertIn("[victron]", content)
        poll.assert_not_awaited()

    def test_write_example_config_default_filename(self):
        """Bare --write-example-config uses the const default in cwd."""
        prev = os.getcwd()
        os.chdir(self.dir)
        try:
            with patch.object(main_mod, "poll_all", AsyncMock()):
                self.run_main(main_mod, ["--write-example-config"])
            self.assertTrue(
                Path(self.dir, "monitor.ini.example").exists())
        finally:
            os.chdir(prev)

    def test_invalid_theme_exits_code_2(self):
        with self.assertRaises(SystemExit) as ctx:
            self.run_main(main_mod, ["--theme", "neon"])
        self.assertEqual(ctx.exception.code, 2)

    def test_invalid_log_level_exits_code_2(self):
        with self.assertRaises(SystemExit) as ctx:
            self.run_main(main_mod, ["--log-level", "VERBOSE"])
        self.assertEqual(ctx.exception.code, 2)


# ─────────────────────────────────────────────────────────────────────────────
# 7. solar_monitor/__main__.py — --once polling flow
# ─────────────────────────────────────────────────────────────────────────────

class TestMainModuleOnceFlow(_EntryPointCase):

    def _ini(self, extra=""):
        return self.write_ini(f"""
            [general]
            log_level  = ERROR
            state_file = {self.state_path}
            {extra}
        """)

    def _once_args(self, *extra) -> list:
        # NB: __main__ has no --state-file flag; state_file comes from INI.
        return ["--config", self.ini_path, "--output", self.out_path,
                "--once", *extra]

    def test_once_writes_both_state_sections_and_dashboard(self):
        self._ini()
        poll = AsyncMock(return_value=([_bms_reading()], [_victron_reading()]))
        with patch.object(main_mod, "poll_all", poll):
            self.run_main(main_mod, self._once_args())

        poll.assert_awaited_once()
        raw = json.loads(Path(self.state_path).read_text())
        self.assertEqual(len(raw["bms"]["readings"]),     1)
        self.assertEqual(len(raw["victron"]["readings"]), 1)
        self.assertTrue(Path(self.out_path).exists())
        html = Path(self.out_path).read_text()
        self.assertIn("<html", html.lower())

    def test_poll_failure_recovered_with_empty_readings(self):
        self._ini()
        poll = AsyncMock(side_effect=RuntimeError("adapter reset"))
        with patch.object(main_mod, "poll_all", poll):
            self.run_main(main_mod, self._once_args())   # must not raise
        raw = json.loads(Path(self.state_path).read_text())
        self.assertEqual(raw["bms"]["readings"],     [])
        self.assertEqual(raw["victron"]["readings"], [])

    def test_bms_cli_override_replaces_ini_devices(self):
        self._ini(extra="[bms]\nIni Bank = 11:22:33:44:55:66\n")
        poll = AsyncMock(return_value=([], []))
        with patch.object(main_mod, "poll_all", poll):
            self.run_main(main_mod,
                          self._once_args("--bms", "aa-bb-cc-dd-ee-ff"))
        bms_devices = poll.await_args.args[0]
        self.assertEqual(len(bms_devices), 1)
        self.assertEqual(bms_devices[0].mac, "AA:BB:CC:DD:EE:FF")

    def test_mppt_cli_override_parses_mac_and_key(self):
        self._ini()
        poll = AsyncMock(return_value=([], []))
        key = "aabbccddeeff00112233445566778899"
        with patch.object(main_mod, "poll_all", poll):
            self.run_main(main_mod, self._once_args(
                "--mppt", f"AA:BB:CC:DD:EE:FF:{key}"))
        mppt_devices = poll.await_args.args[1]
        self.assertEqual(len(mppt_devices), 1)
        self.assertEqual(mppt_devices[0].mac,     "AA:BB:CC:DD:EE:FF")
        self.assertEqual(mppt_devices[0].enc_key, key)

    def test_scan_timeout_cli_override_passed_to_poll(self):
        self._ini()
        poll = AsyncMock(return_value=([], []))
        with patch.object(main_mod, "poll_all", poll):
            self.run_main(main_mod, self._once_args("--scan-timeout", "7.5"))
        self.assertEqual(poll.await_args.args[2], 7.5)

    def test_ini_devices_used_when_no_cli_override(self):
        self._ini(extra="[bms]\nHouse Bank = AA:BB:CC:DD:EE:01 : 0000\n")
        poll = AsyncMock(return_value=([], []))
        with patch.object(main_mod, "poll_all", poll):
            self.run_main(main_mod, self._once_args())
        bms_devices = poll.await_args.args[0]
        self.assertEqual(len(bms_devices), 1)
        self.assertEqual(bms_devices[0].name, "House Bank")
        self.assertEqual(bms_devices[0].mac,  "AA:BB:CC:DD:EE:01")

    def test_log_level_cli_override_applied(self):
        self._ini()   # INI says ERROR
        with patch.object(main_mod, "poll_all",
                          AsyncMock(return_value=([], []))):
            self.run_main(main_mod, self._once_args("--log-level", "DEBUG"))
        self.assertEqual(logging.getLogger().level, logging.DEBUG)

    def test_bad_interval_value_in_ini_raises(self):
        self.write_ini("[general]\ninterval = soon\n")
        with patch.object(main_mod, "poll_all",
                          AsyncMock(return_value=([], []))):
            with self.assertRaises(ValueError):
                self.run_main(main_mod, self._once_args())

    def test_run_entry_point_once(self):
        """run() wrapper drives one full cycle via asyncio.run."""
        self._ini()
        argv = ["solar_monitor", "--config", self.ini_path,
                "--output", self.out_path, "--once", "--log-level", "ERROR"]
        with patch.object(sys, "argv", argv), \
             patch.object(main_mod, "poll_all",
                          AsyncMock(return_value=([_bms_reading()], []))):
            main_mod.run()
        raw = json.loads(Path(self.state_path).read_text())
        self.assertEqual(len(raw["bms"]["readings"]), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
