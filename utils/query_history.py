#!/usr/bin/env python3
"""
utils/query_history.py — Solar Monitor history query and export utility
========================================================================
Query the SQLite history database and export readings as CSV or JSON.

Usage
-----
    # Show recent readings for all devices
    python utils/query_history.py --config config.ini

    # Export a specific device's history as CSV
    python utils/query_history.py --config config.ini \\
        --device "House Bank" --format csv > house_bank.csv

    # Query a date range
    python utils/query_history.py --config config.ini \\
        --start 2024-01-01 --end 2024-01-31 --format csv

    # Export only specific fields
    python utils/query_history.py --config config.ini \\
        --device "House Bank" \\
        --fields recorded_at,voltage_v,current_a,capacity_pct \\
        --format csv

    # Query all MPPT devices for today
    python utils/query_history.py --config config.ini \\
        --type mppt --start today

    # Show the most recent N readings per device
    python utils/query_history.py --config config.ini --limit 10

    # List all devices with row counts
    python utils/query_history.py --config config.ini --list-devices

    # Show database statistics
    python utils/query_history.py --config config.ini --stats
"""

import argparse
import csv
import json
import os
import sys
from datetime import date
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from solar_monitor.history import HistoryDB, load_history_config, _SCALAR_FIELDS, _LIST_FIELDS

_ALL_EXPORTABLE = (
    "recorded_at", "device_name", "device_type", "address"
) + _SCALAR_FIELDS + _LIST_FIELDS


def _resolve_date(value: str) -> str:
    """Resolve 'today' / 'yesterday' shortcuts."""
    if value.lower() == "today":
        return date.today().isoformat()
    if value.lower() == "yesterday":
        from datetime import timedelta
        return (date.today() - timedelta(days=1)).isoformat()
    return value


def _print_table(rows: list[dict], max_rows: int = 40) -> None:
    """Print a human-readable table of results."""
    if not rows:
        print("  (no results)")
        return

    # Choose a useful subset of columns for terminal display
    display_cols = [
        "recorded_at", "device_name", "device_type",
        "voltage_v", "current_a", "power_w", "capacity_pct",
        "pv_power_w", "charger_state", "inverter_state", "error",
    ]
    # Only show columns that have at least one non-None value
    cols = [c for c in display_cols if any(r.get(c) is not None for r in rows)]

    widths = {c: max(len(c), max(len(str(r.get(c) or "")) for r in rows)) for c in cols}
    header = "  " + "  ".join(f"{c:<{widths[c]}}" for c in cols)
    sep    = "  " + "  ".join("-" * widths[c] for c in cols)

    print(header)
    print(sep)

    shown = rows[:max_rows]
    for r in shown:
        line = "  " + "  ".join(
            f"{str(r.get(c) or ''):<{widths[c]}}" for c in cols
        )
        print(line)

    if len(rows) > max_rows:
        print(f"\n  … {len(rows) - max_rows:,} more rows not shown. Use --format csv to export all.")


def _export_csv(rows: list[dict], fields: list[str]) -> None:
    """Write rows as CSV to stdout."""
    if not rows:
        return
    writer = csv.DictWriter(sys.stdout, fieldnames=fields,
                            extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)


def _export_json(rows: list[dict]) -> None:
    """Write rows as JSON array to stdout."""
    json.dump(rows, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Solar Monitor — query and export historical readings",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Usage")[1] if "Usage" in __doc__ else "",
    )

    # Config / database
    parser.add_argument("--config",  metavar="FILE", default="config.ini",
                        help="Config file (reads [history] section)")
    parser.add_argument("--db",      metavar="FILE",
                        help="Override database path from config")

    # Filters
    parser.add_argument("--device",  metavar="NAME",
                        help="Filter by device name (exact match)")
    parser.add_argument("--type",    metavar="TYPE",
                        choices=["bms", "mppt", "inverter", "monitor", "dcdc", "meter"],
                        help="Filter by device type")
    parser.add_argument("--start",   metavar="DATE",
                        help="Start date (e.g. 2024-01-01, or 'today', 'yesterday')")
    parser.add_argument("--end",     metavar="DATE",
                        help="End date inclusive (e.g. 2024-01-31)")
    parser.add_argument("--limit",   metavar="N", type=int,
                        help="Maximum number of rows to return")
    parser.add_argument("--order",   choices=["asc", "desc"], default="asc",
                        help="Sort order (default: asc = oldest first)")

    # Output
    parser.add_argument("--format",  choices=["table", "csv", "json"],
                        default="table", help="Output format (default: table)")
    parser.add_argument("--fields",  metavar="FIELDS",
                        help="Comma-separated column list to include in output")

    # Info actions
    parser.add_argument("--list-devices", action="store_true",
                        help="List all devices in the database with row counts")
    parser.add_argument("--list-fields",  action="store_true",
                        help="List all available column names and exit")
    parser.add_argument("--stats",        action="store_true",
                        help="Show database statistics and exit")

    args = parser.parse_args()

    # ── List fields (no DB needed) ─────────────────────────────────────────────
    if args.list_fields:
        print("\nAvailable fields for --fields filter:")
        for f in _ALL_EXPORTABLE:
            print(f"  {f}")
        print()
        return

    # ── Load config ────────────────────────────────────────────────────────────
    config_path = Path(args.config)
    if not config_path.exists():
        print(f"Error: config file not found: {config_path}", file=sys.stderr)
        sys.exit(1)

    cfg = load_history_config(str(config_path))
    if args.db:
        cfg.db_path = args.db

    db_path = Path(cfg.db_path)
    if not db_path.exists():
        print(f"Error: database not found: {db_path}", file=sys.stderr)
        sys.exit(1)

    db = HistoryDB(cfg)

    # ── Info-only actions ──────────────────────────────────────────────────────
    if args.stats:
        stats = db.get_stats()
        print("\nDatabase statistics:")
        for k, v in stats.items():
            print(f"  {k:<22} {v}")
        print()
        db.close()
        return

    if args.list_devices:
        devices = db.get_devices()
        print(f"\n{'Device':<24} {'Type':<12} {'First seen':<22} {'Last seen':<22} {'Rows':>8}")
        print(f"{'-'*24} {'-'*12} {'-'*22} {'-'*22} {'-'*8}")
        for d in devices:
            print(f"{d['device_name']:<24} {d['device_type']:<12} "
                  f"{(d['first_seen'] or ''):<22} {(d['last_seen'] or ''):<22} "
                  f"{d['reading_count']:>8,}")
        print()
        db.close()
        return

    # ── Resolve field list ─────────────────────────────────────────────────────
    if args.fields:
        requested = [f.strip() for f in args.fields.split(",") if f.strip()]
        invalid = [f for f in requested if f not in _ALL_EXPORTABLE]
        if invalid:
            print(f"Error: unknown field(s): {', '.join(invalid)}", file=sys.stderr)
            print(f"Use --list-fields to see available columns.", file=sys.stderr)
            sys.exit(1)
        fields = requested
    else:
        fields = None   # all fields

    # ── Resolve date shortcuts ─────────────────────────────────────────────────
    start = _resolve_date(args.start) if args.start else None
    end   = _resolve_date(args.end)   if args.end   else None

    # ── Query ──────────────────────────────────────────────────────────────────
    rows = db.query(
        device_name  = args.device,
        device_type  = args.type,
        start        = start,
        end          = end,
        fields       = fields,
        limit        = args.limit,
        order        = args.order.upper(),
    )

    db.close()

    if not rows:
        if args.format == "table":
            print("No results found.")
        elif args.format == "json":
            print("[]")
        return

    # ── Output ─────────────────────────────────────────────────────────────────
    if args.format == "table":
        print(f"\nResults: {len(rows):,} row(s)\n")
        _print_table(rows)
        print()

    elif args.format == "csv":
        # Use requested fields, or all columns present in first row
        export_fields = fields or list(rows[0].keys())
        _export_csv(rows, export_fields)

    elif args.format == "json":
        _export_json(rows)


if __name__ == "__main__":
    main()
