"""
tests/test_history_utils.py — unit tests for utils/purge_history.py and
utils/query_history.py
========================================================================
Covers:
  - query_history.main(): table / csv / json output, field projection,
    device / type / start / end filters, 'today' and 'yesterday' shortcuts,
    --limit, --order, --stats, --list-devices, --list-fields, --db override,
    empty results per format, invalid --fields, missing config / db,
    argparse choice validation
  - purge_history.main(): --dry-run, --before / --after / --device / --type
    filters (rows older than cutoff removed, newer kept), combined filters,
    confirmation prompt (accept / decline), --yes, --vacuum, --stats,
    --list-devices, --enforce-retention (delete / dry-run / retention 0),
    --db override, disabled-history warning, empty db, partial legacy
    schema, missing config / db, argparse choice validation

All tests use temporary SQLite databases built with the real schema from
solar_monitor.history.HistoryDB, populated with known rows via raw SQL so
recorded_at values are fully deterministic (no reliance on write-time
timestamps except the explicitly relative retention tests).
"""

import contextlib
import csv as csv_mod
import importlib.util
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from solar_monitor.history import HistoryDB, HistoryConfig


def _load_util(filename: str, mod_name: str):
    spec = importlib.util.spec_from_file_location(
        mod_name, os.path.join(REPO_ROOT, "utils", filename)
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


query_history = _load_util("query_history.py", "_test_query_history_util")
purge_history = _load_util("purge_history.py", "_test_purge_history_util")


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _tmp_path(suffix: str) -> str:
    f = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    f.close()
    os.unlink(f.name)
    return f.name


def _cleanup(*paths: str) -> None:
    for p in paths:
        for candidate in (p, p + "-wal", p + "-shm", p + ".tmp"):
            try:
                os.unlink(candidate)
            except (FileNotFoundError, TypeError):
                pass


def _make_schema_db() -> str:
    """Create an empty database with the real production schema."""
    path = _tmp_path(".db")
    cfg = HistoryConfig(enabled=True, db_path=path, retention_days=0)
    HistoryDB(cfg).close()
    return path


def _row(recorded_at: str, name: str = "House Bank", dtype: str = "bms",
         **kw) -> dict:
    d = {
        "recorded_at": recorded_at,
        "device_name": name,
        "device_type": dtype,
        "address":     "AA:BB:CC:DD:EE:FF",
        "voltage_v":   54.0,
        "current_a":   -10.0,
        "power_w":     -540.0,
        "capacity_pct": 80,
    }
    d.update(kw)
    return d


def _insert_rows(db_path: str, rows: list[dict]) -> None:
    conn = sqlite3.connect(db_path)
    try:
        with conn:
            for r in rows:
                cols = list(r.keys())
                sql = (f"INSERT INTO readings ({', '.join(cols)}) "
                       f"VALUES ({', '.join('?' for _ in cols)})")
                conn.execute(sql, [r[c] for c in cols])
    finally:
        conn.close()


def _make_db(rows: list[dict]) -> str:
    path = _make_schema_db()
    _insert_rows(path, rows)
    return path


def _write_config(db_path: str, retention_days: int = 0,
                  enabled: bool = True) -> str:
    path = _tmp_path(".ini")
    with open(path, "w", encoding="utf-8") as f:
        f.write("[history]\n")
        f.write(f"enabled = {'true' if enabled else 'false'}\n")
        f.write(f"db_path = {db_path}\n")
        f.write(f"retention_days = {retention_days}\n")
    return path


def _count_rows(db_path: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
    finally:
        conn.close()


def _remaining_names(db_path: str) -> set:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT DISTINCT device_name FROM readings").fetchall()
        return {r[0] for r in rows}
    finally:
        conn.close()


def _run_main(mod, argv: list[str], input_response: str = None):
    """Run mod.main() with patched argv, returning (stdout, stderr, exit_code)."""
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with patch.object(sys, "argv", ["prog"] + argv), \
         contextlib.redirect_stdout(out), \
         contextlib.redirect_stderr(err):
        try:
            if input_response is not None:
                with patch("builtins.input", return_value=input_response):
                    mod.main()
            else:
                mod.main()
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
    return out.getvalue(), err.getvalue(), code


# Deterministic fixture rows shared by the query tests
_QUERY_ROWS = [
    _row("2024-01-10T08:00:00", "House Bank",  "bms",  voltage_v=53.5,
         capacity_pct=82),
    _row("2024-01-15T08:00:00", "House Bank",  "bms",  voltage_v=54.32,
         capacity_pct=84),
    _row("2024-01-15T09:00:00", "South Array", "mppt", voltage_v=54.1,
         capacity_pct=None, pv_power_w=680.0, charger_state="Bulk"),
    _row("2024-02-01T10:00:00", "House Bank",  "bms",  voltage_v=55.0,
         capacity_pct=90),
]


# ─────────────────────────────────────────────────────────────────────────────
# 1. query_history — error / info paths
# ─────────────────────────────────────────────────────────────────────────────

class TestQueryHistoryErrorsAndInfo(unittest.TestCase):

    def setUp(self):
        self.db_path = _make_db(_QUERY_ROWS)
        self.cfg_path = _write_config(self.db_path)

    def tearDown(self):
        _cleanup(self.db_path, self.cfg_path)

    def test_missing_config_exits_1(self):
        out, err, code = _run_main(query_history,
                                   ["--config", "/nonexistent/nope.ini"])
        self.assertEqual(code, 1)
        self.assertIn("config file not found", err)

    def test_missing_db_exits_1(self):
        cfg = _write_config("/nonexistent/nope.db")
        try:
            out, err, code = _run_main(query_history, ["--config", cfg])
            self.assertEqual(code, 1)
            self.assertIn("database not found", err)
        finally:
            _cleanup(cfg)

    def test_list_fields_needs_no_db(self):
        # --list-fields returns before config/db are touched
        out, err, code = _run_main(
            query_history,
            ["--config", "/nonexistent/nope.ini", "--list-fields"])
        self.assertEqual(code, 0)
        self.assertIn("recorded_at", out)
        self.assertIn("voltage_v",   out)
        self.assertIn("temp_c",      out)

    def test_stats_action(self):
        out, err, code = _run_main(query_history,
                                   ["--config", self.cfg_path, "--stats"])
        self.assertEqual(code, 0)
        self.assertIn("total_rows", out)
        self.assertIn("4", out)
        self.assertIn("oldest_reading", out)

    def test_list_devices_action(self):
        out, err, code = _run_main(query_history,
                                   ["--config", self.cfg_path, "--list-devices"])
        self.assertEqual(code, 0)
        self.assertIn("House Bank",  out)
        self.assertIn("South Array", out)
        self.assertIn("mppt", out)

    def test_invalid_fields_exits_1(self):
        out, err, code = _run_main(
            query_history,
            ["--config", self.cfg_path, "--fields", "voltage_v,bogus_col"])
        self.assertEqual(code, 1)
        self.assertIn("unknown field", err)
        self.assertIn("bogus_col", err)

    def test_db_override_flag(self):
        cfg = _write_config("/nonexistent/other.db")
        try:
            out, err, code = _run_main(
                query_history,
                ["--config", cfg, "--db", self.db_path, "--format", "json"])
            self.assertEqual(code, 0)
            self.assertEqual(len(json.loads(out)), 4)
        finally:
            _cleanup(cfg)


# ─────────────────────────────────────────────────────────────────────────────
# 2. query_history — filtering
# ─────────────────────────────────────────────────────────────────────────────

class TestQueryHistoryFiltering(unittest.TestCase):

    def setUp(self):
        self.db_path = _make_db(_QUERY_ROWS)
        self.cfg_path = _write_config(self.db_path)

    def tearDown(self):
        _cleanup(self.db_path, self.cfg_path)

    def _json(self, *argv):
        out, err, code = _run_main(
            query_history,
            ["--config", self.cfg_path, "--format", "json"] + list(argv))
        self.assertEqual(code, 0, msg=err)
        return json.loads(out) if out.strip() else []

    def test_no_filters_returns_all(self):
        self.assertEqual(len(self._json()), 4)

    def test_filter_by_device_name(self):
        rows = self._json("--device", "South Array")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["device_type"], "mppt")
        self.assertAlmostEqual(rows[0]["pv_power_w"], 680.0)

    def test_filter_by_device_type(self):
        rows = self._json("--type", "bms")
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(r["device_type"] == "bms" for r in rows))

    def test_start_filter(self):
        rows = self._json("--start", "2024-01-15")
        self.assertEqual(len(rows), 3)   # excludes the 2024-01-10 row

    def test_end_filter_inclusive_whole_day(self):
        rows = self._json("--end", "2024-01-15")
        self.assertEqual(len(rows), 3)   # includes both 2024-01-15 rows

    def test_start_end_range(self):
        rows = self._json("--start", "2024-01-15", "--end", "2024-01-15")
        self.assertEqual(len(rows), 2)

    def test_limit(self):
        rows = self._json("--limit", "2")
        self.assertEqual(len(rows), 2)

    def test_order_asc_is_default(self):
        rows = self._json()
        self.assertEqual(rows[0]["recorded_at"],  "2024-01-10T08:00:00")
        self.assertEqual(rows[-1]["recorded_at"], "2024-02-01T10:00:00")

    def test_order_desc(self):
        rows = self._json("--order", "desc")
        self.assertEqual(rows[0]["recorded_at"],  "2024-02-01T10:00:00")
        self.assertEqual(rows[-1]["recorded_at"], "2024-01-10T08:00:00")

    def test_combined_filters(self):
        rows = self._json("--device", "House Bank", "--type", "bms",
                          "--start", "2024-01-15")
        self.assertEqual(len(rows), 2)

    def test_today_shortcut(self):
        today_iso = date.today().isoformat()
        db = _make_db([
            _row("2024-01-01T00:00:00", "Old Pack"),
            _row(f"{today_iso}T06:00:00", "Fresh Pack"),
        ])
        cfg = _write_config(db)
        try:
            out, err, code = _run_main(
                query_history,
                ["--config", cfg, "--start", "today", "--format", "json"])
            self.assertEqual(code, 0)
            rows = json.loads(out)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["device_name"], "Fresh Pack")
        finally:
            _cleanup(db, cfg)

    def test_yesterday_shortcut(self):
        today_iso = date.today().isoformat()
        db = _make_db([
            _row("2024-01-01T00:00:00", "Old Pack"),
            _row(f"{today_iso}T06:00:00", "Fresh Pack"),
        ])
        cfg = _write_config(db)
        try:
            out, err, code = _run_main(
                query_history,
                ["--config", cfg, "--end", "yesterday", "--format", "json"])
            self.assertEqual(code, 0)
            rows = json.loads(out)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["device_name"], "Old Pack")
        finally:
            _cleanup(db, cfg)


# ─────────────────────────────────────────────────────────────────────────────
# 3. query_history — output formats
# ─────────────────────────────────────────────────────────────────────────────

class TestQueryHistoryOutputFormats(unittest.TestCase):

    def setUp(self):
        self.db_path = _make_db(_QUERY_ROWS)
        self.cfg_path = _write_config(self.db_path)

    def tearDown(self):
        _cleanup(self.db_path, self.cfg_path)

    def test_table_output(self):
        out, err, code = _run_main(query_history, ["--config", self.cfg_path])
        self.assertEqual(code, 0)
        self.assertIn("4 row(s)", out)
        self.assertIn("House Bank", out)
        self.assertIn("recorded_at", out)
        self.assertIn("54.32", out)

    def test_table_hides_all_none_columns(self):
        # No inverter rows exist, so inverter_state must not appear as a column
        out, err, code = _run_main(query_history, ["--config", self.cfg_path])
        self.assertEqual(code, 0)
        self.assertNotIn("inverter_state", out)
        self.assertIn("pv_power_w", out)   # present on the mppt row

    def test_csv_output_all_columns(self):
        out, err, code = _run_main(
            query_history, ["--config", self.cfg_path, "--format", "csv"])
        self.assertEqual(code, 0)
        rows = list(csv_mod.reader(io.StringIO(out)))
        header, data = rows[0], rows[1:]
        self.assertIn("recorded_at", header)
        self.assertIn("device_name", header)
        self.assertIn("voltage_v",   header)
        self.assertEqual(len(data), 4)

    def test_csv_output_selected_fields(self):
        out, err, code = _run_main(
            query_history,
            ["--config", self.cfg_path, "--format", "csv",
             "--fields", "recorded_at,voltage_v"])
        self.assertEqual(code, 0)
        rows = list(csv_mod.reader(io.StringIO(out)))
        self.assertEqual(rows[0], ["recorded_at", "voltage_v"])
        self.assertEqual(len(rows) - 1, 4)
        self.assertEqual(rows[1], ["2024-01-10T08:00:00", "53.5"])

    def test_json_output_values(self):
        out, err, code = _run_main(
            query_history,
            ["--config", self.cfg_path, "--format", "json",
             "--device", "House Bank", "--limit", "1"])
        self.assertEqual(code, 0)
        rows = json.loads(out)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["device_name"], "House Bank")
        self.assertAlmostEqual(rows[0]["voltage_v"], 53.5, places=2)
        self.assertEqual(rows[0]["capacity_pct"], 82)

    def test_empty_result_table(self):
        out, err, code = _run_main(
            query_history,
            ["--config", self.cfg_path, "--device", "No Such Device"])
        self.assertEqual(code, 0)
        self.assertIn("No results found.", out)

    def test_empty_result_json(self):
        out, err, code = _run_main(
            query_history,
            ["--config", self.cfg_path, "--device", "No Such Device",
             "--format", "json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), [])

    def test_empty_result_csv_is_silent(self):
        out, err, code = _run_main(
            query_history,
            ["--config", self.cfg_path, "--device", "No Such Device",
             "--format", "csv"])
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "")

    def test_empty_db_no_results(self):
        db = _make_schema_db()
        cfg = _write_config(db)
        try:
            out, err, code = _run_main(query_history, ["--config", cfg])
            self.assertEqual(code, 0)
            self.assertIn("No results found.", out)
        finally:
            _cleanup(db, cfg)


# ─────────────────────────────────────────────────────────────────────────────
# 4. query_history — argument parsing
# ─────────────────────────────────────────────────────────────────────────────

class TestQueryHistoryArgParsing(unittest.TestCase):

    def test_invalid_type_choice_exits_2(self):
        out, err, code = _run_main(query_history, ["--type", "toaster"])
        self.assertEqual(code, 2)
        self.assertIn("invalid choice", err)

    def test_invalid_format_choice_exits_2(self):
        out, err, code = _run_main(query_history, ["--format", "xml"])
        self.assertEqual(code, 2)
        self.assertIn("invalid choice", err)

    def test_invalid_order_choice_exits_2(self):
        out, err, code = _run_main(query_history, ["--order", "random"])
        self.assertEqual(code, 2)
        self.assertIn("invalid choice", err)

    def test_non_integer_limit_exits_2(self):
        out, err, code = _run_main(query_history, ["--limit", "many"])
        self.assertEqual(code, 2)
        self.assertIn("invalid int value", err)

    def test_resolve_date_passthrough(self):
        self.assertEqual(query_history._resolve_date("2024-03-01"), "2024-03-01")

    def test_all_exportable_includes_identity_and_data_columns(self):
        for col in ("recorded_at", "device_name", "device_type", "address",
                    "voltage_v", "temp_c", "faults", "balance_cells"):
            self.assertIn(col, query_history._ALL_EXPORTABLE)


# ─────────────────────────────────────────────────────────────────────────────
# 5. purge_history — error / info paths
# ─────────────────────────────────────────────────────────────────────────────

_PURGE_ROWS = [
    _row("2023-01-01T00:00:00", "House Bank",  "bms"),
    _row("2023-06-15T12:00:00", "South Array", "mppt", pv_power_w=500.0),
    _row("2024-06-01T08:00:00", "House Bank",  "bms"),
    _row("2024-06-02T08:00:00", "South Array", "mppt", pv_power_w=650.0),
]


class TestPurgeHistoryErrorsAndInfo(unittest.TestCase):

    def setUp(self):
        self.db_path = _make_db(_PURGE_ROWS)
        self.cfg_path = _write_config(self.db_path)

    def tearDown(self):
        _cleanup(self.db_path, self.cfg_path)

    def test_missing_config_exits_1(self):
        out, err, code = _run_main(purge_history,
                                   ["--config", "/nonexistent/nope.ini"])
        self.assertEqual(code, 1)
        self.assertIn("config file not found", err)

    def test_missing_db_exits_1(self):
        cfg = _write_config("/nonexistent/nope.db")
        try:
            out, err, code = _run_main(purge_history, ["--config", cfg])
            self.assertEqual(code, 1)
            self.assertIn("database not found", err)
        finally:
            _cleanup(cfg)

    def test_no_filter_exits_1(self):
        out, err, code = _run_main(purge_history, ["--config", self.cfg_path])
        self.assertEqual(code, 1)
        self.assertIn("specify at least one filter", err)
        self.assertEqual(_count_rows(self.db_path), 4)   # nothing deleted

    def test_stats_action(self):
        out, err, code = _run_main(purge_history,
                                   ["--config", self.cfg_path, "--stats"])
        self.assertEqual(code, 0)
        self.assertIn("Total rows",   out)
        self.assertIn("4",            out)
        self.assertIn("2023-01-01T00:00:00", out)   # oldest
        self.assertIn("2024-06-02T08:00:00", out)   # newest
        self.assertIn("keep forever", out)          # retention 0

    def test_list_devices_action(self):
        out, err, code = _run_main(purge_history,
                                   ["--config", self.cfg_path, "--list-devices"])
        self.assertEqual(code, 0)
        self.assertIn("House Bank",  out)
        self.assertIn("South Array", out)
        self.assertIn("bms",  out)
        self.assertIn("mppt", out)

    def test_list_devices_empty_db(self):
        db = _make_schema_db()
        cfg = _write_config(db)
        try:
            out, err, code = _run_main(purge_history,
                                       ["--config", cfg, "--list-devices"])
            self.assertEqual(code, 0)
            self.assertIn("no devices in database", out)
        finally:
            _cleanup(db, cfg)

    def test_disabled_history_warns_but_continues(self):
        cfg = _write_config(self.db_path, enabled=False)
        try:
            out, err, code = _run_main(purge_history,
                                       ["--config", cfg, "--stats"])
            self.assertEqual(code, 0)
            self.assertIn("history is disabled", err)
            self.assertIn("Total rows", out)
        finally:
            _cleanup(cfg)

    def test_db_override_flag(self):
        cfg = _write_config("/nonexistent/other.db")
        try:
            out, err, code = _run_main(
                purge_history, ["--config", cfg, "--db", self.db_path, "--stats"])
            self.assertEqual(code, 0)
            self.assertIn("Total rows", out)
            self.assertIn("4", out)
        finally:
            _cleanup(cfg)


# ─────────────────────────────────────────────────────────────────────────────
# 6. purge_history — purge logic
# ─────────────────────────────────────────────────────────────────────────────

class TestPurgeHistoryPurge(unittest.TestCase):

    def setUp(self):
        self.db_path = _make_db(_PURGE_ROWS)
        self.cfg_path = _write_config(self.db_path)

    def tearDown(self):
        _cleanup(self.db_path, self.cfg_path)

    def test_dry_run_counts_without_deleting(self):
        out, err, code = _run_main(
            purge_history,
            ["--config", self.cfg_path, "--before", "2024-01-01", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn("[DRY RUN] Would delete 2", out)
        self.assertEqual(_count_rows(self.db_path), 4)

    def test_purge_before_removes_old_keeps_new(self):
        out, err, code = _run_main(
            purge_history,
            ["--config", self.cfg_path, "--before", "2024-01-01", "--yes"])
        self.assertEqual(code, 0)
        self.assertIn("Deleted 2 row(s)", out)
        conn = sqlite3.connect(self.db_path)
        try:
            remaining = [r[0] for r in conn.execute(
                "SELECT recorded_at FROM readings ORDER BY recorded_at")]
        finally:
            conn.close()
        self.assertEqual(remaining,
                         ["2024-06-01T08:00:00", "2024-06-02T08:00:00"])

    def test_purge_after_removes_newer_rows(self):
        out, err, code = _run_main(
            purge_history,
            ["--config", self.cfg_path, "--after", "2024-06-01", "--yes"])
        self.assertEqual(code, 0)
        self.assertIn("Deleted 2 row(s)", out)
        conn = sqlite3.connect(self.db_path)
        try:
            remaining = [r[0] for r in conn.execute(
                "SELECT recorded_at FROM readings ORDER BY recorded_at")]
        finally:
            conn.close()
        self.assertEqual(remaining,
                         ["2023-01-01T00:00:00", "2023-06-15T12:00:00"])

    def test_purge_by_device_name(self):
        out, err, code = _run_main(
            purge_history,
            ["--config", self.cfg_path, "--device", "South Array", "--yes"])
        self.assertEqual(code, 0)
        self.assertIn("Deleted 2 row(s)", out)
        self.assertEqual(_remaining_names(self.db_path), {"House Bank"})

    def test_purge_by_device_type(self):
        out, err, code = _run_main(
            purge_history,
            ["--config", self.cfg_path, "--type", "mppt", "--yes"])
        self.assertEqual(code, 0)
        self.assertIn("Deleted 2 row(s)", out)
        self.assertEqual(_remaining_names(self.db_path), {"House Bank"})

    def test_purge_combined_filters(self):
        out, err, code = _run_main(
            purge_history,
            ["--config", self.cfg_path, "--device", "House Bank",
             "--before", "2024-01-01", "--yes"])
        self.assertEqual(code, 0)
        self.assertIn("Deleted 1 row(s)", out)
        self.assertEqual(_count_rows(self.db_path), 3)

    def test_no_matching_rows_nothing_deleted(self):
        out, err, code = _run_main(
            purge_history,
            ["--config", self.cfg_path, "--before", "1990-01-01", "--yes"])
        self.assertEqual(code, 0)
        self.assertIn("nothing to delete", out)
        self.assertEqual(_count_rows(self.db_path), 4)

    def test_confirmation_declined_aborts(self):
        out, err, code = _run_main(
            purge_history,
            ["--config", self.cfg_path, "--before", "2099-01-01"],
            input_response="n")
        self.assertEqual(code, 0)
        self.assertIn("Aborted.", out)
        self.assertEqual(_count_rows(self.db_path), 4)

    def test_confirmation_accepted_deletes(self):
        out, err, code = _run_main(
            purge_history,
            ["--config", self.cfg_path, "--before", "2099-01-01"],
            input_response="y")
        self.assertEqual(code, 0)
        self.assertIn("Deleted 4 row(s)", out)
        self.assertEqual(_count_rows(self.db_path), 0)

    def test_purge_shows_updated_stats(self):
        out, err, code = _run_main(
            purge_history,
            ["--config", self.cfg_path, "--before", "2024-01-01", "--yes"])
        self.assertEqual(code, 0)
        self.assertIn("Updated statistics:", out)
        self.assertIn("Total rows",         out)

    def test_vacuum_after_purge(self):
        out, err, code = _run_main(
            purge_history,
            ["--config", self.cfg_path, "--before", "2024-01-01",
             "--yes", "--vacuum"])
        self.assertEqual(code, 0)
        self.assertIn("VACUUM complete.", out)
        # Database must remain intact and queryable after VACUUM
        conn = sqlite3.connect(self.db_path)
        try:
            integrity = conn.execute("PRAGMA integrity_check;").fetchone()[0]
            count = conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(integrity, "ok")
        self.assertEqual(count, 2)

    def test_partial_legacy_schema_handled_gracefully(self):
        """A db whose readings table lacks most columns must not crash."""
        db = _tmp_path(".db")
        conn = sqlite3.connect(db)
        with conn:
            conn.execute(
                "CREATE TABLE readings ("
                "id INTEGER PRIMARY KEY, recorded_at TEXT, "
                "device_name TEXT, device_type TEXT)")
        conn.close()
        cfg = _write_config(db)
        try:
            out, err, code = _run_main(
                purge_history,
                ["--config", cfg, "--before", "2099-01-01", "--yes"])
            self.assertEqual(code, 0)
            self.assertIn("nothing to delete", out)
        finally:
            _cleanup(db, cfg)


# ─────────────────────────────────────────────────────────────────────────────
# 7. purge_history — retention enforcement
# ─────────────────────────────────────────────────────────────────────────────

class TestPurgeHistoryRetention(unittest.TestCase):

    def _retention_db(self) -> str:
        """Two rows: one 60 days old (expired), one 1 day old (kept)."""
        now = datetime.now(timezone.utc)
        old = (now - timedelta(days=60)).isoformat(timespec="seconds")
        new = (now - timedelta(days=1)).isoformat(timespec="seconds")
        return _make_db([
            _row(old, "House Bank", "bms"),
            _row(new, "House Bank", "bms"),
        ])

    def test_retention_zero_is_noop(self):
        db = _make_db(_PURGE_ROWS)
        cfg = _write_config(db, retention_days=0)
        try:
            out, err, code = _run_main(
                purge_history, ["--config", cfg, "--enforce-retention", "--yes"])
            self.assertEqual(code, 0)
            self.assertIn("nothing to purge", out)
            self.assertEqual(_count_rows(db), 4)
        finally:
            _cleanup(db, cfg)

    def test_retention_deletes_expired_keeps_recent(self):
        db = self._retention_db()
        cfg = _write_config(db, retention_days=30)
        try:
            out, err, code = _run_main(
                purge_history, ["--config", cfg, "--enforce-retention", "--yes"])
            self.assertEqual(code, 0)
            self.assertIn("Retention policy: 30 days", out)
            self.assertIn("Deleted 1 row(s)", out)
            self.assertEqual(_count_rows(db), 1)
        finally:
            _cleanup(db, cfg)

    def test_retention_dry_run_keeps_everything(self):
        db = self._retention_db()
        cfg = _write_config(db, retention_days=30)
        try:
            out, err, code = _run_main(
                purge_history,
                ["--config", cfg, "--enforce-retention", "--dry-run"])
            self.assertEqual(code, 0)
            self.assertIn("[DRY RUN] Would delete 1", out)
            self.assertEqual(_count_rows(db), 2)
        finally:
            _cleanup(db, cfg)

    def test_retention_confirmation_declined(self):
        db = self._retention_db()
        cfg = _write_config(db, retention_days=30)
        try:
            out, err, code = _run_main(
                purge_history, ["--config", cfg, "--enforce-retention"],
                input_response="n")
            self.assertEqual(code, 0)
            self.assertIn("Aborted.", out)
            self.assertEqual(_count_rows(db), 2)
        finally:
            _cleanup(db, cfg)

    def test_retention_nothing_expired(self):
        now = datetime.now(timezone.utc)
        new = (now - timedelta(days=1)).isoformat(timespec="seconds")
        db = _make_db([_row(new, "House Bank", "bms")])
        cfg = _write_config(db, retention_days=365)
        try:
            out, err, code = _run_main(
                purge_history, ["--config", cfg, "--enforce-retention", "--yes"])
            self.assertEqual(code, 0)
            self.assertIn("Deleted 0 row(s)", out)
            self.assertEqual(_count_rows(db), 1)
        finally:
            _cleanup(db, cfg)

    def test_retention_with_vacuum(self):
        db = self._retention_db()
        cfg = _write_config(db, retention_days=30)
        try:
            out, err, code = _run_main(
                purge_history,
                ["--config", cfg, "--enforce-retention", "--yes", "--vacuum"])
            self.assertEqual(code, 0)
            self.assertIn("VACUUM complete.", out)
            self.assertEqual(_count_rows(db), 1)
        finally:
            _cleanup(db, cfg)


# ─────────────────────────────────────────────────────────────────────────────
# 8. purge_history — argument parsing
# ─────────────────────────────────────────────────────────────────────────────

class TestPurgeHistoryArgParsing(unittest.TestCase):

    def test_invalid_type_choice_exits_2(self):
        out, err, code = _run_main(purge_history, ["--type", "toaster"])
        self.assertEqual(code, 2)
        self.assertIn("invalid choice", err)

    def test_valid_type_choices_pass_parser(self):
        # Parser accepts every documented device type; config check fails
        # afterwards (exit 1, not the argparse exit 2)
        for t in ("bms", "mppt", "inverter", "monitor", "dcdc", "meter"):
            out, err, code = _run_main(
                purge_history,
                ["--config", "/nonexistent/nope.ini", "--type", t])
            self.assertEqual(code, 1, f"type={t}")
            self.assertIn("config file not found", err)

    def test_yes_short_flag(self):
        db = _make_db(_PURGE_ROWS)
        cfg = _write_config(db)
        try:
            out, err, code = _run_main(
                purge_history,
                ["--config", cfg, "--before", "2024-01-01", "-y"])
            self.assertEqual(code, 0)
            self.assertIn("Deleted 2 row(s)", out)
        finally:
            _cleanup(db, cfg)

    def test_unknown_argument_exits_2(self):
        out, err, code = _run_main(purge_history, ["--frobnicate"])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
