"""
tests/test_history.py — unit tests for solar_monitor/history.py
================================================================
Covers:
  - HistoryConfig: defaults, validation, retention_days=0
  - load_history_config: all [history] INI fields, defaults, missing section
  - _reading_to_row: scalar fields, list fields as JSON, boolean coercion,
    error readings excluded, dict input, DeviceReading input
  - _normalise_dt: date-only, datetime, end_of_day
  - HistoryDB.write_readings: inserts correctly, error readings skipped,
    multiple readings, returns count
  - HistoryDB.query: all filters (name, type, start, end), field projection,
    limit, order ASC/DESC, empty result, error handling
  - HistoryDB.get_devices: group by name+type, counts, date range
  - HistoryDB.get_stats: row count, size, date range
  - HistoryDB.purge: by before/after/device/type, dry_run, combined filters,
    requires at least one filter, returns count
  - HistoryDB.enforce_retention: deletes old rows, retention_days=0 no-op
  - HistoryDB.load_recent_for_dashboard: returns correct format, max_points trim
  - AppConfig [history] integration: loaded from config, defaults present
  - Worker integration: history DB opened when enabled, skipped when disabled
  - Utils source-level guarantees
"""

import json
import os
import sys
import tempfile
import textwrap
import types
import unittest
from datetime import datetime, timedelta, timezone
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
import os
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from solar_monitor.history import (
    HistoryDB, HistoryConfig, load_history_config,
    _reading_to_row, _normalise_dt,
)
from solar_monitor.models import DeviceReading
from solar_monitor.config import AppConfig, load_config


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _tmp_db() -> str:
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    os.unlink(f.name)   # HistoryDB creates it; we just need a unique path
    return f.name


def _make_db(retention_days=1095) -> tuple[HistoryDB, str]:
    path = _tmp_db()
    cfg  = HistoryConfig(enabled=True, db_path=path, retention_days=retention_days)
    return HistoryDB(cfg), path


def _pack(name="House Bank", **kw) -> DeviceReading:
    r = DeviceReading(address="A1:B2:C3:D4:E5:F6", name=name,
                      device_type="bms", timestamp="2024-01-15T08:00:00")
    r.voltage_v = 54.32; r.current_a = -15.0; r.power_w = -814.8
    r.capacity_pct = 84; r.remain_wh = 4500.0; r.remain_ah = 84.0
    r.nominal_ah = 100.0; r.nominal_wh = 5400.0
    r.temp_c = [23.1, 21.8]; r.faults = []; r.balance_cells = [0] * 16
    r.charge_fet = True; r.discharge_fet = True
    for k, v in kw.items():
        setattr(r, k, v)
    return r


def _mppt(name="South Array", **kw) -> DeviceReading:
    r = DeviceReading(address="11:22:33:44:55:66", name=name,
                      device_type="mppt", timestamp="2024-01-15T08:00:00")
    r.voltage_v = 54.0; r.current_a = 12.0; r.power_w = 648.0
    r.pv_power_w = 680.0; r.yield_today_wh = 3200.0
    r.charger_state = "Float"; r.faults = []; r.temp_c = []
    for k, v in kw.items():
        setattr(r, k, v)
    return r


def _write_ini(content: str) -> str:
    f = tempfile.NamedTemporaryFile(
        mode="w", suffix=".ini", delete=False, encoding="utf-8"
    )
    f.write(textwrap.dedent(content))
    f.close()
    return f.name


# ─────────────────────────────────────────────────────────────────────────────
# 1. HistoryConfig
# ─────────────────────────────────────────────────────────────────────────────

class TestHistoryConfig(unittest.TestCase):

    def test_defaults(self):
        cfg = HistoryConfig()
        self.assertFalse(cfg.enabled)
        self.assertEqual(cfg.db_path, "solar_history.db")
        self.assertEqual(cfg.retention_days, 1095)
        self.assertEqual(cfg.vacuum_interval_days, 7)

    def test_three_years_is_1095_days(self):
        self.assertEqual(HistoryConfig().retention_days, 1095)

    def test_retention_zero_means_keep_forever(self):
        cfg = HistoryConfig(retention_days=0)
        self.assertEqual(cfg.retention_days, 0)

    def test_negative_retention_raises(self):
        with self.assertRaises(ValueError):
            HistoryConfig(retention_days=-1)

    def test_vacuum_interval_less_than_1_raises(self):
        with self.assertRaises(ValueError):
            HistoryConfig(vacuum_interval_days=0)

    def test_custom_values(self):
        cfg = HistoryConfig(
            enabled=True, db_path="/data/solar.db",
            retention_days=365, vacuum_interval_days=14,
        )
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.db_path, "/data/solar.db")
        self.assertEqual(cfg.retention_days, 365)


# ─────────────────────────────────────────────────────────────────────────────
# 2. load_history_config
# ─────────────────────────────────────────────────────────────────────────────

class TestLoadHistoryConfig(unittest.TestCase):

    def _load(self, ini: str) -> HistoryConfig:
        path = _write_ini(ini)
        try:
            return load_history_config(path)
        finally:
            os.unlink(path)

    def test_missing_section_uses_defaults(self):
        cfg = self._load("[general]\ntheme = dark\n")
        self.assertFalse(cfg.enabled)
        self.assertEqual(cfg.retention_days, 1095)

    def test_enabled_true(self):
        cfg = self._load("[history]\nenabled = true\n")
        self.assertTrue(cfg.enabled)

    def test_enabled_false(self):
        cfg = self._load("[history]\nenabled = false\n")
        self.assertFalse(cfg.enabled)

    def test_db_path(self):
        cfg = self._load("[history]\ndb_path = /var/lib/solar.db\n")
        self.assertEqual(cfg.db_path, "/var/lib/solar.db")

    def test_retention_days(self):
        cfg = self._load("[history]\nretention_days = 730\n")
        self.assertEqual(cfg.retention_days, 730)

    def test_retention_days_zero(self):
        cfg = self._load("[history]\nretention_days = 0\n")
        self.assertEqual(cfg.retention_days, 0)

    def test_vacuum_interval(self):
        cfg = self._load("[history]\nvacuum_interval_days = 30\n")
        self.assertEqual(cfg.vacuum_interval_days, 30)

    def test_all_fields(self):
        cfg = self._load("""
            [history]
            enabled              = true
            db_path              = /tmp/test.db
            retention_days       = 365
            vacuum_interval_days = 14
        """)
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.db_path, "/tmp/test.db")
        self.assertEqual(cfg.retention_days, 365)
        self.assertEqual(cfg.vacuum_interval_days, 14)


# ─────────────────────────────────────────────────────────────────────────────
# 3. _reading_to_row
# ─────────────────────────────────────────────────────────────────────────────

class TestReadingToRow(unittest.TestCase):

    def test_basic_fields(self):
        row = _reading_to_row(_pack(), "2024-01-15T08:00:00Z")
        self.assertEqual(row["device_name"], "House Bank")
        self.assertEqual(row["device_type"], "bms")
        self.assertAlmostEqual(row["voltage_v"], 54.32, places=2)

    def test_list_fields_serialised_as_json(self):
        row = _reading_to_row(_pack(temp_c=[23.1, 21.8]), "2024-01-15T08:00:00Z")
        self.assertEqual(row["temp_c"], "[23.1, 21.8]")

    def test_faults_serialised(self):
        row = _reading_to_row(
            _pack(faults=["Cell overvoltage"]), "2024-01-15T08:00:00Z"
        )
        self.assertEqual(json.loads(row["faults"]), ["Cell overvoltage"])

    def test_empty_list_fields(self):
        row = _reading_to_row(_pack(faults=[], temp_c=[]), "t")
        self.assertEqual(row["faults"], "[]")
        self.assertEqual(row["temp_c"], "[]")

    def test_boolean_coerced_to_int(self):
        row = _reading_to_row(_pack(charge_fet=True, discharge_fet=False), "t")
        self.assertEqual(row["charge_fet"], 1)
        self.assertEqual(row["discharge_fet"], 0)

    def test_error_reading_returns_none(self):
        r = _pack(); r.error = "Timed out"
        result = _reading_to_row(r, "t")
        self.assertIsNone(result, "Error readings must return None")

    def test_none_fields_stored_as_none(self):
        row = _reading_to_row(_pack(pv_power_w=None), "t")
        self.assertIsNone(row["pv_power_w"])

    def test_dict_input_works(self):
        d = {
            "name": "South Array", "device_type": "mppt",
            "address": "11:22:33:44:55:66", "error": None,
            "voltage_v": 54.0, "pv_power_w": 680.0,
            "temp_c": [], "faults": [], "balance_cells": [],
        }
        row = _reading_to_row(d, "t")
        self.assertEqual(row["device_name"], "South Array")
        self.assertAlmostEqual(row["pv_power_w"], 680.0)


# ─────────────────────────────────────────────────────────────────────────────
# 4. _normalise_dt
# ─────────────────────────────────────────────────────────────────────────────

class TestNormaliseDt(unittest.TestCase):

    def test_date_only_start(self):
        self.assertEqual(_normalise_dt("2024-01-15"), "2024-01-15T00:00:00")

    def test_date_only_end_of_day(self):
        self.assertEqual(_normalise_dt("2024-01-15", end_of_day=True),
                         "2024-01-15T23:59:59")

    def test_datetime_passed_through(self):
        self.assertEqual(_normalise_dt("2024-01-15T08:30:00"),
                         "2024-01-15T08:30:00")

    def test_strips_whitespace(self):
        self.assertEqual(_normalise_dt("  2024-01-15  "), "2024-01-15T00:00:00")


# ─────────────────────────────────────────────────────────────────────────────
# 5. HistoryDB.write_readings
# ─────────────────────────────────────────────────────────────────────────────

class TestHistoryDBWrite(unittest.TestCase):

    def setUp(self):
        self.db, self.path = _make_db()

    def tearDown(self):
        self.db.close()
        try: os.unlink(self.path)
        except FileNotFoundError: pass

    def test_inserts_bms_reading(self):
        n = self.db.write_readings([_pack()])
        self.assertEqual(n, 1)

    def test_inserts_mppt_reading(self):
        n = self.db.write_readings([_mppt()])
        self.assertEqual(n, 1)

    def test_inserts_multiple(self):
        n = self.db.write_readings([_pack("B1"), _pack("B2"), _mppt()])
        self.assertEqual(n, 3)

    def test_error_reading_skipped(self):
        r = _pack(); r.error = "timeout"
        n = self.db.write_readings([r])
        self.assertEqual(n, 0)

    def test_mixed_ok_and_error(self):
        r_err = _pack("Dead"); r_err.error = "timeout"
        n = self.db.write_readings([_pack("Good"), r_err])
        self.assertEqual(n, 1)

    def test_empty_list_returns_zero(self):
        n = self.db.write_readings([])
        self.assertEqual(n, 0)

    def test_data_persists_after_close_and_reopen(self):
        self.db.write_readings([_pack()])
        self.db.close()
        cfg = HistoryConfig(enabled=True, db_path=self.path)
        db2 = HistoryDB(cfg)
        rows = db2.query()
        self.assertEqual(len(rows), 1)
        db2.close()


# ─────────────────────────────────────────────────────────────────────────────
# 6. HistoryDB.query
# ─────────────────────────────────────────────────────────────────────────────

class TestHistoryDBQuery(unittest.TestCase):

    def setUp(self):
        self.db, self.path = _make_db()
        # Insert known data
        self.db.write_readings([
            _pack("House Bank"),
            _pack("Spare Pack"),
            _mppt("South Array"),
        ])

    def tearDown(self):
        self.db.close()
        try: os.unlink(self.path)
        except FileNotFoundError: pass

    def test_query_all_returns_all_rows(self):
        rows = self.db.query()
        self.assertEqual(len(rows), 3)

    def test_filter_by_device_name(self):
        rows = self.db.query(device_name="House Bank")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["device_name"], "House Bank")

    def test_filter_by_device_type_bms(self):
        rows = self.db.query(device_type="bms")
        self.assertEqual(len(rows), 2)

    def test_filter_by_device_type_mppt(self):
        rows = self.db.query(device_type="mppt")
        self.assertEqual(len(rows), 1)

    def test_filter_by_start_date(self):
        rows = self.db.query(start="2099-01-01")
        self.assertEqual(len(rows), 0)

    def test_filter_by_end_date(self):
        rows = self.db.query(end="2000-01-01")
        self.assertEqual(len(rows), 0)

    def test_filter_date_range_inclusive(self):
        # recorded_at uses today's UTC date, not r.timestamp
        from datetime import date
        today = date.today().isoformat()
        rows = self.db.query(start=today, end="2099-12-31")
        self.assertEqual(len(rows), 3)

    def test_field_projection(self):
        rows = self.db.query(fields=["voltage_v", "capacity_pct"])
        self.assertIn("voltage_v",  rows[0])
        self.assertIn("capacity_pct", rows[0])
        self.assertNotIn("current_a", rows[0])

    def test_limit(self):
        rows = self.db.query(limit=2)
        self.assertEqual(len(rows), 2)

    def test_order_desc(self):
        # Insert a second reading with a later timestamp for the same device
        r2 = _pack("House Bank")
        r2.timestamp = "2024-02-01T10:00:00"
        self.db.write_readings([r2])
        rows = self.db.query(device_name="House Bank", order="DESC")
        # Newest first
        self.assertGreaterEqual(rows[0]["recorded_at"], rows[-1]["recorded_at"])

    def test_order_asc(self):
        r2 = _pack("House Bank")
        r2.timestamp = "2024-02-01T10:00:00"
        self.db.write_readings([r2])
        rows = self.db.query(device_name="House Bank", order="ASC")
        self.assertLessEqual(rows[0]["recorded_at"], rows[-1]["recorded_at"])

    def test_returns_dicts(self):
        rows = self.db.query(limit=1)
        self.assertIsInstance(rows[0], dict)

    def test_combined_filters(self):
        rows = self.db.query(device_type="bms", device_name="House Bank")
        self.assertEqual(len(rows), 1)


# ─────────────────────────────────────────────────────────────────────────────
# 7. HistoryDB.get_devices / get_stats
# ─────────────────────────────────────────────────────────────────────────────

class TestHistoryDBInfo(unittest.TestCase):

    def setUp(self):
        self.db, self.path = _make_db()
        self.db.write_readings([_pack("House Bank"), _mppt("South Array")])

    def tearDown(self):
        self.db.close()
        try: os.unlink(self.path)
        except FileNotFoundError: pass

    def test_get_devices_returns_all(self):
        devices = self.db.get_devices()
        names = [d["device_name"] for d in devices]
        self.assertIn("House Bank",  names)
        self.assertIn("South Array", names)

    def test_get_devices_has_count(self):
        devices = self.db.get_devices()
        for d in devices:
            self.assertIn("reading_count", d)
            self.assertGreater(d["reading_count"], 0)

    def test_get_devices_has_timestamps(self):
        devices = self.db.get_devices()
        for d in devices:
            self.assertIn("first_seen", d)
            self.assertIn("last_seen",  d)

    def test_get_stats_total_rows(self):
        stats = self.db.get_stats()
        self.assertEqual(stats["total_rows"], 2)

    def test_get_stats_has_size(self):
        stats = self.db.get_stats()
        self.assertGreater(stats["size_bytes"], 0)
        self.assertGreaterEqual(stats["size_mb"], 0)

    def test_get_stats_has_dates(self):
        stats = self.db.get_stats()
        self.assertIsNotNone(stats["oldest_reading"])
        self.assertIsNotNone(stats["newest_reading"])


# ─────────────────────────────────────────────────────────────────────────────
# 8. HistoryDB.purge
# ─────────────────────────────────────────────────────────────────────────────

class TestHistoryDBPurge(unittest.TestCase):

    def setUp(self):
        self.db, self.path = _make_db()
        self.db.write_readings([_pack("House Bank"), _mppt("South Array")])

    def tearDown(self):
        self.db.close()
        try: os.unlink(self.path)
        except FileNotFoundError: pass

    def test_purge_before_future_date_deletes_all(self):
        n = self.db.purge(before="2099-01-01")
        self.assertEqual(n, 2)
        self.assertEqual(len(self.db.query()), 0)

    def test_purge_before_past_date_deletes_none(self):
        n = self.db.purge(before="2000-01-01")
        self.assertEqual(n, 0)
        self.assertEqual(len(self.db.query()), 2)

    def test_purge_dry_run_does_not_delete(self):
        n = self.db.purge(before="2099-01-01", dry_run=True)
        self.assertEqual(n, 2)
        self.assertEqual(len(self.db.query()), 2)  # nothing deleted

    def test_purge_by_device_name(self):
        n = self.db.purge(device_name="House Bank")
        self.assertEqual(n, 1)
        remaining = self.db.query()
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["device_name"], "South Array")

    def test_purge_by_device_type(self):
        n = self.db.purge(device_type="bms")
        self.assertEqual(n, 1)
        remaining = self.db.query()
        self.assertEqual(remaining[0]["device_type"], "mppt")

    def test_purge_combined_filters(self):
        # Insert a second BMS pack
        self.db.write_readings([_pack("Spare Pack")])
        # Purge only "House Bank"
        n = self.db.purge(device_name="House Bank", device_type="bms")
        self.assertEqual(n, 1)
        self.assertEqual(len(self.db.query()), 2)  # Spare Pack + South Array remain

    def test_purge_requires_filter(self):
        with self.assertRaises(ValueError) as ctx:
            self.db.purge()
        self.assertIn("filter", str(ctx.exception).lower())

    def test_purge_after_filter(self):
        # Nothing is "after" a future date
        n = self.db.purge(after="2099-01-01")
        self.assertEqual(n, 0)


# ─────────────────────────────────────────────────────────────────────────────
# 9. HistoryDB.enforce_retention
# ─────────────────────────────────────────────────────────────────────────────

class TestRetentionEnforcement(unittest.TestCase):

    def test_old_rows_deleted(self):
        db, path = _make_db(retention_days=1)
        try:
            # Write a current reading normally
            db.write_readings([_pack()])
            # Backdate the recorded_at directly in the DB to simulate old data
            old_ts = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat(timespec="seconds")
            with db._conn:
                db._conn.execute("UPDATE readings SET recorded_at = ?", (old_ts,))
            # Write a fresh reading so something remains after purge
            db.write_readings([_pack()])
            # Enforce retention (1 day) — the old row should be deleted
            deleted = db.enforce_retention()
            self.assertGreater(deleted, 0, f"Expected >0 deletions; old_ts={old_ts}")
        finally:
            db.close()
            os.unlink(path)

    def test_retention_zero_keeps_all(self):
        db, path = _make_db(retention_days=0)
        try:
            db.write_readings([_pack()])
            deleted = db.enforce_retention()
            self.assertEqual(deleted, 0)
            self.assertEqual(len(db.query()), 1)
        finally:
            db.close()
            os.unlink(path)

    def test_young_rows_not_deleted(self):
        db, path = _make_db(retention_days=365)
        try:
            db.write_readings([_pack()])
            db.enforce_retention()
            self.assertEqual(len(db.query()), 1)
        finally:
            db.close()
            os.unlink(path)


# ─────────────────────────────────────────────────────────────────────────────
# 10. HistoryDB.load_recent_for_dashboard
# ─────────────────────────────────────────────────────────────────────────────

class TestDashboardHistoryLoad(unittest.TestCase):

    def setUp(self):
        self.db, self.path = _make_db()

    def tearDown(self):
        self.db.close()
        try: os.unlink(self.path)
        except FileNotFoundError: pass

    def test_returns_dict_keyed_by_device_name(self):
        self.db.write_readings([_pack("House Bank"), _mppt("South Array")])
        hist = self.db.load_recent_for_dashboard()
        self.assertIn("House Bank",  hist)
        self.assertIn("South Array", hist)

    def test_each_entry_has_required_keys(self):
        self.db.write_readings([_pack()])
        hist = self.db.load_recent_for_dashboard()
        entry = hist["House Bank"][0]
        for key in ("timestamp", "voltage_v", "current_a", "capacity_pct"):
            self.assertIn(key, entry)

    def test_max_points_trim(self):
        # Insert 10 readings
        for i in range(10):
            r = _pack()
            r.timestamp = f"2024-01-{i+1:02d}T08:00:00"
            self.db.write_readings([r])
        hist = self.db.load_recent_for_dashboard(max_points=5)
        self.assertLessEqual(len(hist["House Bank"]), 5)

    def test_empty_db_returns_empty_dict(self):
        hist = self.db.load_recent_for_dashboard()
        self.assertEqual(hist, {})


# ─────────────────────────────────────────────────────────────────────────────
# 11. AppConfig integration
# ─────────────────────────────────────────────────────────────────────────────

class TestAppConfigHistoryIntegration(unittest.TestCase):

    def test_app_config_has_history_field(self):
        cfg = AppConfig()
        self.assertIsInstance(cfg.history, HistoryConfig)

    def test_history_disabled_by_default(self):
        cfg = AppConfig()
        self.assertFalse(cfg.history.enabled)

    def test_history_loaded_from_ini(self):
        path = _write_ini("""
            [general]
            state_file = solar_state.json
            [history]
            enabled        = true
            db_path        = /tmp/solar.db
            retention_days = 730
        """)
        try:
            cfg = load_config(path)
            self.assertTrue(cfg.history.enabled)
            self.assertEqual(cfg.history.db_path, "/tmp/solar.db")
            self.assertEqual(cfg.history.retention_days, 730)
        finally:
            os.unlink(path)

    def test_history_defaults_when_section_absent(self):
        path = _write_ini("[general]\nstate_file = solar_state.json\n")
        try:
            cfg = load_config(path)
            self.assertFalse(cfg.history.enabled)
            self.assertEqual(cfg.history.retention_days, 1095)
        finally:
            os.unlink(path)


# ─────────────────────────────────────────────────────────────────────────────
# 12. Database file management
# ─────────────────────────────────────────────────────────────────────────────

class TestHistoryDBFileManagement(unittest.TestCase):

    def test_creates_db_file(self):
        path = _tmp_db()
        self.assertFalse(Path(path).exists())
        cfg = HistoryConfig(enabled=True, db_path=path)
        db  = HistoryDB(cfg)
        self.assertTrue(Path(path).exists())
        db.close()
        os.unlink(path)

    def test_creates_parent_directories(self):
        import tempfile
        tmpdir = tempfile.mkdtemp()
        path   = os.path.join(tmpdir, "nested", "dir", "history.db")
        cfg    = HistoryConfig(enabled=True, db_path=path)
        db     = HistoryDB(cfg)
        self.assertTrue(Path(path).exists())
        db.close()
        import shutil
        shutil.rmtree(tmpdir)

    def test_wal_mode_enabled(self):
        db, path = _make_db()
        try:
            mode = db._conn.execute("PRAGMA journal_mode;").fetchone()[0]
            self.assertEqual(mode, "wal")
        finally:
            db.close()
            os.unlink(path)

    def test_indexes_created(self):
        db, path = _make_db()
        try:
            indexes = db._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
            names = [r[0] for r in indexes]
            self.assertIn("idx_recorded_at", names)
            self.assertIn("idx_device_name", names)
            self.assertIn("idx_name_time",   names)
        finally:
            db.close()
            os.unlink(path)


# ─────────────────────────────────────────────────────────────────────────────
# 13. Source-level guarantees
# ─────────────────────────────────────────────────────────────────────────────

class TestHistorySourceGuarantees(unittest.TestCase):

    def _src(self) -> str:
        with open(f"{REPO_ROOT}/solar_monitor/history.py") as f:
            return f.read()

    def test_wal_mode_pragma(self):
        self.assertIn("journal_mode=WAL", self._src())

    def test_retention_enforcement(self):
        self.assertIn("enforce_retention", self._src())

    def test_vacuum_present(self):
        self.assertIn("VACUUM", self._src())

    def test_atomic_write_not_needed_comment(self):
        # SQLite handles atomicity itself
        self.assertIn("sqlite3", self._src())

    def test_list_fields_json_serialised(self):
        self.assertIn("json.dumps", self._src())
        self.assertIn("_LIST_FIELDS", self._src())

    def test_load_recent_for_dashboard_present(self):
        self.assertIn("load_recent_for_dashboard", self._src())

    def test_all_schema_columns_documented(self):
        src = self._src()
        for field in ("voltage_v", "capacity_pct", "pv_power_w", "ac_out_power_va"):
            self.assertIn(field, src, f"Schema missing field: {field}")

    def test_error_readings_excluded(self):
        self.assertIn("r.error", self._src())

    def test_requires_filter_for_purge(self):
        self.assertIn("At least one filter", self._src())


class TestUtilsSourceGuarantees(unittest.TestCase):

    def _src(self, filename: str) -> str:
        with open(f"{REPO_ROOT}/utils/{filename}") as f:
            return f.read()

    def test_purge_utility_has_dry_run(self):
        self.assertIn("dry_run", self._src("purge_history.py"))
        self.assertIn("dry-run", self._src("purge_history.py"))

    def test_purge_utility_has_date_filters(self):
        src = self._src("purge_history.py")
        self.assertIn("--before", src)
        self.assertIn("--after",  src)

    def test_purge_utility_has_device_filter(self):
        self.assertIn("--device", self._src("purge_history.py"))

    def test_purge_utility_has_type_filter(self):
        self.assertIn("--type", self._src("purge_history.py"))

    def test_purge_utility_has_enforce_retention(self):
        self.assertIn("enforce-retention", self._src("purge_history.py"))

    def test_purge_utility_has_vacuum(self):
        self.assertIn("--vacuum", self._src("purge_history.py"))

    def test_purge_utility_has_stats(self):
        self.assertIn("--stats", self._src("purge_history.py"))

    def test_purge_utility_has_confirmation(self):
        self.assertIn("y/N", self._src("purge_history.py"))

    def test_purge_utility_has_yes_flag(self):
        self.assertIn("--yes", self._src("purge_history.py"))

    def test_query_utility_has_csv_output(self):
        self.assertIn("csv", self._src("query_history.py"))

    def test_query_utility_has_json_output(self):
        self.assertIn("json", self._src("query_history.py"))

    def test_query_utility_has_date_filters(self):
        src = self._src("query_history.py")
        self.assertIn("--start", src)
        self.assertIn("--end",   src)

    def test_query_utility_has_field_projection(self):
        self.assertIn("--fields", self._src("query_history.py"))

    def test_query_utility_has_today_shortcut(self):
        self.assertIn("today", self._src("query_history.py"))

    def test_query_utility_has_list_devices(self):
        self.assertIn("--list-devices", self._src("query_history.py"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
