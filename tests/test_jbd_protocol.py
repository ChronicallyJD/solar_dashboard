"""
tests/test_jbd_protocol.py — JBD BMS BLE protocol unit tests
=============================================================
Covers the pure-logic protocol handling in solar_monitor/jbd.py:
  - _checksum: known vectors, wrap-around, empty input
  - _verify_checksum: valid, corrupt, short, truncated frames
  - _packet_complete: length-field driven completeness, corrupt length guard
  - _parse_basic_info: happy path, signed current, derived fields,
    protection-flag decoding, NTC temperature decoding, sw_version
    nibble ordering, production-date bitfield, balance bits, NTC count
    overflow guard, and all framing error paths
  - JBDGattReader._on_notify: resync (leading garbage), chunk reassembly,
    corrupt length byte clears the buffer without setting the event
  - JBDGattReader._write / _send_recv / authenticate: write-with/without
    response selection, stale-buffer clearing, timeout, auth frame
    construction, rejected-password path
  - _discover_chars: known-UUID match (ff00 and ffe0), heuristic fallback,
    no-service error
  - read_basic_info / read_jbd_device: full stubbed round trip, error and
    timeout paths (no real BLE, no real sleeps)
"""

import asyncio
import os
import struct
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch

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

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from solar_monitor.jbd import (          # noqa: E402
    BASIC_INFO_CMD, MAX_PAYLOAD_LEN,
    JBDGattReader, read_jbd_device,
    _checksum, _verify_checksum, _packet_complete, _parse_basic_info,
    _discover_chars,
)
from solar_monitor.models import DeviceReading   # noqa: E402
import solar_monitor.jbd as jbd_mod              # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def run(coro):
    return asyncio.run(coro)


def make_payload(voltage_v=53.2, current_a=0.0, remain_ah=100.0,
                 nominal_ah=200.0, cycles=42, prod=(2023, 6, 15),
                 bal_bits=0, protection=0, sw_ver=0x21, soc=87,
                 fet=0x03, cells=4, ntc_temps=(25.0, -10.0),
                 ntc_count=None):
    """Build a register-0x03 payload from engineering-unit values."""
    prod_raw = ((prod[0] - 2000) << 9) | (prod[1] << 5) | prod[2]
    p = struct.pack(
        ">HhHHHHHHH",
        int(round(voltage_v * 100)),        # 10 mV/LSB
        int(round(current_a * 100)),        # 10 mA/LSB signed
        int(round(remain_ah * 100)),        # 10 mAh/LSB
        int(round(nominal_ah * 100)),
        cycles,
        prod_raw,
        bal_bits & 0xFFFF,                  # bal_lo
        (bal_bits >> 16) & 0xFFFF,          # bal_hi
        protection,
    )
    p += bytes([sw_ver, soc, fet, cells,
                len(ntc_temps) if ntc_count is None else ntc_count])
    for t in ntc_temps:
        p += struct.pack(">H", int(round(t * 10)) + 2731)  # 0.1 K/LSB
    return p


def make_frame(payload, reg=0x03, status=0x00, corrupt_checksum=False,
               end=0x77):
    """Wrap a payload in a full JBD response frame with a valid checksum."""
    body = bytes([len(payload)]) + payload
    chk  = _checksum(body)
    if corrupt_checksum:
        chk = bytes([chk[0] ^ 0xFF, chk[1]])
    return bytes([0xDD, reg, status]) + body + chk + bytes([end])


class _Char:
    def __init__(self, uuid, properties):
        self.uuid       = uuid
        self.properties = properties


class _Service:
    def __init__(self, uuid, chars):
        self.uuid            = uuid
        self.characteristics = chars


class _Services:
    def __init__(self, svcs):
        self._svcs = svcs

    def __iter__(self):
        return iter(self._svcs)

    def get_characteristic(self, uuid):
        for s in self._svcs:
            for c in s.characteristics:
                if c.uuid == uuid:
                    return c
        return None


FF01 = "0000ff01-0000-1000-8000-00805f9b34fb"
FF02 = "0000ff02-0000-1000-8000-00805f9b34fb"


def std_services():
    """A standard JBD ff00 service (ff01 notify, ff02 write)."""
    return _Services([_Service("0000ff00-0000-1000-8000-00805f9b34fb", [
        _Char(FF01, ["notify"]),
        _Char(FF02, ["write", "write-without-response"]),
    ])])


class FakeClient:
    """
    Stub BleakClient — delivers canned notify replies synchronously
    from write_gatt_char, so tests never wait on real timers.

    *reply_for* is a callable (cmd_bytes → iterable of notify chunks).
    """
    def __init__(self, services=None, reply_for=None):
        self.services   = services or std_services()
        self._reply_for = reply_for
        self._cb        = None
        self.writes     = []
        self.write_kwargs = []
        self.notify_started = []
        self.notify_stopped = []

    async def start_notify(self, uuid, cb):
        self.notify_started.append(uuid)
        self._cb = cb

    async def stop_notify(self, uuid):
        self.notify_stopped.append(uuid)

    async def write_gatt_char(self, uuid, data, response=True):
        self.writes.append(bytes(data))
        self.write_kwargs.append({"uuid": uuid, "response": response})
        if self._reply_for and self._cb:
            for chunk in self._reply_for(bytes(data)):
                self._cb(None, bytearray(chunk))


def make_reader(client):
    """Create a JBDGattReader wired to *client* as if already subscribed."""
    r = JBDGattReader(client)
    r._rx_uuid = FF02
    client._cb = r._on_notify
    return r


def basic_reply_for(frame):
    """reply_for that answers any info command with *frame* in one chunk."""
    def _reply(cmd):
        if cmd[1] == 0xA5:            # read command
            return [frame]
        if cmd[1] == 0x5A:            # write (auth) command
            return [make_frame(b"", reg=0x06)]
        return []
    return _reply


# ─────────────────────────────────────────────────────────────────────────────
# 1. _checksum
# ─────────────────────────────────────────────────────────────────────────────

class TestChecksum(unittest.TestCase):

    def test_known_vector_basic_info_cmd(self):
        """DD A5 03 00 FF FD 77 — checksum over reg+len = 0x10000-3."""
        self.assertEqual(_checksum(bytes([0x03, 0x00])), b"\xff\xfd")

    def test_basic_info_cmd_constant_is_self_consistent(self):
        cmd = BASIC_INFO_CMD
        self.assertEqual(cmd[0], 0xDD)
        self.assertEqual(cmd[-1], 0x77)
        self.assertEqual(_checksum(cmd[2:4]), cmd[4:6])

    def test_empty_payload(self):
        self.assertEqual(_checksum(b""), b"\x00\x00")

    def test_wraps_modulo_0x10000(self):
        # sum = 0x100 → 0x10000 - 0x100 = 0xFF00
        self.assertEqual(_checksum(bytes([0xFF, 0x01])), b"\xff\x00")

    def test_big_endian_byte_order(self):
        # sum = 1 → 0xFFFF → hi byte first
        self.assertEqual(_checksum(bytes([0x01])), b"\xff\xff")

    def test_auth_command_vector(self):
        """Password '0000' unlock frame checksum (reg 06, len 04, '0000')."""
        body = bytes([0x06, 0x04]) + b"0000"
        # 0x10000 - (6 + 4 + 4*0x30) = 0xFF36
        self.assertEqual(_checksum(body), b"\xff\x36")


# ─────────────────────────────────────────────────────────────────────────────
# 2. _verify_checksum
# ─────────────────────────────────────────────────────────────────────────────

class TestVerifyChecksum(unittest.TestCase):

    def test_valid_frame(self):
        frame = make_frame(make_payload())
        self.assertTrue(_verify_checksum(frame))

    def test_corrupt_checksum(self):
        frame = make_frame(make_payload(), corrupt_checksum=True)
        self.assertFalse(_verify_checksum(frame))

    def test_too_short(self):
        self.assertFalse(_verify_checksum(b"\xdd\x03\x00"))
        self.assertFalse(_verify_checksum(b""))

    def test_truncated_relative_to_length_byte(self):
        """Length byte claims more payload than the buffer holds."""
        frame = make_frame(make_payload())
        truncated = frame[:10]                       # header + partial payload
        self.assertFalse(_verify_checksum(truncated))

    def test_minimum_valid_empty_payload_frame(self):
        frame = make_frame(b"")                      # 7 bytes total
        self.assertTrue(_verify_checksum(frame))

    def test_covers_len_and_payload_not_register(self):
        """Changing the register byte must not break checksum verification."""
        frame = bytearray(make_frame(make_payload()))
        frame[1] = 0x04                              # different register echo
        self.assertTrue(_verify_checksum(bytes(frame)))

    def test_payload_corruption_detected(self):
        frame = bytearray(make_frame(make_payload()))
        frame[5] ^= 0x01                             # flip a payload bit
        self.assertFalse(_verify_checksum(bytes(frame)))


# ─────────────────────────────────────────────────────────────────────────────
# 3. _packet_complete
# ─────────────────────────────────────────────────────────────────────────────

class TestPacketComplete(unittest.TestCase):

    def test_short_buffer(self):
        self.assertFalse(_packet_complete(bytearray(b"\xdd\x03\x00")))
        self.assertFalse(_packet_complete(bytearray()))

    def test_bad_start_byte(self):
        frame = bytearray(make_frame(make_payload()))
        frame[0] = 0xAA
        self.assertFalse(_packet_complete(frame))

    def test_corrupt_length_byte(self):
        buf = bytearray([0xDD, 0x03, 0x00, MAX_PAYLOAD_LEN + 1])
        self.assertFalse(_packet_complete(buf))

    def test_max_payload_len_boundary_accepted(self):
        """A length byte of exactly MAX_PAYLOAD_LEN is not treated as corrupt."""
        n   = MAX_PAYLOAD_LEN
        buf = bytearray([0xDD, 0x03, 0x00, n]) + bytearray(n + 3)
        self.assertTrue(_packet_complete(buf))

    def test_incomplete_frame(self):
        frame = make_frame(make_payload())
        self.assertFalse(_packet_complete(bytearray(frame[:-1])))

    def test_complete_frame(self):
        frame = make_frame(make_payload())
        self.assertTrue(_packet_complete(bytearray(frame)))

    def test_complete_frame_with_trailing_bytes(self):
        frame = make_frame(make_payload()) + b"\x00\x00"
        self.assertTrue(_packet_complete(bytearray(frame)))


# ─────────────────────────────────────────────────────────────────────────────
# 4. _parse_basic_info — happy paths
# ─────────────────────────────────────────────────────────────────────────────

class TestParseBasicInfoHappyPath(unittest.TestCase):

    def test_core_fields(self):
        frame = make_frame(make_payload(voltage_v=53.2, current_a=-12.34,
                                        soc=87, cells=4))
        d = _parse_basic_info(frame)
        self.assertAlmostEqual(d["voltage_v"], 53.2)
        self.assertAlmostEqual(d["current_a"], -12.34)
        self.assertEqual(d["capacity_pct"], 87)
        self.assertEqual(d["cell_count"], 4)
        self.assertAlmostEqual(d["power_w"], round(53.2 * -12.34, 2))

    def test_capacity_fields(self):
        frame = make_frame(make_payload(voltage_v=53.2, remain_ah=100.0,
                                        nominal_ah=200.0))
        d = _parse_basic_info(frame)
        self.assertAlmostEqual(d["remain_ah"], 100.0)
        self.assertAlmostEqual(d["nominal_ah"], 200.0)
        self.assertAlmostEqual(d["remain_wh"], 5320.0)
        self.assertAlmostEqual(d["nominal_wh"], 10640.0)

    def test_signed_current_negative(self):
        """Discharge current is a signed big-endian 16-bit field."""
        frame = make_frame(make_payload(current_a=-50.0))
        d = _parse_basic_info(frame)
        self.assertAlmostEqual(d["current_a"], -50.0)

    def test_signed_current_positive(self):
        frame = make_frame(make_payload(current_a=25.5))
        d = _parse_basic_info(frame)
        self.assertAlmostEqual(d["current_a"], 25.5)

    def test_time_to_empty_when_discharging(self):
        frame = make_frame(make_payload(current_a=-12.34, remain_ah=100.0))
        d = _parse_basic_info(frame)
        self.assertAlmostEqual(d["time_to_empty_h"], round(100.0 / 12.34, 2))
        self.assertIsNone(d["time_to_full_h"])

    def test_time_to_full_when_charging(self):
        frame = make_frame(make_payload(current_a=20.0, remain_ah=100.0,
                                        nominal_ah=200.0))
        d = _parse_basic_info(frame)
        self.assertAlmostEqual(d["time_to_full_h"], 5.0)
        self.assertIsNone(d["time_to_empty_h"])

    def test_no_runtime_estimates_when_idle(self):
        frame = make_frame(make_payload(current_a=0.0))
        d = _parse_basic_info(frame)
        self.assertIsNone(d["time_to_empty_h"])
        self.assertIsNone(d["time_to_full_h"])

    def test_cycle_count(self):
        frame = make_frame(make_payload(cycles=321))
        self.assertEqual(_parse_basic_info(frame)["cycle_count"], 321)

    def test_sw_version_nibble_order(self):
        """High nibble is the major version, low nibble the minor (0x21 = 2.1)."""
        frame = make_frame(make_payload(sw_ver=0x21))
        self.assertEqual(_parse_basic_info(frame)["sw_version"], "2.1")

    def test_sw_version_nibble_not_inverted(self):
        """0x13 must decode as 1.3, never as 3.1 (nibble inversion guard)."""
        frame = make_frame(make_payload(sw_ver=0x13))
        d = _parse_basic_info(frame)
        self.assertEqual(d["sw_version"], "1.3")
        self.assertNotEqual(d["sw_version"], "3.1")

    def test_production_date_bitfield(self):
        """bits [15:9]=year-2000, [8:5]=month, [4:0]=day."""
        frame = make_frame(make_payload(prod=(2023, 6, 15)))
        self.assertEqual(_parse_basic_info(frame)["production_date"],
                         "2023-06-15")

    def test_production_date_extremes(self):
        frame = make_frame(make_payload(prod=(2000, 1, 1)))
        self.assertEqual(_parse_basic_info(frame)["production_date"],
                         "2000-01-01")
        frame = make_frame(make_payload(prod=(2099, 12, 31)))
        self.assertEqual(_parse_basic_info(frame)["production_date"],
                         "2099-12-31")

    def test_balance_bits_low_word(self):
        """bal_lo LSB = cell 1; 0b0101 → cells 1 and 3 balancing."""
        frame = make_frame(make_payload(bal_bits=0b0101, cells=4))
        self.assertEqual(_parse_basic_info(frame)["balance_cells"],
                         [1, 0, 1, 0])

    def test_balance_bits_span_high_word(self):
        """Cell 17 balance lives in bal_hi (bit 16 of the combined field)."""
        frame = make_frame(make_payload(bal_bits=(1 << 16) | 1, cells=17))
        bal = _parse_basic_info(frame)["balance_cells"]
        self.assertEqual(len(bal), 17)
        self.assertEqual(bal[0], 1)
        self.assertEqual(bal[16], 1)
        self.assertEqual(sum(bal), 2)

    def test_fet_bits(self):
        for fet, chg, dsg in [(0x00, False, False), (0x01, True, False),
                              (0x02, False, True), (0x03, True, True)]:
            frame = make_frame(make_payload(fet=fet))
            d = _parse_basic_info(frame)
            self.assertEqual(d["charge_fet"], chg, f"fet={fet:#04x}")
            self.assertEqual(d["discharge_fet"], dsg, f"fet={fet:#04x}")

    def test_trailing_garbage_after_end_marker_ignored(self):
        frame = make_frame(make_payload(voltage_v=48.0)) + b"\xde\xad"
        self.assertAlmostEqual(_parse_basic_info(frame)["voltage_v"], 48.0)


# ─────────────────────────────────────────────────────────────────────────────
# 5. _parse_basic_info — protection flags
# ─────────────────────────────────────────────────────────────────────────────

class TestParseProtectionFlags(unittest.TestCase):

    def test_no_faults(self):
        frame = make_frame(make_payload(protection=0))
        d = _parse_basic_info(frame)
        self.assertEqual(d["faults"], [])
        self.assertEqual(d["protection_bits"], 0)

    def test_single_fault_bits(self):
        expected = {
            0:  "Cell overvoltage",
            1:  "Cell undervoltage",
            2:  "Pack overvoltage",
            3:  "Pack undervoltage",
            4:  "Charge overtemp",
            5:  "Charge undertemp",
            6:  "Discharge overtemp",
            7:  "Discharge undertemp",
            8:  "Charge overcurrent",
            9:  "Discharge overcurrent",
            10: "Short circuit",
            11: "IC error",
            12: "MOS lock",
        }
        for bit, name in expected.items():
            frame = make_frame(make_payload(protection=1 << bit))
            d = _parse_basic_info(frame)
            self.assertEqual(d["faults"], [name], f"bit {bit}")

    def test_multiple_faults(self):
        frame = make_frame(make_payload(protection=(1 << 0) | (1 << 9)))
        d = _parse_basic_info(frame)
        self.assertEqual(sorted(d["faults"]),
                         ["Cell overvoltage", "Discharge overcurrent"])
        self.assertEqual(d["protection_bits"], 0x0201)

    def test_undefined_bits_ignored(self):
        """Bits 13-15 have no fault mapping and must not crash parsing."""
        frame = make_frame(make_payload(protection=0xE000))
        d = _parse_basic_info(frame)
        self.assertEqual(d["faults"], [])
        self.assertEqual(d["protection_bits"], 0xE000)


# ─────────────────────────────────────────────────────────────────────────────
# 6. _parse_basic_info — NTC temperatures
# ─────────────────────────────────────────────────────────────────────────────

class TestParseTemperatures(unittest.TestCase):

    def test_temperature_decoding(self):
        """0.1 K/LSB with 2731 offset: 2981 → 25.0 °C."""
        frame = make_frame(make_payload(ntc_temps=(25.0, 26.5)))
        self.assertEqual(_parse_basic_info(frame)["temp_c"], [25.0, 26.5])

    def test_negative_temperature(self):
        frame = make_frame(make_payload(ntc_temps=(-10.0,)))
        self.assertEqual(_parse_basic_info(frame)["temp_c"], [-10.0])

    def test_zero_celsius(self):
        frame = make_frame(make_payload(ntc_temps=(0.0,)))
        self.assertEqual(_parse_basic_info(frame)["temp_c"], [0.0])

    def test_no_ntc_sensors(self):
        frame = make_frame(make_payload(ntc_temps=()))
        self.assertEqual(_parse_basic_info(frame)["temp_c"], [])

    def test_ntc_count_overflow_guarded(self):
        """A lying NTC-count byte must be clamped to the bytes present."""
        frame = make_frame(make_payload(ntc_temps=(25.0, 26.0), ntc_count=8))
        self.assertEqual(_parse_basic_info(frame)["temp_c"], [25.0, 26.0])

    def test_four_sensors(self):
        temps = (20.0, 21.5, 23.0, -5.5)
        frame = make_frame(make_payload(ntc_temps=temps))
        self.assertEqual(_parse_basic_info(frame)["temp_c"], list(temps))


# ─────────────────────────────────────────────────────────────────────────────
# 7. _parse_basic_info — error paths
# ─────────────────────────────────────────────────────────────────────────────

class TestParseBasicInfoErrors(unittest.TestCase):

    def test_too_short(self):
        with self.assertRaisesRegex(ValueError, "too short"):
            _parse_basic_info(b"\xdd\x03\x00")

    def test_empty(self):
        with self.assertRaises(ValueError):
            _parse_basic_info(b"")

    def test_bad_start_byte(self):
        frame = bytearray(make_frame(make_payload()))
        frame[0] = 0x77
        with self.assertRaisesRegex(ValueError, "start byte"):
            _parse_basic_info(bytes(frame))

    def test_bms_error_status(self):
        frame = make_frame(b"\x01", status=0x80)   # ≥8B so status is reached
        with self.assertRaisesRegex(ValueError, "BMS error"):
            _parse_basic_info(frame)

    def test_truncated_packet(self):
        frame = make_frame(make_payload())
        with self.assertRaisesRegex(ValueError, "truncated"):
            _parse_basic_info(frame[:-3])

    def test_bad_end_marker(self):
        frame = make_frame(make_payload(), end=0x00)
        with self.assertRaisesRegex(ValueError, "end marker"):
            _parse_basic_info(frame)

    def test_payload_too_short(self):
        """A structurally valid frame whose payload is < 23 bytes."""
        frame = make_frame(b"\x00" * 10)
        with self.assertRaisesRegex(ValueError, "Payload too short"):
            _parse_basic_info(frame)

    def test_checksum_mismatch_warns_but_parses(self):
        """Checksum quirks in some firmware: warn, do not raise."""
        frame = make_frame(make_payload(voltage_v=51.1), corrupt_checksum=True)
        with self.assertLogs("solar_monitor.jbd", level="WARNING") as cm:
            d = _parse_basic_info(frame)
        self.assertAlmostEqual(d["voltage_v"], 51.1)
        self.assertTrue(any("checksum" in m.lower() for m in cm.output))


# ─────────────────────────────────────────────────────────────────────────────
# 8. JBDGattReader._on_notify — reassembly and resync
# ─────────────────────────────────────────────────────────────────────────────

class TestOnNotify(unittest.TestCase):

    def _reader(self):
        return JBDGattReader(FakeClient())

    def test_single_chunk_sets_event(self):
        r = self._reader()
        frame = make_frame(make_payload())
        r._on_notify(None, bytearray(frame))
        self.assertTrue(r._event.is_set())
        self.assertEqual(bytes(r._buf), frame)

    def test_multi_chunk_reassembly(self):
        """A 20-byte-MTU style chunked delivery must reassemble correctly."""
        r = self._reader()
        frame = make_frame(make_payload())
        chunks = [frame[i:i + 20] for i in range(0, len(frame), 20)]
        self.assertGreater(len(chunks), 1)
        for chunk in chunks[:-1]:
            r._on_notify(None, bytearray(chunk))
            self.assertFalse(r._event.is_set(),
                             "event must not fire before the frame completes")
        r._on_notify(None, bytearray(chunks[-1]))
        self.assertTrue(r._event.is_set())
        self.assertEqual(bytes(r._buf), frame)

    def test_leading_garbage_stripped(self):
        r = self._reader()
        frame = make_frame(make_payload())
        r._on_notify(None, bytearray(b"\x01\x02\x03" + frame))
        self.assertTrue(r._event.is_set())
        self.assertEqual(bytes(r._buf), frame)

    def test_garbage_only_chunk_clears_to_empty(self):
        r = self._reader()
        r._on_notify(None, bytearray(b"\x01\x02\x03"))
        self.assertEqual(len(r._buf), 0)
        self.assertFalse(r._event.is_set())

    def test_corrupt_length_clears_buffer_without_event(self):
        r = self._reader()
        r._on_notify(None, bytearray([0xDD, 0x03, 0x00, 0xFF, 0x01, 0x02]))
        self.assertEqual(len(r._buf), 0)
        self.assertFalse(r._event.is_set())

    def test_recovers_after_corrupt_frame(self):
        r = self._reader()
        r._on_notify(None, bytearray([0xDD, 0x03, 0x00, 0xFF]))
        self.assertEqual(len(r._buf), 0)
        frame = make_frame(make_payload())
        r._on_notify(None, bytearray(frame))
        self.assertTrue(r._event.is_set())
        self.assertEqual(bytes(r._buf), frame)

    def test_partial_header_waits(self):
        r = self._reader()
        r._on_notify(None, bytearray([0xDD, 0x03]))
        self.assertFalse(r._event.is_set())
        self.assertEqual(bytes(r._buf), bytes([0xDD, 0x03]))


# ─────────────────────────────────────────────────────────────────────────────
# 9. JBDGattReader._write / _send_recv
# ─────────────────────────────────────────────────────────────────────────────

class TestWriteAndSendRecv(unittest.TestCase):

    def test_write_with_response_when_supported(self):
        client = FakeClient()
        r = JBDGattReader(client)
        r._rx_uuid = FF02
        run(r._write(b"\x01\x02"))
        self.assertEqual(client.write_kwargs[0]["response"], True)
        self.assertEqual(client.writes[0], b"\x01\x02")

    def test_write_without_response_when_write_not_supported(self):
        services = _Services([_Service("0000ff00-0000-1000-8000-00805f9b34fb", [
            _Char(FF02, ["write-without-response"]),
        ])])
        client = FakeClient(services=services)
        r = JBDGattReader(client)
        r._rx_uuid = FF02
        run(r._write(b"\x01"))
        self.assertEqual(client.write_kwargs[0]["response"], False)

    def test_write_raises_when_char_disappeared(self):
        client = FakeClient(services=_Services([]))
        r = JBDGattReader(client)
        r._rx_uuid = FF02
        with self.assertRaisesRegex(ValueError, "disappeared"):
            run(r._write(b"\x01"))

    def test_send_recv_returns_complete_frame(self):
        frame  = make_frame(make_payload())
        client = FakeClient(reply_for=basic_reply_for(frame))
        r = make_reader(client)
        result = run(r._send_recv(BASIC_INFO_CMD))
        self.assertEqual(result, frame)

    def test_send_recv_clears_stale_buffer(self):
        """Bytes left over from a previous exchange must be discarded."""
        frame  = make_frame(make_payload())
        client = FakeClient(reply_for=basic_reply_for(frame))
        r = make_reader(client)
        r._buf.extend(b"\xde\xad\xbe\xef")
        r._event.set()
        result = run(r._send_recv(BASIC_INFO_CMD))
        self.assertEqual(result, frame)

    def test_send_recv_timeout_raises_with_diagnostics(self):
        client = FakeClient(reply_for=lambda cmd: [])   # never answers
        r = make_reader(client)
        with patch.object(jbd_mod, "READ_TIMEOUT", 0):
            with self.assertRaises(asyncio.TimeoutError) as cm:
                run(r._send_recv(BASIC_INFO_CMD))
        self.assertIn("No BMS response", str(cm.exception))

    def test_send_recv_timeout_reports_partial_buffer(self):
        """A partial frame present at timeout must appear in the message."""
        partial = bytes([0xDD, 0x03, 0x00, 0x1B, 0x14])

        def reply(cmd):
            return [partial]           # incomplete — event never set

        client = FakeClient(reply_for=reply)
        r = make_reader(client)
        with patch.object(jbd_mod, "READ_TIMEOUT", 0):
            with self.assertRaises(asyncio.TimeoutError) as cm:
                run(r._send_recv(BASIC_INFO_CMD))
        self.assertIn(partial.hex(), str(cm.exception))


# ─────────────────────────────────────────────────────────────────────────────
# 10. JBDGattReader.authenticate
# ─────────────────────────────────────────────────────────────────────────────

class TestAuthenticate(unittest.TestCase):

    def _reader_with_auth_reply(self, status=0x00):
        def reply(cmd):
            return [make_frame(b"", reg=0x06, status=status)]
        client = FakeClient(reply_for=reply)
        return make_reader(client), client

    def test_auth_command_frame_layout(self):
        """DD 5A 06 <len> <pw…> <chk> 77 for the default password."""
        r, client = self._reader_with_auth_reply()
        run(r.authenticate("0000"))
        cmd = client.writes[0]
        self.assertEqual(cmd,
                         bytes([0xDD, 0x5A, 0x06, 0x04]) + b"0000"
                         + b"\xff\x36" + bytes([0x77]))

    def test_auth_command_uses_password_length(self):
        r, client = self._reader_with_auth_reply()
        run(r.authenticate("secret"))
        cmd = client.writes[0]
        self.assertEqual(cmd[3], 6)
        self.assertEqual(cmd[4:10], b"secret")
        body = cmd[2:10]
        self.assertEqual(cmd[10:12], _checksum(body))

    def test_auth_accepted(self):
        r, _ = self._reader_with_auth_reply(status=0x00)
        run(r.authenticate("0000"))    # must not raise

    def test_auth_rejected_raises(self):
        r, _ = self._reader_with_auth_reply(status=0x80)
        with self.assertRaisesRegex(ValueError, "rejected password"):
            run(r.authenticate("0000"))

    def test_auth_reply_too_short_raises(self):
        r = JBDGattReader(FakeClient())
        with patch.object(r, "_send_recv", new=AsyncMock(return_value=b"\xdd")):
            with self.assertRaisesRegex(ValueError, "too short"):
                run(r.authenticate("0000"))


# ─────────────────────────────────────────────────────────────────────────────
# 11. _discover_chars
# ─────────────────────────────────────────────────────────────────────────────

class TestDiscoverChars(unittest.TestCase):

    def test_standard_ff00_service_matched(self):
        client = FakeClient(services=std_services())
        tx, rx = run(_discover_chars(client))
        self.assertEqual(tx, FF01)
        self.assertEqual(rx, FF02)

    def test_vatrer_ffe0_shared_characteristic(self):
        ffe1 = "0000ffe1-0000-1000-8000-00805f9b34fb"
        services = _Services([_Service("0000ffe0-0000-1000-8000-00805f9b34fb", [
            _Char(ffe1, ["notify", "write", "write-without-response"]),
        ])])
        client = FakeClient(services=services)
        tx, rx = run(_discover_chars(client))
        self.assertEqual(tx, ffe1)
        self.assertEqual(rx, ffe1)

    def test_uuid_match_is_case_insensitive(self):
        services = _Services([_Service("0000FF00-0000-1000-8000-00805F9B34FB", [
            _Char(FF01.upper(), ["notify"]),
            _Char(FF02.upper(), ["write"]),
        ])])
        client = FakeClient(services=services)
        tx, rx = run(_discover_chars(client))
        self.assertEqual(tx.lower(), FF01)
        self.assertEqual(rx.lower(), FF02)

    def test_heuristic_fallback_notify_plus_write(self):
        """Unknown service UUID with notify + write chars must still match."""
        services = _Services([_Service("12345678-0000-1000-8000-00805f9b34fb", [
            _Char("aaaa", ["notify"]),
            _Char("bbbb", ["write-without-response"]),
        ])])
        client = FakeClient(services=services)
        tx, rx = run(_discover_chars(client))
        self.assertEqual(tx, "aaaa")
        self.assertEqual(rx, "bbbb")

    def test_no_compatible_service_raises(self):
        services = _Services([_Service("12345678-0000-1000-8000-00805f9b34fb", [
            _Char("aaaa", ["read"]),
        ])])
        client = FakeClient(services=services)
        with self.assertRaisesRegex(ValueError, "No compatible JBD"):
            run(_discover_chars(client))

    def test_empty_gatt_table_raises(self):
        client = FakeClient(services=_Services([]))
        with self.assertRaises(ValueError):
            run(_discover_chars(client))

    def test_known_uuid_preferred_over_heuristic(self):
        """When both a known JBD service and a generic one exist, the known
        UUID set wins even if the generic service is listed first."""
        generic = _Service("12345678-0000-1000-8000-00805f9b34fb", [
            _Char("aaaa", ["notify"]),
            _Char("bbbb", ["write"]),
        ])
        client = FakeClient(services=_Services([generic,
                                                next(iter(std_services()))]))
        tx, rx = run(_discover_chars(client))
        self.assertEqual((tx, rx), (FF01, FF02))


# ─────────────────────────────────────────────────────────────────────────────
# 12. JBDGattReader.read_basic_info — stubbed round trip
# ─────────────────────────────────────────────────────────────────────────────

class TestReadBasicInfo(unittest.TestCase):

    def test_full_round_trip_without_password(self):
        frame  = make_frame(make_payload())
        client = FakeClient(reply_for=basic_reply_for(frame))
        r = JBDGattReader(client)
        with patch("asyncio.sleep", new=AsyncMock()):
            raw = run(r.read_basic_info())
        self.assertEqual(raw, frame)
        self.assertEqual(client.writes, [BASIC_INFO_CMD])
        self.assertEqual(client.notify_started, [FF01])
        self.assertEqual(client.notify_stopped, [FF01])

    def test_password_sends_auth_before_info(self):
        frame  = make_frame(make_payload())
        client = FakeClient(reply_for=basic_reply_for(frame))
        r = JBDGattReader(client)
        with patch("asyncio.sleep", new=AsyncMock()):
            raw = run(r.read_basic_info(password="0000"))
        self.assertEqual(raw, frame)
        self.assertEqual(len(client.writes), 2)
        self.assertEqual(client.writes[0][:3], bytes([0xDD, 0x5A, 0x06]))
        self.assertEqual(client.writes[1], BASIC_INFO_CMD)

    def test_stop_notify_called_even_on_auth_failure(self):
        def reply(cmd):
            if cmd[1] == 0x5A:
                return [make_frame(b"", reg=0x06, status=0x80)]
            return []
        client = FakeClient(reply_for=reply)
        r = JBDGattReader(client)
        with patch("asyncio.sleep", new=AsyncMock()):
            with self.assertRaises(ValueError):
                run(r.read_basic_info(password="wrong"))
        self.assertEqual(client.notify_stopped, [FF01])

    def test_stop_notify_exception_suppressed(self):
        frame  = make_frame(make_payload())
        client = FakeClient(reply_for=basic_reply_for(frame))

        async def bad_stop(uuid):
            raise RuntimeError("BlueZ already gone")

        client.stop_notify = bad_stop
        r = JBDGattReader(client)
        with patch("asyncio.sleep", new=AsyncMock()):
            raw = run(r.read_basic_info())
        self.assertEqual(raw, frame)


# ─────────────────────────────────────────────────────────────────────────────
# 13. read_jbd_device — top-level reader
# ─────────────────────────────────────────────────────────────────────────────

class _CMClient(FakeClient):
    """FakeClient usable as `async with BleakClient(...)`."""
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class TestReadJbdDevice(unittest.TestCase):

    def test_success_populates_reading(self):
        frame = make_frame(make_payload(voltage_v=53.2, current_a=-12.34,
                                        soc=87, cells=4,
                                        ntc_temps=(25.0, -10.0)))
        created = {}

        def factory(address, timeout=None):
            created["address"] = address
            return _CMClient(reply_for=basic_reply_for(frame))

        with patch.object(jbd_mod, "BleakClient", new=factory), \
             patch("asyncio.sleep", new=AsyncMock()):
            r = run(read_jbd_device("AA:BB:CC:DD:EE:FF", friendly_name="Bank1"))

        self.assertIsInstance(r, DeviceReading)
        self.assertIsNone(r.error)
        self.assertEqual(created["address"], "AA:BB:CC:DD:EE:FF")
        self.assertEqual(r.address, "AA:BB:CC:DD:EE:FF")
        self.assertEqual(r.name, "Bank1")
        self.assertEqual(r.device_type, "bms")
        self.assertAlmostEqual(r.voltage_v, 53.2)
        self.assertAlmostEqual(r.current_a, -12.34)
        self.assertEqual(r.capacity_pct, 87)
        self.assertEqual(r.cell_count, 4)
        self.assertEqual(r.temp_c, [25.0, -10.0])

    def test_mac_string_used_as_name_fallback(self):
        def factory(address, timeout=None):
            raise RuntimeError("boom")

        with patch.object(jbd_mod, "BleakClient", new=factory):
            r = run(read_jbd_device("AA:BB:CC:DD:EE:FF"))
        self.assertEqual(r.name, "AA:BB:CC:DD:EE:FF")

    def test_bledevice_input_uses_device_fields(self):
        dev = _BLEDevice(address="11:22:33:44:55:66", name="MyBMS")

        def factory(address, timeout=None):
            raise RuntimeError("boom")

        with patch.object(jbd_mod, "BleakClient", new=factory):
            r = run(read_jbd_device(dev))
        self.assertEqual(r.address, "11:22:33:44:55:66")
        self.assertEqual(r.name, "MyBMS")

    def test_exception_sets_error_not_raise(self):
        def factory(address, timeout=None):
            raise RuntimeError("connect exploded")

        with patch.object(jbd_mod, "BleakClient", new=factory):
            r = run(read_jbd_device("AA:BB:CC:DD:EE:FF"))
        self.assertEqual(r.error, "connect exploded")
        self.assertIsNone(r.voltage_v)

    def test_parse_error_sets_error(self):
        """A corrupt (short-payload) frame from the BMS becomes r.error."""
        bad_frame = make_frame(b"\x00" * 5)

        def factory(address, timeout=None):
            return _CMClient(reply_for=basic_reply_for(bad_frame))

        with patch.object(jbd_mod, "BleakClient", new=factory), \
             patch("asyncio.sleep", new=AsyncMock()):
            r = run(read_jbd_device("AA:BB:CC:DD:EE:FF"))
        self.assertIsNotNone(r.error)
        self.assertIn("too short", r.error)

    def test_timeout_sets_timeout_error(self):
        class HangingClient:
            def __init__(self, *a, **kw): pass

            async def __aenter__(self):
                await asyncio.Event().wait()   # hangs until cancelled

            async def __aexit__(self, *a):
                return False

        with patch.object(jbd_mod, "BleakClient", new=HangingClient), \
             patch.object(jbd_mod, "PER_DEVICE_TIMEOUT", 0):
            r = run(read_jbd_device("AA:BB:CC:DD:EE:FF"))
        self.assertIsNotNone(r.error)
        self.assertIn("Timed out", r.error)

    def test_timestamp_populated(self):
        def factory(address, timeout=None):
            raise RuntimeError("boom")

        with patch.object(jbd_mod, "BleakClient", new=factory):
            r = run(read_jbd_device("AA:BB:CC:DD:EE:FF"))
        self.assertTrue(r.timestamp)


if __name__ == "__main__":
    unittest.main(verbosity=2)
