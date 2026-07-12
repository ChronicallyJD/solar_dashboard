"""
tests/test_solar_monitor.py — Solar Monitor unit test suite
============================================================
Covers:
  - JBD BMS protocol (checksum, parsing, fault decoding, derived fields)
  - Victron BLE protocol (all record types, bit layouts, NA sentinels)
  - DeviceReading model (field population, setattr mapping)
  - Dashboard rendering (BMS card, Victron cards, aggregates)
  - Config parsing
"""

import struct
import sys
import types
import unittest

# ── Stub bleak so victron/jbd modules import without BLE hardware ─────────────
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

from solar_monitor import jbd as jbd_mod
from solar_monitor import models as m_mod
from solar_monitor import dashboard as dash_mod
from solar_monitor.dashboard import (
    render_mppt_aggregate_card, render_inverter_aggregate_card,
    render_battery_aggregate_card, render_bms_card, render_victron_card,
    build_html,
)
from solar_monitor import victron as v_mod
from solar_monitor.jbd import _checksum, _verify_checksum, _parse_basic_info
from solar_monitor.models import DeviceReading
from solar_monitor.victron import (
    _parse_vebus, _parse_solar, _parse_inverter, _parse_bmv,
    _parse_dcenergy, _parse_inverter_rs, PARSERS,
    _INVERTER_STATES, _VALID_STATES, _RECORDS_WITH_STATE,
    read_victron_advertisement, parse_payload,
)

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

# Real packet captured from Batt2 (Vatrer 48V 100Ah, 16-cell LiFePO4)
LIVE_BASIC_INFO = bytes.fromhex(
    "dd03002615380000276327100008337a00000000000062640310"
    "030b920b850b86000000277427630000fa5877"
)

def _build_vebus_payload(**kwargs) -> bytes:
    """Pack a synthetic VE.Bus decrypted payload from field values."""
    state       = kwargs.get("state", 0x09)
    error       = kwargs.get("error", 0x00)
    batt_cur    = int(kwargs.get("batt_cur_a", 0.0) / 0.1)   # → int16 raw
    batt_v      = int(kwargs.get("batt_v", 52.0) / 0.01)     # → uint14 raw
    active_ac   = kwargs.get("active_ac", 2)
    ac_in_w     = kwargs.get("ac_in_w", 0)
    ac_out_w    = kwargs.get("ac_out_w", 0)
    alarm       = kwargs.get("alarm", 0)
    temp_raw    = kwargs.get("temp_raw", 65)   # 25°C + 40
    soc         = kwargs.get("soc", 0x7F)      # NA by default

    val = 0
    val |= (state & 0xFF)                   <<  0
    val |= (error & 0xFF)                   <<  8
    val |= (batt_cur & 0xFFFF)              << 16
    val |= (batt_v & 0x3FFF)               << 32
    val |= (active_ac & 0x3)               << 46
    val |= (ac_in_w & 0x7FFFF)             << 48
    val |= (ac_out_w & 0x7FFFF)            << 67
    val |= (alarm & 0x3)                   << 86
    val |= (temp_raw & 0x7F)               << 88
    val |= (soc & 0x7F)                    << 95
    return val.to_bytes(20, "little")


# ─────────────────────────────────────────────────────────────────────────────
# 1. JBD checksum
# ─────────────────────────────────────────────────────────────────────────────

class TestJBDChecksum(unittest.TestCase):

    def test_live_packet_verifies(self):
        """The checksum of the real captured packet must pass."""
        self.assertTrue(_verify_checksum(LIVE_BASIC_INFO))

    def test_checksum_covers_len_plus_payload_only(self):
        """Checksum must NOT include register or status bytes."""
        n    = LIVE_BASIC_INFO[3]
        body = LIVE_BASIC_INFO[3:4 + n]           # len byte + payload
        chk  = _checksum(body)
        expected_hi = LIVE_BASIC_INFO[4 + n]
        expected_lo = LIVE_BASIC_INFO[4 + n + 1]
        self.assertEqual(chk[0], expected_hi)
        self.assertEqual(chk[1], expected_lo)

    def test_single_byte_corruption_fails(self):
        """Flipping one payload byte must fail verification."""
        bad = bytearray(LIVE_BASIC_INFO)
        bad[5] ^= 0xFF
        self.assertFalse(_verify_checksum(bytes(bad)))

    def test_too_short_fails(self):
        self.assertFalse(_verify_checksum(b"\xDD\x03\x00\x01\x00"))

    def test_roundtrip(self):
        """_checksum(len+payload) should reproduce the bytes in the packet."""
        n    = LIVE_BASIC_INFO[3]
        body = LIVE_BASIC_INFO[3:4 + n]
        chk  = _checksum(body)
        self.assertEqual(len(chk), 2)
        chk_val = (chk[0] << 8) | chk[1]
        self.assertEqual((sum(body) + chk_val) & 0xFFFF, 0)


# ─────────────────────────────────────────────────────────────────────────────
# 2. JBD basic-info parsing
# ─────────────────────────────────────────────────────────────────────────────

class TestJBDParseBasicInfo(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.r = _parse_basic_info(LIVE_BASIC_INFO)

    # Core electrical
    def test_voltage(self):
        self.assertAlmostEqual(self.r["voltage_v"], 54.32, places=2)

    def test_current_zero(self):
        self.assertAlmostEqual(self.r["current_a"], 0.0, places=3)

    def test_power_zero(self):
        self.assertAlmostEqual(self.r["power_w"], 0.0, places=1)

    # Capacity
    def test_remain_ah(self):
        self.assertAlmostEqual(self.r["remain_ah"], 100.83, places=1)

    def test_nominal_ah(self):
        self.assertAlmostEqual(self.r["nominal_ah"], 100.0, places=1)

    def test_remain_wh(self):
        expected = round(100.83 * 54.32, 1)
        self.assertAlmostEqual(self.r["remain_wh"], expected, places=0)

    def test_nominal_wh(self):
        expected = round(100.0 * 54.32, 1)
        self.assertAlmostEqual(self.r["nominal_wh"], expected, places=0)

    # SoC
    def test_soc(self):
        self.assertEqual(self.r["capacity_pct"], 100)

    # Runtime estimates — not available when current ≈ 0
    def test_tte_none_when_idle(self):
        self.assertIsNone(self.r["time_to_empty_h"])

    def test_ttf_none_when_idle(self):
        self.assertIsNone(self.r["time_to_full_h"])

    # Pack info
    def test_cycle_count(self):
        self.assertEqual(self.r["cycle_count"], 8)

    def test_cell_count(self):
        self.assertEqual(self.r["cell_count"], 16)

    def test_production_date(self):
        self.assertEqual(self.r["production_date"], "2025-11-26")

    def test_sw_version(self):
        self.assertEqual(self.r["sw_version"], "6.2")

    # FET status (0x03 = both enabled)
    def test_charge_fet_enabled(self):
        self.assertTrue(self.r["charge_fet"])

    def test_discharge_fet_enabled(self):
        self.assertTrue(self.r["discharge_fet"])

    # Protection — all clear
    def test_protection_bits_zero(self):
        self.assertEqual(self.r["protection_bits"], 0x0000)

    def test_faults_empty(self):
        self.assertEqual(self.r["faults"], [])

    # Balance — none active
    def test_balance_cells_length(self):
        self.assertEqual(len(self.r["balance_cells"]), 16)

    def test_balance_cells_inactive(self):
        self.assertTrue(all(b == 0 for b in self.r["balance_cells"]))

    # Temperatures
    def test_temperature_count(self):
        self.assertEqual(len(self.r["temp_c"]), 3)

    def test_temperatures_plausible(self):
        for t in self.r["temp_c"]:
            self.assertGreater(t, 0)
            self.assertLess(t, 60)

    def test_temperature_values(self):
        self.assertAlmostEqual(self.r["temp_c"][0], 23.1, places=1)
        self.assertAlmostEqual(self.r["temp_c"][1], 21.8, places=1)
        self.assertAlmostEqual(self.r["temp_c"][2], 21.9, places=1)


class TestJBDDerivedFields(unittest.TestCase):
    """Test TTE/TTF and fault decoding with synthetic payloads."""

    def _build_packet(self, voltage_raw, current_raw, remain_raw, nominal_raw,
                      protection=0, fet=0x03, soc=80, ntc_temps=None):
        """Build a minimal valid 0x03 basic-info packet."""
        ntc_temps = ntc_temps or [2981, 2981]  # 25°C each
        n_ntc = len(ntc_temps)
        payload = bytearray()
        payload += struct.pack(">H", voltage_raw)     # [0:2] voltage
        payload += struct.pack(">h", current_raw)     # [2:4] current (signed)
        payload += struct.pack(">H", remain_raw)      # [4:6] remain
        payload += struct.pack(">H", nominal_raw)     # [6:8] nominal
        payload += struct.pack(">H", 5)               # [8:10] cycles
        payload += struct.pack(">H", 0b0_1001_01011_00110)  # [10:12] date 2025-11-06
        payload += struct.pack(">H", 0)               # [12:14] bal_lo
        payload += struct.pack(">H", 0)               # [14:16] bal_hi
        payload += struct.pack(">H", protection)      # [16:18] prot
        payload += bytes([0x30])                      # [18] sw ver 3.0
        payload += bytes([soc])                       # [19] soc
        payload += bytes([fet])                       # [20] fet
        payload += bytes([4])                         # [21] n_cells
        payload += bytes([n_ntc])                     # [22] n_ntc
        for t in ntc_temps:
            payload += struct.pack(">H", t)

        n = len(payload)
        body = bytes([n]) + bytes(payload)
        chk = _checksum(body)
        return bytes([0xDD, 0x03, 0x00, n]) + bytes(payload) + chk + bytes([0x77])

    def test_tte_when_discharging(self):
        """TTE = remain / |current|. 50Ah at 10A = 5.0h."""
        pkt = self._build_packet(5000, -1000, 5000, 10000, soc=50)
        r   = _parse_basic_info(pkt)
        # remain_ah = 5000 * 10 / 1000 = 50Ah; current = -10.0A
        self.assertIsNotNone(r["time_to_empty_h"])
        self.assertAlmostEqual(r["time_to_empty_h"], 5.0, places=1)

    def test_ttf_when_charging(self):
        """TTF = (nominal - remain) / current. 20Ah left to fill at 4A = 5.0h."""
        pkt = self._build_packet(5400, 400, 8000, 10000, soc=80)
        r   = _parse_basic_info(pkt)
        # nominal=100Ah, remain=80Ah, gap=20Ah, current=4A
        self.assertIsNotNone(r["time_to_full_h"])
        self.assertAlmostEqual(r["time_to_full_h"], 5.0, places=1)

    def test_tte_none_when_charging(self):
        pkt = self._build_packet(5400, 400, 8000, 10000, soc=80)
        r   = _parse_basic_info(pkt)
        self.assertIsNone(r["time_to_empty_h"])

    def test_ttf_none_when_discharging(self):
        pkt = self._build_packet(5000, -1000, 5000, 10000, soc=50)
        r   = _parse_basic_info(pkt)
        self.assertIsNone(r["time_to_full_h"])

    def test_fault_cell_overvoltage(self):
        pkt = self._build_packet(5000, 0, 5000, 10000, protection=0x0001)
        r   = _parse_basic_info(pkt)
        self.assertIn("Cell overvoltage", r["faults"])

    def test_fault_discharge_overcurrent(self):
        pkt = self._build_packet(5000, 0, 5000, 10000, protection=0x0200)
        r   = _parse_basic_info(pkt)
        self.assertIn("Discharge overcurrent", r["faults"])

    def test_fault_multiple(self):
        pkt = self._build_packet(5000, 0, 5000, 10000, protection=0x0201)
        r   = _parse_basic_info(pkt)
        self.assertIn("Cell overvoltage",       r["faults"])
        self.assertIn("Discharge overcurrent",  r["faults"])

    def test_no_faults(self):
        pkt = self._build_packet(5000, 0, 5000, 10000, protection=0x0000)
        r   = _parse_basic_info(pkt)
        self.assertEqual(r["faults"], [])

    def test_charge_fet_disabled(self):
        pkt = self._build_packet(5000, 0, 5000, 10000, fet=0x02)
        r   = _parse_basic_info(pkt)
        self.assertFalse(r["charge_fet"])
        self.assertTrue(r["discharge_fet"])

    def test_both_fets_disabled(self):
        pkt = self._build_packet(5000, 0, 5000, 10000, fet=0x00)
        r   = _parse_basic_info(pkt)
        self.assertFalse(r["charge_fet"])
        self.assertFalse(r["discharge_fet"])

    def test_balance_cell_flags(self):
        """balance_cells length must equal cell count."""
        pkt = self._build_packet(5000, 0, 5000, 10000)
        r   = _parse_basic_info(pkt)
        self.assertEqual(len(r["balance_cells"]), 4)

    def test_negative_current_signed(self):
        """10A discharge: raw signed int16 = -1000 (10mA/LSB)."""
        pkt = self._build_packet(5400, -1000, 5000, 10000)
        r   = _parse_basic_info(pkt)
        self.assertAlmostEqual(r["current_a"], -10.0, places=1)
        self.assertLess(r["power_w"], 0)

    def test_remain_wh_computed(self):
        """remain_wh = remain_ah × voltage_v."""
        pkt = self._build_packet(5000, 0, 5000, 10000)  # 50V, 50Ah
        r   = _parse_basic_info(pkt)
        self.assertAlmostEqual(r["remain_wh"], 50.0 * 50.0, delta=5.0)

    def test_truncated_packet_raises(self):
        with self.assertRaises(ValueError):
            _parse_basic_info(b"\xDD\x03\x00\x05\x00\x00\x00\x00\x77")

    def test_bad_start_byte_raises(self):
        bad = bytearray(LIVE_BASIC_INFO)
        bad[0] = 0xAA
        with self.assertRaises(ValueError):
            _parse_basic_info(bytes(bad))

    def test_error_status_raises(self):
        bad = bytearray(LIVE_BASIC_INFO)
        bad[2] = 0x80  # error status
        with self.assertRaises(ValueError):
            _parse_basic_info(bytes(bad))

    def test_ntc_count_overflow_guarded(self):
        """Corrupt NTC count must not cause IndexError."""
        pkt = self._build_packet(5000, 0, 5000, 10000, ntc_temps=[2981])
        pkt_bad = bytearray(pkt)
        pkt_bad[4 + 22] = 0xFF  # NTC count = 255
        r = _parse_basic_info(bytes(pkt_bad))   # must not raise
        self.assertIsInstance(r["temp_c"], list)


# ─────────────────────────────────────────────────────────────────────────────
# 3. Victron VE.Bus parser
# ─────────────────────────────────────────────────────────────────────────────

class TestVEBusParser(unittest.TestCase):

    def test_live_packet(self):
        """Confirmed live packet from VE.Bus Smart Dongle."""
        d = bytes.fromhex("09006aff18950000981700c2ff")
        r = _parse_vebus(d)
        self.assertEqual(r["inverter_state"], "Inverting")
        self.assertAlmostEqual(r["voltage_v"], 54.0, places=1)
        self.assertAlmostEqual(r["current_a"], -15.0, places=1)
        self.assertEqual(r["ac_out_power_va"], 755)
        self.assertAlmostEqual(r["temperature_c"], 26.0, places=0)
        self.assertIsNone(r["alarm_reason"])

    def test_inverting_state(self):
        d = _build_vebus_payload(state=0x09, batt_v=54.0, batt_cur_a=-15.0,
                                  ac_out_w=755, temp_raw=66)
        r = _parse_vebus(d)
        self.assertEqual(r["inverter_state"], "Inverting")
        self.assertAlmostEqual(r["voltage_v"], 54.0, places=1)
        self.assertAlmostEqual(r["current_a"], -15.0, places=1)
        self.assertEqual(r["ac_out_power_va"], 755)

    def test_passthrough_state(self):
        d = _build_vebus_payload(state=0x08, batt_v=54.0, batt_cur_a=0.0,
                                  active_ac=0, ac_in_w=500, ac_out_w=500)
        r = _parse_vebus(d)
        self.assertEqual(r["inverter_state"], "Passthrough")
        self.assertEqual(r["ac_in_source"], "AC1")
        self.assertEqual(r["ac_in_power_w"], 500)

    def test_charging_state(self):
        d = _build_vebus_payload(state=0xFD, batt_v=51.0, batt_cur_a=20.0,
                                  active_ac=0, ac_in_w=1200, ac_out_w=200)
        r = _parse_vebus(d)
        self.assertEqual(r["inverter_state"], "Charge")
        self.assertGreater(r["current_a"], 0)

    def test_battery_current_positive_charging(self):
        d = _build_vebus_payload(batt_cur_a=10.0)
        r = _parse_vebus(d)
        self.assertAlmostEqual(r["current_a"], 10.0, places=1)

    def test_battery_current_negative_discharging(self):
        d = _build_vebus_payload(batt_cur_a=-19.0)
        r = _parse_vebus(d)
        self.assertAlmostEqual(r["current_a"], -19.0, places=1)

    def test_ac_in_negative_feed_in(self):
        """Negative ac_in_power means feeding back to grid."""
        d = _build_vebus_payload(ac_in_w=-150, active_ac=0)
        r = _parse_vebus(d)
        self.assertEqual(r["ac_in_power_w"], -150)

    def test_temperature_offset(self):
        """Temperature raw value 65 → 65−40 = 25°C."""
        d = _build_vebus_payload(temp_raw=65)
        r = _parse_vebus(d)
        self.assertEqual(r["temperature_c"], 25)

    def test_temperature_na(self):
        d = _build_vebus_payload(temp_raw=0x7F)
        r = _parse_vebus(d)
        self.assertIsNone(r["temperature_c"])

    def test_soc_present(self):
        d = _build_vebus_payload(soc=84)
        r = _parse_vebus(d)
        self.assertEqual(r["capacity_pct"], 84)

    def test_soc_na(self):
        d = _build_vebus_payload(soc=0x7F)
        r = _parse_vebus(d)
        self.assertIsNone(r["capacity_pct"])

    def test_alarm_warning(self):
        d = _build_vebus_payload(alarm=1)
        r = _parse_vebus(d)
        self.assertEqual(r["alarm_reason"], "Warning")

    def test_alarm_alarm(self):
        d = _build_vebus_payload(alarm=2)
        r = _parse_vebus(d)
        self.assertEqual(r["alarm_reason"], "Alarm")

    def test_alarm_na(self):
        d = _build_vebus_payload(alarm=3)
        r = _parse_vebus(d)
        self.assertIsNone(r["alarm_reason"])

    def test_alarm_ok(self):
        d = _build_vebus_payload(alarm=0)
        r = _parse_vebus(d)
        self.assertIsNone(r["alarm_reason"])

    def test_ac_in_ac1(self):
        d = _build_vebus_payload(active_ac=0)
        r = _parse_vebus(d)
        self.assertEqual(r["ac_in_source"], "AC1")

    def test_ac_in_ac2(self):
        d = _build_vebus_payload(active_ac=1)
        r = _parse_vebus(d)
        self.assertEqual(r["ac_in_source"], "AC2")

    def test_ac_in_not_connected(self):
        d = _build_vebus_payload(active_ac=2)
        r = _parse_vebus(d)
        self.assertEqual(r["ac_in_source"], "Not connected")

    def test_ac_in_unknown(self):
        d = _build_vebus_payload(active_ac=3)
        r = _parse_vebus(d)
        self.assertIsNone(r["ac_in_source"])

    def test_all_na_sentinels(self):
        val = 0
        val |= 0x09   <<  0   # state = Inverting (not NA)
        val |= 0xFF   <<  8   # error = NA
        val |= 0x7FFF << 16   # batt_current = NA
        val |= 0x3FFF << 32   # batt_voltage = NA
        val |= 3      << 46   # active_ac = NA
        val |= 0x3FFFF << 48  # ac_in = NA
        val |= 0x3FFFF << 67  # ac_out = NA
        val |= 3      << 86   # alarm = NA
        val |= 0x7F   << 88   # temp = NA
        val |= 0x7F   << 95   # soc = NA
        d = val.to_bytes(20, "little")
        r = _parse_vebus(d)
        self.assertIsNone(r["vebus_error"])
        self.assertIsNone(r["current_a"])
        self.assertIsNone(r["voltage_v"])
        self.assertIsNone(r["ac_in_power_w"])
        self.assertIsNone(r["ac_out_power_va"])
        self.assertIsNone(r["alarm_reason"])
        self.assertIsNone(r["temperature_c"])
        self.assertIsNone(r["capacity_pct"])

    def test_too_short_raises(self):
        with self.assertRaises(ValueError):
            _parse_vebus(b"\x09\x00\x00\x00")

    def test_battery_power_computed(self):
        d = _build_vebus_payload(batt_v=54.0, batt_cur_a=-15.0)
        r = _parse_vebus(d)
        self.assertIsNotNone(r["power_w"])
        self.assertAlmostEqual(r["power_w"], 54.0 * (-15.0), delta=1.0)


# ─────────────────────────────────────────────────────────────────────────────
# 4. Victron Solar Charger parser (0x01)
# ─────────────────────────────────────────────────────────────────────────────

class TestSolarParser(unittest.TestCase):

    def _build(self, state=3, error=0, batt_v_raw=1280, batt_a_raw=500,
               yield_raw=100, pv_w_raw=640, load_a_raw=25):
        # Solar parser uses little-endian at these offsets:
        # [0]=state [1]=error [2:4]=batt_v int16 LE [4:6]=batt_a int16 LE
        # [6:8]=yield uint16 LE [8:10]=pv_power uint16 LE [10:12]=load_a 9-bit LE
        p = bytearray(12)
        p[0] = state
        p[1] = error
        struct.pack_into("<h", p, 2, batt_v_raw)   # int16 LE, 0.01V
        struct.pack_into("<h", p, 4, batt_a_raw)   # int16 LE, 0.1A
        struct.pack_into("<H", p, 6, yield_raw)    # uint16 LE, 0.01kWh
        struct.pack_into("<H", p, 8, pv_w_raw)     # uint16 LE, 1W
        struct.pack_into("<H", p, 10, load_a_raw & 0x1FF)  # 9-bit LE
        return bytes(p)

    def test_voltage(self):
        r = _parse_solar(self._build(batt_v_raw=1280))
        self.assertAlmostEqual(r["voltage_v"], 12.8, places=2)

    def test_current(self):
        r = _parse_solar(self._build(batt_a_raw=50))   # 50 * 0.1A = 5.0A
        self.assertAlmostEqual(r["current_a"], 5.0, places=2)

    def test_yield_today(self):
        r = _parse_solar(self._build(yield_raw=100))
        self.assertAlmostEqual(r["yield_today_wh"], 1000.0, places=0)

    def test_pv_power(self):
        r = _parse_solar(self._build(pv_w_raw=640))
        self.assertAlmostEqual(r["pv_power_w"], 640.0, places=0)

    def test_load_current(self):
        r = _parse_solar(self._build(load_a_raw=25))
        self.assertAlmostEqual(r["load_current_a"], 2.5, places=1)

    def test_charger_state_bulk(self):
        r = _parse_solar(self._build(state=3))
        self.assertEqual(r["charger_state"], "Bulk")

    def test_charger_state_float(self):
        r = _parse_solar(self._build(state=5))
        self.assertEqual(r["charger_state"], "Float")

    def test_na_voltage(self):
        r = _parse_solar(self._build(batt_v_raw=0x7FFF))
        self.assertIsNone(r["voltage_v"])


# ─────────────────────────────────────────────────────────────────────────────
# 5. Victron Inverter parser (0x03)
# ─────────────────────────────────────────────────────────────────────────────

class TestInverterParser(unittest.TestCase):

    def _build(self, state=0x09, alarm=0, batt_v_raw=5200,
               ac_va=306, ac_v_raw=12000, ac_i_raw=32):
        p = bytearray(13)
        p[0] = state
        struct.pack_into("<H", p, 1, alarm)
        struct.pack_into("<h", p, 3, batt_v_raw)   # int16 0.01V
        struct.pack_into("<H", p, 5, ac_va)
        word = (ac_v_raw & 0x7FFF) | ((ac_i_raw & 0x7FF) << 15)
        struct.pack_into("<I", p, 7, word)
        return bytes(p)

    def test_battery_voltage(self):
        r = _parse_inverter(self._build(batt_v_raw=5200))
        self.assertAlmostEqual(r["voltage_v"], 52.0, places=2)

    def test_ac_apparent_power(self):
        r = _parse_inverter(self._build(ac_va=306))
        self.assertEqual(r["ac_out_power_va"], 306.0)

    def test_ac_voltage(self):
        r = _parse_inverter(self._build(ac_v_raw=12000))
        self.assertAlmostEqual(r["ac_out_voltage_v"], 120.0, places=1)

    def test_ac_current(self):
        r = _parse_inverter(self._build(ac_i_raw=32))
        self.assertAlmostEqual(r["ac_out_current_a"], 3.2, places=1)

    def test_inverting_state(self):
        r = _parse_inverter(self._build(state=0x09))
        self.assertEqual(r["inverter_state"], "Inverting")

    def test_batt_v_na(self):
        r = _parse_inverter(self._build(batt_v_raw=0x7FFF))
        self.assertIsNone(r["voltage_v"])


# ─────────────────────────────────────────────────────────────────────────────
# 6. Victron BMV parser (0x02)
# ─────────────────────────────────────────────────────────────────────────────

class TestBMVParser(unittest.TestCase):

    def _build(self, ttg=1000, batt_mv=1280, alarm=0, aux=0,
               current_u22=0, soc_raw=1000):
        p = bytearray(16)
        struct.pack_into("<H", p, 0, ttg)
        struct.pack_into("<H", p, 2, batt_mv)
        struct.pack_into("<H", p, 4, alarm)
        struct.pack_into("<H", p, 6, aux)
        word = (aux & 0x3) | ((current_u22 & 0x3FFFFF) << 2)
        struct.pack_into("<I", p, 8, word)
        # SoC at bit 108 = byte 13 bit 4
        ws = (soc_raw & 0x3FF) << 4
        struct.pack_into("<H", p, 13, ws)
        return bytes(p)

    def test_na_current_sentinel(self):
        """NA current (0x3FFFFF) must map to None."""
        r = _parse_bmv(self._build(current_u22=0x3FFFFF))
        self.assertIsNone(r["current_a"])

    def test_negative_current(self):
        """Negative current: raw 0x3FFFFE = -2 mA = -0.002A."""
        r = _parse_bmv(self._build(current_u22=0x3FFFFE))
        self.assertAlmostEqual(r["current_a"], -0.002, places=4)

    def test_positive_current(self):
        r = _parse_bmv(self._build(current_u22=5000))
        self.assertAlmostEqual(r["current_a"], 5.0, places=2)

    def test_battery_voltage(self):
        r = _parse_bmv(self._build(batt_mv=1280))
        self.assertAlmostEqual(r["voltage_v"], 12.80, places=2)

    def test_soc(self):
        r = _parse_bmv(self._build(soc_raw=1000))
        self.assertAlmostEqual(r["capacity_pct"], 100.0, places=0)


# ─────────────────────────────────────────────────────────────────────────────
# 7b. Voltage plausibility checks (per-type ceilings)
# ─────────────────────────────────────────────────────────────────────────────

class TestVoltagePlausibilityCeilings(unittest.TestCase):
    """
    The decode pipeline applies per-type voltage ceilings to catch garbage
    decryptions that happen to parse without error:

      monitor  → 80V  (SmartShunt, BMV — DC bus only, 48V max system)
      mppt     → 150V (solar charger DC output)
      inverter → 150V (with 9V floor)
      unknown  → 150V

    This is the fix for SmartShunt reporting 142.3V / -1499A — values which
    are below the old 150V ceiling but physically impossible for any supported
    battery monitor.
    """

    def _read_with_type(self, voltage_v, current_a, dtype):
        """
        Run controlled values through the plausibility gate by patching
        the parsing pipeline to return specific voltage/current values,
        then calling read_victron_advertisement with the device type set.

        Uses record type 0x02 for monitor and 0x07 for inverter — both are
        valid for their respective configured types.
        """
        from unittest.mock import patch, MagicMock
        from solar_monitor.victron import read_victron_advertisement, PARSERS

        class _BLE:
            address = "AA:BB:CC:DD:EE:FF"
            name    = "TestDevice"

        # Choose a record type that is allowed for the given device type
        rec_type = 0x07 if dtype == "inverter" else 0x02

        def _fake_parse(dec):
            return {
                "voltage_v":    voltage_v,
                "current_a":    current_a,
                "power_w":      None,
                "capacity_pct": 80 if dtype == "monitor" else None,
                "ttg_minutes":  None,
                "alarm_reason": None,
                # inverter fields
                "ac_out_power_va": 755.0 if dtype == "inverter" else None,
                "inverter_state":  "Inverting" if dtype == "inverter" else None,
                "ac_in_power_w":   None,
                "ac_in_source":    None,
                "vebus_error":     0,
            }

        fake_adv = MagicMock()
        fake_adv.manufacturer_data = {0x02E1: bytes(12)}

        with patch("solar_monitor.victron.extract_victron_mfr",
                   return_value=bytes(12)), \
             patch("solar_monitor.victron.parse_payload",
                   return_value=(rec_type, 0, bytes(16))), \
             patch("solar_monitor.victron.try_decrypt",
                   return_value=bytes(16)), \
             patch.dict(PARSERS, {rec_type: _fake_parse}):
            result = read_victron_advertisement(
                device               = _BLE(),
                adv_data             = fake_adv,
                friendly_name        = "TestDevice",
                enc_key              = "00" * 16,
                device_type_override = dtype,
            )
        return result

    def test_monitor_142v_rejected(self):
        """142.3V must be rejected for monitor type — the SmartShunt bug."""
        r = self._read_with_type(142.3, -1.5, "monitor")
        self.assertIsNotNone(r.error,
            "142.3V must be rejected as implausible for a monitor device")

    def test_monitor_55v_accepted(self):
        """55V is valid for a 48V system SmartShunt."""
        r = self._read_with_type(55.0, -10.0, "monitor")
        self.assertIsNone(r.error,
            "55V must be accepted as valid for a monitor device")

    def test_monitor_80v_boundary_accepted(self):
        """80V is exactly the ceiling for monitor — must be accepted."""
        r = self._read_with_type(80.0, 0.0, "monitor")
        self.assertIsNone(r.error,
            "80.0V is the monitor ceiling and must be accepted")

    def test_monitor_80_1v_rejected(self):
        """80.1V is just above the monitor ceiling — must be rejected."""
        r = self._read_with_type(80.1, 0.0, "monitor")
        self.assertIsNotNone(r.error,
            "80.1V must be rejected as implausible for a monitor device")

    def test_inverter_120v_accepted(self):
        """120V is valid for an inverter-type device."""
        r = self._read_with_type(120.0, -10.0, "inverter")
        # Inverter ceiling is 150V; 120V should pass
        self.assertIsNone(r.error,
            "120V should be accepted for an inverter device")

    def test_inverter_150v_boundary_accepted(self):
        """150V is the inverter ceiling — must be accepted."""
        r = self._read_with_type(150.0, -1.0, "inverter")
        self.assertIsNone(r.error,
            "150V is the inverter ceiling and must be accepted")

    def test_inverter_151v_rejected(self):
        """151V is above the inverter ceiling — must be rejected."""
        r = self._read_with_type(151.0, -1.0, "inverter")
        self.assertIsNotNone(r.error,
            "151V must be rejected as implausible for an inverter device")

    def test_current_over_2000a_rejected(self):
        """±2000A is the current ceiling for all types."""
        r = self._read_with_type(54.0, -2001.0, "monitor")
        self.assertIsNotNone(r.error,
            "-2001A must be rejected as implausible")

    def test_current_1999a_accepted(self):
        """1999A is just below the current ceiling."""
        r = self._read_with_type(54.0, 1999.0, "monitor")
        self.assertIsNone(r.error,
            "1999A must be accepted (within ±2000A ceiling)")

    def test_v_ceilings_in_source(self):
        """Per-type ceiling dict must be present in the source."""
        with open(f"{REPO_ROOT}/solar_monitor/victron.py") as f:
            src = f.read()
        self.assertIn("80.0", src, "80V monitor ceiling must be in source")
        self.assertIn("v_ceiling", src, "v_ceiling variable must be in source")
        self.assertIn("v_ceilings", src, "v_ceilings dict must be in source")




    def _build(self, batt_mv=5200, alarm=0, current_u22=0):
        # _parse_dcenergy: voltage = int16 LE at byte[2:4], 0.01V scale
        p = bytearray(16)
        struct.pack_into("<H", p, 0, 0)           # byte[0:2] unused
        struct.pack_into("<h", p, 2, batt_mv)     # int16 LE, 0.01V
        struct.pack_into("<H", p, 4, alarm)
        word = current_u22 & 0x3FFFFF
        struct.pack_into("<I", p, 12, word)
        return bytes(p)

    def test_na_current(self):
        r = _parse_dcenergy(self._build(current_u22=0x3FFFFF))
        self.assertIsNone(r["current_a"])

    def test_negative_current(self):
        r = _parse_dcenergy(self._build(current_u22=0x3FFFFE))
        self.assertAlmostEqual(r["current_a"], -0.002, places=4)

    def test_voltage(self):
        r = _parse_dcenergy(self._build(batt_mv=5200))
        self.assertAlmostEqual(r["voltage_v"], 52.0, places=1)

    def test_voltage_na(self):
        r = _parse_dcenergy(self._build(batt_mv=0x7FFF))
        self.assertIsNone(r["voltage_v"])


# ─────────────────────────────────────────────────────────────────────────────
# 8. PARSERS dispatch table
# ─────────────────────────────────────────────────────────────────────────────

class TestParsersTable(unittest.TestCase):

    def test_0x07_maps_to_vebus(self):
        self.assertIs(PARSERS[0x07], _parse_vebus)

    def test_0x0C_maps_to_vebus(self):
        self.assertIs(PARSERS[0x0C], _parse_vebus)

    def test_0x01_maps_to_solar(self):
        self.assertIs(PARSERS[0x01], _parse_solar)

    def test_0x03_maps_to_inverter(self):
        self.assertIs(PARSERS[0x03], _parse_inverter)

    def test_0x02_maps_to_bmv(self):
        self.assertIs(PARSERS[0x02], _parse_bmv)

    def test_0x0D_maps_to_dcenergy(self):
        self.assertIs(PARSERS[0x0D], _parse_dcenergy)

    def test_all_expected_types_registered(self):
        expected = {0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07,
                    0x08, 0x09, 0x0B, 0x0C, 0x0D, 0x0E}
        self.assertEqual(set(PARSERS.keys()), expected)

    def test_0x0C_not_in_records_with_state(self):
        """VE.Bus (0x0C) must bypass the state-byte gatekeeper."""
        self.assertNotIn(0x0C, _RECORDS_WITH_STATE)

    def test_0x07_in_records_with_state(self):
        """0x07 IS in _RECORDS_WITH_STATE — state byte check applies.
        VE.Bus dongles broadcasting 0x07 use _parse_vebus which handles
        all states; the state-check only affects non-VE.Bus 0x07 devices."""
        self.assertIn(0x07, _RECORDS_WITH_STATE)

    def test_passthrough_in_valid_states(self):
        self.assertIn(0x08, _VALID_STATES)

    def test_charge_in_valid_states(self):
        self.assertIn(0xFD, _VALID_STATES)

    def test_external_control_in_valid_states(self):
        self.assertIn(0xF7, _VALID_STATES)

    def test_inverter_states_complete(self):
        for state in [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 246, 247, 252, 253, 255]:
            self.assertIn(state, _INVERTER_STATES, msg=f"State {state} missing")


# ─────────────────────────────────────────────────────────────────────────────
# 9b. parse_payload — Format A nibble order (critical correctness test)
# ─────────────────────────────────────────────────────────────────────────────

class TestParsePayloadNibbleOrder(unittest.TestCase):
    """
    Format A byte[3] encodes:  high nibble = record type, low nibble = key index.
    An earlier bug had this inverted (low=rec, high=key), causing record type 0x0C
    (VE.Bus newer firmware) to be read as 0x00 (unknown) and silently discarded.

    Reference: victron-ble open-source library (keshavdv/victron-ble):
        record_type = (data[3] & 0xF0) >> 4
        key_index   =  data[3] & 0x0F
    """

    def _format_a(self, rec_type, key_idx, nonce=0xAF1D, model=0x3502):
        """Build a minimal Format A payload with the given record type and key index."""
        byte3 = ((rec_type & 0x0F) << 4) | (key_idx & 0x0F)
        payload = bytearray(12)
        payload[0] = 0x10
        import struct
        struct.pack_into("<H", payload, 1, model)
        payload[3] = byte3
        payload[4] = 0x02  # counter
        struct.pack_into("<H", payload, 5, nonce)
        payload[7] = 0xA8  # key verification byte
        return bytes(payload)

    def test_high_nibble_is_record_type(self):
        """Record type must come from the HIGH nibble of byte[3]."""
        from solar_monitor.victron import parse_payload
        # rec_type=0x0C (VE.Bus), key_idx=0 → byte[3] = 0xC0
        payload = self._format_a(rec_type=0x0C, key_idx=0)
        self.assertEqual(payload[3], 0xC0)
        rt, _, _ = parse_payload(payload)
        self.assertEqual(rt, 0x0C,
            f"Expected record type 0x0C (VE.Bus), got 0x{rt:02X}. "
            f"byte[3]=0xC0: high nibble (C) should be record type, "
            f"not low nibble (0).")

    def test_low_nibble_is_key_index(self):
        """Key index lives in the LOW nibble — does not affect record type."""
        from solar_monitor.victron import parse_payload
        # rec_type=0x02 (BMV), key_idx=3 → byte[3] = 0x23
        payload = self._format_a(rec_type=0x02, key_idx=3)
        self.assertEqual(payload[3], 0x23)
        rt, _, _ = parse_payload(payload)
        self.assertEqual(rt, 0x02,
            f"Record type should be 0x02, got 0x{rt:02X}. "
            f"Low nibble (key_idx=3) must not corrupt the record type.")

    def test_real_hci_packet_record_type(self):
        """
        Regression test for bug report: raw HCI packet where byte[3]=0xC0
        was incorrectly parsed as record type 0x00 instead of 0x0C (VE.Bus).

        HCI dump (from user bug report):
            0020: 1a ff e1 02 10 02 35 c0 02 1d af a8 ...
                          ^^^^^ = Victron company ID
                               ^^ = Format A marker (0x10)
                                  ^^ ^^ = model ID 0x3502
                                        ^^ = 0xC0 → high nibble = 0xC → rec 0x0C
        """
        from solar_monitor.victron import parse_payload
        # Manufacturer payload (after stripping company ID):
        mfr_payload = bytes.fromhex("100235c0021dafa830a47bdf44079b750b880495ae6f96")
        rt, nonce, cipher = parse_payload(mfr_payload)
        self.assertEqual(rt, 0x0C,
            f"byte[3]=0xC0 must decode to record type 0x0C (VE.Bus), "
            f"got 0x{rt:02X}. This was the bug causing the device to be "
            f"silently ignored.")
        self.assertEqual(nonce, 0xAF1D)
        self.assertEqual(len(cipher), 15)

    def test_record_type_0x01_mppt(self):
        from solar_monitor.victron import parse_payload
        # rec=0x01, key=0 → byte[3]=0x10
        payload = self._format_a(rec_type=0x01, key_idx=0)
        rt, _, _ = parse_payload(payload)
        self.assertEqual(rt, 0x01)

    def test_record_type_0x07_vebus_old(self):
        from solar_monitor.victron import parse_payload
        # rec=0x07, key=0 → byte[3]=0x70
        payload = self._format_a(rec_type=0x07, key_idx=0)
        rt, _, _ = parse_payload(payload)
        self.assertEqual(rt, 0x07)

    def test_record_type_0x02_bmv(self):
        from solar_monitor.victron import parse_payload
        # rec=0x02, key=0 → byte[3]=0x20
        payload = self._format_a(rec_type=0x02, key_idx=0)
        rt, _, _ = parse_payload(payload)
        self.assertEqual(rt, 0x02)

    def test_high_nibble_not_low(self):
        """Explicitly confirm: if nibbles were swapped, 0xC0 would give 0x00."""
        byte3 = 0xC0
        wrong_rec  = byte3 & 0x0F          # old (wrong) code
        correct_rec = (byte3 & 0xF0) >> 4  # corrected code
        self.assertEqual(wrong_rec,   0x00, "Sanity: old code gives 0x00")
        self.assertEqual(correct_rec, 0x0C, "Sanity: new code gives 0x0C")

    def test_docstring_matches_implementation(self):
        """parse_payload docstring must document high nibble = record type."""
        from solar_monitor.victron import parse_payload
        doc = parse_payload.__doc__ or ""
        self.assertIn("high nibble = record type", doc)



class TestDeviceReading(unittest.TestCase):

    def _make(self, **kwargs) -> DeviceReading:
        return DeviceReading(
            address="AA:BB:CC:DD:EE:FF", name="test",
            device_type=kwargs.pop("device_type", "bms"),
            timestamp="2024-01-01T00:00:00",
            **kwargs
        )

    def test_defaults_none(self):
        r = self._make()
        self.assertIsNone(r.voltage_v)
        self.assertIsNone(r.current_a)
        self.assertIsNone(r.remain_wh)
        self.assertIsNone(r.time_to_empty_h)
        self.assertIsNone(r.faults)
        self.assertIsNone(r.balance_cells)

    def test_setattr_from_parse_dict(self):
        parsed = {
            "voltage_v": 54.0, "current_a": -15.0, "power_w": -810.0,
            "capacity_pct": 84, "remain_ah": 84.0, "nominal_ah": 100.0,
            "remain_wh": 4536.0, "nominal_wh": 5400.0,
            "time_to_empty_h": 5.6, "time_to_full_h": None,
            "cycle_count": 8, "cell_count": 16,
            "sw_version": "6.2", "production_date": "2025-11-26",
            "balance_cells": [0]*16, "protection_bits": 0,
            "faults": [], "charge_fet": True, "discharge_fet": True,
            "temp_c": [23.1, 21.8, 21.9],
        }
        r = self._make()
        for k, v in parsed.items():
            if hasattr(r, k):
                setattr(r, k, v)
        self.assertEqual(r.voltage_v, 54.0)
        self.assertEqual(r.remain_ah, 84.0)
        self.assertEqual(r.remain_wh, 4536.0)
        self.assertEqual(r.time_to_empty_h, 5.6)
        self.assertEqual(r.cell_count, 16)
        self.assertTrue(r.charge_fet)
        self.assertEqual(r.faults, [])

    def test_vebus_fields_present(self):
        r = self._make(device_type="inverter")
        self.assertIsNone(r.ac_in_power_w)
        self.assertIsNone(r.ac_in_source)
        self.assertIsNone(r.vebus_error)
        self.assertIsNone(r.temperature_c)

    def test_raw_load_indicator_field(self):
        r = self._make(device_type="inverter")
        r.raw_load_indicator = 56
        self.assertEqual(r.raw_load_indicator, 56)


# ─────────────────────────────────────────────────────────────────────────────
# 10. Dashboard rendering
# ─────────────────────────────────────────────────────────────────────────────

def _make_bms(**kwargs) -> DeviceReading:
    defaults = dict(
        address="AA:BB:CC:DD:EE:FF", name="House Bank",
        device_type="bms", timestamp="t",
        voltage_v=54.32, current_a=0.0, power_w=0.0,
        capacity_pct=100, remain_ah=100.83, nominal_ah=100.0,
        remain_wh=5455.1, nominal_wh=5432.0,
        time_to_empty_h=None, time_to_full_h=None,
        cycle_count=8, cell_count=16,
        sw_version="6.2", production_date="2025-11-26",
        balance_cells=[0]*16, protection_bits=0, faults=[],
        charge_fet=True, discharge_fet=True,
        temp_c=[23.1, 21.8, 21.9],
    )
    defaults.update(kwargs)
    r = DeviceReading(**{k: v for k, v in defaults.items()
                         if hasattr(DeviceReading, k) or
                         k in ("address","name","device_type","timestamp")})
    for k, v in defaults.items():
        if hasattr(r, k):
            setattr(r, k, v)
    return r


def _make_inverter(**kwargs) -> DeviceReading:
    r = DeviceReading(address="E6:2E", name="Multiplus-Ii",
                      device_type="inverter", timestamp="t")
    defaults = dict(
        voltage_v=54.0, current_a=-15.0, power_w=-810.0,
        ac_out_power_va=755.0, ac_in_power_w=0.0,
        ac_in_source="Not connected", inverter_state="Inverting",
        temperature_c=26.0, alarm_reason=None, vebus_error=0,
        capacity_pct=None,
    )
    defaults.update(kwargs)
    for k, v in defaults.items():
        if hasattr(r, k):
            setattr(r, k, v)
    return r


class TestBMSCard(unittest.TestCase):

    def setUp(self):
        self.r = _make_bms()
        self.html = dash_mod.render_bms_card(self.r)

    def test_voltage_shown(self):
        self.assertIn("54.32", self.html)

    def test_soc_shown(self):
        self.assertIn("100%", self.html)

    def test_remain_wh_shown(self):
        self.assertIn("5455", self.html)

    def test_remain_ah_shown(self):
        self.assertIn("100.8", self.html)

    def test_cell_count_shown(self):
        self.assertIn("16 cells", self.html)

    def test_cycle_count_shown(self):
        self.assertIn("8 cycles", self.html)

    def test_temperatures_shown(self):
        self.assertIn("23.1", self.html)
        self.assertIn("21.8", self.html)

    def test_fet_status_shown(self):
        self.assertIn("CHG ✓", self.html)
        self.assertIn("DSG ✓", self.html)

    def test_no_faults_shown_no_fault_div(self):
        self.assertNotIn("⚠", self.html)

    def test_active_fault_shown(self):
        r = _make_bms(faults=["Cell overvoltage"])
        html = dash_mod.render_bms_card(r)
        self.assertIn("Cell overvoltage", html)

    def test_balancing_cell_shown(self):
        bal = [0] * 16
        bal[3] = 1  # cell 4 balancing
        r = _make_bms(balance_cells=bal)
        html = dash_mod.render_bms_card(r)
        self.assertIn("Balancing", html)
        self.assertIn("4", html)

    def test_tte_shown_when_discharging(self):
        r = _make_bms(current_a=-10.0, time_to_empty_h=8.5)
        html = dash_mod.render_bms_card(r)
        self.assertIn("TTE", html)

    def test_ttf_shown_when_charging(self):
        r = _make_bms(current_a=5.0, time_to_full_h=2.25)
        html = dash_mod.render_bms_card(r)
        self.assertIn("TTF", html)

    def test_offline_shows_error(self):
        r = _make_bms()
        r.error = "Connection refused"
        html = dash_mod.render_bms_card(r)
        self.assertIn("OFFLINE", html)
        self.assertIn("Connection refused", html)


class TestInverterCard(unittest.TestCase):

    def setUp(self):
        self.r = _make_inverter()
        self.html = dash_mod.build_html([], [self.r], {}, theme="business")

    def test_ac_output_l1_section(self):
        self.assertIn("AC Output L1", self.html)

    def test_battery_section(self):
        self.assertIn("Battery", self.html)

    def test_voltage_120(self):
        self.assertIn(">120<", self.html)

    def test_power_label(self):
        self.assertIn("Power (W)", self.html)

    def test_ac_power_value(self):
        self.assertIn(">755<", self.html)

    def test_computed_ac_current(self):
        # 755 / 120 = 6.29A
        self.assertIn("6.29", self.html)

    def test_battery_voltage(self):
        self.assertIn("54.00", self.html)

    def test_battery_current_signed(self):
        self.assertIn("-15.00", self.html)

    def test_temperature(self):
        self.assertIn("26", self.html)

    def test_state_shown(self):
        self.assertIn("Inverting", self.html)

    def test_ac_in_source(self):
        self.assertIn("Not connected", self.html)

    def test_alarm_none(self):
        self.assertIn("None", self.html)

    def test_passthrough_shows_ac_in_power(self):
        r = _make_inverter(inverter_state="Passthrough",
                           ac_in_source="AC1", ac_in_power_w=755.0)
        html = dash_mod.build_html([], [r], {}, theme="business")
        self.assertIn("Passthrough", html)
        self.assertIn("AC1", html)


class TestMpptAggregateCard(unittest.TestCase):
    """render_mppt_aggregate_card — total PV power, yield, charger states."""

    def _mppt(self, name="South", **kw):
        r = _make_inverter.__func__(name=name) if False else DeviceReading(
            address="CC:DD", name=name, device_type="mppt", timestamp="t")
        r.pv_power_w = kw.get("pv_power_w", 400.0)
        r.yield_today_wh = kw.get("yield_today_wh", 2000.0)
        r.charger_state = kw.get("charger_state", "Float")
        r.voltage_v = 54.0; r.current_a = 7.0; r.power_w = 378.0
        r.faults = []; r.temp_c = []
        if kw.get("error"):
            r.error = kw["error"]
        return r

    def test_sums_pv_power(self):
        readings = [self._mppt("S1", pv_power_w=400.0),
                    self._mppt("S2", pv_power_w=280.0)]
        html = dash_mod.render_mppt_aggregate_card(readings)
        self.assertIn("680", html)   # 400 + 280

    def test_sums_yield_today(self):
        readings = [self._mppt("S1", yield_today_wh=2000.0),
                    self._mppt("S2", yield_today_wh=1200.0)]
        html = dash_mod.render_mppt_aggregate_card(readings)
        self.assertIn("3200", html)

    def test_online_count(self):
        err = self._mppt("Dead", error="timeout")
        html = dash_mod.render_mppt_aggregate_card(
            [self._mppt("S1"), err])
        self.assertIn("1/2", html)

    def test_charger_states_summarised(self):
        readings = [self._mppt("S1", charger_state="Float"),
                    self._mppt("S2", charger_state="Float"),
                    self._mppt("S3", charger_state="Bulk")]
        html = dash_mod.render_mppt_aggregate_card(readings)
        self.assertIn("Float", html)
        self.assertIn("Bulk", html)

    def test_empty_readings_no_crash(self):
        html = dash_mod.render_mppt_aggregate_card([])
        self.assertIsInstance(html, str)

    def test_returns_agg_mppt_card(self):
        html = dash_mod.render_mppt_aggregate_card([self._mppt()])
        self.assertIn("agg-mppt", html)


class TestInverterAggregateCard(unittest.TestCase):
    """render_inverter_aggregate_card — AC output, states, alarms."""

    def test_sums_ac_out(self):
        inv1 = _make_inverter(name="Inv1")
        inv2 = _make_inverter(name="Inv2")
        inv1.ac_out_power_va = 755.0
        inv2.ac_out_power_va = 245.0
        html = dash_mod.render_inverter_aggregate_card([inv1, inv2])
        self.assertIn("1000", html)

    def test_inverter_state_shown(self):
        html = dash_mod.render_inverter_aggregate_card([_make_inverter()])
        self.assertIn("Inverting", html)

    def test_no_alarm_shown(self):
        html = dash_mod.render_inverter_aggregate_card([_make_inverter()])
        self.assertIn("None", html)

    def test_online_count(self):
        err = _make_inverter(name="Dead")
        err.error = "timeout"
        html = dash_mod.render_inverter_aggregate_card(
            [_make_inverter(), err])
        self.assertIn("1/2", html)

    def test_non_inverter_devices_ignored(self):
        """MPPT readings should not pollute the inverter aggregate."""
        mppt_r = DeviceReading(address="X", name="MPPT", device_type="mppt",
                               timestamp="t")
        mppt_r.ac_out_power_va = 9999.0
        mppt_r.faults = []; mppt_r.temp_c = []
        html = dash_mod.render_inverter_aggregate_card([_make_inverter(), mppt_r])
        # Only the inverter's 755W should appear, not 9999
        self.assertNotIn("9999", html)

    def test_empty_no_crash(self):
        html = dash_mod.render_inverter_aggregate_card([])
        self.assertIsInstance(html, str)

    def test_returns_agg_inv_card(self):
        html = dash_mod.render_inverter_aggregate_card([_make_inverter()])
        self.assertIn("agg-inv", html)


class TestBatteryAggregateCard(unittest.TestCase):
    """render_battery_aggregate_card — avg SoC, Wh, Ah, packs online."""

    def test_average_soc(self):
        b1 = _make_bms(capacity_pct=84)
        b2 = _make_bms(capacity_pct=76)
        html = dash_mod.render_battery_aggregate_card([b1, b2])
        self.assertIn("80%", html)   # (84+76)/2 = 80

    def test_soc_bar_present(self):
        html = dash_mod.render_battery_aggregate_card([_make_bms(capacity_pct=84)])
        self.assertIn("soc-fill", html)

    def test_total_wh(self):
        b1 = _make_bms(remain_wh=4500.0)
        b2 = _make_bms(remain_wh=3900.0)
        html = dash_mod.render_battery_aggregate_card([b1, b2])
        self.assertIn("8400", html)

    def test_online_count(self):
        err = _make_bms(name="Dead")
        err.error = "timeout"
        html = dash_mod.render_battery_aggregate_card([_make_bms(), err])
        self.assertIn("1/2", html)

    def test_net_amps(self):
        b1 = _make_bms(current_a=-10.0)
        b2 = _make_bms(current_a=-5.0)
        html = dash_mod.render_battery_aggregate_card([b1, b2])
        self.assertIn("-15", html)

    def test_empty_no_crash(self):
        html = dash_mod.render_battery_aggregate_card([])
        self.assertIsInstance(html, str)

    def test_returns_agg_bat_card(self):
        html = dash_mod.render_battery_aggregate_card([_make_bms()])
        self.assertIn("agg-bat", html)


class TestDashboardLayout(unittest.TestCase):
    """build_html — verify the new layout order and section structure."""

    def setUp(self):
        bms  = [_make_bms(name="Batt1"), _make_bms(name="Batt2")]
        mppt_r = DeviceReading(address="CC:DD", name="South", device_type="mppt",
                               timestamp="t")
        mppt_r.pv_power_w = 680.0; mppt_r.yield_today_wh = 3200.0
        mppt_r.charger_state = "Float"; mppt_r.voltage_v = 54.0
        mppt_r.current_a = 12.0; mppt_r.power_w = 648.0
        mppt_r.faults = []; mppt_r.temp_c = []
        inv  = _make_inverter(name="MultiPlus")
        self.html = dash_mod.build_html(bms, [mppt_r, inv], {}, theme="business")

    def test_all_device_names_present(self):
        for name in ("Batt1", "Batt2", "South", "MultiPlus"):
            self.assertIn(name, self.html)

    def test_all_placeholders_replaced(self):
        for ph in ("__MPPT_AGG__", "__INV_AGG__", "__BAT_AGG__",
                   "__MPPT_CARDS__", "__INV_CARDS__", "__BMS_CARDS__"):
            self.assertNotIn(ph, self.html, f"Placeholder {ph} not replaced")

    def test_no_old_placeholders(self):
        for ph in ("__AGG_CARD__", "__TOTAL_V__", "__TOTAL_A__",
                   "__TOTAL_W__", "__TOTAL_PV__", "__TOTAL_YLD__"):
            self.assertNotIn(ph, self.html, f"Old placeholder {ph} still present")

    def test_system_overview_section(self):
        self.assertIn("System Overview", self.html)

    def test_three_agg_cards_in_one_container(self):
        self.assertEqual(self.html.count('<div class="agg-cards">'), 1)

    def test_mppt_individual_section(self):
        self.assertIn("MPPT Chargers", self.html)

    def test_inverter_individual_section(self):
        self.assertIn("Inverters", self.html)

    def test_bms_individual_section(self):
        self.assertIn("Battery Packs", self.html)

    def test_layout_order_aggregates_before_individuals(self):
        pos_agg     = self.html.index("System Overview")
        pos_ind     = self.html.index("MPPT Chargers — Individual")
        self.assertLess(pos_agg, pos_ind, "Aggregates must come before individual cards")

    def test_agg_cards_responsive_css(self):
        # Mobile-first: stacks vertically, then side-by-side at 600px breakpoint
        self.assertIn("flex-direction:column", self.html)
        self.assertIn("min-width:600px", self.html)

    def test_agg_cards_min_width_breakpoint(self):
        # Agg cards use flex:1 1 220px at 600px breakpoint
        self.assertIn("220px", self.html)

    def test_html_is_valid_string(self):
        self.assertIn("<!DOCTYPE html>", self.html)
        self.assertIn("</html>", self.html)


class TestDashboardAggregates(unittest.TestCase):
    """build_html — basic smoke tests."""

    def test_build_html_no_crash(self):
        bms1 = _make_bms(name="Batt1")
        bms2 = _make_bms(name="Batt2", voltage_v=53.9, current_a=-5.0)
        inv  = _make_inverter()
        html = dash_mod.build_html([bms1, bms2], [inv], {}, theme="business")
        self.assertIn("Batt1", html)
        self.assertIn("Batt2", html)
        self.assertIn("Multiplus-Ii", html)

    def test_build_html_empty(self):
        html = dash_mod.build_html([], [], {}, theme="dark")
        self.assertIsInstance(html, str)
        self.assertGreater(len(html), 100)

    def test_all_themes_render(self):
        r = _make_bms()
        for theme in ("dark", "light", "business"):
            html = dash_mod.build_html([r], [], {}, theme=theme)
            self.assertIn(theme, html)

    def test_mppt_and_inverter_split_into_separate_sections(self):
        """MPPT and inverter cards must appear in their own sections."""
        mppt_r = DeviceReading(address="M1", name="SolarArray",
                               device_type="mppt", timestamp="t")
        mppt_r.pv_power_w = 500.0; mppt_r.yield_today_wh = 1000.0
        mppt_r.charger_state = "Float"; mppt_r.voltage_v = 54.0
        mppt_r.current_a = 9.0; mppt_r.power_w = 486.0
        mppt_r.faults = []; mppt_r.temp_c = []
        inv = _make_inverter(name="MyInverter")
        html = dash_mod.build_html([], [mppt_r, inv], {}, theme="dark")
        pos_mppt_sec = html.index("MPPT Chargers — Individual")
        pos_inv_sec  = html.index("Inverters — Individual")
        pos_solar    = html.index("SolarArray")
        pos_inv_name = html.index("MyInverter")
        self.assertLess(pos_mppt_sec, pos_inv_sec)
        # SolarArray card should appear in its own MPPT section
        self.assertGreater(pos_solar, pos_mppt_sec)




# ─────────────────────────────────────────────────────────────────────────────
# 11. VE.Bus integration — parse → DeviceReading → card
# ─────────────────────────────────────────────────────────────────────────────

class TestVEBusIntegration(unittest.TestCase):
    """Full pipeline: live bytes → _parse_vebus → DeviceReading → HTML."""

    def test_live_packet_full_pipeline(self):
        d = bytes.fromhex("09006aff18950000981700c2ff")
        parsed = _parse_vebus(d)

        r = DeviceReading(address="E6:2E", name="Multiplus-Ii",
                          device_type="inverter", timestamp="t")
        for k, v in parsed.items():
            if hasattr(r, k):
                setattr(r, k, v)

        self.assertAlmostEqual(r.voltage_v, 54.0, places=1)
        self.assertAlmostEqual(r.current_a, -15.0, places=1)
        self.assertEqual(r.ac_out_power_va, 755)
        self.assertEqual(r.temperature_c, 26)
        self.assertEqual(r.inverter_state, "Inverting")

        html = dash_mod.build_html([], [r], {}, theme="business")
        self.assertIn("AC Output L1", html)
        self.assertIn("Battery", html)
        self.assertIn("755", html)
        self.assertIn("54.00", html)
        self.assertIn("-15.00", html)

    def test_jbd_packet_full_pipeline(self):
        parsed = _parse_basic_info(LIVE_BASIC_INFO)

        r = DeviceReading(address="AA:BB", name="House Bank",
                          device_type="bms", timestamp="t")
        for k, v in parsed.items():
            if hasattr(r, k):
                setattr(r, k, v)

        self.assertAlmostEqual(r.voltage_v, 54.32, places=1)
        self.assertEqual(r.capacity_pct, 100)
        self.assertEqual(r.faults, [])
        self.assertGreater(r.remain_wh, 5000)

        html = dash_mod.render_bms_card(r)
        self.assertIn("100%", html)
        self.assertIn("54.32", html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
