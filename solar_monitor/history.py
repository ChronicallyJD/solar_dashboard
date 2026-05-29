"""
solar_monitor/history.py — SQLite persistent history store
===========================================================
Stores every DeviceReading written by a worker into a local SQLite database,
providing long-term history independent of the in-memory rolling window used
by the dashboard charts.

Design
------
- One table: ``readings``.  Every DeviceReading field gets its own column.
  List fields (temp_c, faults, balance_cells) are stored as JSON strings.
- ``recorded_at`` is the primary time index (ISO 8601, UTC).
- ``device_name`` + ``device_type`` + ``recorded_at`` together form a
  composite index that makes device-range queries fast.
- Automatic retention enforcement: rows older than ``retention_days``
  (default 1095 = 3 years) are deleted on each write cycle.
- ``PRAGMA journal_mode=WAL`` so readers (dashboard) never block writers
  (workers) and vice versa.
- All public functions are safe to call from multiple processes; SQLite
  handles the locking.

Usage
-----
    from solar_monitor.history import HistoryDB, HistoryConfig

    cfg = HistoryConfig(enabled=True, db_path="history.db", retention_days=1095)
    db  = HistoryDB(cfg)
    db.write_readings(readings)                  # called after every poll
    rows = db.query(device_name="House Bank",    # fetch for dashboard / export
                    start="2024-01-01",
                    end="2024-12-31")
    db.purge(before="2023-01-01")                # manual purge
    db.close()

Configuration
-------------
    [history]
    enabled           = true
    db_path           = solar_history.db
    retention_days    = 1095        # 3 years; 0 = keep forever
    vacuum_interval_days = 7        # run VACUUM weekly to reclaim space
"""

import json
import logging
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

# All numeric / scalar fields from DeviceReading
_SCALAR_FIELDS: tuple[str, ...] = (
    "voltage_v", "current_a", "power_w",
    "capacity_pct", "cell_count", "ttg_minutes", "alarm_reason",
    "remain_ah", "nominal_ah", "remain_wh", "nominal_wh",
    "time_to_empty_h", "time_to_full_h", "cycle_count",
    "sw_version", "production_date",
    "protection_bits", "charge_fet", "discharge_fet",
    "pv_power_w", "yield_today_wh", "load_current_a", "charger_state",
    "ac_out_power_va", "ac_out_voltage_v", "ac_out_current_a", "inverter_state",
    "ac_in_power_w", "ac_in_source", "vebus_error", "temperature_c",
    "raw_load_indicator", "error_code", "error",
)

# List fields — serialised to JSON strings
_LIST_FIELDS: tuple[str, ...] = ("temp_c", "faults", "balance_cells")

_ALL_FIELDS = _SCALAR_FIELDS + _LIST_FIELDS

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS readings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at  TEXT    NOT NULL,          -- ISO 8601, UTC
    device_name  TEXT    NOT NULL,
    device_type  TEXT    NOT NULL,
    address      TEXT    NOT NULL,

    -- Electrical fundamentals
    voltage_v         REAL,
    current_a         REAL,
    power_w           REAL,

    -- BMS / Battery Monitor
    capacity_pct      INTEGER,
    cell_count        INTEGER,
    ttg_minutes       INTEGER,
    alarm_reason      INTEGER,
    remain_ah         REAL,
    nominal_ah        REAL,
    remain_wh         REAL,
    nominal_wh        REAL,
    time_to_empty_h   REAL,
    time_to_full_h    REAL,
    cycle_count       INTEGER,
    sw_version        TEXT,
    production_date   TEXT,
    protection_bits   INTEGER,
    charge_fet        INTEGER,    -- 0/1
    discharge_fet     INTEGER,    -- 0/1
    temp_c            TEXT,       -- JSON array e.g. '[23.1, 21.8]'
    faults            TEXT,       -- JSON array e.g. '["Cell overvoltage"]'
    balance_cells     TEXT,       -- JSON array e.g. '[0,0,1,0,...]'

    -- Solar Charger (MPPT)
    pv_power_w        REAL,
    yield_today_wh    REAL,
    load_current_a    REAL,
    charger_state     TEXT,

    -- Inverter / VE.Bus
    ac_out_power_va   REAL,
    ac_out_voltage_v  REAL,
    ac_out_current_a  REAL,
    inverter_state    TEXT,
    ac_in_power_w     REAL,
    ac_in_source      TEXT,
    vebus_error       INTEGER,
    temperature_c     REAL,
    raw_load_indicator INTEGER,

    -- Error
    error_code        INTEGER,
    error             TEXT
);
"""

_CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_recorded_at  ON readings (recorded_at);",
    "CREATE INDEX IF NOT EXISTS idx_device_name  ON readings (device_name);",
    "CREATE INDEX IF NOT EXISTS idx_device_type  ON readings (device_type);",
    "CREATE INDEX IF NOT EXISTS idx_name_time    ON readings (device_name, recorded_at);",
]

_VACUUM_TABLE = """
CREATE TABLE IF NOT EXISTS _meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class HistoryConfig:
    """Settings for the SQLite history store."""
    enabled:              bool  = False
    db_path:              str   = "solar_history.db"
    retention_days:       int   = 1095      # 3 years; 0 = keep forever
    vacuum_interval_days: int   = 7         # run VACUUM every N days

    def __post_init__(self) -> None:
        if self.retention_days < 0:
            raise ValueError(f"retention_days must be >= 0, got {self.retention_days}")
        if self.vacuum_interval_days < 1:
            raise ValueError(f"vacuum_interval_days must be >= 1, got {self.vacuum_interval_days}")


def load_history_config(ini_path: str) -> "HistoryConfig":
    """Read [history] section from an INI file and return HistoryConfig."""
    import configparser
    p = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
    p.read(ini_path)

    if "history" not in p:
        return HistoryConfig()

    s = p["history"]

    def _bool(key: str, default: bool) -> bool:
        return s.get(key, str(default)).strip().lower() in ("1", "true", "yes", "on")

    return HistoryConfig(
        enabled              = _bool("enabled",              False),
        db_path              = s.get("db_path",              "solar_history.db"),
        retention_days       = int(s.get("retention_days",       "1095")),
        vacuum_interval_days = int(s.get("vacuum_interval_days", "7")),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Database
# ─────────────────────────────────────────────────────────────────────────────

class HistoryDB:
    """
    Thread-safe SQLite history store.

    Multiple worker processes may call ``write_readings`` concurrently;
    SQLite WAL mode handles the locking.  The connection is opened once per
    process and reused.
    """

    def __init__(self, cfg: HistoryConfig) -> None:
        self.cfg   = cfg
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None
        self._open()

    # ── Connection management ─────────────────────────────────────────────────

    def _open(self) -> None:
        db_path = Path(self.cfg.db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(db_path),
            check_same_thread=False,
            detect_types=sqlite3.PARSE_DECLTYPES,
        )
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA synchronous=NORMAL;")
            self._conn.execute("PRAGMA foreign_keys=ON;")
            self._conn.executescript(_CREATE_TABLE)
            self._conn.execute(_VACUUM_TABLE)
            for idx in _CREATE_INDEXES:
                self._conn.execute(idx)
        log.info(f"HistoryDB opened: {self.cfg.db_path}")

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    # ── Write ─────────────────────────────────────────────────────────────────

    def write_readings(self, readings: list) -> int:
        """
        Insert a list of DeviceReading objects (or dicts) into the database.

        Returns the number of rows inserted.  Only successful readings are
        stored (those with ``error=None``).  Errors are logged but do not
        raise — a failed write must never crash a worker.
        """
        if not readings:
            return 0

        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        rows = []
        for r in readings:
            d = r if isinstance(r, dict) else _reading_to_row(r, now)
            if d:
                rows.append(d)

        if not rows:
            return 0

        cols   = ["recorded_at", "device_name", "device_type", "address"] + list(_ALL_FIELDS)
        placeholders = ", ".join("?" for _ in cols)
        sql    = f"INSERT INTO readings ({', '.join(cols)}) VALUES ({placeholders})"

        try:
            with self._lock, self._conn:
                self._conn.executemany(sql, [
                    tuple(row.get(c) for c in cols) for row in rows
                ])
            log.debug(f"HistoryDB: inserted {len(rows)} reading(s)")
            self._maybe_enforce_retention()
            return len(rows)
        except sqlite3.Error as exc:
            log.error(f"HistoryDB write error: {exc}")
            return 0

    # ── Query ─────────────────────────────────────────────────────────────────

    def query(
        self,
        device_name:  Optional[str]  = None,
        device_type:  Optional[str]  = None,
        start:        Optional[str]  = None,   # ISO date or datetime
        end:          Optional[str]  = None,   # ISO date or datetime (inclusive)
        fields:       Optional[list] = None,   # column whitelist; None = all
        limit:        Optional[int]  = None,
        order:        str            = "ASC",
    ) -> list[dict]:
        """
        Query historical readings with optional filters.

        Args:
            device_name:  Exact device name match (case-sensitive).
            device_type:  Filter by type: "bms", "mppt", "inverter", etc.
            start:        Earliest ``recorded_at`` to include (ISO format).
            end:          Latest  ``recorded_at`` to include (ISO format).
            fields:       List of column names to return. None = all columns.
            limit:        Maximum number of rows to return.
            order:        ``"ASC"`` (oldest first) or ``"DESC"`` (newest first).

        Returns:
            List of dicts, one per row.  Empty list if no matches or on error.
        """
        if fields:
            # Always include the key identity columns
            sel_cols = list(dict.fromkeys(
                ["recorded_at", "device_name", "device_type"] + fields
            ))
            select = ", ".join(sel_cols)
        else:
            select = "*"

        where: list[str] = []
        params: list[Any] = []

        if device_name:
            where.append("device_name = ?");   params.append(device_name)
        if device_type:
            where.append("device_type = ?");   params.append(device_type)
        if start:
            where.append("recorded_at >= ?");  params.append(_normalise_dt(start))
        if end:
            where.append("recorded_at <= ?");  params.append(_normalise_dt(end, end_of_day=True))

        sql = f"SELECT {select} FROM readings"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += f" ORDER BY recorded_at {order}"
        if limit:
            sql += f" LIMIT {int(limit)}"

        try:
            with self._lock:
                rows = self._conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
        except sqlite3.Error as exc:
            log.error(f"HistoryDB query error: {exc}")
            return []

    def get_devices(self) -> list[dict]:
        """Return one row per distinct (device_name, device_type) combination."""
        sql = """
            SELECT device_name, device_type, address,
                   MIN(recorded_at) AS first_seen,
                   MAX(recorded_at) AS last_seen,
                   COUNT(*)         AS reading_count
            FROM readings
            GROUP BY device_name, device_type
            ORDER BY device_name
        """
        try:
            with self._lock:
                rows = self._conn.execute(sql).fetchall()
            return [dict(r) for r in rows]
        except sqlite3.Error as exc:
            log.error(f"HistoryDB get_devices error: {exc}")
            return []

    def get_stats(self) -> dict:
        """Return database statistics: row count, date range, size on disk."""
        try:
            with self._lock:
                total  = self._conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
                oldest = self._conn.execute("SELECT MIN(recorded_at) FROM readings").fetchone()[0]
                newest = self._conn.execute("SELECT MAX(recorded_at) FROM readings").fetchone()[0]
            size_bytes = Path(self.cfg.db_path).stat().st_size if Path(self.cfg.db_path).exists() else 0
            return {
                "total_rows":      total,
                "oldest_reading":  oldest,
                "newest_reading":  newest,
                "db_path":         self.cfg.db_path,
                "size_bytes":      size_bytes,
                "size_mb":         round(size_bytes / 1_048_576, 2),
                "retention_days":  self.cfg.retention_days,
            }
        except sqlite3.Error as exc:
            log.error(f"HistoryDB stats error: {exc}")
            return {}

    # ── Purge / retention ─────────────────────────────────────────────────────

    def purge(
        self,
        before:      Optional[str] = None,   # ISO date/datetime (exclusive upper bound)
        after:       Optional[str] = None,   # ISO date/datetime (exclusive lower bound)
        device_name: Optional[str] = None,
        device_type: Optional[str] = None,
        dry_run:     bool          = False,
    ) -> int:
        """
        Delete readings matching the given filters.

        At least one of ``before``, ``after``, ``device_name``, or
        ``device_type`` must be provided to prevent accidental full-table
        deletion.

        Args:
            before:      Delete rows with ``recorded_at < before``.
            after:       Delete rows with ``recorded_at > after``.
            device_name: Restrict deletion to this device.
            device_type: Restrict deletion to this device type.
            dry_run:     Count matching rows without deleting them.

        Returns:
            Number of rows deleted (or that would be deleted in dry-run mode).

        Raises:
            ValueError: If no filter is specified.
        """
        if not any([before, after, device_name, device_type]):
            raise ValueError(
                "At least one filter (before, after, device_name, device_type) "
                "is required to prevent accidental deletion of all data."
            )

        where: list[str] = []
        params: list[Any] = []

        if before:
            where.append("recorded_at < ?");   params.append(_normalise_dt(before))
        if after:
            where.append("recorded_at > ?");   params.append(_normalise_dt(after))
        if device_name:
            where.append("device_name = ?");   params.append(device_name)
        if device_type:
            where.append("device_type = ?");   params.append(device_type)

        clause = " WHERE " + " AND ".join(where)

        try:
            with self._lock:
                count = self._conn.execute(
                    f"SELECT COUNT(*) FROM readings{clause}", params
                ).fetchone()[0]

                if dry_run:
                    return count

                with self._conn:
                    self._conn.execute(f"DELETE FROM readings{clause}", params)
                log.info(
                    f"HistoryDB purge: deleted {count} row(s)  "
                    f"before={before} after={after} "
                    f"device={device_name} type={device_type}"
                )
                return count
        except sqlite3.Error as exc:
            log.error(f"HistoryDB purge error: {exc}")
            return 0

    def enforce_retention(self) -> int:
        """
        Delete all rows older than ``retention_days``.

        Called automatically after each write batch.  Returns number of rows
        deleted.  No-op when ``retention_days == 0``.
        """
        if self.cfg.retention_days == 0:
            return 0

        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=self.cfg.retention_days)
        ).isoformat(timespec="seconds")

        return self.purge(before=cutoff)

    def vacuum(self) -> None:
        """Run VACUUM to compact the database file and reclaim space."""
        try:
            log.info("HistoryDB: running VACUUM …")
            with self._lock:
                self._conn.execute("VACUUM;")
            log.info("HistoryDB: VACUUM complete")
        except sqlite3.Error as exc:
            log.error(f"HistoryDB vacuum error: {exc}")

    # ── Dashboard history dict ────────────────────────────────────────────────

    def load_recent_for_dashboard(self, max_points: int = 600) -> dict:
        """
        Load the most recent readings for each device and return them in the
        format expected by ``build_html`` — a dict of
        ``{device_name: [{"timestamp": ..., "voltage_v": ..., ...}, ...]}``.

        Only the fields used by the dashboard charts are returned:
        timestamp, voltage_v, current_a, power_w, capacity_pct,
        pv_power_w, yield_today_wh, ac_out_power_va.

        This is called once at worker startup to pre-populate the in-memory
        history dict from persistent storage.
        """
        chart_fields = [
            "voltage_v", "current_a", "power_w",
            "capacity_pct", "pv_power_w", "yield_today_wh", "ac_out_power_va",
        ]
        rows = self.query(
            fields=["recorded_at"] + chart_fields,
            limit=None,
            order="ASC",
        )

        history: dict = {}
        # Keep only the last max_points for each device
        for row in rows:
            name = row["device_name"]
            entry = {
                "timestamp":       row["recorded_at"],
                "voltage_v":       row.get("voltage_v"),
                "current_a":       row.get("current_a"),
                "power_w":         row.get("power_w"),
                "capacity_pct":    row.get("capacity_pct"),
                "pv_power_w":      row.get("pv_power_w"),
                "yield_today_wh":  row.get("yield_today_wh"),
                "ac_out_power_va": row.get("ac_out_power_va"),
            }
            history.setdefault(name, []).append(entry)

        # Trim to max_points per device
        for name in history:
            if len(history[name]) > max_points:
                history[name] = history[name][-max_points:]

        return history

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _maybe_enforce_retention(self) -> None:
        """Enforce retention if the last enforcement was more than 1 hour ago."""
        if self.cfg.retention_days == 0:
            return
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT value FROM _meta WHERE key = 'last_retention'"
                ).fetchone()
            last = datetime.fromisoformat(row["value"]) if row else None
            now  = datetime.now(timezone.utc)
            if last and (now - last.replace(tzinfo=timezone.utc)).total_seconds() < 3600:
                return
            deleted = self.enforce_retention()
            if deleted:
                log.info(f"HistoryDB retention: removed {deleted} expired row(s)")
            with self._lock, self._conn:
                self._conn.execute(
                    "INSERT OR REPLACE INTO _meta (key, value) VALUES ('last_retention', ?)",
                    (now.isoformat(timespec="seconds"),),
                )
        except Exception as exc:
            log.debug(f"HistoryDB retention check failed: {exc}")

    def _maybe_vacuum(self) -> None:
        """Run VACUUM if it hasn't been run within vacuum_interval_days."""
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT value FROM _meta WHERE key = 'last_vacuum'"
                ).fetchone()
            last = datetime.fromisoformat(row["value"]) if row else None
            now  = datetime.now(timezone.utc)
            if last:
                days_since = (now - last.replace(tzinfo=timezone.utc)).days
                if days_since < self.cfg.vacuum_interval_days:
                    return
            self.vacuum()
            with self._lock, self._conn:
                self._conn.execute(
                    "INSERT OR REPLACE INTO _meta (key, value) VALUES ('last_vacuum', ?)",
                    (now.isoformat(timespec="seconds"),),
                )
        except Exception as exc:
            log.debug(f"HistoryDB vacuum check failed: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _reading_to_row(r: Any, recorded_at: str) -> Optional[dict]:
    """
    Convert a DeviceReading (or dict) to a flat row dict ready for INSERT.

    Returns None for error readings (those with r.error set), since partial
    error readings are not useful for long-term trend analysis.
    """
    # Support both DeviceReading dataclasses and plain dicts
    if isinstance(r, dict):
        d = r
    else:
        try:
            from dataclasses import asdict as _asdict
            d = _asdict(r)
        except TypeError:
            d = {f: getattr(r, f, None) for f in
                 ["address", "name", "device_type", "error"] + list(_ALL_FIELDS)}

    # Skip readings that failed
    if d.get("error"):
        return None

    row: dict = {
        "recorded_at":  recorded_at,
        "device_name":  d.get("name", ""),
        "device_type":  d.get("device_type", ""),
        "address":      d.get("address", ""),
    }

    for f in _SCALAR_FIELDS:
        v = d.get(f)
        # Coerce booleans to integers for SQLite
        if isinstance(v, bool):
            v = int(v)
        row[f] = v

    for f in _LIST_FIELDS:
        v = d.get(f)
        row[f] = json.dumps(v) if v is not None else None

    return row


def _normalise_dt(value: str, end_of_day: bool = False) -> str:
    """
    Accept a date (``"2024-01-15"``) or datetime (``"2024-01-15T08:00:00"``)
    string and return a full ISO datetime string for comparison with
    ``recorded_at`` values.

    When ``end_of_day=True`` and only a date is given, returns
    ``"2024-01-15T23:59:59"`` so the entire day is included.
    """
    value = value.strip()
    if "T" in value or " " in value:
        return value  # already a datetime
    # Date only
    if end_of_day:
        return f"{value}T23:59:59"
    return f"{value}T00:00:00"
