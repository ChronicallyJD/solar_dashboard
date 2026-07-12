"""
solar_monitor/server.py - Async HTTPS dashboard server
=======================================================
Serves the dashboard HTML and raw state JSON over HTTPS using Python's
built-in asyncio + ssl.  No extra dependencies beyond what the rest of
the project already requires.

Design
------
- Runs as an asyncio Task inside the supervisor alongside worker processes
  and the dashboard writer - no separate process needed.
- Uses ssl.SSLContext wrapping asyncio.start_server for TLS.
- Auto-generates a self-signed certificate on first run when no cert/key
  is provided (requires the 'cryptography' package, already a dependency).
- Serves three routes:
    GET /               → dashboard HTML (same as the output file)
    GET /dashboard.html → same as above
    GET /state.json     → raw state file JSON (useful for API consumers)
    GET /health         → 200 OK plaintext (for monitoring / load balancers)
  Everything else → 404

Auto-generated certificates
----------------------------
Self-signed certs are generated with:
  - 2048-bit RSA key
  - SHA-256 signature
  - 10-year validity
  - SAN entries for localhost and the configured host IP
  - Stored at the paths configured in [server] cert_file / key_file

Browsers will show a security warning for self-signed certs.  To dismiss
it permanently: add the cert to your OS / browser trust store, or use a
real certificate from Let's Encrypt (point cert_file / key_file at it).

Configuration
-------------
All settings live in the [server] section of config.ini:

    [server]
    enabled   = true
    host      = 0.0.0.0
    port      = 4443
    cert_file = server.crt
    key_file  = server.key
    auto_cert = true          # generate cert if cert_file doesn't exist
"""

import asyncio
import datetime
import ipaddress
import logging
import os
import ssl
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Configuration dataclass
# ─────────────────────────────────────────────────────────────────────────────

class ServerConfig:
    """All HTTPS server settings, with sensible defaults."""

    def __init__(
        self,
        enabled:   bool = False,
        host:      str  = "0.0.0.0",
        port:      int  = 4443,
        cert_file: str  = "server.crt",
        key_file:  str  = "server.key",
        auto_cert: bool = True,
    ) -> None:
        self.enabled   = enabled
        self.host      = host
        self.port      = port
        self.cert_file = cert_file
        self.key_file  = key_file
        self.auto_cert = auto_cert

    def __repr__(self) -> str:
        return (
            f"ServerConfig(enabled={self.enabled}, host={self.host!r}, "
            f"port={self.port}, cert={self.cert_file!r}, "
            f"auto_cert={self.auto_cert})"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Certificate generation
# ─────────────────────────────────────────────────────────────────────────────

def generate_self_signed_cert(
    cert_path: str,
    key_path:  str,
    host:      str  = "0.0.0.0",
    days:      int  = 3650,
) -> None:
    """
    Generate a self-signed RSA certificate and write it to *cert_path* /
    *key_path*.

    Requires the ``cryptography`` package (already a project dependency).
    The certificate includes Subject Alternative Names for:
      - DNS: localhost
      - DNS: the configured hostname (if it looks like a hostname)
      - IP:  127.0.0.1
      - IP:  the configured host address (if it looks like an IP)

    Raises ``ImportError`` if ``cryptography`` is not installed.
    Raises ``OSError`` if the output paths are not writable.
    """
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError as exc:
        raise ImportError(
            "The 'cryptography' package is required for certificate generation.\n"
            "Install it with:  pip install cryptography"
        ) from exc

    log.info(
        f"Generating self-signed certificate: {cert_path} / {key_path}"
    )

    # 2048-bit RSA private key
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    # Subject / Issuer
    name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "Solar Monitor"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Solar Monitor"),
    ])

    # Subject Alternative Names
    san_entries: list = [
        x509.DNSName("localhost"),
        x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
    ]
    host_stripped = host.strip()
    if host_stripped not in ("0.0.0.0", "::", ""):
        try:
            san_entries.append(x509.IPAddress(ipaddress.ip_address(host_stripped)))
        except ValueError:
            san_entries.append(x509.DNSName(host_stripped))

    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=days))
        .add_extension(
            x509.SubjectAlternativeName(san_entries),
            critical=False,
        )
        .add_extension(
            x509.BasicConstraints(ca=False, path_length=None),
            critical=True,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, key_encipherment=True,
                content_commitment=False, data_encipherment=False,
                key_agreement=False, key_cert_sign=False, crl_sign=False,
                encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )

    # Write key (PEM, no passphrase)
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    # Create the key file with 0600 from the start so it is never readable
    # by other users, even briefly or after a crash mid-write.
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, key_pem)
    finally:
        os.close(fd)
    os.chmod(key_path, 0o600)   # in case the file pre-existed with wider mode

    # Write certificate (PEM)
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    Path(cert_path).write_bytes(cert_pem)

    log.info(
        f"Self-signed certificate generated - valid for {days} days.  "
        f"Add {cert_path} to your browser/OS trust store to avoid "
        f"the 'not trusted' warning."
    )


# ─────────────────────────────────────────────────────────────────────────────
# SSL context builder
# ─────────────────────────────────────────────────────────────────────────────

def build_ssl_context(cfg: ServerConfig) -> ssl.SSLContext:
    """
    Return a configured ``ssl.SSLContext`` for the server.

    Generates a self-signed certificate if:
      - ``cfg.auto_cert`` is True, AND
      - the cert file or key file does not exist

    Raises ``FileNotFoundError`` if auto_cert is False and the files are missing.
    Raises ``ssl.SSLError`` if the cert/key files are invalid.
    """
    cert_missing = not Path(cfg.cert_file).exists()
    key_missing  = not Path(cfg.key_file).exists()

    if cert_missing or key_missing:
        if cfg.auto_cert:
            generate_self_signed_cert(cfg.cert_file, cfg.key_file, cfg.host)
        else:
            missing = []
            if cert_missing: missing.append(cfg.cert_file)
            if key_missing:  missing.append(cfg.key_file)
            raise FileNotFoundError(
                f"HTTPS server: certificate file(s) not found: "
                f"{', '.join(missing)}.  "
                f"Set auto_cert = true in [server] to generate one automatically, "
                f"or provide your own cert/key files."
            )

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(certfile=cfg.cert_file, keyfile=cfg.key_file)
    log.info(
        f"SSL context loaded - cert: {cfg.cert_file}  key: {cfg.key_file}"
    )
    return ctx


# ─────────────────────────────────────────────────────────────────────────────
# HTTP request handling
# ─────────────────────────────────────────────────────────────────────────────

_HTTP_200 = (
    "HTTP/1.1 200 OK\r\n"
    "Content-Type: {content_type}\r\n"
    "Content-Length: {length}\r\n"
    "Connection: close\r\n"
    "\r\n"
)
_HTTP_404 = (
    "HTTP/1.1 404 Not Found\r\n"
    "Content-Type: text/plain\r\n"
    "Content-Length: 9\r\n"
    "Connection: close\r\n"
    "\r\n"
    "Not Found"
)
_HTTP_500 = (
    "HTTP/1.1 500 Internal Server Error\r\n"
    "Content-Type: text/plain\r\n"
    "Content-Length: 21\r\n"
    "Connection: close\r\n"
    "\r\n"
    "Internal Server Error"
)


async def _handle_request(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    dashboard_path: Path,
    state_path:     str,
) -> None:
    """
    Handle a single HTTPS request.

    Reads the request line, routes to the appropriate handler, writes the
    response, and closes the connection.  Any error is caught and logged;
    the connection is always closed.
    """
    try:
        async def _read_request():
            request_line = await reader.readline()
            # Consume remaining headers (we don't use them).  Cap the count so
            # a client cannot hold the connection open by dripping headers.
            for _ in range(100):
                line = await reader.readline()
                if line in (b"\r\n", b"\n", b""):
                    break
            return request_line

        # One deadline for the whole request, not per line.
        request_line = await asyncio.wait_for(_read_request(), timeout=15.0)
        request      = request_line.decode(errors="replace").strip()
        parts        = request.split()
        if len(parts) < 2:
            writer.write(_HTTP_404.encode())
            return

        method, path = parts[0], parts[1]

        log.debug(f"HTTPS {method} {path}")

        # ── Route table ──────────────────────────────────────────────────────
        if method != "GET":
            writer.write(_HTTP_404.encode())
            return

        if path in ("/", "/dashboard.html"):
            try:
                body = dashboard_path.read_bytes()
            except FileNotFoundError:
                body = b"<html><body>Dashboard not yet generated. Check monitor logs.</body></html>"
            header = _HTTP_200.format(
                content_type="text/html; charset=utf-8",
                length=len(body),
            )
            writer.write(header.encode() + body)

        elif path == "/state.json":
            try:
                body = Path(state_path).read_bytes()
                content_type = "application/json"
            except FileNotFoundError:
                body = b"{}"
                content_type = "application/json"
            header = _HTTP_200.format(content_type=content_type, length=len(body))
            writer.write(header.encode() + body)

        elif path == "/health":
            body   = b"OK"
            header = _HTTP_200.format(content_type="text/plain", length=len(body))
            writer.write(header.encode() + body)

        else:
            writer.write(_HTTP_404.encode())

    except (asyncio.TimeoutError, ConnectionResetError, BrokenPipeError):
        pass   # client disconnected - not an error
    except Exception as exc:
        log.debug(f"HTTPS handler error: {exc}")
        try:
            writer.write(_HTTP_500.encode())
        except Exception:
            pass
    finally:
        try:
            await writer.drain()
            writer.close()
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Server entry point
# ─────────────────────────────────────────────────────────────────────────────

async def run_https_server(
    cfg:            ServerConfig,
    dashboard_path: Path,
    state_path:     str,
) -> None:
    """
    Run the HTTPS dashboard server until cancelled.

    This is an ``async`` function designed to run as an asyncio Task inside
    the supervisor.  It blocks (awaiting connections) until the task is
    cancelled on supervisor shutdown.

    Args:
        cfg:            ServerConfig with host, port, cert paths.
        dashboard_path: Path to the HTML file written by the dashboard loop.
        state_path:     Path to the shared state JSON file.
    """
    ssl_ctx = build_ssl_context(cfg)

    def client_connected(reader, writer):
        asyncio.create_task(
            _handle_request(reader, writer, dashboard_path, state_path)
        )

    server = await asyncio.start_server(
        client_connected,
        host=cfg.host,
        port=cfg.port,
        ssl=ssl_ctx,
    )

    addrs = [str(s.getsockname()) for s in server.sockets]
    log.info(
        f"HTTPS server listening on "
        f"https://{cfg.host}:{cfg.port}/  "
        f"(sockets: {', '.join(addrs)})"
    )
    log.info(
        f"  → Dashboard:  https://localhost:{cfg.port}/dashboard.html"
    )
    log.info(
        f"  → State API:  https://localhost:{cfg.port}/state.json"
    )

    try:
        async with server:
            await server.serve_forever()
    except asyncio.CancelledError:
        log.info("HTTPS server stopped.")
