"""
tests/test_security.py — security regression tests
===================================================
Pins the security fixes applied across the project:
  1. dashboard.py — HTML-escaping of all device-originated strings
     (name, address, error, charger_state, inverter_state, ac_in_source,
     sw_version, faults), '</'-escaping inside the __HISTORY_JSON__ script
     block, and no external CDN references (Chart.js is vendored/inlined).
  2. server.py — generate_self_signed_cert emits a leaf certificate
     (BasicConstraints ca=False, KeyUsage digital_signature +
     key_encipherment, EKU serverAuth) and the private key file is 0600.
  3. history.py — HistoryDB.query rejects SQL-injection via the fields /
     order identifier parameters; lowercase order still accepted.
  4. state.py — save_section writes atomically via a unique temp name:
     no leftover *.tmp files, a pre-planted state_path + '.tmp' file or
     symlink is never used as the write target.
  5. mcp_server.py — McpConfig.check_api_key: open when no key configured,
     rejects wrong / None / non-string keys, constant-time comparison.
  6. server.py — _handle_request caps header consumption at 100 lines so a
     drip-feeding client cannot hold the handler forever.
"""

import asyncio
import html
import inspect
import json
import os
import stat
import sys
import tempfile
import types
import unittest
from pathlib import Path

# ── Stub bleak ────────────────────────────────────────────────────────────────
bleak    = types.ModuleType("bleak")
backends = types.ModuleType("bleak.backends")
dev_m    = types.ModuleType("bleak.backends.device")


class _BLEDevice:
    def __init__(self, a="", n="", **kw):
        self.address = a; self.name = n


bleak.BleakClient  = type("BleakClient",  (), {})
bleak.BleakScanner = type("BleakScanner", (), {})
dev_m.BLEDevice    = _BLEDevice
sys.modules.update({
    "bleak": bleak,
    "bleak.backends": backends,
    "bleak.backends.device": dev_m,
})
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from solar_monitor.dashboard import build_html, render_bms_card, render_victron_card
from solar_monitor.history import HistoryDB, HistoryConfig
from solar_monitor.models import DeviceReading
from solar_monitor.server import generate_self_signed_cert, _handle_request
from solar_monitor.state import save_section

# mcp_server.py lives at the repo root — load it the same way test_mcp_server does
import importlib.util
_spec = importlib.util.spec_from_file_location("mcp_server", f"{REPO_ROOT}/mcp_server.py")
ms = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ms)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

XSS_SCRIPT = '<script>alert(1)</script>'
XSS_ATTR   = '"><img src=x onerror=1>'
PAYLOADS   = (XSS_SCRIPT, XSS_ATTR)

TS = "2024-01-15T08:15:42"


def _reading(**kw) -> DeviceReading:
    base = dict(address="AA:BB:CC:DD:EE:FF", name="Pack 1",
                device_type="bms", timestamp=TS)
    base.update(kw)
    return DeviceReading(**base)


def _assert_escaped(tc: unittest.TestCase, out: str, payload: str) -> None:
    """The raw payload must be gone; its html-escaped form must be present."""
    tc.assertNotIn(payload, out,
                   f"raw XSS payload leaked into HTML: {payload!r}")
    tc.assertIn(html.escape(payload, quote=True), out,
                f"escaped payload missing from HTML: {payload!r}")


def run(coro, timeout=10.0):
    return asyncio.run(asyncio.wait_for(coro, timeout))


# ─────────────────────────────────────────────────────────────────────────────
# 1. Dashboard XSS escaping
# ─────────────────────────────────────────────────────────────────────────────

class TestDashboardXss(unittest.TestCase):

    def test_bms_card_name_and_address_escaped(self):
        for payload in PAYLOADS:
            r = _reading(name=payload, address=payload,
                         voltage_v=13.2, current_a=1.0, power_w=13.2,
                         capacity_pct=80)
            out = render_bms_card(r)
            _assert_escaped(self, out, payload)

    def test_bms_card_error_escaped(self):
        for payload in PAYLOADS:
            r = _reading(error=payload)
            out = render_bms_card(r)
            _assert_escaped(self, out, payload)

    def test_bms_card_sw_version_escaped(self):
        for payload in PAYLOADS:
            r = _reading(voltage_v=13.2, capacity_pct=80, sw_version=payload)
            out = render_bms_card(r)
            _assert_escaped(self, out, payload)

    def test_bms_card_faults_escaped(self):
        for payload in PAYLOADS:
            r = _reading(voltage_v=13.2, capacity_pct=80,
                         faults=["Cell overvoltage", payload])
            out = render_bms_card(r)
            _assert_escaped(self, out, payload)

    def test_victron_mppt_charger_state_escaped(self):
        for payload in PAYLOADS:
            r = _reading(device_type="mppt", voltage_v=13.2, current_a=2.0,
                         pv_power_w=100.0, charger_state=payload)
            out = render_victron_card(r)
            _assert_escaped(self, out, payload)

    def test_victron_card_name_address_error_escaped(self):
        for payload in PAYLOADS:
            r = _reading(device_type="mppt", name=payload, address=payload,
                         error=payload)
            out = render_victron_card(r)
            _assert_escaped(self, out, payload)

    def test_victron_inverter_state_and_ac_in_source_escaped(self):
        # VE.Bus dongle layout: inverter_state + ac_in_source both rendered
        for payload in PAYLOADS:
            r = _reading(device_type="inverter", voltage_v=13.2,
                         inverter_state=payload, ac_in_source=payload,
                         ac_out_power_va=240.0)
            out = render_victron_card(r)
            _assert_escaped(self, out, payload)

    def test_victron_standard_inverter_state_escaped(self):
        # Record 0x03/0x07 layout (no ac_in_source / ac_out_power_va)
        for payload in PAYLOADS:
            r = _reading(device_type="inverter", voltage_v=13.2,
                         inverter_state=payload)
            out = render_victron_card(r)
            _assert_escaped(self, out, payload)

    def test_build_html_escapes_reading_payloads_end_to_end(self):
        for payload in PAYLOADS:
            bms = [_reading(name=payload, error=payload)]
            vic = [_reading(device_type="mppt", name=payload,
                            voltage_v=13.0, pv_power_w=50.0,
                            charger_state=payload),
                   _reading(device_type="inverter", name=payload,
                            voltage_v=13.0, inverter_state=payload,
                            ac_in_source=payload, ac_out_power_va=100.0)]
            out = build_html(bms, vic, history={})
            _assert_escaped(self, out, payload)

    def test_history_json_script_close_escaped(self):
        # A '</script>' in history keys or values must not terminate the
        # script block: build_html rewrites '</' to '<\/' inside the JSON.
        key = 'PWNEDKEY</script><svg onload=alert(1)>'
        val = 'PWNEDVAL</script><svg onload=alert(2)>'
        history = {key: [{"timestamp": val, "voltage_v": 13.2}]}
        out = build_html([], [], history=history)
        self.assertNotIn('PWNEDKEY</script>', out)
        self.assertNotIn('PWNEDVAL</script>', out)
        self.assertIn('PWNEDKEY<\\/script>', out)
        self.assertIn('PWNEDVAL<\\/script>', out)

    def test_no_cdn_references_and_chartjs_inlined(self):
        out = build_html([_reading(voltage_v=13.2, capacity_pct=80)],
                         [], history={"Pack 1": [{"timestamp": TS,
                                                  "voltage_v": 13.2}]})
        self.assertNotIn("cdn.jsdelivr", out)
        self.assertNotIn("fonts.googleapis", out)
        self.assertIn("Chart.js v", out)   # vendored bundle banner

    def test_vendored_chartjs_present_on_disk(self):
        vendor = Path(REPO_ROOT) / "solar_monitor" / "vendor" / "chart.umd.min.js"
        self.assertTrue(vendor.exists(), f"missing vendored Chart.js: {vendor}")
        head = vendor.read_text(encoding="utf-8")[:200]
        self.assertIn("Chart.js v", head)


# ─────────────────────────────────────────────────────────────────────────────
# 2. Self-signed certificate hardening
# ─────────────────────────────────────────────────────────────────────────────

class TestCertGeneration(unittest.TestCase):

    def setUp(self):
        from cryptography import x509
        self.x509 = x509
        self.tmp  = tempfile.TemporaryDirectory()
        self.cert_path = os.path.join(self.tmp.name, "server.crt")
        self.key_path  = os.path.join(self.tmp.name, "server.key")
        generate_self_signed_cert(self.cert_path, self.key_path,
                                  host="192.168.1.50")
        self.cert = x509.load_pem_x509_certificate(
            Path(self.cert_path).read_bytes())

    def tearDown(self):
        self.tmp.cleanup()

    def test_basic_constraints_not_ca(self):
        bc = self.cert.extensions.get_extension_for_class(
            self.x509.BasicConstraints)
        self.assertFalse(bc.value.ca,
                         "self-signed cert must not be a CA certificate")

    def test_key_usage_digital_signature_and_key_encipherment(self):
        ku = self.cert.extensions.get_extension_for_class(
            self.x509.KeyUsage).value
        self.assertTrue(ku.digital_signature)
        self.assertTrue(ku.key_encipherment)
        self.assertFalse(ku.key_cert_sign)
        self.assertFalse(ku.crl_sign)

    def test_extended_key_usage_server_auth(self):
        from cryptography.x509.oid import ExtendedKeyUsageOID
        eku = self.cert.extensions.get_extension_for_class(
            self.x509.ExtendedKeyUsage).value
        self.assertIn(ExtendedKeyUsageOID.SERVER_AUTH, eku)

    def test_private_key_mode_0600(self):
        mode = stat.S_IMODE(os.stat(self.key_path).st_mode)
        self.assertEqual(mode, 0o600,
                         f"private key mode is {oct(mode)}, expected 0o600")

    def test_key_created_with_0600_from_the_start(self):
        # The key file must be *created* 0600 (os.open with mode), not
        # chmod'ed after the secret has already been written world-readable.
        src = inspect.getsource(generate_self_signed_cert)
        self.assertIn("0o600", src)
        self.assertIn("os.O_CREAT", src)

    def test_key_mode_tightened_even_if_preexisting(self):
        # A pre-existing wide-open key file must end up 0600 after regen
        loose_key = os.path.join(self.tmp.name, "loose.key")
        Path(loose_key).write_text("placeholder")
        os.chmod(loose_key, 0o644)
        cert2 = os.path.join(self.tmp.name, "loose.crt")
        generate_self_signed_cert(cert2, loose_key, host="127.0.0.1")
        mode = stat.S_IMODE(os.stat(loose_key).st_mode)
        self.assertEqual(mode, 0o600)


# ─────────────────────────────────────────────────────────────────────────────
# 3. HistoryDB.query SQL identifier validation
# ─────────────────────────────────────────────────────────────────────────────

class TestHistoryQueryInjection(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        path = os.path.join(self.tmp.name, "history.db")
        cfg  = HistoryConfig(enabled=True, db_path=path, retention_days=1095)
        self.db = HistoryDB(cfg)
        # Two rows with distinct recorded_at values (dict rows are inserted
        # verbatim, so the timestamps are deterministic).
        self.db.write_readings([
            {"recorded_at": "2024-01-01T00:00:00", "device_name": "Pack 1",
             "device_type": "bms", "address": "AA", "voltage_v": 13.0},
            {"recorded_at": "2024-01-02T00:00:00", "device_name": "Pack 1",
             "device_type": "bms", "address": "AA", "voltage_v": 13.5},
        ])

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_injection_in_fields_raises_value_error(self):
        with self.assertRaises(ValueError):
            self.db.query(fields=["voltage_v; DROP TABLE readings"])

    def test_unknown_field_raises_value_error(self):
        with self.assertRaises(ValueError):
            self.db.query(fields=["not_a_column"])

    def test_injection_in_order_raises_value_error(self):
        with self.assertRaises(ValueError):
            self.db.query(order="ASC; DROP TABLE readings")

    def test_table_survives_injection_attempts(self):
        for kwargs in ({"fields": ["voltage_v; DROP TABLE readings"]},
                       {"order": "ASC; DROP TABLE readings"}):
            with self.assertRaises(ValueError):
                self.db.query(**kwargs)
        rows = self.db.query()
        self.assertEqual(len(rows), 2)

    def test_lowercase_desc_accepted_and_newest_first(self):
        rows = self.db.query(order="desc")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["recorded_at"], "2024-01-02T00:00:00")
        self.assertEqual(rows[1]["recorded_at"], "2024-01-01T00:00:00")

    def test_valid_fields_still_work(self):
        rows = self.db.query(fields=["voltage_v"])
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertIn("voltage_v", row)
            self.assertIn("recorded_at", row)
            self.assertIn("device_name", row)


# ─────────────────────────────────────────────────────────────────────────────
# 4. state.py atomic write hardening
# ─────────────────────────────────────────────────────────────────────────────

class TestStateSaveSection(unittest.TestCase):

    def _readings(self):
        return [_reading(voltage_v=13.2, capacity_pct=80)]

    def test_no_tmp_files_left_after_write(self):
        with tempfile.TemporaryDirectory() as d:
            state_path = os.path.join(d, "state.json")
            save_section(state_path, "bms", self._readings())
            leftovers = [f for f in os.listdir(d) if f.endswith(".tmp")]
            self.assertEqual(leftovers, [],
                             f"temp files left behind: {leftovers}")

    def test_state_file_is_valid_json_with_section(self):
        with tempfile.TemporaryDirectory() as d:
            state_path = os.path.join(d, "state.json")
            save_section(state_path, "bms", self._readings())
            data = json.loads(Path(state_path).read_text(encoding="utf-8"))
            self.assertIn("bms", data)
            self.assertEqual(len(data["bms"]["readings"]), 1)
            self.assertEqual(data["bms"]["readings"][0]["name"], "Pack 1")

    def test_predictable_tmp_path_not_used(self):
        # A file pre-planted at the old predictable temp path must be
        # neither overwritten nor removed: the write goes to a unique name.
        with tempfile.TemporaryDirectory() as d:
            state_path = os.path.join(d, "state.json")
            planted = state_path + ".tmp"
            Path(planted).write_text("SENTINEL", encoding="utf-8")
            save_section(state_path, "bms", self._readings())
            self.assertTrue(os.path.exists(planted),
                            "pre-planted .tmp file was removed")
            self.assertEqual(Path(planted).read_text(encoding="utf-8"),
                             "SENTINEL",
                             "pre-planted .tmp file was overwritten")
            # And the real state file was still written correctly
            data = json.loads(Path(state_path).read_text(encoding="utf-8"))
            self.assertIn("bms", data)

    def test_symlink_at_tmp_path_cannot_redirect_write(self):
        # A symlink planted at state_path + '.tmp' pointing at a victim
        # file must not cause the victim to be overwritten.
        with tempfile.TemporaryDirectory() as d:
            state_path = os.path.join(d, "state.json")
            victim = os.path.join(d, "victim.txt")
            Path(victim).write_text("VICTIM-ORIGINAL", encoding="utf-8")
            os.symlink(victim, state_path + ".tmp")
            save_section(state_path, "bms", self._readings())
            self.assertEqual(Path(victim).read_text(encoding="utf-8"),
                             "VICTIM-ORIGINAL",
                             "symlink attack overwrote the victim file")
            data = json.loads(Path(state_path).read_text(encoding="utf-8"))
            self.assertIn("bms", data)

    def test_uses_mkstemp_for_unique_temp_name(self):
        src = inspect.getsource(save_section)
        self.assertIn("mkstemp", src)


# ─────────────────────────────────────────────────────────────────────────────
# 5. McpConfig.check_api_key
# ─────────────────────────────────────────────────────────────────────────────

class TestMcpApiKey(unittest.TestCase):

    def test_no_key_configured_allows_any(self):
        cfg = ms.McpConfig(api_key="")
        self.assertTrue(cfg.check_api_key(None))
        self.assertTrue(cfg.check_api_key("anything"))

    def test_wrong_key_rejected(self):
        cfg = ms.McpConfig(api_key="secret-key")
        self.assertFalse(cfg.check_api_key("wrong"))
        self.assertFalse(cfg.check_api_key(""))
        self.assertFalse(cfg.check_api_key("secret-key "))

    def test_none_rejected_when_key_required(self):
        cfg = ms.McpConfig(api_key="secret-key")
        self.assertFalse(cfg.check_api_key(None))

    def test_non_string_rejected(self):
        cfg = ms.McpConfig(api_key="secret-key")
        self.assertFalse(cfg.check_api_key(12345))
        self.assertFalse(cfg.check_api_key(["secret-key"]))

    def test_exact_match_accepted(self):
        cfg = ms.McpConfig(api_key="secret-key")
        self.assertTrue(cfg.check_api_key("secret-key"))

    def test_constant_time_comparison_used(self):
        src = inspect.getsource(ms.McpConfig.check_api_key)
        self.assertIn("hmac.compare_digest", src)


# ─────────────────────────────────────────────────────────────────────────────
# 6. _handle_request header-count cap
# ─────────────────────────────────────────────────────────────────────────────

class _FakeWriter:
    """Duck-typed asyncio StreamWriter that just collects bytes."""

    def __init__(self):
        self.data   = b""
        self.closed = False

    def write(self, b: bytes) -> None:
        self.data += b

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class TestHeaderFloodCapped(unittest.TestCase):

    def test_header_drip_without_blank_line_still_answered(self):
        # Client sends the request line, then 150 header lines and never a
        # blank line or EOF.  The 100-line cap must break the loop and let
        # the handler respond rather than waiting forever.
        with tempfile.TemporaryDirectory() as d:
            dashboard_path = Path(d) / "dashboard.html"
            dashboard_path.write_text("<html>ok</html>", encoding="utf-8")
            state_path = os.path.join(d, "state.json")
            Path(state_path).write_text("{}", encoding="utf-8")

            async def scenario():
                reader = asyncio.StreamReader()
                reader.feed_data(b"GET /health HTTP/1.1\r\n")
                reader.feed_data(b"X-Filler: junk\r\n" * 150)
                # deliberately: no blank line, no EOF
                writer = _FakeWriter()
                await _handle_request(reader, writer,
                                      dashboard_path, state_path)
                return writer

            # If the cap were missing, _handle_request would block on
            # readline() until its own 15 s timeout; the 5 s wait_for here
            # fails the test fast instead.
            writer = run(scenario(), timeout=5.0)
            self.assertIn(b"HTTP/1.1 200 OK", writer.data)
            self.assertIn(b"OK", writer.data)
            self.assertTrue(writer.closed)

    def test_normal_request_with_blank_line_still_works(self):
        with tempfile.TemporaryDirectory() as d:
            dashboard_path = Path(d) / "dashboard.html"
            dashboard_path.write_text("<html>ok</html>", encoding="utf-8")
            state_path = os.path.join(d, "state.json")
            Path(state_path).write_text("{}", encoding="utf-8")

            async def scenario():
                reader = asyncio.StreamReader()
                reader.feed_data(b"GET /health HTTP/1.1\r\n"
                                 b"Host: localhost\r\n"
                                 b"\r\n")
                reader.feed_eof()
                writer = _FakeWriter()
                await _handle_request(reader, writer,
                                      dashboard_path, state_path)
                return writer

            writer = run(scenario(), timeout=5.0)
            self.assertIn(b"HTTP/1.1 200 OK", writer.data)

    def test_source_caps_header_lines(self):
        src = inspect.getsource(_handle_request)
        self.assertIn("range(100)", src)


if __name__ == "__main__":
    unittest.main()
