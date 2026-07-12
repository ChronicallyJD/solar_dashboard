#!/usr/bin/env python3
"""
utils/purge_history.py - Solar Monitor history database purge utility
======================================================================
Delete historical readings from the SQLite database by date range,
device name, or device type.

Usage
-----
    # Dry run - see what would be deleted without deleting anything
    python utils/purge_history.py --config config.ini --before 2023-01-01 --dry-run

    # Delete everything before a specific date
    python utils/purge_history.py --config config.ini --before 2023-01-01

    # Delete a specific date range (useful for a bad data window)
    python utils/purge_history.py --config config.ini \\
        --after 2024-03-01 --before 2024-03-05

    # Delete all data for a specific device
    python utils/purge_history.py --config config.ini --device "Old Pack"

    # Delete all BMS readings before a date
    python utils/purge_history.py --config config.ini \\
        --type bms --before 2023-06-01

    # Enforce the configured retention policy right now
    python utils/purge_history.py --config config.ini --enforce-retention

    # Compact the database file after large deletions
    python utils/purge_history.py --config config.ini --vacuum

    # Show database statistics
    python utils/purge_history.py --config config.ini --stats

Examples of combining filters (AND logic):
    # Delete "House Bank" readings from last year only
    python utils/purge_history.py --config config.ini \\
        --device "House Bank" --before 2024-01-01

    # Delete all MPPT data from a specific month
    python utils/purge_history.py --config config.ini \\
        --type mppt --after 2024-06-01 --before 2024-06-30
"""

import argparse
import os
import sys
from pathlib import Path

# Allow running from the repo root or from the utils directory
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from solar_monitor.history import HistoryDB, load_history_config


# ─────────────────────────────────────────────────────────────────────────────
# Output helpers
# ─────────────────────────────────────────────────────────────────────────────

def _print_stats(db: HistoryDB) -> None:
    stats = db.get_stats()
    if not stats:
        print("  (no statistics available)")
        return
    print(f"  Database:       {stats['db_path']}")
    print(f"  Total rows:     {stats['total_rows']:,}")
    print(f"  Oldest reading: {stats['oldest_reading'] or '(none)'}")
    print(f"  Newest reading: {stats['newest_reading'] or '(none)'}")
    print(f"  Size on disk:   {stats['size_mb']} MB")
    print(f"  Retention:      {stats['retention_days']} days"
          + (" (keep forever)" if stats['retention_days'] == 0 else ""))


def _print_devices(db: HistoryDB) -> None:
    devices = db.get_devices()
    if not devices:
        print("  (no devices in database)")
        return
    print(f"  {'Device':<22} {'Type':<10} {'First seen':<22} {'Last seen':<22} {'Rows':>8}")
    print(f"  {'-'*22} {'-'*10} {'-'*22} {'-'*22} {'-'*8}")
    for d in devices:
        print(f"  {d['device_name']:<22} {d['device_type']:<10} "
              f"{(d['first_seen'] or ''):<22} {(d['last_seen'] or ''):<22} "
              f"{d['reading_count']:>8,}")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Solar Monitor - purge historical readings from SQLite",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Usage")[1] if "Usage" in __doc__ else "",
    )

    # Config / database
    parser.add_argument("--config",  metavar="FILE", default="config.ini",
                        help="Config file (reads [history] section)")
    parser.add_argument("--db",      metavar="FILE",
                        help="Override database path from config")

    # Filters
    parser.add_argument("--before",  metavar="DATE",
                        help="Delete rows with recorded_at < DATE (e.g. 2023-01-01)")
    parser.add_argument("--after",   metavar="DATE",
                        help="Delete rows with recorded_at > DATE (e.g. 2024-12-31)")
    parser.add_argument("--device",  metavar="NAME",
                        help="Restrict deletion to this device name (exact match)")
    parser.add_argument("--type",    metavar="TYPE",
                        choices=["bms", "mppt", "inverter", "monitor", "dcdc", "meter"],
                        help="Restrict deletion to this device type")

    # Actions
    parser.add_argument("--dry-run",  action="store_true",
                        help="Count matching rows without deleting them")
    parser.add_argument("--enforce-retention", action="store_true",
                        help="Delete all rows older than the configured retention_days")
    parser.add_argument("--vacuum",   action="store_true",
                        help="Run VACUUM to compact the database after deletion")
    parser.add_argument("--stats",    action="store_true",
                        help="Show database statistics and exit")
    parser.add_argument("--list-devices", action="store_true",
                        help="List all devices in the database and exit")
    parser.add_argument("--yes", "-y", action="store_true",
                        help="Skip confirmation prompt")

    args = parser.parse_args()

    # ── Load config ────────────────────────────────────────────────────────────
    config_path = Path(args.config)
    if not config_path.exists():
        print(f"Error: config file not found: {config_path}", file=sys.stderr)
        sys.exit(1)

    cfg = load_history_config(str(config_path))
    if args.db:
        cfg.db_path = args.db

    if not cfg.enabled and not args.db:
        print(
            "Warning: history is disabled in config ([history] enabled = false).\n"
            "Use --db to point directly at a database file, or enable history first.",
            file=sys.stderr,
        )

    db_path = Path(cfg.db_path)
    if not db_path.exists():
        print(f"Error: database not found: {db_path}", file=sys.stderr)
        print("The history database is created automatically when the monitor first runs.", file=sys.stderr)
        sys.exit(1)

    db = HistoryDB(cfg)

    # ── Info-only actions ──────────────────────────────────────────────────────
    if args.stats:
        print("\nDatabase statistics:")
        _print_stats(db)
        print()
        return

    if args.list_devices:
        print("\nDevices in history database:")
        _print_devices(db)
        print()
        return

    # ── Enforce retention ──────────────────────────────────────────────────────
    if args.enforce_retention:
        if cfg.retention_days == 0:
            print("Retention is set to 0 (keep forever) - nothing to purge.")
            return

        from datetime import datetime, timedelta, timezone
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=cfg.retention_days)
        ).strftime("%Y-%m-%d")

        print(f"\nRetention policy: {cfg.retention_days} days")
        print(f"Cutoff date:      {cutoff}")

        if args.dry_run:
            count = db.purge(before=cutoff, dry_run=True)
            print(f"[DRY RUN] Would delete {count:,} row(s) older than {cutoff}")
        else:
            if not args.yes:
                ans = input(f"Delete all rows before {cutoff}? [y/N] ").strip().lower()
                if ans not in ("y", "yes"):
                    print("Aborted.")
                    return
            count = db.enforce_retention()
            print(f"Deleted {count:,} row(s) older than {cutoff}")

        if args.vacuum:
            print("Running VACUUM …")
            db.vacuum()
            print("VACUUM complete.")
        db.close()
        return

    # ── Manual purge ───────────────────────────────────────────────────────────
    if not any([args.before, args.after, args.device, args.type]):
        print(
            "Error: specify at least one filter (--before, --after, --device, --type)\n"
            "       or use --enforce-retention to apply the configured policy.\n"
            "       Use --stats to inspect the database first.",
            file=sys.stderr,
        )
        parser.print_help()
        sys.exit(1)

    # Show what we're about to do
    print("\nPurge filters:")
    if args.before:  print(f"  before:  {args.before}")
    if args.after:   print(f"  after:   {args.after}")
    if args.device:  print(f"  device:  {args.device}")
    if args.type:    print(f"  type:    {args.type}")

    # Dry run always runs without confirmation
    if args.dry_run:
        count = db.purge(
            before=args.before, after=args.after,
            device_name=args.device, device_type=args.type,
            dry_run=True,
        )
        print(f"\n[DRY RUN] Would delete {count:,} matching row(s)")
        db.close()
        return

    # Count first so we can show the user what they're deleting
    count = db.purge(
        before=args.before, after=args.after,
        device_name=args.device, device_type=args.type,
        dry_run=True,
    )

    if count == 0:
        print("\nNo matching rows found - nothing to delete.")
        db.close()
        return

    print(f"\nMatching rows: {count:,}")

    if not args.yes:
        ans = input(f"Permanently delete {count:,} row(s)? [y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            print("Aborted.")
            db.close()
            return

    deleted = db.purge(
        before=args.before, after=args.after,
        device_name=args.device, device_type=args.type,
    )
    print(f"Deleted {deleted:,} row(s).")

    if args.vacuum:
        print("Running VACUUM …")
        db.vacuum()
        print("VACUUM complete.")

    print("\nUpdated statistics:")
    _print_stats(db)
    db.close()


if __name__ == "__main__":
    main()
