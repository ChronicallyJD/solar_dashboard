"""
tests/test_supervisor_main.py — Supervisor entry-point (main) coverage
=======================================================================
Closes the remaining coverage gaps in solar_monitor.py:

  - main(): argument wiring (--config, --log-level, --list-workers)
  - worker registry selection from populated config sections
  - missing-config and no-workers exit paths
  - printed worker summary (--list-workers, populated and empty)
  - WorkerProcess construction parameters passed by main()
  - dashboard-loop wiring (state file, output path, theme, interval)
  - HTTPS server enable/disable branches (server stubbed)
  - shutdown path (workers stopped, tasks cancelled)
  - WorkerProcess edge branches: stream-read exception, spawn failure
    backoff, stop-flag break, backoff reset after long run, SIGTERM
    timeout → SIGKILL

No real subprocesses are spawned - WorkerProcess / _dashboard_loop /
run_https_server are replaced with recording stubs.
"""

import asyncio
import contextlib
import io
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


dev_m.BLEDevice   = _BLEDevice
bleak.BleakClient = _BleakClient
bleak.BleakScanner= _BleakScanner
sys.modules.update({
    "bleak": bleak,
    "bleak.backends": backends,
    "bleak.backends.device": dev_m,
})

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import importlib.util
_spec = importlib.util.spec_from_file_location(
    "supervisor_main_module", f"{REPO_ROOT}/solar_monitor.py"
)
sup_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sup_mod)

WorkerSpec    = sup_mod.WorkerSpec
WorkerProcess = sup_mod.WorkerProcess
MAX_BACKOFF   = sup_mod.MAX_BACKOFF


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


VICTRON_ONLY_INI = """
    [general]
    output = {out}
    state_file = {state}
    theme = business
    victron_interval = 30
    bms_interval = 120

    [victron]
    MultiPlus = AA:BB:CC:DD:EE:FF : aabbccddeeff00112233445566778899
"""

BOTH_WORKERS_INI = VICTRON_ONLY_INI + """
    [bms]
    House Bank = AA:BB:CC:DD:EE:FF : 123456
"""


class FakeWorker:
    """Drop-in replacement for WorkerProcess used by main()."""
    instances: list = []

    def __init__(self, **kwargs):
        self.kwargs   = kwargs
        self.spec     = kwargs["spec"]
        self.stopped  = False
        FakeWorker.instances.append(self)

    @property
    def name(self):
        return self.spec.name

    async def run(self):
        return None

    async def stop(self):
        self.stopped = True


class FakeDashboard:
    """Records the arguments _dashboard_loop was wired with."""
    calls: list = []

    async def __call__(self, state_file, output_path, theme, interval):
        FakeDashboard.calls.append(
            dict(state_file=state_file, output_path=output_path,
                 theme=theme, interval=interval))
        return None


class _MainHarness(unittest.TestCase):
    """Common setup: temp config/output paths + stubbed collaborators."""

    def setUp(self):
        FakeWorker.instances = []
        FakeDashboard.calls  = []
        self.out_html   = tempfile.mktemp(suffix=".html")
        self.state_json = tempfile.mktemp(suffix=".json")
        self._cleanup   = []

    def tearDown(self):
        for p in self._cleanup + [self.out_html, self.state_json]:
            try:
                os.unlink(p)
            except FileNotFoundError:
                pass

    def _ini(self, template) -> str:
        p = _write_ini(template.format(out=self.out_html,
                                       state=self.state_json))
        self._cleanup.append(p)
        return p

    def _run_main(self, argv, dash=None):
        dash = dash or FakeDashboard()
        with patch.object(sys, "argv", ["solar_monitor.py"] + argv), \
             patch.object(sup_mod, "WorkerProcess", FakeWorker), \
             patch.object(sup_mod, "_dashboard_loop", dash):
            run(sup_mod.main())


# ─────────────────────────────────────────────────────────────────────────────
# 1. Exit paths
# ─────────────────────────────────────────────────────────────────────────────

class TestMainExitPaths(_MainHarness):

    def test_missing_config_exits_1(self):
        with patch.object(sys, "argv",
                          ["solar_monitor.py", "--config", "/nonexistent/x.ini"]):
            with self.assertRaises(SystemExit) as cm:
                run(sup_mod.main())
        self.assertEqual(cm.exception.code, 1)

    def test_no_workers_exits_1(self):
        p = self._ini("[general]\noutput = {out}\nstate_file = {state}\n")
        with patch.object(sys, "argv",
                          ["solar_monitor.py", "--config", p]):
            with self.assertRaises(SystemExit) as cm:
                run(sup_mod.main())
        self.assertEqual(cm.exception.code, 1)
        self.assertEqual(FakeWorker.instances, [])


# ─────────────────────────────────────────────────────────────────────────────
# 2. --list-workers
# ─────────────────────────────────────────────────────────────────────────────

class TestMainListWorkers(_MainHarness):

    def test_list_workers_prints_active_and_exits_cleanly(self):
        p = self._ini(BOTH_WORKERS_INI)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self._run_main(["--config", p, "--list-workers"])
        out = buf.getvalue()
        self.assertIn("Workers that would start:", out)
        self.assertIn("Victron", out)
        self.assertIn("victron_monitor.py", out)
        self.assertIn("BMS", out)
        self.assertIn("bms_monitor.py", out)
        # list mode must not construct or start any workers
        self.assertEqual(FakeWorker.instances, [])
        self.assertEqual(FakeDashboard.calls, [])

    def test_list_workers_empty_config_prints_none(self):
        p = self._ini("[general]\noutput = {out}\nstate_file = {state}\n")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self._run_main(["--config", p, "--list-workers"])
        self.assertIn("(none - all config sections empty)", buf.getvalue())

    def test_list_workers_only_victron(self):
        p = self._ini(VICTRON_ONLY_INI)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self._run_main(["--config", p, "--list-workers"])
        out = buf.getvalue()
        self.assertIn("Victron", out)
        self.assertNotIn("bms_monitor.py", out)


# ─────────────────────────────────────────────────────────────────────────────
# 3. Worker selection + wiring
# ─────────────────────────────────────────────────────────────────────────────

class TestMainWorkerWiring(_MainHarness):

    def test_both_workers_constructed_in_registry_order(self):
        p = self._ini(BOTH_WORKERS_INI)
        self._run_main(["--config", p])
        names = [w.name for w in FakeWorker.instances]
        self.assertEqual(names, ["Victron", "BMS"])

    def test_only_victron_constructed(self):
        p = self._ini(VICTRON_ONLY_INI)
        with self.assertLogs("supervisor", level="INFO") as cm:
            self._run_main(["--config", p])
        names = [w.name for w in FakeWorker.instances]
        self.assertEqual(names, ["Victron"])
        joined = "\n".join(cm.output)
        self.assertIn("Worker enabled: Victron", joined)
        self.assertIn("Worker skipped: BMS", joined)

    def test_worker_kwargs_from_args_and_config(self):
        p = self._ini(VICTRON_ONLY_INI)
        self._run_main(["--config", p, "--log-level", "DEBUG"])
        kw = FakeWorker.instances[0].kwargs
        self.assertEqual(kw["python"],     sys.executable)
        self.assertEqual(kw["config"],     str(Path(p).resolve()))
        self.assertEqual(kw["state_file"], self.state_json)
        self.assertEqual(kw["log_level"],  "DEBUG")
        self.assertEqual(kw["script_dir"], REPO_ROOT)

    def test_workers_stopped_on_shutdown(self):
        p = self._ini(BOTH_WORKERS_INI)
        self._run_main(["--config", p])
        self.assertTrue(all(w.stopped for w in FakeWorker.instances))

    def test_shutdown_message_on_cancellation(self):
        """A worker raising CancelledError triggers the shutdown branch."""
        p = self._ini(VICTRON_ONLY_INI)

        class CancellingWorker(FakeWorker):
            async def run(self):
                raise asyncio.CancelledError()

        dash = FakeDashboard()
        with patch.object(sys, "argv", ["solar_monitor.py", "--config", p]), \
             patch.object(sup_mod, "WorkerProcess", CancellingWorker), \
             patch.object(sup_mod, "_dashboard_loop", dash), \
             self.assertLogs("supervisor", level="INFO") as cm:
            run(sup_mod.main())
        joined = "\n".join(cm.output)
        self.assertIn("shutting down", joined)
        self.assertIn("Supervisor stopped.", joined)
        self.assertTrue(all(w.stopped for w in FakeWorker.instances))


# ─────────────────────────────────────────────────────────────────────────────
# 4. Dashboard wiring
# ─────────────────────────────────────────────────────────────────────────────

class TestMainDashboardWiring(_MainHarness):

    def test_dashboard_loop_receives_config_values(self):
        p = self._ini(BOTH_WORKERS_INI)
        self._run_main(["--config", p])
        self.assertEqual(len(FakeDashboard.calls), 1)
        call = FakeDashboard.calls[0]
        self.assertEqual(call["state_file"], self.state_json)
        self.assertEqual(call["output_path"], Path(self.out_html).resolve())
        self.assertEqual(call["theme"], "business")
        # fastest interval = victron 30s → dashboard = max(10, 30/2) = 15
        self.assertAlmostEqual(call["interval"], 15.0, places=1)

    def test_dashboard_interval_floor_10s(self):
        ini = VICTRON_ONLY_INI.replace("victron_interval = 30",
                                       "victron_interval = 12")
        p = self._ini(ini)
        self._run_main(["--config", p])
        self.assertAlmostEqual(FakeDashboard.calls[0]["interval"], 10.0,
                               places=1)


# ─────────────────────────────────────────────────────────────────────────────
# 5. HTTPS server branches
# ─────────────────────────────────────────────────────────────────────────────

class TestMainHttpsServer(_MainHarness):

    def test_server_disabled_by_default(self):
        p = self._ini(VICTRON_ONLY_INI)
        with self.assertLogs("supervisor", level="INFO") as cm:
            self._run_main(["--config", p])
        self.assertTrue(any("HTTPS server disabled" in m for m in cm.output))

    def test_server_enabled_starts_stub(self):
        ini = VICTRON_ONLY_INI + """
            [server]
            enabled = true
            host = 127.0.0.1
            port = 4443
        """
        p = self._ini(ini)
        server_calls = []

        async def fake_server(server_cfg, output_path, state_file):
            server_calls.append((server_cfg, output_path, state_file))
            return None

        with patch("solar_monitor.server.run_https_server", new=fake_server), \
             self.assertLogs("supervisor", level="INFO") as cm:
            self._run_main(["--config", p])

        self.assertTrue(any("HTTPS server enabled" in m for m in cm.output))
        self.assertEqual(len(server_calls), 1)
        server_cfg, output_path, state_file = server_calls[0]
        self.assertTrue(server_cfg.enabled)
        self.assertEqual(server_cfg.host, "127.0.0.1")
        self.assertEqual(server_cfg.port, 4443)
        self.assertEqual(output_path, Path(self.out_html).resolve())
        self.assertEqual(state_file, self.state_json)


# ─────────────────────────────────────────────────────────────────────────────
# 6. WorkerProcess edge branches
# ─────────────────────────────────────────────────────────────────────────────

def _make_worker(**kwargs) -> WorkerProcess:
    spec = WorkerSpec(
        name="Test", script="test_monitor.py",
        state_section="test", config_sections=["test"],
        interval_cfg_key="test_interval", min_gap=10.0,
    )
    defaults = dict(
        python="/usr/bin/python3", config="/tmp/config.ini",
        state_file="/tmp/state.json", log_level="INFO", script_dir="/tmp",
    )
    defaults.update(kwargs)
    return WorkerProcess(spec=spec, **defaults)


class TestWorkerProcessEdges(unittest.TestCase):

    def test_stream_read_exception_terminates_stream(self):
        """A readline() exception must end _stream_output, not propagate."""
        w = _make_worker()
        stream = MagicMock()
        stream.readline = AsyncMock(side_effect=RuntimeError("pipe broke"))
        run(w._stream_output(stream, logging.INFO))   # must not raise

    def test_spawn_failure_backs_off_and_retries(self):
        """create_subprocess_exec failure → sleep(backoff), backoff doubles."""
        w = _make_worker()
        sleeps = []

        async def fail_spawn(*a, **kw):
            raise OSError("no such file")

        async def fake_sleep(t):
            sleeps.append(t)
            if len(sleeps) >= 2:
                w._stopped = True

        with patch("asyncio.create_subprocess_exec", new=fail_spawn), \
             patch("asyncio.sleep", new=fake_sleep):
            run(w.run())

        self.assertEqual(sleeps[0], 1.0)
        self.assertEqual(sleeps[1], 2.0)

    def _proc_mock(self, exit_code=1, on_wait=None):
        proc = MagicMock()
        proc.stdout = MagicMock()
        proc.stderr = MagicMock()
        proc.returncode = exit_code
        proc.stdout.readline = AsyncMock(return_value=b"")
        proc.stderr.readline = AsyncMock(return_value=b"")

        async def _wait():
            if on_wait:
                on_wait()
            return exit_code
        proc.wait = _wait
        return proc

    def test_stop_flag_breaks_loop_without_crash_count(self):
        """If stop() lands while the worker runs, exit is not a crash."""
        w = _make_worker()

        async def fake_spawn(*a, **kw):
            return self._proc_mock(on_wait=lambda: setattr(w, "_stopped", True))

        with patch("asyncio.create_subprocess_exec", new=fake_spawn):
            run(w.run())

        self.assertTrue(w._stopped)
        self.assertEqual(w._crash_times, [],
                         "A stop-requested exit must not count as a crash")

    def test_backoff_resets_after_long_run(self):
        """Runtime ≥ 30s before crash resets backoff to 1s in run() itself."""
        w = _make_worker()
        w._backoff = 32.0
        # Unbounded fake clock: every call advances 50s, so the measured
        # runtime is always >= 30s.  (asyncio's event loop shares
        # time.monotonic, so the clock must never run dry.)
        state = [0.0]

        def fake_clock():
            state[0] += 50.0
            return state[0]

        async def fake_spawn(*a, **kw):
            return self._proc_mock()

        async def fake_sleep(t):
            w._stopped = True

        with patch("asyncio.create_subprocess_exec", new=fake_spawn), \
             patch.object(sup_mod.time, "monotonic", side_effect=fake_clock):
            with patch("asyncio.sleep", new=fake_sleep):
                run(w.run())

        self.assertEqual(w._backoff, 1.0)

    def test_stop_sigterm_timeout_sends_sigkill(self):
        w = _make_worker()
        proc = MagicMock()
        proc.returncode = None
        proc.terminate = MagicMock()
        proc.kill = MagicMock()
        # Plain MagicMock: the patched wait_for raises without awaiting,
        # so an AsyncMock here would leave an un-awaited coroutine behind.
        proc.wait = MagicMock(return_value=None)
        w._proc = proc

        with patch("asyncio.wait_for", side_effect=asyncio.TimeoutError):
            run(w.stop())

        proc.terminate.assert_called_once()
        proc.kill.assert_called_once()

    def test_stop_sigkill_failure_swallowed(self):
        """kill() raising (process already gone) must not propagate."""
        w = _make_worker()
        proc = MagicMock()
        proc.returncode = None
        proc.terminate = MagicMock()
        proc.kill = MagicMock(side_effect=ProcessLookupError)
        proc.wait = MagicMock(return_value=None)
        w._proc = proc

        with patch("asyncio.wait_for", side_effect=asyncio.TimeoutError):
            run(w.stop())   # must not raise
        proc.kill.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
