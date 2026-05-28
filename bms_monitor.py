"""
bms_monitor.py — JBD/Vatrer BMS worker process
================================================
Connects directly to BMS devices by MAC — NO BLE scanning.
Designed to run standalone or under solar_monitor.py supervisor.

Worker contract
---------------
Accepts: --config FILE  --state-file FILE  --log-level LEVEL  --once
Writes:  state_file["bms"] section after each poll cycle
Exits:   non-zero on unrecoverable error (supervisor will restart)
"""

import argparse
import asyncio
import logging
import time
from pathlib import Path

from solar_monitor.config import DEFAULT_INI_PATH, load_config, apply_cli_overrides
from solar_monitor.dashboard import build_html
from solar_monitor.scanner import _poll_bms
from solar_monitor.state import load_state, save_section

log = logging.getLogger(__name__)

_MIN_BMS_GAP = 30.0


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Solar Monitor — BMS worker (JBD/Vatrer, direct connection)")
    parser.add_argument("--config",      metavar="FILE",
                        help=f"Config file (default: {DEFAULT_INI_PATH})")
    parser.add_argument("--state-file",  metavar="FILE",
                        help="Shared state file (overrides config)")
    parser.add_argument("--interval",    type=float, metavar="SECS",
                        help="Override bms_interval from config")
    parser.add_argument("--output",      metavar="FILE")
    parser.add_argument("--scan-timeout",type=float, metavar="SECS")
    parser.add_argument("--once",        action="store_true")
    parser.add_argument("--log-level",   metavar="LEVEL", default=None,
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    parser.add_argument("--theme",       metavar="THEME", default=None,
                        choices=["dark", "light", "business"])
    args = parser.parse_args()

    cfg = load_config(args.config)
    cfg = apply_cli_overrides(cfg, args)
    if args.log_level:              cfg.log_level    = args.log_level
    if args.theme:                  cfg.theme        = args.theme
    if args.interval is not None:   cfg.bms_interval = args.interval
    if args.state_file:             cfg.state_file   = args.state_file

    logging.getLogger().setLevel(getattr(logging, cfg.log_level, logging.INFO))

    interval    = max(cfg.bms_interval, _MIN_BMS_GAP)
    output_path = Path(cfg.output)
    history: dict = {}

    log.info(
        f"BMS worker starting — state: {cfg.state_file}  "
        f"interval: {interval}s  (direct MAC connection, no scan)"
    )

    while True:
        cycle_start = time.monotonic()

        bms_readings = []
        try:
            if cfg.bms_devices:
                bms_readings = await _poll_bms(cfg.bms_devices)
            else:
                log.info("No BMS devices configured")
        except Exception as exc:
            log.error(f"BMS poll failed: {exc}", exc_info=True)

        try:
            save_section(cfg.state_file, "bms", bms_readings)
        except Exception as exc:
            log.error(f"Failed to write BMS state: {exc}")

        for r in bms_readings:
            entry = {"timestamp": r.timestamp, "voltage_v": r.voltage_v,
                     "current_a": r.current_a, "power_w": r.power_w,
                     "capacity_pct": r.capacity_pct}
            history.setdefault(r.name, []).append(entry)
            if len(history[r.name]) > cfg.max_history:
                history[r.name] = history[r.name][-cfg.max_history:]

        try:
            state = load_state(cfg.state_file)
            victron_readings = state["victron"]["readings"]
            html = build_html(bms_readings, victron_readings, history,
                              theme=cfg.theme)
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
        elif arg.startswith(("--log-level=", "--log_level=")):
            _pre_level = getattr(logging, arg.split("=", 1)[1].upper(), logging.INFO)
    logging.basicConfig(level=_pre_level,
                        format="%(asctime)s [%(levelname)s]  %(message)s",
                        datefmt="%H:%M:%S")
    asyncio.run(main())


if __name__ == "__main__":
    run()
