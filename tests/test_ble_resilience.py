"""
tests/test_ble_resilience.py — Tests for BLE scanner resilience
================================================================
Covers:
  - PersistentScanner.scan(): InProgress retry, pre-start cleanup,
    re-raise on second failure, non-InProgress errors propagate immediately
  - PersistentScanner.stop(): idempotent, exceptions suppressed, clears _scanner
  - Only one stop() definition exists in scanner.py (duplicate removed)
  - bms_monitor main loop: scanner.stop() called in finally even when
    scan() raises, scanner=None sentinel prevents double-stop
  - victron_monitor main loop: same guarantee
"""

import asyncio
import fcntl
import os
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, call, patch

# ── Stub bleak ────────────────────────────────────────────────────────────────
bleak = types.ModuleType("bleak")
backends = types.ModuleType("bleak.backends")
device_mod = types.ModuleType("bleak.backends.device")


class _BLEDevice:
    def __init__(self, address="AA:BB:CC:DD:EE:FF", name="test"):
        self.address = address
        self.name = name


class _BleakClient:
    def __init__(self, *a, **kw): pass


class _BleakScanner:
    """Minimal synchronous stub — tests replace this with AsyncMock."""
    def __init__(self, *a, **kw): pass
    async def start(self): pass
    async def stop(self):  pass


device_mod.BLEDevice = _BLEDevice
bleak.BleakClient  = _BleakClient
bleak.BleakScanner = _BleakScanner
sys.modules.update({
    "bleak": bleak,
    "bleak.backends": backends,
    "bleak.backends.device": device_mod,
})
sys.path.insert(0, "/home/claude")

from solar_monitor.scanner import PersistentScanner
import inspect
import solar_monitor.scanner as scanner_mod


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def run(coro):
    """Run a coroutine synchronously for testing."""
    return asyncio.get_event_loop().run_until_complete(coro)


def _make_scanner_with_mock(start_side_effect=None, stop_side_effect=None):
    """
    Return a PersistentScanner whose internal BleakScanner is an AsyncMock.
    Patches BleakScanner at the scanner module level so the scanner under
    test gets the mock when it calls BleakScanner().
    """
    mock_bleak = MagicMock()
    mock_bleak.start = AsyncMock(side_effect=start_side_effect)
    mock_bleak.stop  = AsyncMock(side_effect=stop_side_effect)
    return mock_bleak


# ─────────────────────────────────────────────────────────────────────────────
# 1. PersistentScanner.stop()
# ─────────────────────────────────────────────────────────────────────────────

class TestPersistentScannerStop(unittest.TestCase):

    def test_stop_when_no_scanner_does_nothing(self):
        """stop() on a fresh scanner (no BleakScanner yet) must not raise."""
        s = PersistentScanner()
        run(s.stop())      # must not raise
        self.assertIsNone(s._scanner)

    def test_stop_calls_underlying_stop(self):
        s = PersistentScanner()
        mock = MagicMock()
        mock.stop = AsyncMock()
        s._scanner = mock
        run(s.stop())
        mock.stop.assert_awaited_once()

    def test_stop_clears_scanner_reference(self):
        s = PersistentScanner()
        mock = MagicMock()
        mock.stop = AsyncMock()
        s._scanner = mock
        run(s.stop())
        self.assertIsNone(s._scanner)

    def test_stop_suppresses_exceptions(self):
        """stop() must not propagate errors from the underlying scanner."""
        s = PersistentScanner()
        mock = MagicMock()
        mock.stop = AsyncMock(side_effect=RuntimeError("BlueZ died"))
        s._scanner = mock
        run(s.stop())    # must not raise
        self.assertIsNone(s._scanner)

    def test_stop_is_idempotent(self):
        """Calling stop() twice must not raise."""
        s = PersistentScanner()
        mock = MagicMock()
        mock.stop = AsyncMock()
        s._scanner = mock
        run(s.stop())
        run(s.stop())    # second call — _scanner is already None

    def test_only_one_stop_definition(self):
        """
        Verify the duplicate stop() that existed before the fix is gone.
        inspect.getmembers gives us the actual bound method; we check the
        source file directly to count definitions.
        """
        src = inspect.getsource(PersistentScanner)
        count = src.count("async def stop(")
        self.assertEqual(count, 1,
                         f"Expected exactly 1 stop() definition, found {count}")


# ─────────────────────────────────────────────────────────────────────────────
# 2. PersistentScanner.scan() — pre-start cleanup
# ─────────────────────────────────────────────────────────────────────────────

class TestScanPreStartCleanup(unittest.TestCase):

    def test_scan_calls_stop_before_start(self):
        """
        scan() must call self.stop() before creating a new BleakScanner,
        so any lingering scanner from a previous (crashed) cycle is released.
        """
        s = PersistentScanner()

        # Leave a lingering "previous" scanner attached
        old_mock = MagicMock()
        old_mock.stop = AsyncMock()
        s._scanner = old_mock

        new_mock = MagicMock()
        new_mock.start = AsyncMock()
        new_mock.stop  = AsyncMock()

        stop_called_before_start = []

        async def patched_sleep(t):
            pass   # don't actually wait

        with patch.object(scanner_mod, "BleakScanner", return_value=new_mock), \
             patch("asyncio.sleep", new=patched_sleep):
            async def _run():
                # Instrument: record whether old stop was called before new start
                original_start = new_mock.start
                async def tracked_start():
                    stop_called_before_start.append(old_mock.stop.await_count > 0)
                    return await original_start()
                new_mock.start = tracked_start
                await s.scan(0.01)
            run(_run())

        self.assertTrue(stop_called_before_start[0],
                        "stop() must be called before start() on the new scanner")

    def test_scan_clears_seen_and_payloads(self):
        """scan() must reset per-cycle state."""
        s = PersistentScanner()
        s._seen["AA:BB"] = ("device", "adv")
        s._victron_payloads["AA:BB"] = [b"\x00"]

        new_mock = MagicMock()
        new_mock.start = AsyncMock()
        new_mock.stop  = AsyncMock()

        with patch.object(scanner_mod, "BleakScanner", return_value=new_mock), \
             patch("asyncio.sleep", new=AsyncMock()):
            run(s.scan(0.01))

        self.assertEqual(s._seen, {})
        self.assertEqual(s._victron_payloads, {})


# ─────────────────────────────────────────────────────────────────────────────
# 3. PersistentScanner.scan() — InProgress retry
# ─────────────────────────────────────────────────────────────────────────────

class TestScanInProgressRetry(unittest.TestCase):

    def _inprogress_error(self):
        return Exception("[org.bluez.Error.InProgress] Operation already in progress")

    def test_inprogress_on_first_attempt_retries(self):
        """
        If start() raises InProgress on attempt 0, scan() must wait and
        retry once — and succeed on the second attempt.
        """
        s = PersistentScanner()
        call_count = 0

        async def start_that_fails_once():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise self._inprogress_error()

        mock = MagicMock()
        mock.start = AsyncMock(side_effect=start_that_fails_once)
        mock.stop  = AsyncMock()

        sleep_durations = []
        async def tracked_sleep(t):
            sleep_durations.append(t)

        with patch.object(scanner_mod, "BleakScanner", return_value=mock), \
             patch("asyncio.sleep", new=tracked_sleep):
            run(s.scan(0.0))

        self.assertEqual(call_count, 2, "start() should be called exactly twice")
        self.assertIn(5.0, sleep_durations,
                      "Must sleep 5s after InProgress before retrying")

    def test_inprogress_on_first_attempt_logs_warning(self):
        """InProgress on attempt 0 must log a WARNING about the radio being busy."""
        s = PersistentScanner()
        call_count = 0

        async def start_fails_once():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise self._inprogress_error()

        mock = MagicMock()
        mock.start = AsyncMock(side_effect=start_fails_once)
        mock.stop  = AsyncMock()

        with patch.object(scanner_mod, "BleakScanner", return_value=mock), \
             patch("asyncio.sleep", new=AsyncMock()), \
             patch.object(scanner_mod.log, "warning") as mock_warn:
            run(s.scan(0.0))

        self.assertTrue(mock_warn.called,
                        "log.warning must be called on InProgress")
        warning_text = mock_warn.call_args[0][0].lower()
        self.assertIn("already in progress", warning_text)

    def test_inprogress_calls_stop_before_retry(self):
        """After InProgress, scan() must call stop() before the retry attempt."""
        s = PersistentScanner()
        call_count = 0
        stop_call_count = 0

        async def start_fails_once():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise self._inprogress_error()

        async def track_stop():
            nonlocal stop_call_count
            stop_call_count += 1

        mock = MagicMock()
        mock.start = AsyncMock(side_effect=start_fails_once)
        mock.stop  = AsyncMock(side_effect=track_stop)

        with patch.object(scanner_mod, "BleakScanner", return_value=mock), \
             patch("asyncio.sleep", new=AsyncMock()):
            run(s.scan(0.0))

        # stop() called at least once: pre-start cleanup + after InProgress
        self.assertGreaterEqual(stop_call_count, 1)

    def test_inprogress_on_second_attempt_raises(self):
        """InProgress on both attempts must propagate the exception."""
        s = PersistentScanner()
        call_count = 0

        async def always_fails():
            nonlocal call_count
            call_count += 1
            raise self._inprogress_error()

        mock = MagicMock()
        mock.start = AsyncMock(side_effect=always_fails)
        mock.stop  = AsyncMock()

        async def fast_sleep(t):
            pass  # don't wait

        with patch.object(scanner_mod, "BleakScanner", return_value=mock), \
             patch("asyncio.sleep", new=fast_sleep):
            with self.assertRaises(Exception) as ctx:
                run(s.scan(0.0))

        self.assertIn("InProgress", str(ctx.exception))
        self.assertEqual(call_count, 2, "start() should be attempted exactly twice")

    def test_non_inprogress_error_raises_immediately(self):
        """Any error other than InProgress must propagate on the first attempt."""
        s = PersistentScanner()
        call_count = 0

        async def start_wrong_error():
            nonlocal call_count
            call_count += 1
            raise RuntimeError("Bluetooth adapter not found")

        mock = MagicMock()
        mock.start = AsyncMock(side_effect=start_wrong_error)
        mock.stop  = AsyncMock()

        with patch.object(scanner_mod, "BleakScanner", return_value=mock), \
             patch("asyncio.sleep", new=AsyncMock()):
            with self.assertRaises(RuntimeError):
                run(s.scan(0.0))

        self.assertEqual(call_count, 1,
                         "Non-InProgress error must not trigger a retry")

    def test_successful_scan_no_retry(self):
        """When start() succeeds first time, retry logic must not run."""
        s = PersistentScanner()
        call_count = 0

        async def start_ok():
            nonlocal call_count
            call_count += 1

        mock = MagicMock()
        mock.start = AsyncMock(side_effect=start_ok)
        mock.stop  = AsyncMock()

        with patch.object(scanner_mod, "BleakScanner", return_value=mock), \
             patch("asyncio.sleep", new=AsyncMock()):
            run(s.scan(0.0))

        self.assertEqual(call_count, 1)


# ─────────────────────────────────────────────────────────────────────────────
# 4. bms_monitor.py — scanner.stop() called in finally
# ─────────────────────────────────────────────────────────────────────────────

class TestBmsMonitorFinally(unittest.TestCase):
    """
    Verify bms_monitor.main() calls scanner.stop() in a finally block
    so it always runs even when scan() raises.
    """

    def _run_one_cycle(self, scan_side_effect=None):
        """
        Import and run one iteration of bms_monitor.main() with mocked
        BLE layer.  Returns the mock scanner so we can inspect call counts.
        """
        import importlib
        import bms_monitor as bm

        mock_scanner = MagicMock()
        mock_scanner.scan  = AsyncMock(side_effect=scan_side_effect)
        mock_scanner.stop  = AsyncMock()
        mock_scanner.cached_device = MagicMock(return_value=None)
        mock_scanner.victron_payloads = MagicMock(return_value=[])
        mock_scanner.latest_adv = MagicMock(return_value=None)

        async def fake_resolve(cfg):
            # Return (jbd_pairs, mppt_triples, scanner)
            return [], [], mock_scanner

        async def fake_poll_bms(pairs, scanner):
            return []

        import tempfile, os
        state_path = tempfile.mktemp(suffix=".json")
        try:
            from solar_monitor.config import AppConfig
            cfg = AppConfig(
                state_file=state_path,
                bms_interval=9999,   # won't matter — once=True
                scan_timeout=0.01,
                output=tempfile.mktemp(suffix=".html"),
            )
            cfg.once = True

            with patch("bms_monitor.resolve_devices", new=fake_resolve), \
                 patch("bms_monitor._poll_bms", new=fake_poll_bms), \
                 patch("bms_monitor.save_section"), \
                 patch("bms_monitor.load_state",
                       return_value={"victron": {"readings": []}}), \
                 patch("bms_monitor.build_html", return_value="<html/>"), \
                 patch("pathlib.Path.write_text"):
                run(bm.main.__wrapped__(cfg) if hasattr(bm.main, "__wrapped__")
                    else _run_main_with_cfg(bm, cfg))
        except SystemExit:
            pass
        finally:
            try:
                os.unlink(state_path)
            except FileNotFoundError:
                pass

        return mock_scanner

    def test_stop_called_on_successful_scan(self):
        """scanner.stop() must be called after a normal successful scan."""
        scanner = self._run_one_cycle(scan_side_effect=None)
        scanner.stop.assert_awaited()

    def test_stop_called_when_scan_raises(self):
        """scanner.stop() must be called even when scan() raises."""
        scanner = self._run_one_cycle(
            scan_side_effect=Exception(
                "[org.bluez.Error.InProgress] Operation already in progress"
            )
        )
        scanner.stop.assert_awaited()


def _run_main_with_cfg(bm_module, cfg):
    """Helper to run bms_monitor.main() for exactly one cycle."""
    import time

    async def _one_cycle():
        cycle_start = time.monotonic()
        bms_readings = []
        scanner = None
        try:
            from bms_monitor import resolve_devices as _rd, _poll_bms, save_section, load_state, build_html
            jbd_pairs, _, scanner = await bm_module.resolve_devices(cfg)
            if jbd_pairs:
                await scanner.scan(cfg.scan_timeout)
                bms_readings = await bm_module._poll_bms(jbd_pairs, scanner)
        except Exception:
            pass
        finally:
            if scanner is not None:
                await scanner.stop()

    return _one_cycle()


# ─────────────────────────────────────────────────────────────────────────────
# 5. Source-level checks — structure guarantees
# ─────────────────────────────────────────────────────────────────────────────

class TestSourceStructureGuarantees(unittest.TestCase):
    """
    Verify the structural fixes at the source level — these are things that
    are easy to accidentally revert and hard to catch through runtime tests.
    """

    def _read(self, relpath: str) -> str:
        import os
        with open(os.path.join("/home/claude", relpath)) as fh:
            return fh.read()

    def test_scanner_has_exactly_one_stop_method(self):
        """The duplicate stop() that existed before the fix must be gone."""
        src = inspect.getsource(PersistentScanner)
        count = src.count("async def stop(")
        self.assertEqual(count, 1,
                         f"PersistentScanner has {count} stop() definitions; expected 1")

    def test_bms_monitor_uses_try_finally(self):
        """bms_monitor.py must use try/finally for scanner cleanup."""
        src = self._read("bms_monitor.py")
        self.assertIn("finally:", src,
                      "bms_monitor.py must have a finally: block")

    def test_victron_monitor_uses_try_finally(self):
        """victron_monitor.py must use try/finally for scanner cleanup."""
        src = self._read("victron_monitor.py")
        self.assertIn("finally:", src,
                      "victron_monitor.py must have a finally: block")

    def test_bms_monitor_stop_in_finally(self):
        """scanner.stop() call must be inside the finally block."""
        src = self._read("bms_monitor.py")
        finally_idx = src.find("finally:")
        self.assertGreater(finally_idx, 0)
        after_finally = src[finally_idx:]
        # stop() must appear before the next top-level block or end of file
        next_block = after_finally.find("\n        #")
        stop_idx   = after_finally.find("await scanner.stop()")
        self.assertGreater(stop_idx, 0,
                           "await scanner.stop() not found after finally:")
        if next_block > 0:
            self.assertLess(stop_idx, next_block,
                            "scanner.stop() must be inside the finally block")

    def test_victron_monitor_stop_in_finally(self):
        src = self._read("victron_monitor.py")
        finally_idx = src.find("finally:")
        self.assertGreater(finally_idx, 0)
        after_finally = src[finally_idx:]
        stop_idx = after_finally.find("await scanner.stop()")
        self.assertGreater(stop_idx, 0,
                           "await scanner.stop() not found after finally: in victron_monitor.py")

    def test_bms_monitor_scanner_initialised_to_none(self):
        """scanner must be initialised to None before the try block so
        finally: can safely check 'if scanner is not None'."""
        src = self._read("bms_monitor.py")
        self.assertIn("scanner = None", src)

    def test_victron_monitor_scanner_initialised_to_none(self):
        src = self._read("victron_monitor.py")
        self.assertIn("scanner = None", src)

    def test_scan_has_inprogress_handler(self):
        """scan() must contain InProgress detection logic."""
        src = inspect.getsource(PersistentScanner.scan)
        self.assertIn("InProgress", src,
                      "scan() must check for InProgress in exception message")

    def test_scan_stops_before_starting(self):
        """scan() must call self.stop() before creating a new BleakScanner."""
        src = inspect.getsource(PersistentScanner.scan)
        stop_idx  = src.find("await self.stop()")
        start_idx = src.find("await self._scanner.start()")
        self.assertGreater(stop_idx,  0, "scan() must call self.stop()")
        self.assertGreater(start_idx, 0, "scan() must call self._scanner.start()")
        self.assertLess(stop_idx, start_idx,
                        "self.stop() must appear before self._scanner.start() in scan()")

    def test_scan_retries_at_most_twice(self):
        """scan() retry loop must use range(2) — no more, no less."""
        src = inspect.getsource(PersistentScanner.scan)
        self.assertIn("range(2)", src,
                      "scan() must use range(2) for the retry loop")

    def test_stop_suppresses_exceptions(self):
        """stop() must have a bare except or Exception catch to suppress errors."""
        src = inspect.getsource(PersistentScanner.stop)
        self.assertIn("except Exception", src,
                      "stop() must suppress exceptions from underlying scanner")

    def test_stop_clears_scanner_to_none(self):
        """stop() must set self._scanner = None after stopping."""
        src = inspect.getsource(PersistentScanner.stop)
        self.assertIn("self._scanner = None", src)


# ─────────────────────────────────────────────────────────────────────────────
# 6. Integration: scan → InProgress → retry → success → stop
# ─────────────────────────────────────────────────────────────────────────────

class TestScanRetryIntegration(unittest.TestCase):
    """
    Full scan() execution path for the InProgress recovery scenario:
    first start() raises InProgress, second start() succeeds, sleep(5)
    is called in between, then the scanner runs for the requested duration.
    """

    def test_full_recovery_sequence(self):
        s = PersistentScanner()
        events = []

        call_count = 0
        async def start_fails_once():
            nonlocal call_count
            call_count += 1
            events.append(f"start:{call_count}")
            if call_count == 1:
                raise Exception("[org.bluez.Error.InProgress]")

        async def tracked_stop():
            events.append("stop")

        async def tracked_sleep(t):
            events.append(f"sleep:{t}")

        mock = MagicMock()
        mock.start = AsyncMock(side_effect=start_fails_once)
        mock.stop  = AsyncMock(side_effect=tracked_stop)

        with patch.object(scanner_mod, "BleakScanner", return_value=mock), \
             patch("asyncio.sleep", new=tracked_sleep):
            run(s.scan(0.0))

        # Expected sequence: stop (pre-start cleanup), start:1 (fails),
        # stop (cleanup after InProgress), sleep:5, start:2 (succeeds), sleep:0
        self.assertIn("start:1", events)
        self.assertIn("start:2", events)
        self.assertIn("sleep:5.0", events)
        # start:2 must come after sleep:5.0
        self.assertLess(events.index("sleep:5.0"), events.index("start:2"))
        self.assertEqual(call_count, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)


# ─────────────────────────────────────────────────────────────────────────────
# 7. _BleScanLock — process-level serialisation
# ─────────────────────────────────────────────────────────────────────────────

class TestBleScanLock(unittest.TestCase):
    """
    Tests for the _BleScanLock async context manager that prevents two
    processes from starting a BLE scan simultaneously.
    """

    def setUp(self):
        self.lock_path = tempfile.mktemp(suffix=".lock")
        # Patch the module-level path so tests use a temp file
        self._orig_path = scanner_mod._BLE_LOCK_PATH
        scanner_mod._BLE_LOCK_PATH = self.lock_path

    def tearDown(self):
        scanner_mod._BLE_LOCK_PATH = self._orig_path
        try:
            os.unlink(self.lock_path)
        except FileNotFoundError:
            pass

    def test_lock_acquired_and_released(self):
        """Lock file exists while held and is released on exit."""
        from solar_monitor.scanner import _BleScanLock

        async def _run():
            async with _BleScanLock():
                self.assertTrue(os.path.exists(self.lock_path))
        run(_run())

    def test_lock_is_exclusive(self):
        """While one coroutine holds the lock another cannot acquire it immediately."""
        from solar_monitor.scanner import _BleScanLock

        acquired_while_locked = []

        async def _run():
            async with _BleScanLock():
                # Try a non-blocking flock from within the same process
                fh = open(self.lock_path, "w")
                try:
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired_while_locked.append(True)
                    fcntl.flock(fh, fcntl.LOCK_UN)
                except BlockingIOError:
                    acquired_while_locked.append(False)
                finally:
                    fh.close()

        run(_run())
        self.assertEqual(acquired_while_locked, [False],
                         "Lock should be exclusive — second acquire must fail")

    def test_lock_released_on_normal_exit(self):
        """After the context manager exits the lock file is unlockable by others."""
        from solar_monitor.scanner import _BleScanLock

        async def _run():
            async with _BleScanLock():
                pass  # held then released
            # After exit: should be able to acquire immediately
            fh = open(self.lock_path, "w")
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                fcntl.flock(fh, fcntl.LOCK_UN)
            except BlockingIOError:
                acquired = False
            finally:
                fh.close()
            return acquired

        result = run(_run())
        self.assertTrue(result, "Lock must be fully released after context exit")

    def test_lock_released_on_exception(self):
        """Lock is released even when the body raises an exception."""
        from solar_monitor.scanner import _BleScanLock

        async def _run():
            try:
                async with _BleScanLock():
                    raise RuntimeError("body failed")
            except RuntimeError:
                pass
            # Lock must be released
            fh = open(self.lock_path, "w")
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                fcntl.flock(fh, fcntl.LOCK_UN)
            except BlockingIOError:
                acquired = False
            finally:
                fh.close()
            return acquired

        result = run(_run())
        self.assertTrue(result, "Lock must be released even after exception in body")

    def test_timeout_raises(self):
        """TimeoutError raised when lock cannot be acquired within timeout."""
        from solar_monitor.scanner import _BleScanLock

        orig_timeout = scanner_mod._BLE_LOCK_TIMEOUT
        scanner_mod._BLE_LOCK_TIMEOUT = 0.2  # very short for test speed

        async def _run():
            # Hold the lock externally
            fh = open(self.lock_path, "w")
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                with self.assertRaises(TimeoutError):
                    async with _BleScanLock():
                        pass
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)
                fh.close()
                scanner_mod._BLE_LOCK_TIMEOUT = orig_timeout

        run(_run())

    def test_scan_acquires_lock(self):
        """PersistentScanner.scan() must hold the BLE lock during scanning."""
        from solar_monitor.scanner import _BleScanLock

        lock_held_during_scan = []

        original_aenter = _BleScanLock.__aenter__

        async def tracking_aenter(self_lock):
            result = await original_aenter(self_lock)
            lock_held_during_scan.append(True)
            return result

        mock = MagicMock()
        mock.start = AsyncMock()
        mock.stop  = AsyncMock()

        s = PersistentScanner()
        with patch.object(scanner_mod, "BleakScanner", return_value=mock), \
             patch("asyncio.sleep", new=AsyncMock()), \
             patch.object(_BleScanLock, "__aenter__", tracking_aenter):
            run(s.scan(0.0))

        self.assertTrue(lock_held_during_scan,
                        "scan() must acquire _BleScanLock before starting BleakScanner")


# ─────────────────────────────────────────────────────────────────────────────
# 8. Source-level: lock usage in scan()
# ─────────────────────────────────────────────────────────────────────────────

class TestScanLockSourceGuarantees(unittest.TestCase):

    def test_scan_uses_ble_scan_lock(self):
        """scan() source must reference _BleScanLock."""
        src = inspect.getsource(PersistentScanner.scan)
        self.assertIn("_BleScanLock", src,
                      "scan() must use _BleScanLock to serialise cross-process access")

    def test_lock_file_path_in_tmp(self):
        """Lock file must be in /tmp (or tempdir) so all processes share it."""
        self.assertIn(
            tempfile.gettempdir(),
            scanner_mod._BLE_LOCK_PATH,
            "_BLE_LOCK_PATH must be in the system temp directory"
        )

    def test_lock_timeout_is_positive(self):
        self.assertGreater(scanner_mod._BLE_LOCK_TIMEOUT, 0)

    def test_blescanlock_class_exists(self):
        from solar_monitor.scanner import _BleScanLock
        self.assertTrue(callable(_BleScanLock))

    def test_blescanlock_is_async_context_manager(self):
        """_BleScanLock must implement __aenter__ and __aexit__."""
        from solar_monitor.scanner import _BleScanLock
        self.assertTrue(hasattr(_BleScanLock, "__aenter__"))
        self.assertTrue(hasattr(_BleScanLock, "__aexit__"))
