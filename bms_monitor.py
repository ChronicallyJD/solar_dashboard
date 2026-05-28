"""
bms_monitor.py — Standalone JBD/Vatrer BMS polling process
===========================================================
Polls JBD BMS packs over GATT Bluetooth on a configurable interval
(default 120 s) and writes results to the shared state file.

Usage
-----
    python bms_monitor.py [--config FILE] [--interval SECS] [--once]

The Victron monitor (victron_monitor.py) runs as a separate process.
Both write to the same shared state file; either process can render the
dashboard by reading both sections.

Why a separate process?
-----------------------
GATT connections are slow (5-35 s per device) and occasionally hang,
causing the entire poll cycle to block.  Running BMS polling in its own
process means:

  - Victron data refreshes on a short interval (30 s) independently of
    how long BMS connections take.
  - A hung BMS connection cannot starve the Victron monitor.
  - BMS can be polled on a much longer interval (2-5 min) since battery
    state changes slowly, reducing BlueZ churn and improving reliability.
  - The two processes can be restarted independently.
"""

import argparse
import asyncio
import logging
import time
from datetime import datetime
from pathlib import Path

from solar_monitor.config import DEFAULT_INI_PATH, load_config, apply_cli_overrides
from solar_monitor.dashboard import build_html
from solar_monitor.scanner import resolve_devices, _poll_bms
from solar_monitor.state import load_state, save_section

log = logging.getLogger(__name__)

# Minimum gap between BMS poll cycles regardless of configured interval.
# BlueZ needs this to fully release GATT connections.
_MIN_BMS_GAP = 30.0


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Solar Monitor — BMS process (JBD/Vatrer BMS via GATT)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python bms_monitor.py\n"
            "  python bms_monitor.py --interval 180\n"
            "  python bms_monitor.py --once --log-level DEBUG\n"
        ),
    )
    parser.add_argument("--config", metavar="FILE",
                        help=f"Config file (default: {DEFAULT_INI_PATH})")
    parser.add_argument("--interval", type=float, metavar="SECS",
                        help="BMS poll interval in seconds (overrides bms_interval in INI)")
    parser.add_argument("--output", metavar="FILE",
                        help="HTML dashboard output path (overrides INI)")
    parser.add_argument("--scan-timeout", type=float, metavar="SECS",
                        help="BLE scan timeout (overrides INI)")
    parser.add_argument("--once", action="store_true",
                        help="Poll once and exit")
    parser.add_argument("--log-level", metavar="LEVEL", default=None,
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    parser.add_argument("--theme", metavar="THEME", default=None,
                        choices=["dark", "light", "business"])
    args = parser.parse_args()

    cfg = load_config(args.config)
    cfg = apply_cli_overrides(cfg, args)
    if args.log_level:
        cfg.log_level = args.log_level
    if args.theme:
        cfg.theme = args.theme
    if args.interval is not None:
        cfg.bms_interval = args.interval

    logging.getLogger().setLevel(getattr(logging, cfg.log_level, logging.INFO))

    interval    = max(cfg.bms_interval, _MIN_BMS_GAP)
    output_path = Path(cfg.output)
    history: dict[str, list[dict]] = {}

    log.info(
        f"BMS Monitor starting — state: {cfg.state_file}  "
        f"dashboard: {output_path}  interval: {interval}s"
    )

    while True:
        cycle_start = time.monotonic()
        ts = datetime.now().isoformat(timespec="seconds")

        # ── BMS poll ──────────────────────────────────────────────────────────
        bms_readings = []
        scanner = None
        try:
            jbd_pairs, _, scanner = await resolve_devices(cfg)
            if jbd_pairs:
                log.info("Scanning for BLE devices … (BMS)")
                await scanner.scan(cfg.scan_timeout)
                bms_readings = await _poll_bms(jbd_pairs, scanner)
            else:
                log.info("No BMS devices configured — skipping BMS poll")
        except Exception as exc:
            log.error(f"BMS poll failed: {exc}", exc_info=True)
        finally:
            if scanner is not None:
                await scanner.stop()

        # ── Write shared state ────────────────────────────────────────────────
        try:
            save_section(cfg.state_file, "bms", bms_readings)
        except Exception as exc:
            log.error(f"Failed to write BMS state: {exc}")

        # ── Update history ────────────────────────────────────────────────────
        for r in bms_readings:
            entry = {
                "timestamp":    r.timestamp,
                "voltage_v":    r.voltage_v,
                "current_a":    r.current_a,
                "power_w":      r.power_w,
                "capacity_pct": r.capacity_pct,
            }
            history.setdefault(r.name, []).append(entry)
            if len(history[r.name]) > cfg.max_history:
                history[r.name] = history[r.name][-cfg.max_history:]

        # ── Render dashboard (merge with latest Victron state) ────────────────
        try:
            state = load_state(cfg.state_file)
            victron_readings = state["victron"]["readings"]
            html = build_html(bms_readings, victron_readings, history, theme=cfg.theme)
            output_path.write_text(html, encoding="utf-8")
            log.info(f"Dashboard written -> {output_path.resolve()}")
        except Exception as exc:
            log.error(f"Failed to write dashboard: {exc}", exc_info=True)

        if cfg.once or args.once:
            break

        elapsed   = time.monotonic() - cycle_start
        sleep_for = max(_MIN_BMS_GAP, interval - elapsed)
        log.info(f"BMS: next poll in {sleep_for:.0f}s ...")
        await asyncio.sleep(sleep_for)


def run() -> None:
    import sys
    _pre_level = logging.INFO
    for i, arg in enumerate(sys.argv):
        if arg in ("--log-level", "--log_level") and i + 1 < len(sys.argv):
            _pre_level = getattr(logging, sys.argv[i + 1].upper(), logging.INFO)
        elif arg.startswith("--log-level=") or arg.startswith("--log_level="):
            _pre_level = getattr(logging, arg.split("=", 1)[1].upper(), logging.INFO)

    logging.basicConfig(
        level=_pre_level,
        format="%(asctime)s [%(levelname)s]  %(message)s",
        datefmt="%H:%M:%S",
    )
    asyncio.run(main())


if __name__ == "__main__":
    run()
