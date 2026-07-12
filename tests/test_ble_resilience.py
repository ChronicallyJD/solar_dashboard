"""
tests/test_ble_resilience.py — BLE scanner resilience and architecture tests
=============================================================================
Covers the new scan architecture:
  - VictronScanner: passive mode, MAC filter, payload accumulation,
    scan clears state between cycles, stop() always called in finally
  - _poll_bms: direct connection by MAC (no scan), retry logic,
    no-MAC returns error reading, empty list returns empty list
  - _poll_victron: reads from VictronScanner, missing device → error reading
  - bms_monitor/victron_monitor: no scanner reference, no scan lock,
    try/finally in monitors still present for general exception safety
  - Source-level: no _BleScanLock, no PersistentScanner, no fcntl,
    VictronScanner uses passive mode, no resolve_devices
"""

import asyncio
import inspect
import os
import sys
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# ── Stub bleak ────────────────────────────────────────────────────────────────
bleak = types.ModuleType("bleak")
backends = types.ModuleType("bleak.backends")
device_mod = types.ModuleType("bleak.backends.device")


class _BLEDevice:
    def __init__(self, address="AA:BB:CC:DD:EE:FF", name="test",
                 details=None, rssi=0):
        self.address = address
        self.name    = name


class _BleakClient:
    def __init__(self, *a, **kw): pass


class _BleakScanner:
    def __init__(self, *a, **kw): pass
    async def start(self): pass
    async def stop(self):  pass


device_mod.BLEDevice  = _BLEDevice
bleak.BleakClient     = _BleakClient
bleak.BleakScanner    = _BleakScanner
sys.modules.update({
    "bleak": bleak,
    "bleak.backends": backends,
    "bleak.backends.device": device_mod,
})
import os
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from solar_monitor.scanner import (
    VictronScanner, _poll_bms, _poll_victron, poll_all,
    VICTRON_MFR_ID,
)
from solar_monitor.config import AppConfig, DeviceConfig
from solar_monitor.models import DeviceReading
import solar_monitor.scanner as scanner_mod


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _dc(name="Test", mac="AA:BB:CC:DD:EE:FF", key=None, dtype=None, pw=None):
    return DeviceConfig(name=name, mac=mac, ble_name=None,
                        enc_key=key, password=pw, device_type=dtype)


def _adv_data(mfr_payload: bytes = None):
    """Fake advertisement data object."""
    adv = MagicMock()
    if mfr_payload:
        adv.manufacturer_data = {VICTRON_MFR_ID: mfr_payload}
    else:
        adv.manufacturer_data = {}
    return adv


# ─────────────────────────────────────────────────────────────────────────────
# 1. VictronScanner construction
# ─────────────────────────────────────────────────────────────────────────────

class TestVictronScannerConstruction(unittest.TestCase):

    def test_mac_filter_stored_uppercase(self):
        vs = VictronScanner(["aa:bb:cc:dd:ee:ff"])
        self.assertIn("AA:BB:CC:DD:EE:FF", vs._macs)

    def test_multiple_macs(self):
        macs = ["AA:BB:CC:DD:EE:FF", "11:22:33:44:55:66"]
        vs = VictronScanner(macs)
        self.assertEqual(vs._macs, {m.upper() for m in macs})

    def test_empty_mac_list(self):
        vs = VictronScanner([])
        self.assertEqual(vs._macs, set())

    def test_initial_state_empty(self):
        vs = VictronScanner(["AA:BB"])
        self.assertEqual(vs._adv, {})
        self.assertEqual(vs._payloads, {})
        self.assertIsNone(vs._scanner)


# ─────────────────────────────────────────────────────────────────────────────
# 2. VictronScanner._cb — payload accumulation
# ─────────────────────────────────────────────────────────────────────────────

class TestVictronScannerCallback(unittest.TestCase):

    def _vs(self, *macs):
        return VictronScanner(list(macs))

    def test_adv_stored_for_known_mac(self):
        vs = self._vs("AA:BB:CC:DD:EE:FF")
        dev = _BLEDevice("AA:BB:CC:DD:EE:FF")
        adv = _adv_data()
        vs._cb(dev, adv)
        self.assertIn("AA:BB:CC:DD:EE:FF", vs._adv)

    def test_adv_ignored_for_unknown_mac(self):
        vs = self._vs("AA:BB:CC:DD:EE:FF")
        dev = _BLEDevice("11:22:33:44:55:66")
        adv = _adv_data()
        vs._cb(dev, adv)
        self.assertNotIn("11:22:33:44:55:66", vs._adv)

    def test_empty_filter_accepts_all(self):
        """Empty MAC list = auto-discovery mode; all devices accepted."""
        vs = self._vs()  # no filter
        dev = _BLEDevice("11:22:33:44:55:66")
        adv = _adv_data()
        vs._cb(dev, adv)
        self.assertIn("11:22:33:44:55:66", vs._adv)

    def test_victron_payload_accumulated(self):
        vs = self._vs("AA:BB:CC:DD:EE:FF")
        dev = _BLEDevice("AA:BB:CC:DD:EE:FF")
        # Format A payload with valid record type 0x01 (Solar Charger)
        payload = bytes([0x10, 0x03, 0x00, 0x01, 0x00, 0xEB, 0xB8, 0xDD,
                         0x03, 0xB4, 0xB8, 0x7C, 0xE1])
        adv = _adv_data(payload)
        vs._cb(dev, adv)
        self.assertEqual(len(vs._payloads.get("AA:BB:CC:DD:EE:FF", [])), 1)

    def test_duplicate_payload_not_accumulated(self):
        vs = self._vs("AA:BB:CC:DD:EE:FF")
        dev = _BLEDevice("AA:BB:CC:DD:EE:FF")
        payload = bytes([0x10, 0x03, 0x00, 0x01, 0x00, 0xEB, 0xB8, 0xDD,
                         0x03, 0xB4, 0xB8, 0x7C, 0xE1])
        adv = _adv_data(payload)
        vs._cb(dev, adv)
        vs._cb(dev, adv)   # same payload again
        self.assertEqual(len(vs._payloads.get("AA:BB:CC:DD:EE:FF", [])), 1)

    def test_no_mfr_data_no_payload(self):
        vs = self._vs("AA:BB:CC:DD:EE:FF")
        dev = _BLEDevice("AA:BB:CC:DD:EE:FF")
        adv = _adv_data()   # no manufacturer data
        vs._cb(dev, adv)
        self.assertEqual(vs._payloads.get("AA:BB:CC:DD:EE:FF", []), [])

    def test_address_case_normalised(self):
        vs = self._vs("AA:BB:CC:DD:EE:FF")
        dev = _BLEDevice("aa:bb:cc:dd:ee:ff")   # lowercase from BlueZ
        adv = _adv_data()
        vs._cb(dev, adv)
        self.assertIn("AA:BB:CC:DD:EE:FF", vs._adv)


# ─────────────────────────────────────────────────────────────────────────────
# 3. VictronScanner.scan()
# ─────────────────────────────────────────────────────────────────────────────

class TestVictronScannerScan(unittest.TestCase):

    def test_scan_clears_previous_data(self):
        vs = VictronScanner(["AA:BB:CC:DD:EE:FF"])
        vs._adv["AA:BB"] = ("dev", "adv")
        vs._payloads["AA:BB"] = [b"\x00"]

        mock = MagicMock()
        mock.start = AsyncMock()
        mock.stop  = AsyncMock()

        with patch.object(scanner_mod, "BleakScanner", return_value=mock), \
             patch("asyncio.sleep", new=AsyncMock()):
            run(vs.scan(0.0))

        self.assertEqual(vs._adv, {})
        self.assertEqual(vs._payloads, {})

    def test_scan_uses_passive_mode(self):
        """BleakScanner must be created with scanning_mode='passive' on first attempt."""
        vs = VictronScanner([])
        created_kwargs = {}

        def capture_scanner(*a, **kw):
            created_kwargs.update(kw)
            m = MagicMock()
            m.start = AsyncMock()
            m.stop  = AsyncMock()
            return m

        with patch.object(scanner_mod, "BleakScanner", side_effect=capture_scanner), \
             patch("asyncio.sleep", new=AsyncMock()):
            run(vs.scan(0.0))

        self.assertEqual(created_kwargs.get("scanning_mode"), "passive",
                         "VictronScanner must try passive mode first")

    def test_scan_includes_or_patterns(self):
        """Passive mode must include or_patterns — required by BlueZ."""
        vs = VictronScanner([])
        created_kwargs = {}

        def capture_scanner(*a, **kw):
            created_kwargs.update(kw)
            m = MagicMock()
            m.start = AsyncMock()
            m.stop  = AsyncMock()
            return m

        with patch.object(scanner_mod, "BleakScanner", side_effect=capture_scanner), \
             patch("asyncio.sleep", new=AsyncMock()):
            run(vs.scan(0.0))

        self.assertIn("or_patterns", created_kwargs,
                      "or_patterns must be passed to BleakScanner for passive mode")
        or_patterns = created_kwargs["or_patterns"]
        self.assertIsInstance(or_patterns, list)
        self.assertGreater(len(or_patterns), 0)
        # Pattern must reference manufacturer data (AD type 0xFF)
        ad_types = [p[1] for p in or_patterns]
        self.assertIn(0xFF, ad_types,
                      "or_patterns must include AD type 0xFF (manufacturer data)")

    def test_scan_or_patterns_contain_victron_company_id(self):
        """or_patterns must match on Victron's company ID (0x02E1 LE = E1 02)."""
        vs = VictronScanner([])
        created_kwargs = {}

        def capture_scanner(*a, **kw):
            created_kwargs.update(kw)
            m = MagicMock()
            m.start = AsyncMock()
            m.stop  = AsyncMock()
            return m

        with patch.object(scanner_mod, "BleakScanner", side_effect=capture_scanner), \
             patch("asyncio.sleep", new=AsyncMock()):
            run(vs.scan(0.0))

        or_patterns = created_kwargs.get("or_patterns", [])
        victron_id_le = bytes([0xE1, 0x02])  # 0x02E1 little-endian
        matched = any(
            isinstance(p, tuple) and len(p) >= 3 and victron_id_le in bytes(p[2])
            for p in or_patterns
        )
        self.assertTrue(matched,
                        "or_patterns must contain Victron company ID E1:02")

    def test_scan_falls_back_to_active_on_passive_error(self):
        """Any passive mode failure must fall back to active — not just specific errors."""
        vs = VictronScanner([])
        call_log = []

        # Test with several different error types to confirm the catch is broad
        for error_msg in [
            "passive scanning mode requires bluez or_patterns",
            "Invalid argument",              # wrong or_patterns format
            "BleakError: something else",    # any other passive failure
        ]:
            call_log.clear()

            def capture_scanner(*a, **kw):
                mode = kw.get("scanning_mode", "unknown")
                call_log.append(mode)
                m = MagicMock()
                if mode == "passive":
                    m.start = AsyncMock(side_effect=Exception(error_msg))
                else:
                    m.start = AsyncMock()
                m.stop = AsyncMock()
                return m

            with patch.object(scanner_mod, "BleakScanner", side_effect=capture_scanner), \
                 patch("asyncio.sleep", new=AsyncMock()):
                run(vs.scan(0.0))

            self.assertIn("passive", call_log,
                          f"Must try passive first (error: {error_msg!r})")
            self.assertIn("active",  call_log,
                          f"Must fall back to active on: {error_msg!r}")

    def test_scan_non_passive_error_propagates(self):
        """Errors unrelated to passive mode must not be swallowed."""
        vs = VictronScanner([])

        def capture_scanner(*a, **kw):
            m = MagicMock()
            m.start = AsyncMock(side_effect=RuntimeError("Bluetooth adapter gone"))
            m.stop  = AsyncMock()
            return m

        with patch.object(scanner_mod, "BleakScanner", side_effect=capture_scanner), \
             patch("asyncio.sleep", new=AsyncMock()):
            with self.assertRaises(RuntimeError):
                run(vs.scan(0.0))

    def test_scan_stop_called_in_finally_on_success(self):
        """scanner.stop() must be called even on success."""
        vs = VictronScanner([])
        mock = MagicMock()
        mock.start = AsyncMock()
        mock.stop  = AsyncMock()

        with patch.object(scanner_mod, "BleakScanner", return_value=mock), \
             patch("asyncio.sleep", new=AsyncMock()):
            run(vs.scan(0.0))

        mock.stop.assert_awaited()

    def test_scan_stop_called_in_finally_on_exception(self):
        """scanner.stop() must be called even when start() raises."""
        vs = VictronScanner([])
        mock = MagicMock()
        mock.start = AsyncMock(side_effect=RuntimeError("adapter gone"))
        mock.stop  = AsyncMock()

        with patch.object(scanner_mod, "BleakScanner", return_value=mock), \
             patch("asyncio.sleep", new=AsyncMock()):
            with self.assertRaises(RuntimeError):
                run(vs.scan(0.0))

        mock.stop.assert_awaited()

    def test_scan_clears_scanner_ref_after_stop(self):
        """_scanner attribute must be None after scan completes."""
        vs = VictronScanner([])
        mock = MagicMock()
        mock.start = AsyncMock()
        mock.stop  = AsyncMock()

        with patch.object(scanner_mod, "BleakScanner", return_value=mock), \
             patch("asyncio.sleep", new=AsyncMock()):
            run(vs.scan(0.0))

        self.assertIsNone(vs._scanner)

    def test_latest_adv_returns_none_before_scan(self):
        vs = VictronScanner(["AA:BB"])
        self.assertIsNone(vs.latest_adv("AA:BB"))

    def test_payloads_returns_empty_before_scan(self):
        vs = VictronScanner(["AA:BB"])
        self.assertEqual(vs.payloads("AA:BB"), [])

    def test_seen_macs_returns_set(self):
        vs = VictronScanner(["AA:BB"])
        dev = _BLEDevice("AA:BB")
        vs._adv["AA:BB"] = (dev, MagicMock())
        self.assertIn("AA:BB", vs.seen_macs())


# ─────────────────────────────────────────────────────────────────────────────
# 4. _poll_bms — direct connection, no scan
# ─────────────────────────────────────────────────────────────────────────────

class TestPollBmsDirect(unittest.TestCase):

    def test_empty_list_returns_empty(self):
        result = run(_poll_bms([]))
        self.assertEqual(result, [])

    def test_no_mac_returns_error_reading(self):
        """DeviceConfig with no MAC must return an error reading, not crash."""
        dc = DeviceConfig(name="NoMac", mac=None, ble_name="BT-TH-ABCD",
                          enc_key=None, password=None)
        results = run(_poll_bms([dc]))
        self.assertEqual(len(results), 1)
        self.assertIsNotNone(results[0].error)
        self.assertIn("MAC", results[0].error)

    def test_returns_list_of_device_readings(self):
        dc = DeviceConfig(name="TestBMS", mac="AA:BB:CC:DD:EE:FF",
                          ble_name=None, enc_key=None, password=None)

        async def fake_read(dev, name, password=None):
            # dev is now a MAC string (not BLEDevice)
            address = dev if isinstance(dev, str) else dev.address
            r = DeviceReading(address=address, name=name,
                              device_type="bms", timestamp="t")
            r.voltage_v = 54.0
            return r

        with patch.object(scanner_mod, "read_jbd_device", new=fake_read):
            results = run(_poll_bms([dc]))

        self.assertEqual(len(results), 1)
        self.assertIsInstance(results[0], DeviceReading)
        self.assertEqual(results[0].voltage_v, 54.0)

    def test_direct_connection_no_scanner_argument(self):
        """_poll_bms must not require a scanner argument."""
        import inspect as _inspect
        sig = _inspect.signature(_poll_bms)
        params = list(sig.parameters.keys())
        self.assertNotIn("scanner", params,
                         "_poll_bms must not take a scanner parameter")

    def test_transient_error_retried(self):
        call_count = 0

        async def fail_once(dev, name, password=None):
            nonlocal call_count
            call_count += 1
            address = dev if isinstance(dev, str) else dev.address
            r = DeviceReading(address=address, name=name,
                              device_type="bms", timestamp="t")
            if call_count == 1:
                r.error = "timed out"
            return r

        dc = _dc(mac="AA:BB:CC:DD:EE:FF")
        with patch.object(scanner_mod, "read_jbd_device", new=fail_once), \
             patch("asyncio.sleep", new=AsyncMock()):
            results = run(_poll_bms([dc]))

        self.assertGreaterEqual(call_count, 2, "Transient error should trigger a retry")
        self.assertIsNone(results[0].error)

    def test_permanent_error_not_retried(self):
        call_count = 0

        async def perm_fail(dev, name, password=None):
            nonlocal call_count
            call_count += 1
            address = dev if isinstance(dev, str) else dev.address
            r = DeviceReading(address=address, name=name,
                              device_type="bms", timestamp="t")
            r.error = "BMS rejected password"
            return r

        dc = _dc(mac="AA:BB:CC:DD:EE:FF")
        with patch.object(scanner_mod, "read_jbd_device", new=perm_fail), \
             patch("asyncio.sleep", new=AsyncMock()):
            results = run(_poll_bms([dc]))

        self.assertEqual(call_count, 1, "Permanent error must not be retried")
        self.assertIsNotNone(results[0].error)


# ─────────────────────────────────────────────────────────────────────────────
# 5. _poll_victron — reads from VictronScanner
# ─────────────────────────────────────────────────────────────────────────────

class TestPollVictron(unittest.TestCase):

    def test_empty_config_returns_empty(self):
        vs = VictronScanner([])
        result = _poll_victron([], vs)
        self.assertEqual(result, [])

    def test_device_not_seen_returns_error_reading(self):
        vs = VictronScanner(["AA:BB:CC:DD:EE:FF"])
        dc = _dc(mac="AA:BB:CC:DD:EE:FF")
        results = _poll_victron([dc], vs)
        self.assertEqual(len(results), 1)
        self.assertIsNotNone(results[0].error)
        self.assertIn("not seen", results[0].error.lower())

    def test_device_seen_calls_read_victron(self):
        vs = VictronScanner(["AA:BB:CC:DD:EE:FF"])
        dev = _BLEDevice("AA:BB:CC:DD:EE:FF", "TestVictron")
        adv = MagicMock()
        adv.manufacturer_data = {}
        vs._adv["AA:BB:CC:DD:EE:FF"] = (dev, adv)

        dc = _dc(name="TestVictron", mac="AA:BB:CC:DD:EE:FF", key="abc")

        fake_reading = DeviceReading(address="AA:BB:CC:DD:EE:FF",
                                     name="TestVictron",
                                     device_type="inverter", timestamp="t")
        fake_reading.faults = []; fake_reading.temp_c = []

        with patch.object(scanner_mod, "read_victron_advertisement",
                          return_value=fake_reading):
            results = _poll_victron([dc], vs)

        self.assertEqual(len(results), 1)
        self.assertIsNone(results[0].error)

    def test_returns_list_of_device_readings(self):
        vs = VictronScanner([])
        dc = _dc(mac="AA:BB")
        results = _poll_victron([dc], vs)
        self.assertIsInstance(results, list)
        for r in results:
            self.assertIsInstance(r, DeviceReading)


# ─────────────────────────────────────────────────────────────────────────────
# 6. poll_all — combined mode
# ─────────────────────────────────────────────────────────────────────────────

class TestPollAll(unittest.TestCase):

    def test_empty_configs_return_empty_lists(self):
        async def fake_scan(self_vs, duration):
            pass
        with patch.object(VictronScanner, "scan", new=fake_scan):
            bms, vic = run(poll_all([], [], scan_timeout=0.0))
        self.assertEqual(bms, [])
        self.assertEqual(vic, [])

    def test_returns_tuple_of_two_lists(self):
        async def fake_scan(self_vs, duration):
            pass
        with patch.object(VictronScanner, "scan", new=fake_scan):
            result = run(poll_all([], [], scan_timeout=0.0))
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 2)

    def test_bms_poll_called_without_scanner(self):
        """poll_all must call _poll_bms with only the configs — no scanner."""
        calls = []

        async def fake_poll_bms(configs):
            calls.append(configs)
            return []

        async def fake_scan(self_vs, duration):
            pass

        dc = _dc(mac="AA:BB")
        with patch.object(scanner_mod, "_poll_bms",    new=fake_poll_bms), \
             patch.object(VictronScanner, "scan", new=fake_scan):
            run(poll_all([dc], [], scan_timeout=0.0))

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], [dc])


# ─────────────────────────────────────────────────────────────────────────────
# 7. Source-level architecture guarantees
# ─────────────────────────────────────────────────────────────────────────────

class TestArchitectureGuarantees(unittest.TestCase):

    def _src(self, name):
        path = f"{REPO_ROOT}/{name}"
        with open(path) as fh:
            return fh.read()

    def _scanner_src(self):
        return self._src("solar_monitor/scanner.py")

    # ── New classes/functions present ────────────────────────────────────────

    def test_victron_scanner_class_present(self):
        self.assertIn("class VictronScanner", self._scanner_src())

    def test_poll_bms_present(self):
        self.assertIn("def _poll_bms", self._scanner_src())

    def test_poll_victron_present(self):
        self.assertIn("def _poll_victron", self._scanner_src())

    def test_poll_all_present(self):
        self.assertIn("async def poll_all", self._scanner_src())

    # ── Old classes/functions removed ────────────────────────────────────────

    def test_persistent_scanner_removed(self):
        self.assertNotIn("class PersistentScanner", self._scanner_src(),
                         "PersistentScanner must be removed — use VictronScanner")

    def test_resolve_devices_removed(self):
        self.assertNotIn("async def resolve_devices", self._scanner_src(),
                         "resolve_devices must be removed — replaced by poll_all")

    def test_ble_scan_lock_removed(self):
        """_BleScanLock class must not be defined in scanner.py."""
        src = self._scanner_src()
        self.assertNotIn("class _BleScanLock", src,
                         "class _BleScanLock must be removed — no longer needed")

    def test_fcntl_not_imported(self):
        self.assertNotIn("import fcntl", self._scanner_src(),
                         "fcntl must be removed — no lock file needed")

    # ── VictronScanner uses passive mode ─────────────────────────────────────

    def test_passive_mode_in_scanner(self):
        src = inspect.getsource(VictronScanner.scan)
        self.assertIn("passive", src,
                      "VictronScanner.scan() must use passive scanning mode")

    def test_or_patterns_in_scanner_source(self):
        src = inspect.getsource(VictronScanner.scan)
        self.assertIn("or_patterns", src,
                      "scan() must pass or_patterns — required by BlueZ passive mode")

    def test_active_fallback_in_scanner_source(self):
        src = inspect.getsource(VictronScanner.scan)
        self.assertIn("active", src,
                      "scan() must fall back to active when passive unavailable")

    def test_victron_scanner_stop_in_finally(self):
        src = inspect.getsource(VictronScanner.scan)
        self.assertIn("finally:", src)
        finally_idx = src.find("finally:")
        # stop is now delegated to _stop_scanner() from the finally block
        stop_idx = src.find("_stop_scanner()", finally_idx)
        if stop_idx < 0:
            stop_idx = src.find("stop()", finally_idx)
        self.assertGreater(stop_idx, 0,
                           "Scanner must be stopped in the finally block of scan()")

    # ── _poll_bms signature — no scanner ─────────────────────────────────────

    def test_poll_bms_no_scanner_param(self):
        sig    = inspect.signature(_poll_bms)
        params = list(sig.parameters.keys())
        self.assertNotIn("scanner", params)

    def test_poll_bms_is_coroutine(self):
        self.assertTrue(asyncio.iscoroutinefunction(_poll_bms))

    def test_poll_victron_is_sync(self):
        self.assertFalse(asyncio.iscoroutinefunction(_poll_victron))

    # ── Monitors don't reference scanner objects ──────────────────────────────

    def test_bms_monitor_no_scanner_scan(self):
        src = self._src("bms_monitor.py")
        self.assertNotIn("scanner.scan(", src,
                         "bms_monitor must not call scanner.scan()")
        self.assertNotIn("VictronScanner", src,
                         "bms_monitor must not use VictronScanner")

    def test_victron_monitor_uses_victron_scanner(self):
        src = self._src("victron_monitor.py")
        self.assertIn("VictronScanner", src)

    def test_bms_monitor_calls_poll_bms_directly(self):
        src = self._src("bms_monitor.py")
        self.assertIn("_poll_bms", src)

    def test_victron_monitor_uses_passive_scanner(self):
        src = self._src("victron_monitor.py")
        self.assertIn("VictronScanner", src)
        self.assertIn("scanner.scan", src)

    # ── No stale lock references in monitors ─────────────────────────────────

    def test_bms_monitor_no_lock_reference(self):
        src = self._src("bms_monitor.py")
        self.assertNotIn("_BleScanLock", src)
        self.assertNotIn("fcntl", src)

    def test_victron_monitor_no_lock_reference(self):
        src = self._src("victron_monitor.py")
        self.assertNotIn("_BleScanLock", src)
        self.assertNotIn("fcntl", src)


# ─────────────────────────────────────────────────────────────────────────────
# 8. VictronScanner payload helpers
# ─────────────────────────────────────────────────────────────────────────────

class TestVictronScannerHelpers(unittest.TestCase):

    def test_latest_adv_none_for_unseen_mac(self):
        vs = VictronScanner(["AA:BB"])
        self.assertIsNone(vs.latest_adv("AA:BB"))

    def test_latest_adv_returns_tuple_after_cb(self):
        vs = VictronScanner(["AA:BB:CC:DD:EE:FF"])
        dev = _BLEDevice("AA:BB:CC:DD:EE:FF")
        adv = MagicMock(); adv.manufacturer_data = {}
        vs._cb(dev, adv)
        result = vs.latest_adv("AA:BB:CC:DD:EE:FF")
        self.assertIsNotNone(result)
        self.assertEqual(result[0].address, "AA:BB:CC:DD:EE:FF")

    def test_payloads_empty_for_unseen_mac(self):
        vs = VictronScanner(["AA:BB"])
        self.assertEqual(vs.payloads("AA:BB"), [])

    def test_seen_macs_empty_initially(self):
        vs = VictronScanner(["AA:BB"])
        self.assertEqual(vs.seen_macs(), set())

    def test_seen_macs_populated_after_cb(self):
        vs = VictronScanner([])   # no filter
        dev = _BLEDevice("AA:BB:CC:DD:EE:FF")
        adv = MagicMock(); adv.manufacturer_data = {}
        vs._cb(dev, adv)
        self.assertIn("AA:BB:CC:DD:EE:FF", vs.seen_macs())


if __name__ == "__main__":
    unittest.main(verbosity=2)
