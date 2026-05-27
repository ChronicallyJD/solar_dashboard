"""
solar_monitor/__main__.py — CLI entry point
===========================================
Run the Solar Monitor as a package:
    python -m solar_monitor

Or via the installed script:
    solar_monitor
"""

import argparse
import asyncio
import logging
import time
from pathlib import Path

from .config import (
    DEFAULT_INI_PATH, load_config, apply_cli_overrides, write_example_ini,
)
from .dashboard import build_html
from .scanner import resolve_devices, poll_all

log = logging.getLogger(__name__)


async def main() -> None:
    """Parse CLI arguments, load config, and run the polling loop."""
    parser = argparse.ArgumentParser(
        description=(
            "Solar Monitor — JBD/Vatrer BMS + Victron Instant Readout "
            "Bluetooth Dashboard"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python -m solar_monitor\n"
            "  python -m solar_monitor --config /etc/solar/monitor.ini\n"
            "  python -m solar_monitor --once --log-level DEBUG\n"
            "  python -m solar_monitor --write-example-config monitor.ini.example\n"
        ),
    )
    parser.add_argument(
        "--config", metavar="FILE",
        help=f"INI configuration file (default: {DEFAULT_INI_PATH})",
    )
    parser.add_argument(
        "--write-example-config", metavar="FILE", nargs="?",
        const="monitor.ini.example",
        help="Write an annotated example INI file and exit",
    )
    parser.add_argument(
        "--interval", type=float, metavar="SECS",
        help="Poll interval in seconds (overrides INI [general] interval)",
    )
    parser.add_argument(
        "--output", metavar="FILE",
        help="HTML dashboard output path (overrides INI [general] output)",
    )
    parser.add_argument(
        "--scan-timeout", type=float, metavar="SECS",
        help="BLE scan duration per poll cycle (overrides INI [general] scan_timeout)",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Poll once and exit (useful for cron or debugging)",
    )
    parser.add_argument(
        "--bms", nargs="+", metavar="MAC",
        help="Explicit BMS MAC addresses — overrides [bms] section entirely",
    )
    parser.add_argument(
        "--mppt", nargs="+", metavar="MAC:KEY",
        help="Explicit Victron MAC:advertisement_key pairs — overrides [victron]",
    )
    parser.add_argument(
        "--log-level", metavar="LEVEL", default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help=(
            "Console log verbosity.  DEBUG logs full GATT tables and all "
            "Victron advertisement candidate payloads."
        ),
    )
    parser.add_argument(
        "--theme", metavar="THEME", default=None,
        choices=["dark", "light", "business"],
        help="Dashboard colour theme (overrides INI [general] theme)",
    )
    args = parser.parse_args()

    # --write-example-config shortcut ─────────────────────────────────────────
    if args.write_example_config:
        write_example_ini(args.write_example_config)
        return

    # Load config, overlay CLI, apply log level ───────────────────────────────
    cfg = load_config(args.config)
    cfg = apply_cli_overrides(cfg, args)

    if args.log_level:
        cfg.log_level = args.log_level
    if args.theme:
        cfg.theme = args.theme

    logging.getLogger().setLevel(
        getattr(logging, cfg.log_level, logging.INFO)
    )

    output_path = Path(cfg.output)
    history: dict[str, list[dict]] = {}

    log.info(
        f"Solar Monitor starting -- output: {output_path}  "
        f"interval: {cfg.interval}s  theme: {cfg.theme}"
    )

    # ── Main polling loop ─────────────────────────────────────────────────────
    while True:
        loop_start = time.monotonic()

        try:
            jbd_pairs, mppt_triples, scanner = await resolve_devices(cfg)
            bms_readings, mppt_readings = await poll_all(
                jbd_pairs, mppt_triples, scanner
            )
        except Exception as exc:
            log.error(f"Poll cycle failed: {exc}", exc_info=True)
            bms_readings, mppt_readings = [], []

        # Update rolling history ──────────────────────────────────────────────
        for r in bms_readings + mppt_readings:
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

        # Write dashboard ─────────────────────────────────────────────────────
        try:
            html = build_html(
                bms_readings, mppt_readings, history, theme=cfg.theme
            )
            output_path.write_text(html, encoding="utf-8")
            log.info(f"Dashboard written -> {output_path.resolve()}")
        except Exception as exc:
            log.error(f"Failed to write dashboard: {exc}", exc_info=True)

        if cfg.once:
            break

        # Enforce minimum inter-poll gap ──────────────────────────────────────
        # BlueZ needs at least 10 seconds to fully release all GATT connections
        # before the next scan begins.  Shorter gaps cause "Operation already
        # in progress" errors at the start of the next poll cycle.
        elapsed   = time.monotonic() - loop_start
        min_gap   = max(cfg.interval, 10.0)
        sleep_for = max(10.0, min_gap - elapsed)
        log.info(f"Next poll in {sleep_for:.1f}s ...")
        await asyncio.sleep(sleep_for)


def run() -> None:
    """Synchronous entry point for installed script / ``python -m solar_monitor``."""
    # Pre-parse --log-level so basicConfig uses the right level from the start.
    # A full argparse run happens inside main(); this is just a quick peek.
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
