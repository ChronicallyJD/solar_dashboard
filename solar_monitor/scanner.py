"""
solar_monitor/scanner.py — BLE scanning, device resolution, and poll orchestration
====================================================================================
Coordinates the full polling cycle:
  1. BLE scan (PersistentScanner keeps radio on throughout)
  2. Device resolution: match configured devices to scan results
  3. Sequential BMS polling (BlueZ reliability constraint)
  4. Victron reading from accumulated advertisement payloads

BlueZ sequential polling rationale
------------------------------------
BlueZ on Linux serialises all GATT operations through a single D-Bus socket.
Firing more than ~2 concurrent BleakClient.connect() calls produces:
  - "org.bluez.Error.Failed: Operation already in progress"
  - "br-connection-canceled"
Polling BMS devices strictly one-at-a-time with a gap between each is slower
but produces far more reliable results across 5–10 devices.

Victron advertisement accumulation
-------------------------------------
Victron devices cycle through broadcasting multiple record types within a
single advertisement period.  PersistentScanner accumulates every distinct
payload seen per MAC so poll_all can give victron.read_victron_advertisement
a complete picture of what the device is broadcasting.
"""

import asyncio
import fcntl
import logging
import os
import tempfile
from datetime import datetime
from typing import Optional

from bleak import BleakScanner
from bleak.backends.device import BLEDevice

from .config import AppConfig
from .models import DeviceReading
from .jbd import JBD_NAME_KEYWORDS, read_jbd_device
from .victron import (
    VICTRON_MFR_ID, VICTRON_NAME_KEYWORDS, VICTRON_RECORD_TYPES,
    read_victron_advertisement,
)

log = logging.getLogger(__name__)

# ── BLE scan lock ─────────────────────────────────────────────────────────────
# BlueZ allows only ONE active BLE scan at a time across all processes.
# When bms_monitor.py and victron_monitor.py run concurrently they can both
# call BleakScanner.start() in the same second, producing:
#   [org.bluez.Error.InProgress] Operation already in progress
#
# We serialise scans with a POSIX advisory lock file.  The lock is held only
# for the duration of scanner.scan() + device resolution; it is never held
# while GATT connections are open (those don't require the radio to be in
# scan mode).
_BLE_LOCK_PATH    = os.path.join(tempfile.gettempdir(), "solar_monitor_ble.lock")
_BLE_LOCK_TIMEOUT = 60.0   # seconds to wait before giving up


class _BleScanLock:
    """
    Async context manager that acquires a process-level POSIX advisory lock
    on ``_BLE_LOCK_PATH`` before entering.

    Polls with a short sleep so the event loop stays responsive while waiting.
    Raises ``TimeoutError`` if the lock cannot be acquired within
    ``_BLE_LOCK_TIMEOUT`` seconds.
    """

    _POLL_INTERVAL = 0.5   # seconds between lock-acquire attempts

    async def __aenter__(self):
        self._fh = open(_BLE_LOCK_PATH, "w")
        deadline = asyncio.get_event_loop().time() + _BLE_LOCK_TIMEOUT
        while True:
            try:
                fcntl.flock(self._fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except BlockingIOError:
                if asyncio.get_event_loop().time() >= deadline:
                    self._fh.close()
                    raise TimeoutError(
                        f"Could not acquire BLE scan lock within "
                        f"{_BLE_LOCK_TIMEOUT:.0f}s — another process may be "
                        f"stuck. Delete {_BLE_LOCK_PATH} if this persists."
                    )
                await asyncio.sleep(self._POLL_INTERVAL)

    async def __aexit__(self, *_):
        fcntl.flock(self._fh, fcntl.LOCK_UN)
        self._fh.close()




BMS_RETRIES      = 3    # total attempts per BMS device (including the first)
RETRY_DELAY      = 4.0  # seconds between retry attempts
INTER_DEVICE_GAP = 1.5  # seconds between successive BMS connections

# Error substrings that indicate a transient BlueZ failure worth retrying.
# NOTE: do NOT include empty string here — that would match every error,
# causing permanent failures (bad password, no GATT service) to be retried.
_TRANSIENT_ERRORS: tuple[str, ...] = (
    "operation already in progress",
    "br-connection-canceled",
    "connection attempt",
    "failed to connect",
    "not found",
    "removed from bluez",
    "le connection",
    "timed out",
    "timeout",
    "payload too short",
    "no response",
    "disconnected",
    "device disconnected",
)

# Error substrings that indicate a permanent failure — stop retrying immediately.
# These take priority over _TRANSIENT_ERRORS.
_PERMANENT_ERRORS: tuple[str, ...] = (
    "rejected password",
    "no compatible jbd",
    "bad password",
    "authentication",
    "no compatible",
    "not found during scan",
)


# ── Persistent BLE scanner ────────────────────────────────────────────────────

class PersistentScanner:
    """
    Long-lived BLE scanner that keeps the radio on throughout the poll cycle.

    Why persistent?
    ---------------
    BleakScanner.discover() stops the radio after its timeout, causing
    BlueZ to evict cached device objects.  Subsequent BleakClient(device)
    calls then raise "device was not found / removed from BlueZ".
    Keeping the scanner running avoids this entirely.

    Device cache
    ------------
    Every BLEDevice seen across ALL scan cycles is retained in
    _device_cache.  When a configured BMS device is not visible in the
    current scan window (temporarily out of range, slow to advertise after
    a power cycle), the cached BLEDevice is used for the GATT connection
    attempt rather than giving up immediately.

    Victron payload accumulation
    ----------------------------
    Victron devices cycle through multiple record types per advertisement
    period.  All distinct payloads are accumulated per MAC so the reader
    can try each record type and choose the most informative one.
    """

    def __init__(self) -> None:
        self._seen:              dict[str, tuple]       = {}   # this scan window
        self._device_cache:      dict[str, BLEDevice]   = {}   # all-time
        self._victron_payloads:  dict[str, list[bytes]] = {}
        self._scanner: Optional[BleakScanner]           = None

    def _cb(self, device: BLEDevice, adv_data) -> None:
        """BleakScanner detection callback."""
        mac = device.address.upper()
        self._seen[mac]         = (device, adv_data)
        self._device_cache[mac] = device   # always keep freshest BLEDevice

        mfr = getattr(adv_data, "manufacturer_data", {}) or {}
        raw = mfr.get(VICTRON_MFR_ID)
        if not raw:
            return

        payloads = raw if isinstance(raw, list) else [raw]
        seen_set = {bytes(p) for p in self._victron_payloads.get(mac, [])}

        for p in payloads:
            pb = bytes(p)
            if not pb or pb in seen_set:
                continue
            if pb[0] == 0x10:
                if len(pb) < 9:
                    continue
                record_type = pb[3] & 0x0F
            elif len(pb) >= 5:
                record_type = pb[0]
            else:
                continue
            if record_type not in VICTRON_RECORD_TYPES:
                continue
            self._victron_payloads.setdefault(mac, []).append(pb)
            seen_set.add(pb)

    async def scan(self, duration: float) -> dict[str, tuple]:
        """
        Scan for *duration* seconds.  Clears per-cycle state but preserves
        _device_cache across calls.

        Acquires a process-level lock (``_BLE_LOCK_PATH``) before starting
        so that bms_monitor.py and victron_monitor.py never scan simultaneously.
        BlueZ only allows one active scan system-wide; concurrent starts produce
        ``[org.bluez.Error.InProgress]``.

        If the radio is already in use by another process the call blocks until
        the lock is released (up to ``_BLE_LOCK_TIMEOUT`` seconds).

        Also retries once on ``InProgress`` in case a stale scanner from a
        previous cycle is still registered with BlueZ.
        """
        self._seen.clear()
        self._victron_payloads.clear()

        async with _BleScanLock():
            # Stop any scanner left over from a previous cycle before starting
            await self.stop()

            for attempt in range(2):
                try:
                    self._scanner = BleakScanner(detection_callback=self._cb)
                    await self._scanner.start()
                    break
                except Exception as exc:
                    if "InProgress" in str(exc) and attempt == 0:
                        log.warning(
                            "BLE scanner: Operation already in progress — "
                            "waiting 5s for BlueZ to release the radio …"
                        )
                        await self.stop()
                        await asyncio.sleep(5.0)
                        continue
                    raise   # re-raise on second attempt or other errors

            await asyncio.sleep(duration)

        return dict(self._seen)

    def latest_adv(self, mac: str) -> Optional[tuple]:
        """Return the most recent (BLEDevice, adv_data) from this scan."""
        return self._seen.get(mac.upper())

    def cached_device(self, mac: str) -> Optional[BLEDevice]:
        """
        Return the most recently seen BLEDevice for *mac* from any scan.

        Used to retry GATT connections to configured devices that were not
        visible in the most recent scan window.
        """
        return self._device_cache.get(mac.upper())

    def victron_payloads(self, mac: str) -> list[bytes]:
        """All distinct Victron payloads accumulated for *mac* this cycle."""
        return list(self._victron_payloads.get(mac.upper(), []))

    async def stop(self) -> None:
        """Stop the BLE scanner and release the radio."""
        if self._scanner:
            try:
                await self._scanner.stop()
            except Exception:
                pass
            self._scanner = None


# ── Device resolution ─────────────────────────────────────────────────────────

async def resolve_devices(cfg: AppConfig) -> tuple[list, list, PersistentScanner]:
    """
    Scan for BLE devices and resolve them against the current configuration.

    Returns:
        ``(jbd_pairs, mppt_triples, scanner)`` where:

        - ``jbd_pairs``: list of ``(BLEDevice, friendly_name, password)`` for
          BMS devices found in the scan, or ``(None, name, ident, password)``
          for configured devices that were not seen.

        - ``mppt_triples``: list of ``(BLEDevice, adv_data, name, enc_key)``
          for Victron devices found in the scan, or ``(None, None, name, ident)``
          for missing configured devices.

        - ``scanner``: the running PersistentScanner; caller must ``await
          scanner.stop()`` after all connections are finished.

    Device matching order:
      1. Configured devices matched by MAC (exact, case-insensitive).
      2. Configured BMS devices matched by BLE advertisement name.
      3. Auto-discovered devices matched by BLE name keyword patterns.
    """
    log.info("Scanning for BLE devices …")
    scanner = PersistentScanner()
    seen    = await scanner.scan(cfg.scan_timeout)
    log.info(f"Scan complete — {len(seen)} device(s) found")

    jbd_pairs    = []
    mppt_triples = []

    # ── Explicit BMS devices ──────────────────────────────────────────────────
    if not cfg.auto_discover_bms:
        for dc in cfg.bms_devices:
            entry = None
            if dc.mac:
                entry = seen.get(dc.mac.upper())
                if not entry:
                    log.warning(
                        f"  [BMS]  '{dc.name}' (MAC {dc.mac}) "
                        f"not seen in scan — will show as OFFLINE"
                    )
            elif dc.ble_name:
                ble_lower = dc.ble_name.lower()
                for _mac, (dev, adv) in seen.items():
                    if (dev.name or "").lower() == ble_lower:
                        entry = (dev, adv)
                        log.info(
                            f"  [BMS]  '{dc.name}' matched BLE name "
                            f"'{dc.ble_name}' -> {dev.address}"
                        )
                        break
                if not entry:
                    log.warning(
                        f"  [BMS]  '{dc.name}' (BLE name '{dc.ble_name}') "
                        f"not seen in scan — will show as OFFLINE"
                    )

            if entry:
                jbd_pairs.append((entry[0], dc.name, dc.password))
            else:
                placeholder = dc.mac or dc.ble_name or "??"
                jbd_pairs.append((None, dc.name, placeholder, dc.password))

    # ── Explicit Victron devices ──────────────────────────────────────────────
    # Pre-build key lookups so auto-discovery can attach keys too
    mppt_key_by_mac  = {
        dc.mac.upper(): dc.enc_key
        for dc in cfg.mppt_devices if dc.mac and dc.enc_key
    }
    mppt_key_by_name = {
        (dc.ble_name or "").lower(): dc.enc_key
        for dc in cfg.mppt_devices if dc.ble_name and dc.enc_key
    }

    if not cfg.auto_discover_mppt:
        for dc in cfg.mppt_devices:
            entry = None
            if dc.mac:
                entry = seen.get(dc.mac.upper())
            if entry is None and dc.ble_name:
                ble_lower = dc.ble_name.lower()
                for _mac, (dev, adv) in seen.items():
                    if (dev.name or "").lower() == ble_lower:
                        entry = (dev, adv)
                        log.info(
                            f"  [Victron] '{dc.name}' matched BLE name "
                            f"'{dc.ble_name}' -> {dev.address}"
                        )
                        break
            if entry:
                mppt_triples.append((entry[0], entry[1], dc.name, dc.enc_key, dc.device_type))
            else:
                log.warning(
                    f"  [Victron] '{dc.name}' "
                    f"({dc.mac or dc.ble_name}) not seen in scan"
                )
                mppt_triples.append(
                    (None, None, dc.name, dc.mac or dc.ble_name or "unknown", dc.device_type)
                )

    # ── Auto-discovery ────────────────────────────────────────────────────────
    explicit_macs  = ({dc.mac.upper() for dc in cfg.bms_devices  if dc.mac} |
                      {dc.mac.upper() for dc in cfg.mppt_devices if dc.mac})
    explicit_names = {(dc.ble_name or "").lower()
                      for dc in cfg.bms_devices if dc.ble_name}

    for mac, (dev, adv) in seen.items():
        if mac in explicit_macs:
            continue
        name_lower = (dev.name or "").lower()
        if name_lower in explicit_names:
            continue

        if cfg.auto_discover_bms and any(
            kw in name_lower for kw in JBD_NAME_KEYWORDS
        ):
            jbd_pairs.append((dev, dev.name or mac, None))
            log.info(f"  [BMS]  Auto-discovered: {dev.name or mac} ({mac})")

        elif cfg.auto_discover_mppt and (
            any(kw in name_lower for kw in VICTRON_NAME_KEYWORDS)
            or VICTRON_MFR_ID in (getattr(adv, "manufacturer_data", {}) or {})
        ):
            key = mppt_key_by_mac.get(mac) or mppt_key_by_name.get(name_lower)
            mppt_triples.append((dev, adv, dev.name or mac, key, None))
            log.info(f"  [Victron] Auto-discovered: {dev.name or mac} ({mac})"
                     + (" (key set)" if key else ""))

    log.info(
        f"Resolved {len(jbd_pairs)} BMS device(s), "
        f"{len(mppt_triples)} Victron device(s)"
    )
    return jbd_pairs, mppt_triples, scanner


# ── Poll orchestration ────────────────────────────────────────────────────────

async def _poll_bms(
    jbd_pairs: list,
    scanner: "PersistentScanner",
) -> list[DeviceReading]:
    """
    Poll all configured BMS devices sequentially and return their readings.

    Separated from Victron polling so it can run in its own process.
    Retries transient errors, stops immediately on permanent ones.
    """

    async def _read_bms_with_retry(entry) -> DeviceReading:
        dev, friendly, password = entry[0], entry[1], entry[2]

        if dev is None:
            name  = entry[1]
            ident = entry[2] if len(entry) > 2 else "??"
            cached = scanner.cached_device(ident) if ident and ':' in str(ident) else None
            if cached:
                log.info(
                    f"  [BMS]  {name}: not in scan — using cached device "
                    f"({cached.address})"
                )
                dev      = cached
                password = entry[3] if len(entry) > 3 else None
            else:
                return DeviceReading(
                    address=str(ident), name=name, device_type="bms",
                    timestamp=datetime.now().isoformat(timespec="seconds"),
                    error="Device not found in scan and not in cache",
                )

        result: Optional[DeviceReading] = None

        for attempt in range(BMS_RETRIES):
            if attempt > 0:
                log.info(f"  [BMS]  {friendly}: retry {attempt}/{BMS_RETRIES - 1} ...")
                await asyncio.sleep(RETRY_DELAY)

            result = await read_jbd_device(dev, friendly, password=password)

            if result.error is None:
                return result

            err_lower = (result.error or "").lower()

            if any(p in err_lower for p in _PERMANENT_ERRORS):
                log.warning(
                    f"  [BMS]  {friendly}: permanent error, "
                    f"not retrying: {result.error}"
                )
                break

            if any(t in err_lower for t in _TRANSIENT_ERRORS):
                log.debug(
                    f"  [BMS]  {friendly}: transient error on attempt "
                    f"{attempt + 1}: {result.error}"
                )
                continue

            log.warning(
                f"  [BMS]  {friendly}: unclassified error on attempt "
                f"{attempt + 1}, will retry: {result.error}"
            )

        return result   # type: ignore[return-value]

    bms_readings: list[DeviceReading] = []
    for i, entry in enumerate(jbd_pairs):
        if i > 0:
            await asyncio.sleep(INTER_DEVICE_GAP)
        bms_readings.append(await _read_bms_with_retry(entry))
    return bms_readings


def _poll_victron(
    mppt_triples: list,
    scanner: "PersistentScanner",
) -> list[DeviceReading]:
    """
    Build Victron DeviceReadings from accumulated BLE advertisement payloads.

    This is synchronous — no GATT connections, no waiting.  All data comes
    from the packets the scanner already accumulated during its scan window.

    Separated from BMS polling so it can run in its own process.
    """
    victron_readings: list[DeviceReading] = []
    for entry in mppt_triples:
        if entry[0] is None:
            _, _, name, ident, *_ = entry
            victron_readings.append(DeviceReading(
                address=ident, name=name, device_type="mppt",
                timestamp=datetime.now().isoformat(timespec="seconds"),
                error="Device not found during scan",
            ))
        else:
            dev, adv_snapshot, name, key, dtype = entry
            all_payloads = scanner.victron_payloads(dev.address)
            fresh        = scanner.latest_adv(dev.address)
            adv          = fresh[1] if fresh else adv_snapshot
            victron_readings.append(
                read_victron_advertisement(
                    dev, adv, name, key, all_payloads,
                    device_type_override=dtype,
                )
            )
    return victron_readings


async def poll_all(
    jbd_pairs: list,
    mppt_triples: list,
    scanner: "PersistentScanner",
) -> tuple[list[DeviceReading], list[DeviceReading]]:
    """
    Read all devices and return ``(bms_readings, victron_readings)``.

    This is the combined entry point used by the legacy single-process
    launcher (``jbd_bms_monitor.py``).  For new deployments, prefer running
    ``bms_monitor.py`` and ``victron_monitor.py`` as separate processes with
    independent poll intervals.

    BMS devices are polled sequentially over GATT (slow, unreliable).
    Victron devices are read from accumulated BLE advertisements (fast).
    """
    bms_readings     = await _poll_bms(jbd_pairs, scanner)
    victron_readings = _poll_victron(mppt_triples, scanner)
    await scanner.stop()
    return bms_readings, victron_readings
