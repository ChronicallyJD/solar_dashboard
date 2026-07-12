"""
tests/test_https_server.py — unit tests for solar_monitor/server.py
====================================================================
Covers:
  - ServerConfig: defaults, repr, all fields
  - generate_self_signed_cert: file creation, PEM format, SAN entries,
    key permissions, validity period
  - build_ssl_context: loads real certs, auto-generates when missing,
    raises FileNotFoundError when auto_cert=False and files missing,
    returns ssl.SSLContext with TLS 1.2 minimum
  - _handle_request: GET / → 200 HTML, GET /dashboard.html → 200 HTML,
    GET /state.json → 200 JSON, GET /health → 200 OK,
    unknown path → 404, non-GET method → 404,
    missing dashboard file → 200 placeholder HTML,
    missing state file → 200 empty JSON
  - AppConfig [server] section: loaded from INI, all fields, defaults,
    boolean parsing, missing section uses defaults
  - Supervisor integration: server task started when enabled,
    server task skipped when disabled
  - Source-level guarantees
"""

import asyncio
import configparser
import os
import ssl
import sys
import tempfile
import textwrap
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call

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
import os
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from solar_monitor.server import (
    ServerConfig, build_ssl_context, generate_self_signed_cert,
    _handle_request, run_https_server,
)
from solar_monitor.config import AppConfig, load_config


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _tmp_path() -> str:
    f = tempfile.NamedTemporaryFile(delete=True)
    p = f.name; f.close()
    try: os.unlink(p)
    except FileNotFoundError: pass
    return p


def _write_ini(content: str) -> str:
    f = tempfile.NamedTemporaryFile(
        mode="w", suffix=".ini", delete=False, encoding="utf-8"
    )
    f.write(textwrap.dedent(content))
    f.close()
    return f.name


def _make_reader(request_lines: list[str]) -> asyncio.StreamReader:
    """Fake asyncio.StreamReader that returns request lines in sequence."""
    reader = MagicMock(spec=asyncio.StreamReader)
    data   = [line.encode() for line in request_lines] + [b""]
    reader.readline = AsyncMock(side_effect=data)
    return reader


def _make_writer() -> MagicMock:
    writer = MagicMock(spec=asyncio.StreamWriter)
    writer.write   = MagicMock()
    writer.drain   = AsyncMock()
    writer.close   = MagicMock()
    writer._buffer = []
    def capture_write(data): writer._buffer.append(data)
    writer.write.side_effect = capture_write
    return writer


def _response(writer) -> str:
    return b"".join(writer._buffer).decode(errors="replace")


# ─────────────────────────────────────────────────────────────────────────────
# 1. ServerConfig
# ─────────────────────────────────────────────────────────────────────────────

class TestServerConfig(unittest.TestCase):

    def test_defaults(self):
        cfg = ServerConfig()
        self.assertFalse(cfg.enabled)
        self.assertEqual(cfg.host,      "0.0.0.0")
        self.assertEqual(cfg.port,      4443)
        self.assertEqual(cfg.cert_file, "server.crt")
        self.assertEqual(cfg.key_file,  "server.key")
        self.assertTrue(cfg.auto_cert)

    def test_custom_values(self):
        cfg = ServerConfig(
            enabled=True, host="192.168.1.10", port=8443,
            cert_file="/etc/certs/solar.crt",
            key_file="/etc/certs/solar.key",
            auto_cert=False,
        )
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.host,      "192.168.1.10")
        self.assertEqual(cfg.port,      8443)
        self.assertEqual(cfg.cert_file, "/etc/certs/solar.crt")
        self.assertFalse(cfg.auto_cert)

    def test_repr_contains_key_fields(self):
        cfg = ServerConfig(port=9443)
        r = repr(cfg)
        self.assertIn("ServerConfig", r)
        self.assertIn("9443",         r)

    def test_all_fields_present(self):
        cfg = ServerConfig()
        for attr in ("enabled", "host", "port", "cert_file", "key_file", "auto_cert"):
            self.assertTrue(hasattr(cfg, attr), f"Missing field: {attr}")


# ─────────────────────────────────────────────────────────────────────────────
# 2. Certificate generation
# ─────────────────────────────────────────────────────────────────────────────

class TestGenerateSelfSignedCert(unittest.TestCase):
    """All tests that actually generate keys run in a temp directory."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cert = os.path.join(self.tmp, "test.crt")
        self.key  = os.path.join(self.tmp, "test.key")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_creates_cert_file(self):
        generate_self_signed_cert(self.cert, self.key)
        self.assertTrue(Path(self.cert).exists())

    def test_creates_key_file(self):
        generate_self_signed_cert(self.cert, self.key)
        self.assertTrue(Path(self.key).exists())

    def test_cert_is_pem(self):
        generate_self_signed_cert(self.cert, self.key)
        content = Path(self.cert).read_text()
        self.assertIn("BEGIN CERTIFICATE", content)
        self.assertIn("END CERTIFICATE",   content)

    def test_key_is_pem(self):
        generate_self_signed_cert(self.cert, self.key)
        content = Path(self.key).read_text()
        self.assertIn("BEGIN", content)   # RSA PRIVATE KEY or PRIVATE KEY
        self.assertIn("END",   content)

    def test_key_permissions_are_600(self):
        generate_self_signed_cert(self.cert, self.key)
        mode = oct(os.stat(self.key).st_mode)[-3:]
        self.assertEqual(mode, "600",
                         "Private key must be readable only by owner (600)")

    def test_cert_is_loadable_by_ssl(self):
        generate_self_signed_cert(self.cert, self.key)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=self.cert, keyfile=self.key)

    def test_validity_period_default(self):
        """Default cert should be valid for ~10 years (3650 days)."""
        generate_self_signed_cert(self.cert, self.key)
        from cryptography import x509
        cert = x509.load_pem_x509_certificate(Path(self.cert).read_bytes())
        now  = __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        )
        delta = cert.not_valid_after_utc - cert.not_valid_before_utc
        self.assertGreater(delta.days, 3640)

    def test_san_includes_localhost(self):
        generate_self_signed_cert(self.cert, self.key)
        from cryptography import x509
        cert = x509.load_pem_x509_certificate(Path(self.cert).read_bytes())
        san  = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        dns_names = san.value.get_values_for_type(x509.DNSName)
        self.assertIn("localhost", dns_names)

    def test_san_includes_127_0_0_1(self):
        generate_self_signed_cert(self.cert, self.key)
        from cryptography import x509
        import ipaddress
        cert = x509.load_pem_x509_certificate(Path(self.cert).read_bytes())
        san  = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        ips  = san.value.get_values_for_type(x509.IPAddress)
        self.assertIn(ipaddress.IPv4Address("127.0.0.1"), ips)

    def test_san_includes_custom_ip(self):
        generate_self_signed_cert(self.cert, self.key, host="192.168.1.50")
        from cryptography import x509
        import ipaddress
        cert = x509.load_pem_x509_certificate(Path(self.cert).read_bytes())
        san  = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        ips  = san.value.get_values_for_type(x509.IPAddress)
        self.assertIn(ipaddress.IPv4Address("192.168.1.50"), ips)

    def test_san_includes_custom_hostname(self):
        generate_self_signed_cert(self.cert, self.key, host="mypi.local")
        from cryptography import x509
        cert = x509.load_pem_x509_certificate(Path(self.cert).read_bytes())
        san  = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        dns  = san.value.get_values_for_type(x509.DNSName)
        self.assertIn("mypi.local", dns)

    def test_wildcard_host_not_added_to_san(self):
        """0.0.0.0 should not be added as a SAN entry."""
        generate_self_signed_cert(self.cert, self.key, host="0.0.0.0")
        from cryptography import x509
        import ipaddress
        cert = x509.load_pem_x509_certificate(Path(self.cert).read_bytes())
        san  = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        ips  = san.value.get_values_for_type(x509.IPAddress)
        self.assertNotIn(ipaddress.IPv4Address("0.0.0.0"), ips)


# ─────────────────────────────────────────────────────────────────────────────
# 3. build_ssl_context
# ─────────────────────────────────────────────────────────────────────────────

class TestBuildSslContext(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cert = os.path.join(self.tmp, "test.crt")
        self.key  = os.path.join(self.tmp, "test.key")
        generate_self_signed_cert(self.cert, self.key)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_returns_ssl_context(self):
        cfg = ServerConfig(cert_file=self.cert, key_file=self.key,
                           auto_cert=False)
        ctx = build_ssl_context(cfg)
        self.assertIsInstance(ctx, ssl.SSLContext)

    def test_minimum_tls_version_is_1_2(self):
        cfg = ServerConfig(cert_file=self.cert, key_file=self.key,
                           auto_cert=False)
        ctx = build_ssl_context(cfg)
        self.assertEqual(ctx.minimum_version, ssl.TLSVersion.TLSv1_2)

    def test_auto_cert_generates_missing_cert(self):
        cert = os.path.join(self.tmp, "auto.crt")
        key  = os.path.join(self.tmp, "auto.key")
        self.assertFalse(Path(cert).exists())
        cfg = ServerConfig(cert_file=cert, key_file=key, auto_cert=True)
        ctx = build_ssl_context(cfg)
        self.assertIsInstance(ctx, ssl.SSLContext)
        self.assertTrue(Path(cert).exists())
        self.assertTrue(Path(key).exists())

    def test_missing_cert_without_auto_raises_file_not_found(self):
        cfg = ServerConfig(
            cert_file="/nonexistent/cert.crt",
            key_file ="/nonexistent/key.key",
            auto_cert=False,
        )
        with self.assertRaises(FileNotFoundError) as ctx:
            build_ssl_context(cfg)
        self.assertIn("certificate", str(ctx.exception).lower())

    def test_auto_cert_false_missing_key_only_raises(self):
        cfg = ServerConfig(
            cert_file=self.cert,              # cert exists
            key_file="/nonexistent/key.key",  # key missing
            auto_cert=False,
        )
        with self.assertRaises(FileNotFoundError):
            build_ssl_context(cfg)

    def test_existing_cert_not_regenerated(self):
        """If cert already exists, auto_cert=True should NOT regenerate it."""
        mtime_before = os.path.getmtime(self.cert)
        cfg = ServerConfig(cert_file=self.cert, key_file=self.key,
                           auto_cert=True)
        build_ssl_context(cfg)
        mtime_after = os.path.getmtime(self.cert)
        self.assertEqual(mtime_before, mtime_after,
                         "Existing cert should not be overwritten")


# ─────────────────────────────────────────────────────────────────────────────
# 4. _handle_request — HTTP routing
# ─────────────────────────────────────────────────────────────────────────────

class TestHandleRequest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.dashboard = Path(self.tmp) / "dashboard.html"
        self.state     = os.path.join(self.tmp, "state.json")
        self.dashboard.write_text("<html><body>Test Dashboard</body></html>")
        Path(self.state).write_text('{"bms": {}, "victron": {}}')

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_request(self, request_line: str) -> str:
        reader = _make_reader([request_line, "\r\n"])
        writer = _make_writer()
        run(_handle_request(reader, writer, self.dashboard, self.state))
        return _response(writer)

    # ── GET / ────────────────────────────────────────────────────────────────

    def test_get_root_returns_200(self):
        resp = self._run_request("GET / HTTP/1.1")
        self.assertIn("200 OK", resp)

    def test_get_root_returns_html(self):
        resp = self._run_request("GET / HTTP/1.1")
        self.assertIn("Test Dashboard", resp)

    def test_get_root_content_type_html(self):
        resp = self._run_request("GET / HTTP/1.1")
        self.assertIn("text/html", resp)

    # ── GET /dashboard.html ───────────────────────────────────────────────────

    def test_get_dashboard_html_returns_200(self):
        resp = self._run_request("GET /dashboard.html HTTP/1.1")
        self.assertIn("200 OK", resp)

    def test_get_dashboard_html_same_as_root(self):
        resp_root = self._run_request("GET / HTTP/1.1")
        resp_dash = self._run_request("GET /dashboard.html HTTP/1.1")
        # Both should contain the dashboard body
        self.assertIn("Test Dashboard", resp_root)
        self.assertIn("Test Dashboard", resp_dash)

    # ── GET /state.json ───────────────────────────────────────────────────────

    def test_get_state_json_returns_200(self):
        resp = self._run_request("GET /state.json HTTP/1.1")
        self.assertIn("200 OK", resp)

    def test_get_state_json_content_type(self):
        resp = self._run_request("GET /state.json HTTP/1.1")
        self.assertIn("application/json", resp)

    def test_get_state_json_body(self):
        resp = self._run_request("GET /state.json HTTP/1.1")
        self.assertIn('"bms"', resp)

    # ── GET /health ───────────────────────────────────────────────────────────

    def test_get_health_returns_200(self):
        resp = self._run_request("GET /health HTTP/1.1")
        self.assertIn("200 OK", resp)

    def test_get_health_body_is_ok(self):
        resp = self._run_request("GET /health HTTP/1.1")
        self.assertIn("OK", resp)

    # ── 404 paths ─────────────────────────────────────────────────────────────

    def test_unknown_path_returns_404(self):
        resp = self._run_request("GET /robots.txt HTTP/1.1")
        self.assertIn("404", resp)

    def test_post_method_returns_404(self):
        resp = self._run_request("POST / HTTP/1.1")
        self.assertIn("404", resp)

    def test_delete_method_returns_404(self):
        resp = self._run_request("DELETE /dashboard.html HTTP/1.1")
        self.assertIn("404", resp)

    # ── Missing files ─────────────────────────────────────────────────────────

    def test_missing_dashboard_returns_200_placeholder(self):
        missing = Path(self.tmp) / "no_dashboard.html"
        reader  = _make_reader(["GET / HTTP/1.1", "\r\n"])
        writer  = _make_writer()
        run(_handle_request(reader, writer, missing, self.state))
        resp = _response(writer)
        self.assertIn("200", resp)

    def test_missing_state_returns_200_empty_json(self):
        reader = _make_reader(["GET /state.json HTTP/1.1", "\r\n"])
        writer = _make_writer()
        run(_handle_request(reader, writer, self.dashboard, "/nonexistent.json"))
        resp = _response(writer)
        self.assertIn("200 OK", resp)
        self.assertIn("{}", resp)

    # ── Malformed request ─────────────────────────────────────────────────────

    def test_empty_request_does_not_crash(self):
        reader = _make_reader(["\r\n"])
        writer = _make_writer()
        run(_handle_request(reader, writer, self.dashboard, self.state))
        # must not raise

    def test_content_length_header_set(self):
        resp = self._run_request("GET /health HTTP/1.1")
        self.assertIn("Content-Length", resp)

    def test_connection_close_header_set(self):
        resp = self._run_request("GET / HTTP/1.1")
        self.assertIn("Connection: close", resp)


# ─────────────────────────────────────────────────────────────────────────────
# 5. AppConfig [server] INI loading
# ─────────────────────────────────────────────────────────────────────────────

class TestServerConfigIni(unittest.TestCase):

    def _cfg(self, ini: str) -> AppConfig:
        path = _write_ini(ini)
        try:
            return load_config(path)
        finally:
            os.unlink(path)

    def test_server_disabled_by_default(self):
        cfg = self._cfg("[general]\n")
        self.assertFalse(cfg.server.enabled)

    def test_server_section_absent_uses_defaults(self):
        cfg = self._cfg("[general]\ntheme = dark\n")
        self.assertIsInstance(cfg.server, ServerConfig)
        self.assertEqual(cfg.server.port, 4443)

    def test_enabled_true(self):
        cfg = self._cfg("[server]\nenabled = true\n")
        self.assertTrue(cfg.server.enabled)

    def test_enabled_false(self):
        cfg = self._cfg("[server]\nenabled = false\n")
        self.assertFalse(cfg.server.enabled)

    def test_enabled_1(self):
        cfg = self._cfg("[server]\nenabled = 1\n")
        self.assertTrue(cfg.server.enabled)

    def test_enabled_yes(self):
        cfg = self._cfg("[server]\nenabled = yes\n")
        self.assertTrue(cfg.server.enabled)

    def test_host_custom(self):
        cfg = self._cfg("[server]\nhost = 192.168.1.10\n")
        self.assertEqual(cfg.server.host, "192.168.1.10")

    def test_port_custom(self):
        cfg = self._cfg("[server]\nport = 8443\n")
        self.assertEqual(cfg.server.port, 8443)

    def test_cert_file_custom(self):
        cfg = self._cfg("[server]\ncert_file = /etc/ssl/solar.crt\n")
        self.assertEqual(cfg.server.cert_file, "/etc/ssl/solar.crt")

    def test_key_file_custom(self):
        cfg = self._cfg("[server]\nkey_file = /etc/ssl/solar.key\n")
        self.assertEqual(cfg.server.key_file, "/etc/ssl/solar.key")

    def test_auto_cert_false(self):
        cfg = self._cfg("[server]\nauto_cert = false\n")
        self.assertFalse(cfg.server.auto_cert)

    def test_auto_cert_default_true(self):
        cfg = self._cfg("[server]\nenabled = true\n")
        self.assertTrue(cfg.server.auto_cert)

    def test_all_fields_together(self):
        cfg = self._cfg("""
            [server]
            enabled   = true
            host      = 10.0.0.1
            port      = 9443
            cert_file = /tmp/my.crt
            key_file  = /tmp/my.key
            auto_cert = false
        """)
        self.assertTrue(cfg.server.enabled)
        self.assertEqual(cfg.server.host,      "10.0.0.1")
        self.assertEqual(cfg.server.port,      9443)
        self.assertEqual(cfg.server.cert_file, "/tmp/my.crt")
        self.assertEqual(cfg.server.key_file,  "/tmp/my.key")
        self.assertFalse(cfg.server.auto_cert)

    def test_server_config_independent_of_general(self):
        """Server config should not affect other AppConfig fields."""
        cfg = self._cfg("""
            [general]
            theme = business
            [server]
            enabled = true
            port = 9000
        """)
        self.assertEqual(cfg.theme, "business")
        self.assertTrue(cfg.server.enabled)
        self.assertEqual(cfg.server.port, 9000)


# ─────────────────────────────────────────────────────────────────────────────
# 6. Supervisor integration
# ─────────────────────────────────────────────────────────────────────────────

class TestSupervisorServerIntegration(unittest.TestCase):
    """Verify supervisor starts / skips the HTTPS server based on config."""

    def _src(self) -> str:
        with open(f"{REPO_ROOT}/solar_monitor.py") as f:
            return f.read()

    def test_server_enabled_check_in_supervisor(self):
        self.assertIn("cfg.server.enabled", self._src())

    def test_run_https_server_imported_in_supervisor(self):
        self.assertIn("run_https_server", self._src())

    def test_server_task_added_to_tasks_list(self):
        src = self._src()
        # The server task must be appended to the same tasks list as workers
        self.assertIn("tasks.append", src)
        # And it must be conditional on enabled
        enabled_idx = src.index("cfg.server.enabled")
        task_idx    = src.index("run_https_server")
        self.assertGreater(task_idx, enabled_idx,
                           "run_https_server must appear after the enabled check")

    def test_disabled_server_logs_message(self):
        src = self._src()
        self.assertIn("HTTPS server disabled", src)

    def test_enabled_server_logs_url(self):
        src = self._src()
        self.assertIn("cfg.server.port", src)


# ─────────────────────────────────────────────────────────────────────────────
# 7. run_https_server — cancellation
# ─────────────────────────────────────────────────────────────────────────────

class TestRunHttpsServer(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cert = os.path.join(self.tmp, "test.crt")
        self.key  = os.path.join(self.tmp, "test.key")
        generate_self_signed_cert(self.cert, self.key)
        self.dashboard = Path(self.tmp) / "dashboard.html"
        self.dashboard.write_text("<html>test</html>")
        self.state = os.path.join(self.tmp, "state.json")
        Path(self.state).write_text("{}")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_server_starts_and_cancels_cleanly(self):
        """run_https_server must handle CancelledError without raising."""
        cfg = ServerConfig(
            enabled=True, host="127.0.0.1", port=14443,
            cert_file=self.cert, key_file=self.key, auto_cert=False,
        )

        async def _run():
            task = asyncio.create_task(
                run_https_server(cfg, self.dashboard, self.state)
            )
            await asyncio.sleep(0.05)   # let it start
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass   # expected

        run(_run())

    def test_server_binds_to_port(self):
        """After start, the configured port should be reachable (briefly)."""
        cfg = ServerConfig(
            enabled=True, host="127.0.0.1", port=14444,
            cert_file=self.cert, key_file=self.key, auto_cert=False,
        )

        async def _run():
            task = asyncio.create_task(
                run_https_server(cfg, self.dashboard, self.state)
            )
            await asyncio.sleep(0.1)

            # Try connecting (SSL handshake will fail because we don't trust
            # the cert, but the TCP connection proves the port is open)
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode    = ssl.CERT_NONE
            try:
                reader, writer = await asyncio.open_connection(
                    "127.0.0.1", 14444, ssl=ctx
                )
                writer.close()
                connected = True
            except Exception:
                connected = False

            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            return connected

        connected = run(_run())
        self.assertTrue(connected, "Server must accept TCP connections on configured port")


# ─────────────────────────────────────────────────────────────────────────────
# 8. Source-level guarantees
# ─────────────────────────────────────────────────────────────────────────────

class TestServerSourceGuarantees(unittest.TestCase):

    def _src(self) -> str:
        with open(f"{REPO_ROOT}/solar_monitor/server.py") as f:
            return f.read()

    def test_tls_1_2_minimum_enforced(self):
        self.assertIn("TLSv1_2", self._src())

    def test_self_signed_cert_generation_present(self):
        self.assertIn("generate_self_signed_cert", self._src())

    def test_auto_cert_flag_checked(self):
        self.assertIn("auto_cert", self._src())

    def test_key_permissions_set_to_600(self):
        self.assertIn("0o600", self._src())

    def test_health_endpoint_present(self):
        self.assertIn("/health", self._src())

    def test_state_json_endpoint_present(self):
        self.assertIn("/state.json", self._src())

    def test_connection_close_in_response(self):
        self.assertIn("Connection: close", self._src())

    def test_timeout_on_read(self):
        self.assertIn("wait_for", self._src())

    def test_cancelled_error_handled(self):
        self.assertIn("CancelledError", self._src())

    def test_san_extension_added(self):
        self.assertIn("SubjectAlternativeName", self._src())

    def test_server_config_class_present(self):
        self.assertIn("class ServerConfig", self._src())

    def test_run_https_server_is_async(self):
        self.assertIn("async def run_https_server", self._src())


if __name__ == "__main__":
    unittest.main(verbosity=2)
