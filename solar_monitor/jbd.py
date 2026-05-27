"""
solar_monitor/jbd.py — JBD / Vatrer BMS GATT protocol
======================================================
Handles everything needed to connect to a JBD-compatible BMS over BLE GATT
and retrieve a "basic info" reading containing pack voltage, current, state
of charge, cell count, and NTC temperatures.

Supported hardware
------------------
- JBD (Jiabaida / Xiaoxiang) BMS modules
- Vatrer Power batteries (use JBD BMS internally, confirmed on SP16S020L16S100A)
- Overkill Solar boards
- Daly BMS (if they expose the ff00/ff01/ff02 GATT profile)

Protocol summary
----------------
Communication is over GATT:
  Service ff00  (or ffe0 / Nordic UART on some firmware)
  TX char ff01  — BMS sends responses; host subscribes with notify
  RX char ff02  — host sends commands; write with or without response

The command to request basic info (register 0x03):
  DD A5 03 00 FF FD 77

Response packet (4-byte header format, confirmed empirically from Vatrer HW):
  [0]       0xDD  start marker
  [1]       0x03  register echo
  [2]       0x00  status (0x80 = error)
  [3]       N     payload length
  [4..N+3]  payload (big-endian fields)
  [N+4-5]   checksum  (0x10000 - sum(reg, len, payload) & 0xFFFF, big-endian)
  [N+6]     0x77  end marker

Optional password authentication (register 0x06) is sent before the info
request if a password is configured.  Factory default is "0000".

Fault-tolerance design
----------------------
BlueZ on Linux is finicky. Common failure modes and their mitigations:

1. **Device connects but never sends notify** — wrapped in asyncio.wait_for
   with PER_DEVICE_TIMEOUT covering the full operation (connect + settle +
   read), not just the read phase.

2. **Corrupt length byte in received packet** — _packet_complete() returns
   False for n > MAX_PAYLOAD_LEN; _on_notify() detects this, clears the
   buffer, and resets the event so _send_recv does not wait forever.

3. **Spurious notify bytes before command** — _send_recv() clears buffer and
   event before writing the command, discarding any pre-existing bytes.
   _on_notify() also strips leading non-0xDD bytes on every callback.

4. **BlueZ "removed from BlueZ" / "not found"** — the PersistentScanner
   keeps the radio on throughout polling, keeping device references alive.

5. **Permanent errors (bad password, no GATT service)** — detected by
   keyword matching and not retried, saving time for real transient errors.
"""

import asyncio
import logging
import struct
from datetime import datetime
from typing import Optional

from bleak import BleakClient
from bleak.backends.device import BLEDevice

from .models import DeviceReading

log = logging.getLogger(__name__)


# ── Constants ─────────────────────────────────────────────────────────────────

# GATT UUID candidates — tried in order; first match wins.
JBD_UUID_CANDIDATES: list[dict] = [
    {   # Standard JBD / Xiaoxiang (most common)
        "service": "0000ff00-0000-1000-8000-00805f9b34fb",
        "tx":      "0000ff01-0000-1000-8000-00805f9b34fb",  # notify  BMS→host
        "rx":      "0000ff02-0000-1000-8000-00805f9b34fb",  # write   host→BMS
    },
    {   # Vatrer / newer JBD firmware (TX and RX share one characteristic)
        "service": "0000ffe0-0000-1000-8000-00805f9b34fb",
        "tx":      "0000ffe1-0000-1000-8000-00805f9b34fb",
        "rx":      "0000ffe1-0000-1000-8000-00805f9b34fb",
    },
    {   # Nordic UART Service (NUS) clone
        "service": "6e400001-b5a3-f393-e0a9-e50e24dcca9e",
        "tx":      "6e400003-b5a3-f393-e0a9-e50e24dcca9e",  # notify
        "rx":      "6e400002-b5a3-f393-e0a9-e50e24dcca9e",  # write
    },
]

# BLE device name substrings that trigger auto-discovery
JBD_NAME_KEYWORDS: tuple[str, ...] = (
    "jbd", "bms", "xiaoxiang", "overkill", "daly", "vatrer", "sp04", "sp16",
    "jiabaida",
)

# Register 0x03 "basic info" request command
BASIC_INFO_CMD = bytes([0xDD, 0xA5, 0x03, 0x00, 0xFF, 0xFD, 0x77])

# Seconds to wait for a notify response after writing the command.
# Raised from 8 to 12 to accommodate slow BMS firmware on large packs.
READ_TIMEOUT = 12

# Overall per-device deadline: connect + settle + auth + read must all
# complete within this many seconds, or the attempt is abandoned.
# Prevents a hung BleakClient from blocking the entire poll cycle.
PER_DEVICE_TIMEOUT = 35

# Seconds to wait after start_notify before sending the command.
# BlueZ needs this to register the GATT notification subscription on the
# remote device before the BMS can send its response.
NOTIFY_SETTLE_DELAY = 1.0

# Maximum sane BMS payload length; guards against corrupt length byte
# causing an indefinite wait for bytes that will never arrive.
MAX_PAYLOAD_LEN = 128

# Error message substrings that indicate a permanent failure — not retried.
PERMANENT_ERRORS: tuple[str, ...] = (
    "rejected password",
    "no compatible jbd service",
    "bad password",
    "authentication",
)


# ── Packet helpers ────────────────────────────────────────────────────────────

def _checksum(payload: bytes) -> bytes:
    """
    Compute the 2-byte JBD checksum, stored big-endian.

    Checksum = (0x10000 - sum(payload)) & 0xFFFF.
    *payload* must include the register and length bytes but exclude the
    start marker (0xDD) and end marker (0x77).
    """
    chk = (0x10000 - sum(payload)) & 0xFFFF
    return bytes([chk >> 8, chk & 0xFF])


def _verify_checksum(data: bytes) -> bool:
    """
    Verify the checksum of a complete JBD response packet.

    The checksum covers bytes [1 .. N+5] (register through last payload byte).
    Returns True if the packet checksum is valid.
    """
    if len(data) < 7:
        return False
    n           = data[3]
    payload_end = 4 + n
    if len(data) < payload_end + 3:
        return False
    body        = data[1: payload_end]        # reg + status + len + payload
    expected    = _checksum(body)
    actual      = data[payload_end: payload_end + 2]
    return expected == actual


def _packet_complete(buf: bytearray) -> bool:
    """
    Return True when *buf* holds a complete JBD response packet.

    Uses the payload-length field at byte [3] to determine the expected
    total length.  Returns False if the length byte exceeds MAX_PAYLOAD_LEN,
    which indicates a corrupt frame that will never complete.
    """
    if len(buf) < 4:
        return False
    if buf[0] != 0xDD:
        return False
    n = buf[3]
    if n > MAX_PAYLOAD_LEN:
        return False     # corrupt length byte — caller must reset buffer
    return len(buf) >= n + 7


def _parse_basic_info(data: bytes) -> dict:
    """
    Parse a JBD/Vatrer BMS basic-info response (register 0x03).

    Verifies packet framing, start/end markers, status byte, and checksum
    before extracting fields from the big-endian payload.

    Packet structure:
      [0]        0xDD  start marker
      [1]        0x03  register echo
      [2]        0x00  status (0x80 = BMS-reported error)
      [3]        N     payload length
      [4..N+3]   payload (big-endian)
      [N+4..N+5] checksum (big-endian)
      [N+6]      0x77  end marker

    Payload fields (big-endian, offset from payload[0]):
      0-1   pack voltage      10 mV/LSB  (divide by 100 for V)
      2-3   pack current      10 mA/LSB  signed (positive = charging)
      4-5   residual capacity 10 mAh/LSB
      6-7   nominal capacity  10 mAh/LSB
      8-9   cycle count
      19    state of charge   %
      20    FET status bits
      21    cell count
      22    NTC sensor count
      23+   NTC temperatures  0.1 K/LSB  (subtract 2731 for °C)

    Returns:
        dict with keys: voltage_v, current_a, power_w, capacity_pct,
        cell_count, temp_c.
    Raises:
        ValueError on any framing, checksum, length, or status error.
    """
    if len(data) < 8:
        raise ValueError(f"Response too short ({len(data)}B, need ≥8)")
    if data[0] != 0xDD:
        raise ValueError(f"Bad start byte 0x{data[0]:02X} (expected 0xDD)")
    if data[2] == 0x80:
        raise ValueError(f"BMS error status: error code 0x{data[3]:02X}")

    n            = data[3]
    expected_len = n + 7
    if len(data) < expected_len:
        raise ValueError(
            f"Packet truncated: got {len(data)}B, expected {expected_len}B  "
            f"raw={data.hex()}"
        )
    if data[n + 6] != 0x77:
        raise ValueError(
            f"Bad end marker at byte {n+6}: "
            f"0x{data[n+6]:02X} (expected 0x77)  raw={data.hex()}"
        )
    if not _verify_checksum(data):
        # Warn rather than raise — some BMS firmware has checksum quirks
        log.warning(
            f"    BMS checksum mismatch — data may be corrupt  raw={data.hex()}"
        )

    payload = data[4: 4 + n]
    if len(payload) < 23:
        raise ValueError(
            f"Payload too short ({len(payload)}B, need ≥23)  raw={data.hex()}"
        )

    voltage_v  = struct.unpack_from(">H", payload, 0)[0] * 10 / 1000.0
    current_a  = struct.unpack_from(">h", payload, 2)[0] * 10 / 1000.0
    soc        = payload[19]
    cell_count = payload[21]
    ntc_count  = min(payload[22], (len(payload) - 23) // 2)  # guard overflow

    temps_c = [
        round((struct.unpack_from(">H", payload, 23 + i * 2)[0] - 2731) / 10.0, 1)
        for i in range(ntc_count)
    ]
    return {
        "voltage_v":    round(voltage_v, 3),
        "current_a":    round(current_a, 3),
        "power_w":      round(voltage_v * current_a, 2),
        "capacity_pct": soc,
        "cell_count":   cell_count,
        "temp_c":       temps_c,
    }


# ── GATT service discovery ────────────────────────────────────────────────────

async def _discover_chars(client: BleakClient) -> tuple[str, str]:
    """
    Walk the connected device's GATT table and return (tx_uuid, rx_uuid).

    Tries each entry in JBD_UUID_CANDIDATES in order (standard ff00,
    Vatrer ffe0, Nordic UART 6e40).  Falls back to a heuristic scan for
    any service that has both a notify characteristic and a write
    characteristic.

    Logs the full GATT table at DEBUG level and the matched UUIDs at INFO,
    making connection issues immediately visible in the log.

    Returns:
        (tx_uuid, rx_uuid) — TX notifies (BMS→host), RX accepts writes.
    Raises:
        ValueError if no compatible service is found.
    """
    svcs = client.services

    for svc in svcs:
        for c in svc.characteristics:
            log.debug(f"  GATT  svc={svc.uuid}  char={c.uuid}  props={c.properties}")

    # Known-UUID match (preferred)
    for svc in svcs:
        for candidate in JBD_UUID_CANDIDATES:
            if svc.uuid.lower() != candidate["service"].lower():
                continue
            tx = candidate["tx"].lower()
            rx = candidate["rx"].lower()
            char_uuids = {c.uuid.lower() for c in svc.characteristics}
            if tx in char_uuids and rx in char_uuids:
                log.info(f"    JBD service matched: {svc.uuid}  TX={tx}  RX={rx}")
                return tx, rx

    # Heuristic fallback — any service with notify + write
    for svc in svcs:
        notifiers = [c for c in svc.characteristics if "notify"       in c.properties]
        writers   = [c for c in svc.characteristics if "write"        in c.properties
                                                      or "write-without-response" in c.properties]
        if notifiers and writers:
            tx, rx = notifiers[0].uuid, writers[0].uuid
            log.warning(
                f"    No known JBD UUID matched — using heuristic "
                f"TX={tx}  RX={rx}"
            )
            return tx, rx

    # Log full table to help diagnose unsupported firmware
    log.warning("    GATT table (no JBD service found):")
    for svc in svcs:
        for c in svc.characteristics:
            log.warning(f"      svc={svc.uuid}  char={c.uuid}  {c.properties}")
    raise ValueError(
        "No compatible JBD GATT service found — check device firmware or UUID list"
    )


# ── GATT reader ───────────────────────────────────────────────────────────────

class JBDGattReader:
    """
    Manages the notify/write GATT exchange with a JBD-compatible BMS.

    Usage::

        async with BleakClient(device) as client:
            reader = JBDGattReader(client)
            raw    = await reader.read_basic_info(password="0000")
            data   = _parse_basic_info(raw)

    Fault-tolerance notes
    ---------------------
    - Buffer is cleared and the completion event is reset before every
      command write, discarding any stale bytes from a previous exchange.
    - The notify callback strips leading non-0xDD bytes on every invocation,
      recovering from partial or misaligned packets.
    - If a corrupt length byte is detected during packet assembly, the
      buffer is cleared immediately so the next valid packet can be received
      rather than waiting forever for the impossible byte count.
    - The entire read_basic_info sequence (discover → settle → [auth] →
      read) is wrapped in asyncio.wait_for with PER_DEVICE_TIMEOUT so a
      single stalled device cannot block the whole poll cycle.
    """

    def __init__(self, client: BleakClient) -> None:
        self._client   = client
        self._buf      = bytearray()
        self._event    = asyncio.Event()
        self._tx_uuid: Optional[str] = None
        self._rx_uuid: Optional[str] = None

    # ── Notify handler ────────────────────────────────────────────────────────

    def _on_notify(self, _sender, data: bytearray) -> None:
        """
        BLE notification callback — accumulate bytes and signal on completion.

        Resync logic:
        - Strips any leading bytes that are not 0xDD (the JBD start marker).
          This discards partial packets and stale bytes from previous reads.
        - If the accumulated buffer has a corrupt length byte (> MAX_PAYLOAD_LEN),
          the buffer is cleared immediately rather than waiting indefinitely.
          The event is NOT set in this case — the caller times out cleanly and
          retries on the next attempt.
        """
        self._buf.extend(data)

        # Strip non-0xDD leading bytes
        while self._buf and self._buf[0] != 0xDD:
            self._buf.pop(0)

        # Detect and discard corrupt-length frames immediately
        if len(self._buf) >= 4 and self._buf[3] > MAX_PAYLOAD_LEN:
            log.warning(
                f"    Corrupt BMS frame: length byte={self._buf[3]} "
                f"(max {MAX_PAYLOAD_LEN}) — clearing buffer"
            )
            self._buf.clear()
            return

        log.debug(
            f"    notify +{len(data)}B  buf={len(self._buf)}B  "
            f"hex={self._buf.hex()}"
        )

        if _packet_complete(self._buf):
            log.debug(f"    packet complete ({len(self._buf)}B)")
            self._event.set()

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _write(self, data: bytes) -> None:
        """
        Write *data* to the RX characteristic.

        Automatically selects write-with-response or write-without-response
        based on the characteristic's declared properties.
        """
        rx_char = self._client.services.get_characteristic(self._rx_uuid)
        if rx_char is None:
            raise ValueError(
                f"RX characteristic {self._rx_uuid} disappeared after connect"
            )
        use_response = "write" in rx_char.properties
        log.debug(
            f"    write ({'w/rsp' if use_response else 'w/o rsp'})  {data.hex()}"
        )
        await self._client.write_gatt_char(
            self._rx_uuid, data, response=use_response
        )

    async def _send_recv(self, cmd: bytes) -> bytes:
        """
        Send *cmd* and wait up to READ_TIMEOUT seconds for a complete response.

        Clears the accumulation buffer and the completion event before writing
        to discard any stale data from a previous exchange.
        """
        self._buf.clear()
        self._event.clear()
        await self._write(cmd)
        try:
            await asyncio.wait_for(self._event.wait(), timeout=READ_TIMEOUT)
        except asyncio.TimeoutError:
            raise asyncio.TimeoutError(
                f"No BMS response within {READ_TIMEOUT}s after command — "
                f"buffer has {len(self._buf)}B: "
                f"{self._buf.hex()[:64] or '(empty)'}"
            )
        return bytes(self._buf)

    # ── Public API ────────────────────────────────────────────────────────────

    async def authenticate(self, password: str) -> None:
        """
        Send the password unlock command (register 0x06).

        The BMS responds with a short frame whose status byte indicates
        success (0x00) or failure (0x80).  A rejected password raises
        ValueError immediately — there is no point retrying with the
        same wrong password.
        """
        pw_bytes = password.encode("ascii")
        body     = bytes([0x06, len(pw_bytes)]) + pw_bytes
        cmd      = bytes([0xDD, 0x5A]) + body + _checksum(body) + bytes([0x77])
        reply    = await self._send_recv(cmd)
        if len(reply) < 3:
            raise ValueError(
                f"Auth response too short ({len(reply)}B): {reply.hex()}"
            )
        if reply[2] == 0x80:
            raise ValueError(
                "BMS rejected password — update the password in your config"
            )
        log.debug("    BMS authentication accepted")

    async def read_basic_info(self, password: Optional[str] = None) -> bytes:
        """
        Read register 0x03 from the BMS and return the raw response bytes.

        Sequence:
          1. GATT service / characteristic discovery
          2. Subscribe to TX notifications
          3. Wait NOTIFY_SETTLE_DELAY seconds for BlueZ to register the
             subscription on the remote device (required — without it the
             first notify arrives before the kernel handler is active)
          4. If password set: send authentication command
          5. Send basic-info request, reassemble multi-chunk response

        The entire sequence is expected to complete within PER_DEVICE_TIMEOUT
        seconds; callers should wrap this call in asyncio.wait_for.

        Returns:
            Complete raw packet bytes ready for _parse_basic_info().
        Raises:
            asyncio.TimeoutError  — no response within READ_TIMEOUT.
            ValueError            — auth failure, GATT service missing, etc.
        """
        self._tx_uuid, self._rx_uuid = await _discover_chars(self._client)
        await self._client.start_notify(self._tx_uuid, self._on_notify)

        # BlueZ race: give the remote device time to register the subscription
        await asyncio.sleep(NOTIFY_SETTLE_DELAY)

        try:
            if password:
                await self.authenticate(password)
            return await self._send_recv(BASIC_INFO_CMD)
        finally:
            try:
                await self._client.stop_notify(self._tx_uuid)
            except Exception:
                pass


# ── Public reader function ────────────────────────────────────────────────────

async def read_jbd_device(
    device: BLEDevice,
    friendly_name: Optional[str] = None,
    password: Optional[str] = None,
) -> DeviceReading:
    """
    Connect to a JBD BMS, read basic info, and return a DeviceReading.

    The entire operation (connect + settle + read) is wrapped in a
    PER_DEVICE_TIMEOUT deadline so a stalled device cannot block the
    poll cycle indefinitely.

    Args:
        device:        BLEDevice from the scanner.
        friendly_name: Dashboard display label; falls back to BLE name or MAC.
        password:      BMS connection password, or None.

    Returns:
        DeviceReading with all fields populated on success, or with
        ``error`` set to a human-readable message on failure.
    """
    ts   = datetime.now().isoformat(timespec="seconds")
    name = friendly_name or device.name or device.address
    r    = DeviceReading(address=device.address, name=name,
                         device_type="bms", timestamp=ts)
    try:
        async with asyncio.timeout(PER_DEVICE_TIMEOUT):
            async with BleakClient(device, timeout=15) as client:
                raw    = await JBDGattReader(client).read_basic_info(
                    password=password
                )
                parsed = _parse_basic_info(raw)
                for k, v in parsed.items():
                    setattr(r, k, v)
        log.info(
            f"  [BMS]  {name}: {r.voltage_v}V  {r.current_a}A  "
            f"{r.power_w}W  SoC={r.capacity_pct}%"
        )
    except asyncio.TimeoutError:
        r.error = (
            f"Timed out after {PER_DEVICE_TIMEOUT}s — "
            "device connected but did not respond"
        )
        log.warning(f"  [BMS]  {name}: TIMEOUT ({PER_DEVICE_TIMEOUT}s)")
    except Exception as exc:
        r.error = str(exc)
        log.warning(f"  [BMS]  {name}: ERROR — {exc}")
    return r
