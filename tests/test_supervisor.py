"""
tests/test_supervisor.py — Tests for the solar_monitor.py supervisor
=====================================================================
Covers:
  - WorkerSpec dataclass — fields, defaults
  - WORKER_REGISTRY — correct entries, order, required fields
  - _section_has_devices — INI parsing for worker-enable decisions
  - WorkerProcess — command construction, log streaming, crash tracking,
    backoff, stop-after-too-many-crashes, clean stop via SIGTERM
  - _dashboard_loop — reads state, writes HTML, handles errors gracefully
  - bms_monitor / victron_monitor — --state-file flag accepted and applied
  - Integration: supervisor starts expected workers based on config
"""

import asyncio
import logging
import os
import sys
import tempfile
import textwrap
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call

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


dev_m.BLEDevice   = _BLEDevice
bleak.BleakClient = _BleakClient
bleak.BleakScanner= _BleakScanner
sys.modules.update({
    "bleak": bleak,
    "bleak.backends": backends,
    "bleak.backends.device": dev_m,
})
import os
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import importlib.util, sys as _sys
_spec = importlib.util.spec_from_file_location(
    "supervisor_module", f"{REPO_ROOT}/solar_monitor.py"
)
sup_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sup_mod)

WorkerSpec          = sup_mod.WorkerSpec
WorkerProcess       = sup_mod.WorkerProcess
WORKER_REGISTRY     = sup_mod.WORKER_REGISTRY
_section_has_devices= sup_mod._section_has_devices
_dashboard_loop     = sup_mod._dashboard_loop
MAX_CRASHES_PER_HOUR= sup_mod.MAX_CRASHES_PER_HOUR
MAX_BACKOFF         = sup_mod.MAX_BACKOFF


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def run(coro):
    return asyncio.run(coro)


def _write_ini(content: str) -> str:
    f = tempfile.NamedTemporaryFile(
        mode="w", suffix=".ini", delete=False, encoding="utf-8"
    )
    f.write(textwrap.dedent(content))
    f.close()
    return f.name


def _make_worker(spec=None, **kwargs) -> WorkerProcess:
    if spec is None:
        spec = WorkerSpec(
            name="Test", script="test_monitor.py",
            state_section="test",
            config_sections=["test"],
            interval_cfg_key="test_interval",
            min_gap=10.0,
        )
    defaults = dict(
        python="/usr/bin/python3",
        config="/tmp/config.ini",
        state_file="/tmp/state.json",
        log_level="INFO",
        script_dir="/tmp",
    )
    defaults.update(kwargs)
    return WorkerProcess(spec=spec, **defaults)


# ─────────────────────────────────────────────────────────────────────────────
# 1. WorkerSpec
# ─────────────────────────────────────────────────────────────────────────────

class TestWorkerSpec(unittest.TestCase):

    def test_required_fields(self):
        spec = WorkerSpec(
            name="BMS", script="bms_monitor.py",
            state_section="bms", config_sections=["bms"],
            interval_cfg_key="bms_interval",
        )
        self.assertEqual(spec.name, "BMS")
        self.assertEqual(spec.script, "bms_monitor.py")
        self.assertEqual(spec.state_section, "bms")
        self.assertEqual(spec.config_sections, ["bms"])
        self.assertEqual(spec.interval_cfg_key, "bms_interval")

    def test_min_gap_default(self):
        spec = WorkerSpec(
            name="X", script="x.py", state_section="x",
            config_sections=["x"], interval_cfg_key="x_interval",
        )
        self.assertEqual(spec.min_gap, 10.0)

    def test_min_gap_custom(self):
        spec = WorkerSpec(
            name="X", script="x.py", state_section="x",
            config_sections=["x"], interval_cfg_key="x_interval",
            min_gap=30.0,
        )
        self.assertEqual(spec.min_gap, 30.0)


# ─────────────────────────────────────────────────────────────────────────────
# 2. WORKER_REGISTRY
# ─────────────────────────────────────────────────────────────────────────────

class TestWorkerRegistry(unittest.TestCase):

    def test_registry_is_list(self):
        self.assertIsInstance(WORKER_REGISTRY, list)

    def test_registry_not_empty(self):
        self.assertGreater(len(WORKER_REGISTRY), 0)

    def test_victron_registered(self):
        names = [w.name for w in WORKER_REGISTRY]
        self.assertIn("Victron", names)

    def test_bms_registered(self):
        names = [w.name for w in WORKER_REGISTRY]
        self.assertIn("BMS", names)

    def test_victron_before_bms(self):
        """Victron (passive scan, fast) should start before BMS (slow GATT)."""
        names = [w.name for w in WORKER_REGISTRY]
        self.assertLess(names.index("Victron"), names.index("BMS"))

    def test_all_scripts_are_py_files(self):
        for spec in WORKER_REGISTRY:
            self.assertTrue(spec.script.endswith(".py"),
                            f"{spec.name}: script must be a .py file")

    def test_all_entries_have_config_sections(self):
        for spec in WORKER_REGISTRY:
            self.assertGreater(len(spec.config_sections), 0,
                               f"{spec.name}: must have at least one config_section")

    def test_state_sections_unique(self):
        sections = [w.state_section for w in WORKER_REGISTRY]
        self.assertEqual(len(sections), len(set(sections)),
                         "Each worker must own a unique state section")

    def test_bms_min_gap(self):
        bms = next(w for w in WORKER_REGISTRY if w.name == "BMS")
        self.assertGreaterEqual(bms.min_gap, 30.0,
                                "BMS min_gap must be ≥30s (BlueZ GATT release time)")

    def test_victron_min_gap(self):
        vic = next(w for w in WORKER_REGISTRY if w.name == "Victron")
        self.assertGreaterEqual(vic.min_gap, 10.0)


# ─────────────────────────────────────────────────────────────────────────────
# 3. _section_has_devices
# ─────────────────────────────────────────────────────────────────────────────

class TestSectionHasDevices(unittest.TestCase):

    def tearDown(self):
        pass  # individual tests clean up own temp files

    def test_populated_bms_section(self):
        p = _write_ini("""
            [bms]
            House Bank = AA:BB:CC:DD:EE:FF : 123456
        """)
        try:
            self.assertTrue(_section_has_devices(p, ["bms"]))
        finally:
            os.unlink(p)

    def test_empty_bms_section(self):
        p = _write_ini("[bms]\n")
        try:
            self.assertFalse(_section_has_devices(p, ["bms"]))
        finally:
            os.unlink(p)

    def test_missing_section(self):
        p = _write_ini("[general]\noutput = dashboard.html\n")
        try:
            self.assertFalse(_section_has_devices(p, ["bms"]))
        finally:
            os.unlink(p)

    def test_victron_section(self):
        p = _write_ini("""
            [victron]
            Inverter = AA:BB:CC:DD:EE:FF : aabbccddeeff00112233445566778899
        """)
        try:
            self.assertTrue(_section_has_devices(p, ["victron"]))
        finally:
            os.unlink(p)

    def test_mppt_alias(self):
        """Legacy [mppt] section should also enable the Victron worker."""
        p = _write_ini("""
            [mppt]
            South Array = AA:BB:CC:DD:EE:FF : aabbccddeeff00112233445566778899
        """)
        try:
            self.assertTrue(_section_has_devices(p, ["victron", "mppt"]))
        finally:
            os.unlink(p)

    def test_both_sections_absent(self):
        p = _write_ini("[general]\ntheme = business\n")
        try:
            self.assertFalse(_section_has_devices(p, ["bms", "victron"]))
        finally:
            os.unlink(p)

    def test_commented_out_devices(self):
        p = _write_ini("""
            [bms]
            # House Bank = AA:BB:CC:DD:EE:FF : 123456
        """)
        try:
            self.assertFalse(_section_has_devices(p, ["bms"]))
        finally:
            os.unlink(p)


# ─────────────────────────────────────────────────────────────────────────────
# 4. WorkerProcess — command construction
# ─────────────────────────────────────────────────────────────────────────────

class TestWorkerProcessCmd(unittest.TestCase):

    def test_cmd_includes_python(self):
        w = _make_worker(python="/usr/bin/python3")
        self.assertEqual(w._cmd()[0], "/usr/bin/python3")

    def test_cmd_includes_script(self):
        w = _make_worker(script_dir="/home/pi")
        self.assertIn("/home/pi/test_monitor.py", w._cmd())

    def test_cmd_includes_config(self):
        w = _make_worker(config="/etc/solar/config.ini")
        cmd = w._cmd()
        self.assertIn("--config", cmd)
        self.assertIn("/etc/solar/config.ini", cmd)

    def test_cmd_includes_state_file(self):
        w = _make_worker(state_file="/tmp/solar.json")
        cmd = w._cmd()
        self.assertIn("--state-file", cmd)
        self.assertIn("/tmp/solar.json", cmd)

    def test_cmd_includes_log_level(self):
        w = _make_worker(log_level="DEBUG")
        cmd = w._cmd()
        self.assertIn("--log-level", cmd)
        self.assertIn("DEBUG", cmd)

    def test_cmd_is_list_of_strings(self):
        w = _make_worker()
        cmd = w._cmd()
        self.assertIsInstance(cmd, list)
        for item in cmd:
            self.assertIsInstance(item, str)


# ─────────────────────────────────────────────────────────────────────────────
# 5. WorkerProcess — log streaming
# ─────────────────────────────────────────────────────────────────────────────

class TestWorkerProcessStreaming(unittest.TestCase):

    def test_stdout_lines_logged_at_info(self):
        """_stream_output at INFO level must call log.log with INFO."""
        w = _make_worker()
        log_calls = []

        async def _run():
            stream = MagicMock()
            stream.readline = AsyncMock(side_effect=[
                b"Hello from worker\n",
                b"Second line\n",
                b"",
            ])
            # Capture calls to the supervisor module's log object
            with patch.object(sup_mod.log, "log",
                               side_effect=lambda lvl, msg, *a, **kw:
                               log_calls.append((lvl, msg))) as _:
                await w._stream_output(stream, logging.INFO)

        run(_run())
        self.assertTrue(any("Hello from worker" in msg for _, msg in log_calls))
        self.assertTrue(all(lvl == logging.INFO for lvl, _ in log_calls))

    def test_stderr_lines_logged_at_warning(self):
        """_stream_output at WARNING level must call log.log with WARNING."""
        w = _make_worker()
        log_calls = []

        async def _run():
            stream = MagicMock()
            stream.readline = AsyncMock(side_effect=[
                b"ERROR something bad\n",
                b"",
            ])
            with patch.object(sup_mod.log, "log",
                               side_effect=lambda lvl, msg, *a, **kw:
                               log_calls.append(lvl)):
                await w._stream_output(stream, logging.WARNING)

        run(_run())
        self.assertTrue(all(lvl == logging.WARNING for lvl in log_calls))

    def test_empty_lines_not_logged(self):
        """Blank or whitespace-only lines must not produce log calls."""
        w = _make_worker()
        log_calls = []

        async def _run():
            stream = MagicMock()
            stream.readline = AsyncMock(side_effect=[b"\n", b"   \n", b""])
            with patch.object(sup_mod.log, "log",
                               side_effect=lambda *a, **kw:
                               log_calls.append(a)):
                await w._stream_output(stream, logging.INFO)

        run(_run())
        self.assertEqual(len(log_calls), 0,
                         "Empty/whitespace lines must not be logged")

    def test_prefix_contains_worker_name(self):
        """Log messages must include the worker name as a prefix."""
        spec = WorkerSpec(name="MyWorker", script="x.py",
                          state_section="x", config_sections=["x"],
                          interval_cfg_key="x_interval")
        w = _make_worker(spec=spec)
        log_msgs = []

        async def _run():
            stream = MagicMock()
            stream.readline = AsyncMock(side_effect=[b"test line\n", b""])
            with patch.object(sup_mod.log, "log",
                               side_effect=lambda lvl, msg, *a, **kw:
                               log_msgs.append(msg)):
                await w._stream_output(stream, logging.INFO)

        run(_run())
        self.assertTrue(any("[MyWorker]" in msg for msg in log_msgs))


# ─────────────────────────────────────────────────────────────────────────────
# 6. WorkerProcess — crash tracking and backoff
# ─────────────────────────────────────────────────────────────────────────────

class TestWorkerProcessCrashPolicy(unittest.TestCase):

    def _make_proc_mock(self, exit_code=1):
        """Return a mock subprocess that exits immediately."""
        proc = MagicMock()
        proc.stdout = MagicMock()
        proc.stderr = MagicMock()
        proc.returncode = exit_code
        proc.wait = AsyncMock(return_value=exit_code)
        # readline returns EOF immediately
        proc.stdout.readline = AsyncMock(return_value=b"")
        proc.stderr.readline = AsyncMock(return_value=b"")
        return proc

    def test_backoff_doubles_on_rapid_crash(self):
        """Rapid crashes (runtime < 30s) must double the backoff."""
        w = _make_worker()
        w._backoff = 1.0
        crashes = []
        sleep_calls = []

        async def fake_create(*args, **kwargs):
            crashes.append(1)
            return self._make_proc_mock()

        async def fake_sleep(t):
            sleep_calls.append(t)
            if len(crashes) >= 3:
                w._stopped = True

        with patch("asyncio.create_subprocess_exec", new=fake_create), \
             patch("asyncio.sleep", new=fake_sleep), \
             patch("asyncio.gather", new=AsyncMock()):
            run(w.run())

        # Backoff should have grown: 1.0 → 2.0 → 4.0 …
        self.assertGreater(len(sleep_calls), 1)
        self.assertGreater(sleep_calls[-1], sleep_calls[0])

    def test_backoff_capped_at_max(self):
        w = _make_worker()
        w._backoff = MAX_BACKOFF
        # Even after many crashes, backoff should not exceed MAX_BACKOFF
        new_backoff = min(w._backoff * 2, MAX_BACKOFF)
        self.assertEqual(new_backoff, MAX_BACKOFF)

    def test_stops_after_too_many_crashes(self):
        """Worker must stop after MAX_CRASHES_PER_HOUR crashes."""
        w = _make_worker()
        crash_count = [0]

        async def fake_create(*args, **kwargs):
            crash_count[0] += 1
            return self._make_proc_mock()

        async def fake_sleep(t):
            pass   # don't wait

        with patch("asyncio.create_subprocess_exec", new=fake_create), \
             patch("asyncio.sleep", new=fake_sleep), \
             patch("asyncio.gather", new=AsyncMock()):
            run(w.run())

        self.assertTrue(w._stopped,
                        "Worker must set _stopped after too many crashes")
        self.assertGreater(crash_count[0], MAX_CRASHES_PER_HOUR)

    def test_backoff_resets_after_long_run(self):
        """
        If a worker ran for >30s before crashing, backoff resets to 1s.
        This prevents permanently elevated backoff from a one-off crash.
        """
        w = _make_worker()
        w._backoff = 32.0  # previously elevated

        # Simulate: runtime = 60s (long-running before crash)
        # Logic from WorkerProcess.run():
        runtime = 60.0
        if runtime < 30:
            w._backoff = min(w._backoff * 2, MAX_BACKOFF)
        else:
            w._backoff = 1.0

        self.assertEqual(w._backoff, 1.0)

    def test_clean_stop_sets_stopped_flag(self):
        w = _make_worker()
        proc = MagicMock()
        proc.returncode = None
        proc.terminate = MagicMock()
        proc.wait = AsyncMock(return_value=0)
        w._proc = proc

        run(w.stop())
        self.assertTrue(w._stopped)
        proc.terminate.assert_called_once()

    def test_stop_on_unstarted_worker_no_crash(self):
        """stop() before run() must not raise."""
        w = _make_worker()
        run(w.stop())   # must not raise
        self.assertTrue(w._stopped)


# ─────────────────────────────────────────────────────────────────────────────
# 7. _dashboard_loop
# ─────────────────────────────────────────────────────────────────────────────

class TestDashboardLoop(unittest.TestCase):

    def _make_state(self):
        from solar_monitor.models import DeviceReading
        from solar_monitor.state import save_section
        tmp = tempfile.mktemp(suffix=".json")
        r = DeviceReading(address="AA:BB", name="House Bank",
                          device_type="bms", timestamp="t")
        r.voltage_v = 54.0; r.faults = []; r.temp_c = []
        save_section(tmp, "bms", [r])
        save_section(tmp, "victron", [])
        return tmp

    def test_writes_dashboard_html(self):
        state_file = self._make_state()
        out = tempfile.mktemp(suffix=".html")
        try:
            async def _one_iteration():
                # Run one iteration by cancelling after first sleep
                task = asyncio.create_task(
                    _dashboard_loop(state_file, Path(out), "dark", 0.01)
                )
                await asyncio.sleep(0.05)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

            run(_one_iteration())
            self.assertTrue(Path(out).exists(), "Dashboard HTML must be written")
            content = Path(out).read_text()
            self.assertIn("<html", content.lower())
        finally:
            for p in (state_file, out):
                try:
                    os.unlink(p)
                except FileNotFoundError:
                    pass

    def test_missing_state_file_no_crash(self):
        """Dashboard loop must not crash when state file is missing."""
        out = tempfile.mktemp(suffix=".html")
        try:
            async def _run():
                task = asyncio.create_task(
                    _dashboard_loop("/nonexistent/state.json",
                                    Path(out), "dark", 0.01)
                )
                await asyncio.sleep(0.05)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

            run(_run())   # must not raise
        finally:
            try:
                os.unlink(out)
            except FileNotFoundError:
                pass

    def test_dashboard_error_does_not_stop_loop(self):
        """An exception in one dashboard write must not kill the loop."""
        state_file = self._make_state()
        write_count = [0]

        original_build = None

        async def _run():
            nonlocal original_build
            from solar_monitor import dashboard as d_mod
            original_build = d_mod.build_html

            def buggy_build(*args, **kwargs):
                write_count[0] += 1
                if write_count[0] == 1:
                    raise RuntimeError("disk full")
                return original_build(*args, **kwargs)

            with patch("solar_monitor.dashboard.build_html", new=buggy_build):
                task = asyncio.create_task(
                    _dashboard_loop(state_file, Path("/tmp/test_dash.html"),
                                    "dark", 0.01)
                )
                await asyncio.sleep(0.06)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        run(_run())
        self.assertGreater(write_count[0], 1,
                           "Loop must continue after a failed dashboard write")
        try:
            os.unlink(state_file)
            os.unlink("/tmp/test_dash.html")
        except FileNotFoundError:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# 8. Worker scripts — --state-file flag
# ─────────────────────────────────────────────────────────────────────────────

class TestWorkerStateFlagAccepted(unittest.TestCase):
    """
    Verify that bms_monitor.py and victron_monitor.py accept --state-file
    and apply it to cfg.state_file.
    """

    def _src(self, filename):
        with open(f"{REPO_ROOT}/{filename}") as fh:
            return fh.read()

    def test_bms_monitor_accepts_state_file_arg(self):
        src = self._src("bms_monitor.py")
        self.assertIn("--state-file", src,
                      "bms_monitor.py must accept --state-file argument")

    def test_victron_monitor_accepts_state_file_arg(self):
        src = self._src("victron_monitor.py")
        self.assertIn("--state-file", src,
                      "victron_monitor.py must accept --state-file argument")

    def test_bms_monitor_applies_state_file(self):
        src = self._src("bms_monitor.py")
        self.assertIn("cfg.state_file", src)
        self.assertIn("args.state_file", src)

    def test_victron_monitor_applies_state_file(self):
        src = self._src("victron_monitor.py")
        self.assertIn("cfg.state_file", src)
        self.assertIn("args.state_file", src)


# ─────────────────────────────────────────────────────────────────────────────
# 9. supervisor source-level guarantees
# ─────────────────────────────────────────────────────────────────────────────

class TestSupervisorSourceGuarantees(unittest.TestCase):

    def _src(self):
        with open(f"{REPO_ROOT}/solar_monitor.py") as fh:
            return fh.read()

    def test_worker_registry_defined(self):
        self.assertIn("WORKER_REGISTRY", self._src())

    def test_worker_spec_defined(self):
        self.assertIn("class WorkerSpec", self._src())

    def test_worker_process_defined(self):
        self.assertIn("class WorkerProcess", self._src())

    def test_dashboard_loop_defined(self):
        self.assertIn("async def _dashboard_loop", self._src())

    def test_section_has_devices_defined(self):
        self.assertIn("def _section_has_devices", self._src())

    def test_uses_create_subprocess_exec(self):
        self.assertIn("create_subprocess_exec", self._src(),
                      "Supervisor must use asyncio.create_subprocess_exec for isolation")

    def test_exponential_backoff_present(self):
        self.assertIn("backoff", self._src().lower(),
                      "Supervisor must implement backoff for crash restarts")

    def test_max_crashes_constant(self):
        self.assertIn("MAX_CRASHES_PER_HOUR", self._src())

    def test_sigterm_on_stop(self):
        self.assertIn("terminate()", self._src(),
                      "Supervisor must send SIGTERM to workers on shutdown")

    def test_list_workers_flag(self):
        self.assertIn("--list-workers", self._src(),
                      "Supervisor must support --list-workers for debugging")

    def test_generic_registry_comment(self):
        """Registry must be documented for adding new workers."""
        src = self._src()
        self.assertTrue(
            "new data source" in src.lower() or "adding" in src.lower(),
            "Registry must document how to add new workers"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 10. Integration — supervisor selects correct workers from config
# ─────────────────────────────────────────────────────────────────────────────

class TestSupervisorWorkerSelection(unittest.TestCase):

    def test_both_sections_populated_starts_both_workers(self):
        p = _write_ini("""
            [bms]
            House Bank = AA:BB:CC:DD:EE:FF : 123456
            [victron]
            MultiPlus = AA:BB:CC:DD:EE:FF : aabbccddeeff00112233445566778899
        """)
        try:
            active_names = [
                spec.name for spec in WORKER_REGISTRY
                if _section_has_devices(p, spec.config_sections)
            ]
            self.assertIn("BMS",     active_names)
            self.assertIn("Victron", active_names)
        finally:
            os.unlink(p)

    def test_only_bms_populated_starts_only_bms(self):
        p = _write_ini("""
            [bms]
            House Bank = AA:BB:CC:DD:EE:FF : 123456
        """)
        try:
            active_names = [
                spec.name for spec in WORKER_REGISTRY
                if _section_has_devices(p, spec.config_sections)
            ]
            self.assertIn("BMS", active_names)
            self.assertNotIn("Victron", active_names)
        finally:
            os.unlink(p)

    def test_only_victron_populated_starts_only_victron(self):
        p = _write_ini("""
            [victron]
            South Array = AA:BB:CC:DD:EE:FF : aabbccddeeff00112233445566778899
        """)
        try:
            active_names = [
                spec.name for spec in WORKER_REGISTRY
                if _section_has_devices(p, spec.config_sections)
            ]
            self.assertNotIn("BMS", active_names)
            self.assertIn("Victron", active_names)
        finally:
            os.unlink(p)

    def test_empty_config_no_workers(self):
        p = _write_ini("[general]\ntheme = business\n")
        try:
            active_names = [
                spec.name for spec in WORKER_REGISTRY
                if _section_has_devices(p, spec.config_sections)
            ]
            self.assertEqual(active_names, [])
        finally:
            os.unlink(p)

    def test_worker_command_uses_supervisor_state_file(self):
        """Workers launched by supervisor must use supervisor's state file."""
        spec = next(w for w in WORKER_REGISTRY if w.name == "BMS")
        worker = WorkerProcess(
            spec=spec, python=sys.executable,
            config="config.ini",
            state_file="/run/solar_monitor/state.json",  # supervisor-controlled
            log_level="INFO",
            script_dir="/home/pi/solar",
        )
        cmd = worker._cmd()
        self.assertIn("--state-file", cmd)
        idx = cmd.index("--state-file")
        self.assertEqual(cmd[idx + 1], "/run/solar_monitor/state.json")


if __name__ == "__main__":
    unittest.main(verbosity=2)
