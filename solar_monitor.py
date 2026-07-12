"""
solar_monitor.py - Supervisor process
======================================
Starts and supervises worker subprocesses - one per data source.
Each worker runs independently, writes to its own state section, and
is restarted automatically if it crashes.

Why a supervisor?
-----------------
Running workers as separate subprocesses gives true process isolation:
a crashed BMS worker cannot affect Victron data collection, and vice
versa.  The supervisor is a single entry point replacing two terminals.

Worker contract
---------------
Any script that satisfies this interface is a valid worker:
  - Accepts: --config FILE --state-file FILE --log-level LEVEL --once
  - Writes:  its named section to the shared state file each cycle
  - Exits:   0 on clean shutdown, non-zero on error

Adding a new data source
------------------------
1. Write a worker script (e.g. ecoflow_monitor.py) following the contract.
2. Add a WorkerSpec to WORKER_REGISTRY in this file.
3. Add the corresponding config section (e.g. [ecoflow]).
4. That's it - the supervisor picks it up automatically when the section
   is populated.

Crash policy
------------
Workers are restarted with exponential backoff (1s → 2s → 4s → … → 60s)
up to MAX_CRASHES_PER_HOUR times per hour.  If a worker crashes more than
that, it is stopped and an error is logged.  The other workers keep running.

Dashboard
---------
The supervisor writes the dashboard after every worker poll cycle by
merging all current state sections.  This means the dashboard reflects
the latest data from all sources regardless of their individual intervals.
"""

import argparse
import asyncio
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger("supervisor")


# ─────────────────────────────────────────────────────────────────────────────
# Worker registry - add new data sources here
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class WorkerSpec:
    """
    Describes a worker process that the supervisor manages.

    Parameters
    ----------
    name:
        Human-readable label used in log prefixes and status messages.
    script:
        Path to the worker script, relative to the supervisor's directory.
    state_section:
        The key this worker owns in the shared state JSON file
        (e.g. "bms", "victron").
    config_sections:
        INI config section names that must be non-empty for this worker
        to be enabled.  Worker is skipped if all sections are absent/empty.
    interval_cfg_key:
        The AppConfig attribute name that holds this worker's poll interval.
    min_gap:
        Minimum seconds between poll cycles regardless of config.
    """
    name:               str
    script:             str
    state_section:      str
    config_sections:    list[str]
    interval_cfg_key:   str
    min_gap:            float = 10.0


# Registry: add an entry here to add a new data source.
# Workers are started in the order listed.
WORKER_REGISTRY: list[WorkerSpec] = [
    WorkerSpec(
        name             = "Victron",
        script           = "victron_monitor.py",
        state_section    = "victron",
        config_sections  = ["victron", "mppt"],
        interval_cfg_key = "victron_interval",
        min_gap          = 10.0,
    ),
    WorkerSpec(
        name             = "BMS",
        script           = "bms_monitor.py",
        state_section    = "bms",
        config_sections  = ["bms"],
        interval_cfg_key = "bms_interval",
        min_gap          = 30.0,
    ),
    # Example of adding a future data source:
    # WorkerSpec(
    #     name             = "EcoFlow",
    #     script           = "ecoflow_monitor.py",
    #     state_section    = "ecoflow",
    #     config_sections  = ["ecoflow"],
    #     interval_cfg_key = "ecoflow_interval",
    #     min_gap          = 30.0,
    # ),
]

MAX_CRASHES_PER_HOUR = 10
MAX_BACKOFF          = 60.0   # seconds


# ─────────────────────────────────────────────────────────────────────────────
# Worker supervisor
# ─────────────────────────────────────────────────────────────────────────────

class WorkerProcess:
    """
    Manages a single worker subprocess - launch, log streaming, restart.
    """

    def __init__(
        self,
        spec:       WorkerSpec,
        python:     str,
        config:     str,
        state_file: str,
        log_level:  str,
        script_dir: str,
    ) -> None:
        self.spec       = spec
        self._python    = python
        self._config    = config
        self._state_file= state_file
        self._log_level = log_level
        self._script    = os.path.join(script_dir, spec.script)
        self._proc:     Optional[asyncio.subprocess.Process] = None

        # Crash tracking
        self._crash_times:  list[float] = []
        self._backoff:      float       = 1.0
        self._stopped:      bool        = False

    @property
    def name(self) -> str:
        return self.spec.name

    def _cmd(self) -> list[str]:
        return [
            self._python, self._script,
            "--config",     self._config,
            "--state-file", self._state_file,
            "--log-level",  self._log_level,
        ]

    async def _stream_output(self, stream, level: int) -> None:
        """Read lines from *stream* and re-log them with a worker prefix."""
        prefix = f"[{self.name}]"
        while True:
            try:
                line = await stream.readline()
            except Exception:
                break
            if not line:
                break
            text = line.decode(errors="replace").rstrip()
            if text:
                log.log(level, f"{prefix} {text}")

    async def run(self) -> None:
        """
        Main loop: launch the worker, stream its output, restart on exit.
        Stops when self._stopped is set or too many crashes occur.
        """
        while not self._stopped:
            log.info(f"[{self.name}] Starting worker: {' '.join(self._cmd())}")
            start_time = time.monotonic()

            try:
                self._proc = await asyncio.create_subprocess_exec(
                    *self._cmd(),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except Exception as exc:
                log.error(f"[{self.name}] Failed to start: {exc}")
                await asyncio.sleep(self._backoff)
                self._backoff = min(self._backoff * 2, MAX_BACKOFF)
                continue

            # Stream stdout and stderr concurrently
            await asyncio.gather(
                self._stream_output(self._proc.stdout, logging.INFO),
                self._stream_output(self._proc.stderr, logging.WARNING),
            )
            await self._proc.wait()
            exit_code  = self._proc.returncode
            runtime    = time.monotonic() - start_time

            if self._stopped:
                break

            log.warning(
                f"[{self.name}] Worker exited (code={exit_code}, "
                f"runtime={runtime:.1f}s)"
            )

            # Prune crash times older than 1 hour
            now = time.monotonic()
            self._crash_times = [t for t in self._crash_times if now - t < 3600]
            self._crash_times.append(now)

            if len(self._crash_times) > MAX_CRASHES_PER_HOUR:
                log.error(
                    f"[{self.name}] Worker crashed {len(self._crash_times)} times "
                    f"in the last hour - giving up. Fix the error and restart the "
                    f"supervisor to re-enable this worker."
                )
                self._stopped = True
                break

            log.info(
                f"[{self.name}] Restarting in {self._backoff:.1f}s "
                f"(crash #{len(self._crash_times)} this hour) …"
            )
            await asyncio.sleep(self._backoff)
            # Increase backoff only for rapid crashes (runtime < 30s)
            if runtime < 30:
                self._backoff = min(self._backoff * 2, MAX_BACKOFF)
            else:
                self._backoff = 1.0   # long-running before crash → reset

    async def stop(self) -> None:
        """Signal the worker to stop and wait for it to exit."""
        self._stopped = True
        if self._proc and self._proc.returncode is None:
            log.info(f"[{self.name}] Sending SIGTERM …")
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=10.0)
            except asyncio.TimeoutError:
                log.warning(f"[{self.name}] SIGTERM timed out - sending SIGKILL")
                try:
                    self._proc.kill()
                except Exception:
                    pass


# ─────────────────────────────────────────────────────────────────────────────
# Dashboard writer
# ─────────────────────────────────────────────────────────────────────────────

async def _dashboard_loop(
    state_file:  str,
    output_path: Path,
    theme:       str,
    interval:    float,
) -> None:
    """
    Periodically merge all state sections and write the dashboard HTML.
    Runs independently of the worker poll cycles so the dashboard is always
    as fresh as the latest combined state allows.
    """
    # Import here so the module can be imported without bleak at test time
    from solar_monitor.state import load_state
    from solar_monitor.dashboard import build_html

    history: dict = {}

    while True:
        await asyncio.sleep(interval)
        try:
            state        = load_state(state_file)
            bms_readings = state["bms"]["readings"]
            vic_readings = state["victron"]["readings"]

            for r in bms_readings + vic_readings:
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
                if len(history[r.name]) > 600:
                    history[r.name] = history[r.name][-600:]

            html = build_html(bms_readings, vic_readings, history, theme=theme)
            output_path.write_text(html, encoding="utf-8")
            log.info(f"Dashboard written -> {output_path.resolve()}")
        except Exception as exc:
            log.error(f"Dashboard write failed: {exc}", exc_info=True)


# ─────────────────────────────────────────────────────────────────────────────
# Config-section presence check
# ─────────────────────────────────────────────────────────────────────────────

def _section_has_devices(ini_path: str, section_names: list[str]) -> bool:
    """
    Return True if any of *section_names* exists and is non-empty in the INI.
    Used to determine which workers to enable.
    """
    import configparser
    p = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
    p.read(ini_path)
    for sec in section_names:
        if sec in p and dict(p[sec]):
            return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

async def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Solar Monitor supervisor - manages worker subprocesses.\n"
            "Workers are started automatically based on populated config sections."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Workers started based on config sections:\n"
            "  [bms]     → bms_monitor.py\n"
            "  [victron] → victron_monitor.py\n\n"
            "Add new workers by editing WORKER_REGISTRY in this file."
        ),
    )
    parser.add_argument("--config",     metavar="FILE", default="config.ini",
                        help="Config file (default: config.ini)")
    parser.add_argument("--log-level",  metavar="LEVEL", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    parser.add_argument("--list-workers", action="store_true",
                        help="List workers that would be started and exit")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level, logging.INFO),
        format="%(asctime)s [%(levelname)s]  %(message)s",
        datefmt="%H:%M:%S",
    )

    config_path = Path(args.config).resolve()
    if not config_path.exists():
        log.error(f"Config file not found: {config_path}")
        sys.exit(1)

    # Read minimal config for supervisor-level settings
    from solar_monitor.config import load_config
    cfg        = load_config(str(config_path))
    script_dir = str(Path(__file__).parent.resolve())
    python     = sys.executable

    # Determine which workers are needed
    active: list[WorkerSpec] = []
    for spec in WORKER_REGISTRY:
        if _section_has_devices(str(config_path), spec.config_sections):
            active.append(spec)
            log.info(f"Worker enabled: {spec.name} ({spec.script})")
        else:
            log.info(
                f"Worker skipped: {spec.name} - "
                f"no [{'/'.join(spec.config_sections)}] devices configured"
            )

    if args.list_workers:
        print("\nWorkers that would start:")
        for spec in active:
            print(f"  {spec.name:12}  {spec.script}")
        if not active:
            print("  (none - all config sections empty)")
        return

    if not active:
        log.error(
            "No workers to start - add devices to [bms] and/or [victron] "
            "in your config file."
        )
        sys.exit(1)

    log.info(
        f"Solar Monitor supervisor starting - "
        f"{len(active)} worker(s)  config: {config_path}"
    )

    # Build WorkerProcess objects
    workers = [
        WorkerProcess(
            spec       = spec,
            python     = python,
            config     = str(config_path),
            state_file = cfg.state_file,
            log_level  = args.log_level,
            script_dir = script_dir,
        )
        for spec in active
    ]

    # Dashboard refresh interval = fastest worker interval / 2, min 10s
    dash_interval = max(10.0, min(
        getattr(cfg, spec.interval_cfg_key, 30.0) for spec in active
    ) / 2)

    output_path = Path(cfg.output).resolve()
    log.info(
        f"Dashboard: {output_path}  "
        f"refresh interval: {dash_interval:.0f}s  "
        f"theme: {cfg.theme}"
    )

    # Run workers + dashboard writer + optional HTTPS server concurrently
    tasks = [asyncio.create_task(w.run()) for w in workers]
    tasks.append(asyncio.create_task(
        _dashboard_loop(cfg.state_file, output_path, cfg.theme, dash_interval)
    ))

    if cfg.server.enabled:
        from solar_monitor.server import run_https_server
        log.info(
            f"HTTPS server enabled - https://{cfg.server.host}:{cfg.server.port}/"
        )
        tasks.append(asyncio.create_task(
            run_https_server(cfg.server, output_path, cfg.state_file)
        ))
    else:
        log.info("HTTPS server disabled (set [server] enabled = true to enable)")

    try:
        await asyncio.gather(*tasks)
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("Supervisor shutting down …")
    finally:
        for w in workers:
            await w.stop()
        for t in tasks:
            t.cancel()
        log.info("Supervisor stopped.")


if __name__ == "__main__":
    asyncio.run(main())
