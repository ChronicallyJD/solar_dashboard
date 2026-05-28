"""
victron_monitor.py — Standalone Victron BLE polling process
===========================================================
Polls Victron devices (MPPT, VE.Bus Smart Dongle, etc.) via BLE
Instant Readout advertisements on a configurable interval (default 30 s)
and writes results to the shared state file.

Usage
-----
    python victron_monitor.py [--config FILE] [--interval SECS] [--once]

The BMS monitor (bms_monitor.py) runs as a separate process with a longer
interval.  Both write to the same shared state file.

Why a separate process?
-----------------------
Victron Instant Readout is entirely passive — no GATT connection needed.
The BLE scanner listens for advertisements, which are received in seconds.
This makes Victron polling fast and reliable, and it should not be blocked
by slow or hung BMS GATT connections.

Running as a separate process allows:
  - Short refresh interval (30 s or less) for live power/voltage data.
  - Independent restart when the Victron advertisement key changes.
  - No penalty for BMS connection failures.
"""

import argparse
import asyncio
import logging
import time
from datetime import datetime
from pathlib import Path

from solar_monitor.config import DEFAULT_INI_PATH, load_config, apply_cli_overrides
from solar_monitor.dashboard import build_html
from solar_monitor.scanner import resolve_devices, _poll_victron
from solar_monitor.state import load_state, save_section

log = logging.getLogger(__name__)

_MIN_VICTRON_GAP = 10.0


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Solar Monitor — Victron process (MPPT, VE.Bus via BLE)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python victron_monitor.py\n"
            "  python victron_monitor.py --interval 15\n"
            "  python victron_monitor.py --once --log-level DEBUG\n"
        ),
    )
    parser.add_argument("--config", metavar="FILE",
                        help=f"Config file (default: {DEFAULT_INI_PATH})")
    parser.add_argument("--interval", type=float, metavar="SECS",
                        help="Victron poll interval (overrides victron_interval in INI)")
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
        cfg.victron_interval = args.interval

    logging.getLogger().setLevel(getattr(logging, cfg.log_level, logging.INFO))

    interval    = max(cfg.victron_interval, _MIN_VICTRON_GAP)
    output_path = Path(cfg.output)
    history: dict[str, list[dict]] = {}

    log.info(
        f"Victron Monitor starting — state: {cfg.state_file}  "
        f"dashboard: {output_path}  interval: {interval}s"
    )

    while True:
        cycle_start = time.monotonic()

        # ── Victron scan ──────────────────────────────────────────────────────
        victron_readings = []
        scanner = None
        try:
            _, mppt_triples, scanner = await resolve_devices(cfg)
            if mppt_triples:
                log.info("Scanning for BLE devices … (Victron)")
                await scanner.scan(cfg.scan_timeout)
                victron_readings = _poll_victron(mppt_triples, scanner)
            else:
                log.info("No Victron devices configured — skipping")
        except Exception as exc:
            log.error(f"Victron poll failed: {exc}", exc_info=True)
        finally:
            if scanner is not None:
                await scanner.stop()

        # ── Write shared state ────────────────────────────────────────────────
        try:
            save_section(cfg.state_file, "victron", victron_readings)
        except Exception as exc:
            log.error(f"Failed to write Victron state: {exc}")

        # ── Update history ────────────────────────────────────────────────────
        for r in victron_readings:
            entry = {
                "timestamp":       r.timestamp,
                "voltage_v":       r.voltage_v,
                "current_a":       r.current_a,
                "power_w":         r.power_w,
                "capacity_pct":    r.capacity_pct,
                "pv_power_w":      r.pv_power_w,
                "yield_today_wh":  r.yield_today_wh,
                "ac_out_power_va": r.ac_out_power_va,
            }
            history.setdefault(r.name, []).append(entry)
            if len(history[r.name]) > cfg.max_history:
                history[r.name] = history[r.name][-cfg.max_history:]

        # ── Render dashboard (merge with latest BMS state) ────────────────────
        try:
            state = load_state(cfg.state_file)
            bms_readings = state["bms"]["readings"]
            html = build_html(bms_readings, victron_readings, history, theme=cfg.theme)
            output_path.write_text(html, encoding="utf-8")
            log.info(f"Dashboard written -> {output_path.resolve()}")
        except Exception as exc:
            log.error(f"Failed to write dashboard: {exc}", exc_info=True)

        if cfg.once or args.once:
            break

        elapsed   = time.monotonic() - cycle_start
        sleep_for = max(_MIN_VICTRON_GAP, interval - elapsed)
        log.info(f"Victron: next poll in {sleep_for:.0f}s ...")
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
