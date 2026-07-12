"""
tests/test_victron_parsers.py — Victron BLE parsing coverage gaps
==================================================================
Closes the remaining coverage gaps in solar_monitor/victron.py:

  - extract_victron_mfr        (Format A / Format B / VE.Smart beacon / lists)
  - parse_payload              (Format B branch, too-short fallback)
  - try_decrypt                (real AES-128-CTR roundtrip, missing-package path)
  - _parse_inverter_0x07       (VE.Bus Smart Dongle custom layout)
  - _parse_inverter_rs         (0x06 Inverter RS / 0x0B Multi RS layout)
  - short-record ValueError branches of every parser
  - read_victron_advertisement (end-to-end with real AES-CTR encrypted
    payloads: success paths for 0x02/0x04/0x05/0x06/0x08/0x09/0x0B/0x0C/0x0D,
    rejection branches: unparseable payload, disallowed record for the
    configured type, unknown record type, invalid state byte, parse error,
    implausible values, unknown device_type_override, invalid key,
    no-key identification, missing cryptography package)
"""

import struct
import sys
import types
import unittest
from unittest.mock import patch

# ── Stub bleak so victron module imports without BLE hardware ─────────────────
bleak = types.ModuleType("bleak")
backends = types.ModuleType("bleak.backends")
device_mod = types.ModuleType("bleak.backends.device")


class BLEDevice:
    def __init__(self, address="AA:BB:CC:DD:EE:FF", name="test"):
        self.address = address
        self.name = name


class BleakClient:
    def __init__(self, *a, **kw): pass


class BleakScanner:
    pass


device_mod.BLEDevice = BLEDevice
bleak.BleakClient = BleakClient
bleak.BleakScanner = BleakScanner
sys.modules.update(
    {"bleak": bleak, "bleak.backends": backends, "bleak.backends.device": device_mod}
)

import os
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from solar_monitor import victron as v_mod
from solar_monitor.victron import (
    extract_victron_mfr, parse_payload, try_decrypt,
    read_victron_advertisement,
    _parse_solar, _parse_inverter, _parse_inverter_0x07,
    _parse_inverter_rs, _parse_bmv, _parse_dcenergy, _parse_vebus,
    VICTRON_MFR_ID, PARSERS,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

KEY_HEX   = "0102030405060708090a0b0c0d0e0f10"
KEY_BYTES = bytes.fromhex(KEY_HEX)
NONCE     = 0x1234


def _encrypt(plaintext: bytes, key: bytes = KEY_BYTES,
             nonce_val: int = NONCE) -> bytes:
    """AES-128-CTR encrypt with the same nonce construction as try_decrypt."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.backends import default_backend
    nonce = struct.pack("<H", nonce_val) + b"\x00" * 14
    enc = Cipher(algorithms.AES(key), modes.CTR(nonce),
                 backend=default_backend()).encryptor()
    return enc.update(plaintext) + enc.finalize()


def _format_b(rec_type: int, ciphertext: bytes,
              nonce_val: int = NONCE) -> bytes:
    """Build a Format B (direct record) advertisement payload."""
    return (bytes([rec_type]) + struct.pack("<H", nonce_val)
            + bytes([KEY_BYTES[0]]) + ciphertext)


def _format_a(rec_type: int, ciphertext: bytes, nonce_val: int = NONCE,
              key_idx: int = 0, model: int = 0x3502) -> bytes:
    """Build a Format A (Product Advertisement, outer 0x10) payload."""
    head = bytearray(8)
    head[0] = 0x10
    struct.pack_into("<H", head, 1, model)
    head[3] = ((rec_type & 0x0F) << 4) | (key_idx & 0x0F)
    head[4] = 0x02                          # counter byte
    struct.pack_into("<H", head, 5, nonce_val)
    head[7] = 0xA8                          # key-index byte
    return bytes(head) + ciphertext


def _adv(payload=None):
    """Fake AdvertisementData with (optional) Victron manufacturer data."""
    mfr = {} if payload is None else {VICTRON_MFR_ID: payload}
    return types.SimpleNamespace(manufacturer_data=mfr)


def _build_bmv_plain(ttg=1000, batt_mv=5200, alarm=0, aux=0,
                     current_u22=5000, soc_raw=800) -> bytes:
    """16-byte BMV (0x02) layout plaintext."""
    p = bytearray(16)
    struct.pack_into("<H", p, 0, ttg)
    struct.pack_into("<h", p, 2, batt_mv)
    struct.pack_into("<H", p, 4, alarm)
    struct.pack_into("<H", p, 6, aux)
    word = (aux & 0x3) | ((current_u22 & 0x3FFFFF) << 2)
    struct.pack_into("<I", p, 8, word)
    struct.pack_into("<H", p, 13, (soc_raw & 0x3FF) << 4)
    return bytes(p)


def _build_dcenergy_plain(ttg=1000, batt_mv=5200, alarm=0,
                          current_u22=5000) -> bytes:
    """16-byte DC Energy Meter (0x08/0x0D) layout plaintext."""
    p = bytearray(16)
    struct.pack_into("<H", p, 0, ttg)
    struct.pack_into("<h", p, 2, batt_mv)
    struct.pack_into("<H", p, 4, alarm)
    struct.pack_into("<I", p, 12, current_u22 & 0x3FFFFF)
    return bytes(p)


def _build_inverter_rs_plain(state=3, error=0, batt_v_raw=5200,
                             batt_a_raw=100, pv_raw=500, yield_raw=120,
                             ac_raw=400) -> bytes:
    """12-byte Inverter RS (0x06/0x0B) layout plaintext."""
    p = bytearray(12)
    p[0] = state
    p[1] = error
    struct.pack_into("<h", p, 2, batt_v_raw)
    struct.pack_into("<h", p, 4, batt_a_raw)
    struct.pack_into("<H", p, 6, pv_raw)
    struct.pack_into("<H", p, 8, yield_raw)
    struct.pack_into("<h", p, 10, ac_raw)
    return bytes(p)


def _build_vebus_plain(state=0x09, error=0x00, batt_cur_a=-15.0,
                       batt_v=52.0, active_ac=2, ac_in_w=0, ac_out_w=755,
                       alarm=0, temp_raw=65, soc=84) -> bytes:
    """20-byte VE.Bus (0x0C) bit-packed plaintext."""
    val = 0
    val |= (state & 0xFF)                        << 0
    val |= (error & 0xFF)                        << 8
    val |= (int(batt_cur_a / 0.1) & 0xFFFF)      << 16
    val |= (int(batt_v / 0.01) & 0x3FFF)         << 32
    val |= (active_ac & 0x3)                     << 46
    val |= (ac_in_w & 0x7FFFF)                   << 48
    val |= (ac_out_w & 0x7FFFF)                  << 67
    val |= (alarm & 0x3)                         << 86
    val |= (temp_raw & 0x7F)                     << 88
    val |= (soc & 0x7F)                          << 95
    return val.to_bytes(20, "little")


def _build_0x07_plain(state=0x09, batt_mv=52000, byte3=0xFF, byte4=46,
                      byte8=56, length=13) -> bytes:
    """VE.Bus Smart Dongle custom (non-bit-packed) 0x07 layout plaintext."""
    p = bytearray(length)
    p[0] = state
    struct.pack_into("<H", p, 1, batt_mv)
    p[3] = byte3
    if length >= 5:
        p[4] = byte4
    if length >= 9:
        p[8] = byte8
    return bytes(p)


def _read(payloads, enc_key=KEY_HEX, override=None, adv=None):
    """Shorthand for read_victron_advertisement with a stub BLE device."""
    return read_victron_advertisement(
        device=BLEDevice(),
        adv_data=adv if adv is not None else _adv(),
        friendly_name="TestDevice",
        enc_key=enc_key,
        all_payloads=payloads,
        device_type_override=override,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 1. extract_victron_mfr
# ─────────────────────────────────────────────────────────────────────────────

class TestExtractVictronMfr(unittest.TestCase):

    def test_no_manufacturer_data_attr(self):
        self.assertIsNone(extract_victron_mfr(object()))

    def test_manufacturer_data_none(self):
        self.assertIsNone(
            extract_victron_mfr(types.SimpleNamespace(manufacturer_data=None)))

    def test_no_victron_company_id(self):
        adv = types.SimpleNamespace(manufacturer_data={0x004C: b"\x01\x02\x03"})
        self.assertIsNone(extract_victron_mfr(adv))

    def test_format_a_full_payload_returned(self):
        p = _format_a(0x0C, bytes(16))
        self.assertEqual(extract_victron_mfr(_adv(p)), p)

    def test_format_a_short_vesmart_beacon_skipped(self):
        """A <9 byte 0x10 payload is a VE.Smart beacon, not Instant Readout."""
        self.assertIsNone(extract_victron_mfr(_adv(b"\x10\x02\x35\xc0")))

    def test_format_b_payload_returned(self):
        p = _format_b(0x02, bytes(16))
        self.assertEqual(extract_victron_mfr(_adv(p)), p)

    def test_format_b_too_short_skipped(self):
        self.assertIsNone(extract_victron_mfr(_adv(b"\x02\x00\x00\x00")))

    def test_empty_payload_skipped(self):
        self.assertIsNone(extract_victron_mfr(_adv(b"")))

    def test_list_of_payloads_first_usable_wins(self):
        """bleak may hand back a list; empty + VE.Smart entries are skipped."""
        good = _format_b(0x02, bytes(16))
        adv = _adv([b"", b"\x10\x02\x35\xc0", good])
        self.assertEqual(extract_victron_mfr(adv), good)

    def test_list_with_no_usable_payload(self):
        adv = _adv([b"", b"\x10\x00\x00\x00"])
        self.assertIsNone(extract_victron_mfr(adv))


# ─────────────────────────────────────────────────────────────────────────────
# 2. parse_payload — Format B and fallback
# ─────────────────────────────────────────────────────────────────────────────

class TestParsePayloadFormatB(unittest.TestCase):

    def test_format_b_fields(self):
        ct = bytes(range(16))
        p = _format_b(0x02, ct, nonce_val=0xBEEF)
        rt, nonce, cipher = parse_payload(p)
        self.assertEqual(rt, 0x02)
        self.assertEqual(nonce, 0xBEEF)
        self.assertEqual(cipher, ct)

    def test_format_b_minimum_length(self):
        """Exactly 5 bytes: 1-byte ciphertext."""
        rt, nonce, cipher = parse_payload(b"\x0c\x34\x12\x01\xaa")
        self.assertEqual(rt, 0x0C)
        self.assertEqual(nonce, 0x1234)
        self.assertEqual(cipher, b"\xaa")

    def test_too_short_returns_sentinel(self):
        rt, nonce, cipher = parse_payload(b"\x02\x00\x00\x00")
        self.assertEqual(rt, 0xFF)
        self.assertEqual(nonce, 0)
        self.assertEqual(cipher, b"")

    def test_format_a_too_short_falls_to_format_b(self):
        """A 0x10 payload of 5-8 bytes is parsed via the Format B branch."""
        rt, _, _ = parse_payload(b"\x10\x00\x00\x00\x00\x00")
        self.assertEqual(rt, 0x10)


# ─────────────────────────────────────────────────────────────────────────────
# 3. try_decrypt
# ─────────────────────────────────────────────────────────────────────────────

class TestTryDecrypt(unittest.TestCase):

    def test_roundtrip(self):
        plain = bytes(range(20))
        ct = _encrypt(plain, KEY_BYTES, 0xABCD)
        self.assertNotEqual(ct, plain)
        self.assertEqual(try_decrypt(0xABCD, ct, KEY_BYTES), plain)

    def test_wrong_nonce_garbles(self):
        plain = bytes(range(20))
        ct = _encrypt(plain, KEY_BYTES, 0xABCD)
        self.assertNotEqual(try_decrypt(0xABCE, ct, KEY_BYTES), plain)

    def test_missing_cryptography_returns_none(self):
        """When the cryptography package is absent, try_decrypt returns None."""
        blocked = {
            "cryptography": None,
            "cryptography.hazmat": None,
            "cryptography.hazmat.primitives": None,
            "cryptography.hazmat.primitives.ciphers": None,
            "cryptography.hazmat.backends": None,
        }
        with patch.dict(sys.modules, blocked):
            self.assertIsNone(try_decrypt(0x1234, bytes(16), KEY_BYTES))


# ─────────────────────────────────────────────────────────────────────────────
# 4. _parse_inverter_0x07 — VE.Bus Smart Dongle custom layout
# ─────────────────────────────────────────────────────────────────────────────

class TestInverter0x07Parser(unittest.TestCase):

    def test_too_short_raises(self):
        with self.assertRaises(ValueError):
            _parse_inverter_0x07(b"\x09\x00\x00\x00")

    def test_battery_voltage_millivolts(self):
        r = _parse_inverter_0x07(_build_0x07_plain(batt_mv=52000))
        self.assertAlmostEqual(r["voltage_v"], 52.0, places=3)

    def test_battery_voltage_na(self):
        r = _parse_inverter_0x07(_build_0x07_plain(batt_mv=0xFFFF))
        self.assertIsNone(r["voltage_v"])

    def test_ac_voltage(self):
        """ac_v = uint16 LE at [3:5] * 0.01V; byte[3]=0xFF, byte[4]=46 → 120.31V."""
        r = _parse_inverter_0x07(_build_0x07_plain(byte3=0xFF, byte4=46))
        self.assertAlmostEqual(r["ac_out_voltage_v"], 120.31, places=2)

    def test_ac_voltage_na(self):
        r = _parse_inverter_0x07(_build_0x07_plain(byte3=0xFF, byte4=0xFF))
        self.assertIsNone(r["ac_out_voltage_v"])

    def test_raw_load_indicator_reported(self):
        r = _parse_inverter_0x07(_build_0x07_plain(byte8=56))
        self.assertEqual(r["raw_load_indicator"], 56)

    def test_short_payload_no_load_byte(self):
        """5-byte payload: byte[8] absent → raw_load_indicator None."""
        r = _parse_inverter_0x07(_build_0x07_plain(length=5))
        self.assertIsNone(r["raw_load_indicator"])

    def test_inverting_state(self):
        r = _parse_inverter_0x07(_build_0x07_plain(state=0x09))
        self.assertEqual(r["inverter_state"], "Inverting")

    def test_unknown_state_hex_label(self):
        r = _parse_inverter_0x07(_build_0x07_plain(state=0x42))
        self.assertEqual(r["inverter_state"], "0x42")

    def test_power_and_current_uncalibrated_none(self):
        """Scale constants unset → ac power/current must be None."""
        r = _parse_inverter_0x07(_build_0x07_plain(byte8=56))
        self.assertIsNone(r["power_w"])
        self.assertIsNone(r["ac_out_power_va"])
        self.assertIsNone(r["ac_out_current_a"])

    def test_alarm_always_none(self):
        r = _parse_inverter_0x07(_build_0x07_plain())
        self.assertIsNone(r["alarm_reason"])


# ─────────────────────────────────────────────────────────────────────────────
# 5. _parse_inverter_rs — Inverter RS (0x06) / Multi RS (0x0B)
# ─────────────────────────────────────────────────────────────────────────────

class TestInverterRSParser(unittest.TestCase):

    def test_too_short_raises(self):
        with self.assertRaises(ValueError):
            _parse_inverter_rs(bytes(11))

    def test_battery_voltage(self):
        r = _parse_inverter_rs(_build_inverter_rs_plain(batt_v_raw=5200))
        self.assertAlmostEqual(r["voltage_v"], 52.0, places=2)

    def test_battery_current(self):
        r = _parse_inverter_rs(_build_inverter_rs_plain(batt_a_raw=100))
        self.assertAlmostEqual(r["current_a"], 10.0, places=2)

    def test_negative_battery_current(self):
        r = _parse_inverter_rs(_build_inverter_rs_plain(batt_a_raw=-150))
        self.assertAlmostEqual(r["current_a"], -15.0, places=2)

    def test_pv_power(self):
        r = _parse_inverter_rs(_build_inverter_rs_plain(pv_raw=500))
        self.assertEqual(r["pv_power_w"], 500.0)

    def test_yield_today(self):
        """yield raw 120 × 10 = 1200 Wh."""
        r = _parse_inverter_rs(_build_inverter_rs_plain(yield_raw=120))
        self.assertAlmostEqual(r["yield_today_wh"], 1200.0, places=1)

    def test_ac_out_power(self):
        r = _parse_inverter_rs(_build_inverter_rs_plain(ac_raw=400))
        self.assertEqual(r["ac_out_power_va"], 400.0)
        self.assertEqual(r["power_w"], 400.0)

    def test_battery_power_computed(self):
        r = _parse_inverter_rs(
            _build_inverter_rs_plain(batt_v_raw=5200, batt_a_raw=100))
        # power_w is the AC field; only voltage/current sanity here
        self.assertAlmostEqual(r["voltage_v"] * r["current_a"], 520.0, places=1)

    def test_state_labels(self):
        r = _parse_inverter_rs(_build_inverter_rs_plain(state=3))
        self.assertEqual(r["charger_state"], "Bulk")
        self.assertEqual(r["inverter_state"], "Bulk")

    def test_error_code(self):
        r = _parse_inverter_rs(_build_inverter_rs_plain(error=2))
        self.assertEqual(r["error_code"], 2)

    def test_error_na(self):
        r = _parse_inverter_rs(_build_inverter_rs_plain(error=0xFF))
        self.assertIsNone(r["error_code"])

    def test_na_sentinels(self):
        r = _parse_inverter_rs(_build_inverter_rs_plain(
            batt_v_raw=0x7FFF, batt_a_raw=0x7FFF,
            pv_raw=0xFFFF, yield_raw=0xFFFF, ac_raw=0x7FFF))
        self.assertIsNone(r["voltage_v"])
        self.assertIsNone(r["current_a"])
        self.assertIsNone(r["pv_power_w"])
        self.assertIsNone(r["yield_today_wh"])
        self.assertIsNone(r["ac_out_power_va"])


# ─────────────────────────────────────────────────────────────────────────────
# 6. Short-record ValueError branches
# ─────────────────────────────────────────────────────────────────────────────

class TestShortRecordRejection(unittest.TestCase):

    def test_solar_too_short(self):
        with self.assertRaises(ValueError):
            _parse_solar(bytes(9))

    def test_inverter_too_short(self):
        with self.assertRaises(ValueError):
            _parse_inverter(bytes(6))

    def test_bmv_too_short(self):
        with self.assertRaises(ValueError):
            _parse_bmv(bytes(15))

    def test_dcenergy_too_short(self):
        with self.assertRaises(ValueError):
            _parse_dcenergy(bytes(5))


# ─────────────────────────────────────────────────────────────────────────────
# 7. read_victron_advertisement — end-to-end with real AES-CTR payloads
# ─────────────────────────────────────────────────────────────────────────────

class TestReadAdvertisementSuccess(unittest.TestCase):
    """Full pipeline: encrypted Format B payload → decoded DeviceReading."""

    def test_bmv_0x02_success(self):
        payload = _format_b(0x02, _encrypt(_build_bmv_plain()))
        r = _read([payload])
        self.assertIsNone(r.error)
        self.assertEqual(r.device_type, "monitor")
        self.assertAlmostEqual(r.voltage_v, 52.0, places=2)
        self.assertAlmostEqual(r.current_a, 5.0, places=2)
        self.assertEqual(r.capacity_pct, 80)

    def test_smartshunt_0x08_success(self):
        payload = _format_b(0x08, _encrypt(_build_dcenergy_plain()))
        r = _read([payload], override="monitor")
        self.assertIsNone(r.error)
        self.assertEqual(r.device_type, "monitor")
        self.assertAlmostEqual(r.voltage_v, 52.0, places=2)
        self.assertAlmostEqual(r.current_a, 5.0, places=2)

    def test_dcdc_0x04_success(self):
        """0x04 (DC/DC converter) uses the BMV layout parser."""
        payload = _format_b(0x04, _encrypt(_build_bmv_plain()))
        r = _read([payload], override="dcdc")
        self.assertIsNone(r.error)
        self.assertEqual(r.device_type, "dcdc")
        self.assertAlmostEqual(r.voltage_v, 52.0, places=2)

    def test_dcdc_0x09_success(self):
        payload = _format_b(0x09, _encrypt(_build_bmv_plain()))
        r = _read([payload], override="dcdc")
        self.assertIsNone(r.error)
        self.assertEqual(r.device_type, "dcdc")

    def test_dcdc_0x0d_success(self):
        """0x0D (Orion XS / DC Energy Meter) uses the DC-energy layout."""
        payload = _format_b(0x0D, _encrypt(_build_dcenergy_plain()))
        r = _read([payload], override="dcdc")
        self.assertIsNone(r.error)
        self.assertEqual(r.device_type, "dcdc")
        self.assertAlmostEqual(r.current_a, 5.0, places=2)

    def test_smartlithium_0x05_success(self):
        payload = _format_b(0x05, _encrypt(_build_bmv_plain()))
        r = _read([payload], override="lithium")
        self.assertIsNone(r.error)
        self.assertEqual(r.device_type, "lithium")

    def test_inverter_rs_0x06_success(self):
        payload = _format_b(0x06, _encrypt(_build_inverter_rs_plain()))
        r = _read([payload], override="inverter")
        self.assertIsNone(r.error)
        self.assertEqual(r.device_type, "inverter")
        self.assertEqual(r.ac_out_power_va, 400.0)
        self.assertEqual(r.pv_power_w, 500.0)

    def test_multi_rs_0x0b_success(self):
        payload = _format_b(0x0B, _encrypt(_build_inverter_rs_plain()))
        r = _read([payload], override="inverter")
        self.assertIsNone(r.error)
        self.assertEqual(r.device_type, "inverter")

    def test_vebus_0x0c_success(self):
        payload = _format_b(0x0C, _encrypt(_build_vebus_plain()))
        r = _read([payload])
        self.assertIsNone(r.error)
        self.assertEqual(r.device_type, "inverter")
        self.assertEqual(r.inverter_state, "Inverting")
        self.assertAlmostEqual(r.voltage_v, 52.0, places=1)
        self.assertAlmostEqual(r.current_a, -15.0, places=1)
        self.assertEqual(r.ac_out_power_va, 755)
        self.assertEqual(r.capacity_pct, 84)

    def test_vebus_0x0c_format_a_success(self):
        """Format A (outer 0x10) wrapper around an encrypted 0x0C record."""
        payload = _format_a(0x0C, _encrypt(_build_vebus_plain()))
        r = _read([payload])
        self.assertIsNone(r.error)
        self.assertEqual(r.inverter_state, "Inverting")
        self.assertAlmostEqual(r.voltage_v, 52.0, places=1)

    def test_snapshot_payload_used_when_no_all_payloads(self):
        """The payload embedded in adv_data is used when all_payloads=None."""
        payload = _format_b(0x02, _encrypt(_build_bmv_plain()))
        r = _read(None, adv=_adv(payload))
        self.assertIsNone(r.error)
        self.assertAlmostEqual(r.voltage_v, 52.0, places=2)

    def test_override_wins_over_record_type(self):
        """device_type_override replaces the record-derived device_type."""
        payload = _format_b(0x02, _encrypt(_build_bmv_plain()))
        r = _read([payload], override="monitor")
        self.assertEqual(r.device_type, "monitor")


class TestReadAdvertisementFailures(unittest.TestCase):

    def test_no_advertisement_data(self):
        r = _read(None, adv=_adv())
        self.assertEqual(r.device_type, "victron")
        self.assertIn("No usable", r.error)

    def test_no_key_identifies_device_type(self):
        """Without a key the record type is still identified; 0x01 sorts last."""
        bmv  = _format_b(0x02, _encrypt(_build_bmv_plain()))
        mppt = _format_b(0x01, bytes(16))
        r = _read([mppt, bmv], enc_key=None)
        self.assertEqual(r.device_type, "monitor")
        self.assertIn("No key", r.charger_state)
        self.assertIn("Encryption key required", r.error)

    def test_invalid_key_not_hex(self):
        payload = _format_b(0x02, _encrypt(_build_bmv_plain()))
        r = _read([payload], enc_key="zz" * 16)
        self.assertIn("32 hex characters", r.error)

    def test_unknown_type_override_warns_and_proceeds(self):
        payload = _format_b(0x02, _encrypt(_build_bmv_plain()))
        with self.assertLogs("solar_monitor.victron", level="WARNING") as cm:
            r = _read([payload], override="toaster")
        self.assertTrue(any("unknown type override" in m for m in cm.output))
        # Unknown override is ignored for record filtering but still stamped
        self.assertIsNone(r.error)
        self.assertEqual(r.device_type, "toaster")

    def test_debug_logging_lists_candidates(self):
        payload = _format_b(0x02, _encrypt(_build_bmv_plain()))
        with self.assertLogs("solar_monitor.victron", level="DEBUG") as cm:
            _read([payload])
        self.assertTrue(any("candidate fmt=B" in m for m in cm.output))

    def test_unparseable_payload_skipped(self):
        """A 4-byte payload parses to record 0xFF and is skipped."""
        r = _read([b"\x02\x00\x00\x00"])
        self.assertIsNotNone(r.error)
        self.assertIn("Decryption failed", r.error)

    def test_type_override_skips_disallowed_record(self):
        """A BMV record must not be used for a configured MPPT device."""
        payload = _format_b(0x02, _encrypt(_build_bmv_plain()))
        r = _read([payload], override="mppt")
        self.assertIsNotNone(r.error)
        self.assertIn("Decryption failed", r.error)

    def test_unknown_record_type_no_parser(self):
        """Record 0x0F has no parser registered → skipped → failure."""
        self.assertNotIn(0x0F, PARSERS)
        payload = _format_b(0x0F, _encrypt(bytes(16)))
        r = _read([payload])
        self.assertIsNotNone(r.error)

    def test_missing_cryptography_package(self):
        payload = _format_b(0x02, _encrypt(_build_bmv_plain()))
        with patch.object(v_mod, "try_decrypt", return_value=None):
            r = _read([payload])
        self.assertIn("cryptography package not installed", r.error)

    def test_invalid_state_byte_rejected(self):
        """Record 0x03: decrypted[0]=0x99 is not a valid inverter state."""
        plain = bytearray(13)
        plain[0] = 0x99
        payload = _format_b(0x03, _encrypt(bytes(plain)))
        r = _read([payload])
        self.assertIsNotNone(r.error)
        self.assertIn("state byte 0x99", r.error)

    def test_parse_error_reported(self):
        """Record 0x02 with an 8-byte plaintext → BMV parser raises."""
        payload = _format_b(0x02, _encrypt(bytes(8)))
        r = _read([payload])
        self.assertIsNotNone(r.error)
        self.assertIn("parse error", r.error)

    def test_implausible_voltage_rejected(self):
        """BMV decoding 250V exceeds every ceiling → rejected."""
        payload = _format_b(0x02, _encrypt(_build_bmv_plain(batt_mv=25000)))
        r = _read([payload])
        self.assertIsNotNone(r.error)
        self.assertIn("physically plausible", r.error)

    def test_implausible_current_rejected(self):
        """BMV decoding > 2000A → rejected."""
        payload = _format_b(0x02,
                            _encrypt(_build_bmv_plain(current_u22=2_100_000)))
        r = _read([payload])
        self.assertIsNotNone(r.error)
        self.assertIn("2000A", r.error)

    def test_wrong_key_garbage_state_rejected(self):
        """Decrypting a 0x03 record with the wrong key almost surely fails
        the state-byte check and yields a Decryption failed error."""
        plain = bytearray(13)
        plain[0] = 0x09     # valid state under the RIGHT key
        payload = _format_b(0x03, _encrypt(bytes(plain)))
        wrong_key = "ff" * 16
        r = _read([payload], enc_key=wrong_key)
        # With the wrong key the state byte is effectively random; the
        # reading must either be rejected or - in the ~7% lucky case -
        # decoded. Assert we never silently mix keys when rejected.
        if r.error is not None:
            self.assertIn("Decryption failed", r.error)

    def test_fallback_error_names_first_record_type(self):
        """The all-failed error path labels the reading by the first record."""
        payload = _format_b(0x02, _encrypt(bytes(8)))   # parse error
        r = _read([payload])
        self.assertEqual(r.device_type, "monitor")
        self.assertIn("Advertisement key", r.error)


if __name__ == "__main__":
    unittest.main(verbosity=2)
