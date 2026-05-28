"""
solar_monitor/scanner.py — BLE device resolution and poll orchestration
========================================================================
Two separate strategies, matching the two device types:

BMS (JBD/Vatrer)
----------------
Connects directly by MAC address via BleakClient — NO scanning required.
BlueZ connects to the device immediately if it is advertising, or attempts
a direct connection if it is in its BlueZ cache.  This means:
  - No radio contention with the Victron process
  - No InProgress errors
  - No lock file needed

Victron (MPPT, VE.Bus Smart Dongle, etc.)
------------------------------------------
Victron Instant Readout is a passive advertisement protocol: devices
broadcast encrypted packets continuously and there is no request/response
mechanism.  We must scan to receive them.

The scan uses:
  - ``scanning_mode="passive"`` — the adapter listens without sending scan
    requests, reducing radio activity and avoiding interference.
  - A MAC address filter — BlueZ only delivers callbacks for the specific
    MACs in our config, ignoring all other BLE traffic.  This is efficient
    and eliminates the need for post-scan filtering.

Because BMS never scans and Victron scanning is isolated to its own process,
the previous cross-process lock file (_BleScanLock) is no longer needed.

BlueZ sequential GATT rationale
---------------------------------
BlueZ serialises all GATT operations through a single D-Bus socket.
Firing more than ~2 concurrent BleakClient.connect() calls produces
"Operation already in progress" errors.  BMS devices are polled strictly
one-at-a-time with a gap between each for reliability.
"""

import asyncio
import logging
from datetime import datetime
from typing import Optional

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice

from .config import AppConfig, DeviceConfig
from .models import DeviceReading
from .jbd import JBD_NAME_KEYWORDS, read_jbd_device
from .victron import (
    VICTRON_MFR_ID, VICTRON_NAME_KEYWORDS, VICTRON_RECORD_TYPES,
    read_victron_advertisement,
)

log = logging.getLogger(__name__)

# ── Timing constants ──────────────────────────────────────────────────────────

BMS_RETRIES      = 3    # total attempts per BMS device (including the first)
RETRY_DELAY      = 4.0  # seconds between retry attempts
INTER_DEVICE_GAP = 1.5  # seconds between successive BMS connections

# Error substrings that indicate a transient BlueZ failure worth retrying.
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
    "'path'",           # BlueZ D-Bus path lookup failure (device not in cache)
    "keyerror",        # same error caught at a higher level
)

# Error substrings that indicate a permanent failure — stop retrying immediately.
_PERMANENT_ERRORS: tuple[str, ...] = (
    "rejected password",
    "no compatible jbd",
    "bad password",
    "authentication",
    "no compatible",
    "not found during scan",
)


# ── Victron BLE scanner ───────────────────────────────────────────────────────

class VictronScanner:
    """
    Passive, MAC-filtered BLE scanner for Victron Instant Readout.

    Uses ``scanning_mode="passive"`` so the adapter only listens — it never
    sends scan requests.  This is sufficient for Victron devices (they
    broadcast without solicitation) and reduces radio activity.

    A MAC address filter is applied at construction time so BlueZ only
    delivers callbacks for the configured Victron devices.  All other BLE
    traffic is silently ignored at the kernel/HCI level.

    Payload accumulation
    --------------------
    Victron devices broadcast multiple record types in rotation.  All
    distinct payloads seen per MAC are accumulated so the caller can try
    each record type and use the most informative one.
    """

    def __init__(self, mac_addresses: list[str]) -> None:
        """
        Parameters
        ----------
        mac_addresses:
            Upper-cased Bluetooth MAC addresses of Victron devices to watch.
            Empty list means accept all — useful for auto-discovery.
        """
        self._macs:             set[str]             = {m.upper() for m in mac_addresses}
        self._adv:              dict[str, tuple]      = {}   # mac → (BLEDevice, adv_data)
        self._payloads:         dict[str, list[bytes]]= {}   # mac → [raw_payload, …]
        self._scanner: Optional[BleakScanner]         = None

    def _cb(self, device: BLEDevice, adv_data) -> None:
        """BleakScanner detection callback — called for every matching advertisement."""
        mac = device.address.upper()
        if self._macs and mac not in self._macs:
            return

        self._adv[mac] = (device, adv_data)

        mfr = getattr(adv_data, "manufacturer_data", {}) or {}
        raw = mfr.get(VICTRON_MFR_ID)
        if not raw:
            return

        payloads = raw if isinstance(raw, list) else [raw]
        seen_set = {bytes(p) for p in self._payloads.get(mac, [])}

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
            self._payloads.setdefault(mac, []).append(pb)
            seen_set.add(pb)

    async def scan(self, duration: float) -> None:
        """
        Listen for Victron advertisements for *duration* seconds.

        Tries passive scanning first (lower radio footprint).  Passive mode
        on Linux/BlueZ requires ``or_patterns`` telling the kernel which AD
        types to deliver; different bleak versions expect different formats.
        We try both formats before falling back to active scanning.

        Active scanning works identically for Victron — their devices
        broadcast continuously without solicitation, so the adapter receives
        the same advertisement data regardless of scan mode.

        Clears accumulated data from the previous cycle before starting.
        """
        self._adv.clear()
        self._payloads.clear()

        # Victron company ID 0x02E1, little-endian in manufacturer data header.
        _VICTRON_MFR_BYTES = bytes([
            VICTRON_MFR_ID & 0xFF,
            (VICTRON_MFR_ID >> 8) & 0xFF,
        ])

        # Build passive-mode kwargs.  bleak has used two different formats for
        # or_patterns depending on version:
        #   - Tuple format (bleak ≤ 0.20): [(start, ad_type, value_bytes), ...]
        #   - AdvertisementDataFilter (bleak ≥ 0.21): [AdvertisementDataFilter(...)]
        # We build both and try them in sequence.
        _passive_candidates: list[dict] = []

        # Format 1: AdvertisementDataFilter objects (bleak ≥ 0.21)
        try:
            from bleak.backends.bluezdbus.advertisement_monitor import (
                OrPattern as _OrPattern,
            )
            _passive_candidates.append({
                "detection_callback": self._cb,
                "scanning_mode": "passive",
                "or_patterns": [_OrPattern(0, 0xFF, _VICTRON_MFR_BYTES)],
            })
        except ImportError:
            pass

        # Format 2: raw tuple (bleak ≤ 0.20)
        _passive_candidates.append({
            "detection_callback": self._cb,
            "scanning_mode": "passive",
            "or_patterns": [(0, 0xFF, _VICTRON_MFR_BYTES)],
        })

        # Format 3: active fallback (always works)
        _active_kwargs: dict = {
            "detection_callback": self._cb,
            "scanning_mode": "active",
        }
        if self._macs:
            try:
                _active_kwargs["cb_filters"] = [{"address": m} for m in self._macs]
            except Exception:
                pass

        all_attempts = _passive_candidates + [_active_kwargs]

        last_exc = None
        for kwargs in all_attempts:
            mode = kwargs.get("scanning_mode", "active")
            try:
                self._scanner = BleakScanner(**kwargs)
                await self._scanner.start()
                if mode == "active":
                    if last_exc is not None:
                        log.warning(
                            f"VictronScanner: passive scan unavailable "
                            f"({type(last_exc).__name__}: {last_exc}) — "
                            f"using active scanning (data unaffected)"
                        )
                    else:
                        log.debug("VictronScanner: using active scanning")
                else:
                    log.debug("VictronScanner: passive scan started")
                break   # success
            except Exception as exc:
                last_exc = exc
                log.debug(f"VictronScanner: {mode} attempt failed: {exc}")
                await self._stop_scanner()
                continue
        else:
            # All attempts failed — raise the last error
            raise RuntimeError(
                f"VictronScanner: all scan modes failed. "
                f"Last error: {last_exc}"
            ) from last_exc

        try:
            await asyncio.sleep(duration)
        finally:
            await self._stop_scanner()

    async def _stop_scanner(self) -> None:
        """Stop and clear the internal scanner reference."""
        if self._scanner is not None:
            try:
                await self._scanner.stop()
            except Exception:
                pass
            self._scanner = None

    def latest_adv(self, mac: str) -> Optional[tuple]:
        """Most recent (BLEDevice, adv_data) for *mac*, or None."""
        return self._adv.get(mac.upper())

    def payloads(self, mac: str) -> list[bytes]:
        """All distinct Victron payloads accumulated for *mac* this cycle."""
        return list(self._payloads.get(mac.upper(), []))

    def seen_macs(self) -> set[str]:
        """Set of MAC addresses seen during the last scan."""
        return set(self._adv.keys())


# ── BMS direct connection ─────────────────────────────────────────────────────

async def _poll_bms(
    bms_configs: list[DeviceConfig],
) -> list[DeviceReading]:
    """
    Poll all configured BMS devices sequentially by connecting directly.

    No scanning needed — each device is addressed by its MAC address.
    BleakClient(address) asks BlueZ to connect directly, which works as long
    as the device has advertised recently enough to be in BlueZ's cache, or
    the device is currently advertising (BlueZ will discover it on-demand).

    Devices are polled one-at-a-time with INTER_DEVICE_GAP seconds between
    each to give BlueZ time to fully release GATT resources.
    """

    async def _read_one(dc: DeviceConfig) -> DeviceReading:
        address  = dc.mac or dc.ble_name or "??"
        friendly = dc.name

        if not dc.mac:
            # No MAC — cannot connect directly; return an error reading
            return DeviceReading(
                address=address, name=friendly, device_type="bms",
                timestamp=datetime.now().isoformat(timespec="seconds"),
                error=(
                    "BMS device has no MAC address configured. "
                    "Direct connection requires a MAC address."
                ),
            )

        for attempt in range(BMS_RETRIES):
            if attempt > 0:
                log.info(f"  [BMS]  {friendly}: retry {attempt}/{BMS_RETRIES - 1} ...")
                await asyncio.sleep(RETRY_DELAY)

            # Pass the MAC address string directly.  bleak on Linux/BlueZ
            # constructs the D-Bus object path from the MAC itself, so no
            # prior scan is required.  Using a synthetic BLEDevice with
            # details={} would raise KeyError('path') inside bleak's backend.
            result = await read_jbd_device(dc.mac, friendly,
                                           password=dc.password)

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

    readings: list[DeviceReading] = []
    for i, dc in enumerate(bms_configs):
        if i > 0:
            await asyncio.sleep(INTER_DEVICE_GAP)
        readings.append(await _read_one(dc))
    return readings


# ── Victron advertisement reading ─────────────────────────────────────────────

def _poll_victron(
    victron_configs: list[DeviceConfig],
    scanner: VictronScanner,
) -> list[DeviceReading]:
    """
    Build Victron DeviceReadings from the payloads accumulated by *scanner*.

    Synchronous — no BLE connections, no waiting.  All data comes from the
    advertisements received during scanner.scan().
    """
    readings: list[DeviceReading] = []

    for dc in victron_configs:
        mac = (dc.mac or "").upper()
        adv_entry = scanner.latest_adv(mac) if mac else None

        if adv_entry is None:
            log.warning(f"  [Victron] '{dc.name}' ({mac}) not seen in scan")
            readings.append(DeviceReading(
                address=mac or dc.ble_name or "unknown",
                name=dc.name,
                device_type=dc.device_type or "victron",
                timestamp=datetime.now().isoformat(timespec="seconds"),
                error="Device not seen during scan",
            ))
            continue

        dev, adv = adv_entry
        all_payloads = scanner.payloads(mac)
        log.info(
            f"  [Victron] '{dc.name}' ({mac}): "
            f"{len(all_payloads)} payload(s) accumulated"
        )
        readings.append(
            read_victron_advertisement(
                dev, adv, dc.name, dc.enc_key, all_payloads,
                device_type_override=dc.device_type,
            )
        )

    return readings


# ── Auto-discovery (optional) ─────────────────────────────────────────────────

async def discover_devices(scan_timeout: float = 10.0) -> tuple[list, list]:
    """
    Passive scan to auto-discover BMS and Victron devices.

    Used when no explicit devices are configured.  Returns lists of
    DeviceConfig-like objects that can be passed back to _poll_bms /
    VictronScanner.

    Most installations should configure devices explicitly in config.ini
    rather than relying on auto-discovery.
    """
    discovered_bms     = []
    discovered_victron = []

    def cb(device: BLEDevice, adv_data) -> None:
        name_lower = (device.name or "").lower()
        mfr = getattr(adv_data, "manufacturer_data", {}) or {}

        if any(kw in name_lower for kw in JBD_NAME_KEYWORDS):
            discovered_bms.append(device)
            log.info(f"  Auto-discovered BMS: {device.name} ({device.address})")
        elif (any(kw in name_lower for kw in VICTRON_NAME_KEYWORDS)
              or VICTRON_MFR_ID in mfr):
            discovered_victron.append(device)
            log.info(f"  Auto-discovered Victron: {device.name} ({device.address})")

    scanner = BleakScanner(detection_callback=cb, scanning_mode="passive")
    try:
        await scanner.start()
        await asyncio.sleep(scan_timeout)
    finally:
        try:
            await scanner.stop()
        except Exception:
            pass

    return discovered_bms, discovered_victron


# ── Combined poll (legacy / single-process) ───────────────────────────────────

async def poll_all(
    bms_configs: list[DeviceConfig],
    victron_configs: list[DeviceConfig],
    scan_timeout: float,
) -> tuple[list[DeviceReading], list[DeviceReading]]:
    """
    Poll all devices and return ``(bms_readings, victron_readings)``.

    Used by the legacy combined launcher (``jbd_bms_monitor.py``).
    Victron scan runs first (passive, filtered, no GATT), then BMS
    connects directly one-at-a-time.  No radio contention between the two.
    """
    # Victron: passive filtered scan
    macs = [dc.mac for dc in victron_configs if dc.mac]
    vscanner = VictronScanner(macs)
    if victron_configs:
        log.info("Scanning for Victron advertisements (passive) …")
        await vscanner.scan(scan_timeout)

    victron_readings = _poll_victron(victron_configs, vscanner)

    # BMS: direct connections, no scan
    bms_readings = await _poll_bms(bms_configs)

    return bms_readings, victron_readings
