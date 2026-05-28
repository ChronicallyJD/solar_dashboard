"""
console_monitor.py — Rich live console dashboard
==================================================
Displays the same data as the HTML dashboard, but rendered directly in the
terminal using the Rich library.  Polls the shared state file and re-renders
whenever either worker writes new data.

Usage
-----
    python console_monitor.py [--config FILE] [--state-file FILE] [--interval SECS]

The console monitor is read-only — it never writes to the state file.
It can run alongside the supervisor, or standalone when you want a live
terminal view without opening a browser.

Layout (mirrors the HTML dashboard)
-------------------------------------
  Row 1 (header bar): brand · timestamp · pack/device counts
  Row 2 (aggregates):  MPPT | Inverter | Battery  ← side by side
  Row 3+: individual MPPT cards (one column per device)
  Row 4+: individual Inverter cards
  Row 5+: individual BMS cards

Requirements
-------------
    pip install rich

Rich is NOT required for the rest of solar_monitor to function.
If rich is not installed the script exits with a clear message.
"""

import argparse
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

# ── Guard: fail fast with a helpful message if Rich is not installed ──────────
try:
    from rich.console import Console
    from rich.layout import Layout
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    from rich import box
except ImportError:
    print(
        "\nERROR: the 'rich' library is required for the console dashboard.\n"
        "Install it with:\n\n"
        "    pip install rich\n",
        file=sys.stderr,
    )
    sys.exit(1)

sys.path.insert(0, str(Path(__file__).parent))
from solar_monitor.state import load_state
from solar_monitor.models import DeviceReading

# ─────────────────────────────────────────────────────────────────────────────
# Colour palette — mapped to Rich style strings
# ─────────────────────────────────────────────────────────────────────────────

C_VOLT    = "bright_cyan"
C_AMP     = "bright_green"
C_WATT    = "bright_yellow"
C_PV      = "yellow"
C_VIOLET  = "bright_magenta"
C_MUTED   = "bright_black"
C_RED     = "bright_red"
C_GREEN   = "bright_green"
C_AMBER   = "yellow"
C_OK      = "green"
C_ERR     = "red"
C_HEADER  = "bold bright_white"
C_LABEL   = "bright_black"


def _fmt(v, decimals: int = 2) -> str:
    return f"{v:.{decimals}f}" if v is not None else "—"


def _soc_bar(pct: Optional[int], width: int = 20) -> Text:
    """Return a Rich Text object showing a coloured ASCII progress bar."""
    if pct is None:
        return Text("—", style=C_MUTED)
    filled = int(round(pct / 100 * width))
    empty  = width - filled
    color  = C_GREEN if pct >= 60 else (C_AMBER if pct >= 30 else C_RED)
    bar = Text()
    bar.append("█" * filled, style=color)
    bar.append("░" * empty,  style=C_MUTED)
    bar.append(f"  {pct}%",  style=color + " bold")
    return bar


def _tte(h: Optional[float]) -> str:
    if h is None:
        return ""
    hrs  = int(h)
    mins = int((h - hrs) * 60)
    return f"{hrs}h{mins:02d}m"


# ─────────────────────────────────────────────────────────────────────────────
# Aggregate panels
# ─────────────────────────────────────────────────────────────────────────────

def _mppt_aggregate_panel(mppt_readings: list) -> Panel:
    ok    = [r for r in mppt_readings if not r.error and r.device_type == "mppt"]
    total = len([r for r in mppt_readings if r.device_type == "mppt"])

    total_pv    = sum(r.pv_power_w    or 0 for r in ok)
    total_yield = sum(r.yield_today_wh or 0 for r in ok)

    states: dict[str, int] = {}
    for r in ok:
        s = r.charger_state or "?"
        states[s] = states.get(s, 0) + 1
    state_str = "  ·  ".join(f"{n}× {s}" for s, n in sorted(states.items())) or "—"

    t = Table.grid(padding=(0, 2))
    t.add_column(style=C_LABEL, no_wrap=True)
    t.add_column(justify="right", no_wrap=True)

    t.add_row("PV Power",
              Text(f"{_fmt(total_pv, 1)} W", style=C_PV + " bold"))
    t.add_row("Yield Today",
              Text(f"{_fmt(total_yield, 0)} Wh", style=C_VIOLET))
    t.add_row("Online",
              Text(f"{len(ok)}/{total}", style=C_OK if ok else C_ERR))
    t.add_row("States",
              Text(state_str, style=C_MUTED))

    return Panel(t, title="[bold yellow]☀  MPPT Chargers[/]",
                 border_style="yellow", padding=(0, 1))


def _inverter_aggregate_panel(mppt_readings: list) -> Panel:
    ok    = [r for r in mppt_readings if not r.error and r.device_type == "inverter"]
    total = len([r for r in mppt_readings if r.device_type == "inverter"])

    total_ac = sum(r.ac_out_power_va or 0 for r in ok)

    states: dict[str, int] = {}
    for r in ok:
        s = r.inverter_state or "?"
        states[s] = states.get(s, 0) + 1
    state_str = "  ·  ".join(f"{n}× {s}" for s, n in sorted(states.items())) or "—"

    alarms = [r for r in ok if r.alarm_reason and r.alarm_reason not in (None, 0, "None")]
    alarm_style = C_RED if alarms else C_OK
    alarm_str   = f"{len(alarms)} alarm(s)" if alarms else "None"

    t = Table.grid(padding=(0, 2))
    t.add_column(style=C_LABEL, no_wrap=True)
    t.add_column(justify="right", no_wrap=True)

    t.add_row("AC Output",
              Text(f"{_fmt(total_ac, 0)} W", style=C_VIOLET + " bold"))
    t.add_row("Online",
              Text(f"{len(ok)}/{total}", style=C_OK if ok else C_ERR))
    t.add_row("States",
              Text(state_str, style=C_MUTED))
    t.add_row("Alarms",
              Text(alarm_str, style=alarm_style))

    return Panel(t, title="[bold bright_magenta]⚡  Inverter / VE.Bus[/]",
                 border_style="bright_magenta", padding=(0, 1))


def _battery_aggregate_panel(bms_readings: list) -> Panel:
    ok    = [r for r in bms_readings if not r.error and r.capacity_pct is not None]
    total = len(bms_readings)

    avg_soc  = round(sum(r.capacity_pct for r in ok) / len(ok)) if ok else None
    total_wh = sum(r.remain_wh or 0 for r in ok)
    nom_wh   = sum(r.nominal_wh or 0 for r in ok)
    total_ah = sum(r.remain_ah or 0 for r in ok)
    net_a    = sum(r.current_a or 0 for r in ok if r.current_a is not None)

    t = Table.grid(padding=(0, 2))
    t.add_column(style=C_LABEL, no_wrap=True)
    t.add_column(justify="right", no_wrap=True)

    t.add_row("Avg SoC",     _soc_bar(avg_soc, 16))
    t.add_row("Remaining",
              Text(f"{_fmt(total_wh, 0)} / {_fmt(nom_wh, 0)} Wh",
                   style=C_VOLT))
    t.add_row("Ah",
              Text(f"{_fmt(total_ah, 1)} Ah", style=C_MUTED))
    t.add_row("Net current",
              Text(f"{_fmt(net_a, 1)} A",
                   style=C_AMP if net_a >= 0 else C_AMBER))
    t.add_row("Online",
              Text(f"{len(ok)}/{total}",
                   style=C_OK if ok else C_ERR))

    return Panel(t, title="[bold bright_cyan]🔋  Battery Bank[/]",
                 border_style="bright_cyan", padding=(0, 1))


# ─────────────────────────────────────────────────────────────────────────────
# Individual device panels
# ─────────────────────────────────────────────────────────────────────────────

def _bms_device_panel(r: DeviceReading) -> Panel:
    status_style = C_ERR if r.error else C_OK
    status_str   = "OFFLINE" if r.error else "ONLINE"
    title = f"[bold]{r.name}[/]  [{status_style}]{status_str}[/]"

    if r.error:
        body = Table.grid(padding=(0, 1))
        body.add_column()
        body.add_row(Text(f"⚠ {r.error}", style=C_ERR))
        return Panel(body, title=title, border_style=C_ERR, padding=(0, 1))

    t = Table.grid(padding=(0, 2))
    t.add_column(style=C_LABEL, no_wrap=True)
    t.add_column(justify="right", no_wrap=True)

    # Electrical
    t.add_row("Voltage",  Text(f"{_fmt(r.voltage_v, 2)} V",  style=C_VOLT))
    t.add_row("Current",  Text(f"{_fmt(r.current_a, 2)} A",  style=C_AMP))
    t.add_row("Power",    Text(f"{_fmt(r.power_w,   1)} W",  style=C_WATT))

    # SoC
    t.add_row("SoC",      _soc_bar(r.capacity_pct, 16))

    # Capacity
    if r.remain_wh is not None:
        t.add_row("Remaining", Text(f"{_fmt(r.remain_wh, 0)} Wh", style=C_MUTED))
    if r.remain_ah is not None and r.nominal_ah is not None:
        t.add_row("Ah",        Text(f"{_fmt(r.remain_ah, 1)} / {_fmt(r.nominal_ah, 1)}", style=C_MUTED))

    # TTE / TTF
    tte = _tte(r.time_to_empty_h)
    ttf = _tte(r.time_to_full_h)
    if tte:
        t.add_row("TTE", Text(tte, style=C_AMBER))
    if ttf:
        t.add_row("TTF", Text(ttf, style=C_GREEN))

    # Pack info
    info_parts = []
    if r.cell_count:    info_parts.append(f"{r.cell_count} cells")
    if r.cycle_count:   info_parts.append(f"{r.cycle_count} cycles")
    if r.charge_fet is not None:
        cfet = "✓" if r.charge_fet else "✗"
        dfet = "✓" if r.discharge_fet else "✗"
        info_parts.append(f"CHG {cfet} DSG {dfet}")
    if info_parts:
        t.add_row("Info", Text("  ·  ".join(info_parts), style=C_MUTED))

    # Temperatures
    if r.temp_c:
        t.add_row("Temp", Text("  ".join(f"{t_}°C" for t_ in r.temp_c), style=C_MUTED))

    # Faults
    if r.faults:
        t.add_row("Faults", Text(", ".join(r.faults), style=C_RED + " bold"))

    # Balancing
    if r.balance_cells and any(r.balance_cells):
        bal_cells = [str(i+1) for i, b in enumerate(r.balance_cells) if b]
        t.add_row("Balancing", Text(f"⚡ cells {', '.join(bal_cells)}", style=C_AMBER))

    return Panel(t, title=title, border_style=C_VOLT, padding=(0, 1))


def _victron_device_panel(r: DeviceReading) -> Panel:
    status_style = C_ERR if r.error else C_OK
    status_str   = "OFFLINE" if r.error else "ONLINE"

    type_labels = {
        "mppt":     ("MPPT",     C_PV,     "yellow"),
        "inverter": ("INVERTER", C_VIOLET, "bright_magenta"),
        "monitor":  ("MONITOR",  C_AMP,    "bright_green"),
        "dcdc":     ("DC-DC",    C_MUTED,  "bright_black"),
    }
    type_lbl, _, border = type_labels.get(r.device_type, ("VICTRON", C_MUTED, "white"))
    title = f"[bold]{r.name}[/]  [{C_MUTED}]{type_lbl}[/]  [{status_style}]{status_str}[/]"

    if r.error:
        body = Table.grid(padding=(0, 1))
        body.add_column()
        body.add_row(Text(f"⚠ {r.error}", style=C_ERR))
        return Panel(body, title=title, border_style=C_ERR, padding=(0, 1))

    t = Table.grid(padding=(0, 2))
    t.add_column(style=C_LABEL, no_wrap=True)
    t.add_column(justify="right", no_wrap=True)

    if r.device_type == "inverter":
        # VE.Bus layout
        if r.ac_out_power_va is not None:
            t.add_row("AC Out",    Text(f"{_fmt(r.ac_out_power_va, 0)} W", style=C_VIOLET + " bold"))
        t.add_row("DC Batt V",     Text(f"{_fmt(r.voltage_v, 2)} V",  style=C_VOLT))
        t.add_row("DC Batt A",     Text(f"{_fmt(r.current_a, 2)} A",  style=C_AMP))
        if r.temperature_c is not None:
            t.add_row("Temp",      Text(f"{r.temperature_c}°C",       style=C_MUTED))
        state_str = r.inverter_state or "—"
        t.add_row("State",         Text(state_str,                    style=C_MUTED))
        t.add_row("AC In",         Text(r.ac_in_source or "—",        style=C_MUTED))
        if r.alarm_reason and r.alarm_reason not in (None, 0, "None"):
            t.add_row("ALARM",     Text(str(r.alarm_reason),          style=C_RED + " bold"))

    elif r.device_type == "mppt":
        t.add_row("PV Power",      Text(f"{_fmt(r.pv_power_w, 1)} W",      style=C_PV + " bold"))
        t.add_row("Yield Today",   Text(f"{_fmt(r.yield_today_wh, 0)} Wh", style=C_VIOLET))
        t.add_row("Batt V",        Text(f"{_fmt(r.voltage_v, 2)} V",        style=C_VOLT))
        t.add_row("Batt A",        Text(f"{_fmt(r.current_a, 2)} A",        style=C_AMP))
        t.add_row("State",         Text(r.charger_state or "—",             style=C_MUTED))
        if r.load_current_a is not None:
            t.add_row("Load",      Text(f"{_fmt(r.load_current_a, 1)} A",   style=C_MUTED))

    elif r.device_type == "monitor":
        t.add_row("Batt V",        Text(f"{_fmt(r.voltage_v, 2)} V",  style=C_VOLT))
        t.add_row("Current",       Text(f"{_fmt(r.current_a, 2)} A",  style=C_AMP))
        t.add_row("SoC",           _soc_bar(r.capacity_pct, 16))
        if r.ttg_minutes is not None:
            ttg_h = r.ttg_minutes // 60
            ttg_m = r.ttg_minutes % 60
            t.add_row("TTG",       Text(f"{ttg_h}h{ttg_m:02d}m",     style=C_AMBER))

    else:
        # Generic fallback
        if r.voltage_v is not None:
            t.add_row("Voltage",   Text(f"{_fmt(r.voltage_v, 2)} V",  style=C_VOLT))
        if r.current_a is not None:
            t.add_row("Current",   Text(f"{_fmt(r.current_a, 2)} A",  style=C_AMP))
        if r.power_w is not None:
            t.add_row("Power",     Text(f"{_fmt(r.power_w, 1)} W",    style=C_WATT))

    return Panel(t, title=title, border_style=border, padding=(0, 1))


# ─────────────────────────────────────────────────────────────────────────────
# Header bar
# ─────────────────────────────────────────────────────────────────────────────

def _header(bms_readings: list, mppt_readings: list,
            bms_updated: Optional[str], vic_updated: Optional[str]) -> Panel:
    t = Table.grid(padding=(0, 3), expand=True)
    t.add_column(ratio=1)
    t.add_column(ratio=1, justify="center")
    t.add_column(ratio=1, justify="right")

    brand = Text("Solar", style="bold bright_cyan") + Text(" Monitor", style="bold yellow")

    counts = Text()
    counts.append(f"{len(bms_readings)} BMS", style=C_VOLT)
    counts.append("  ·  ", style=C_MUTED)
    counts.append(f"{len(mppt_readings)} Victron", style=C_PV)

    ts_parts = []
    if bms_updated:
        ts_parts.append(f"BMS {bms_updated[-8:]}")
    if vic_updated:
        ts_parts.append(f"Victron {vic_updated[-8:]}")
    ts_str = "  ·  ".join(ts_parts) if ts_parts else "waiting…"
    timestamp = Text(ts_str, style=C_MUTED)

    t.add_row(brand, counts, timestamp)
    return Panel(t, style="on #0d1117", padding=(0, 1))


# ─────────────────────────────────────────────────────────────────────────────
# Full screen render
# ─────────────────────────────────────────────────────────────────────────────

def _render(state: dict) -> Table:
    """Compose the full console layout as a Rich renderable."""
    bms_readings  = state["bms"]["readings"]
    mppt_readings = state["victron"]["readings"]
    bms_updated   = state["bms"].get("updated")
    vic_updated   = state["victron"].get("updated")

    mppt_solar = [r for r in mppt_readings if r.device_type == "mppt"]
    mppt_inv   = [r for r in mppt_readings if r.device_type == "inverter"]
    mppt_other = [r for r in mppt_readings if r.device_type not in ("mppt", "inverter")]

    root = Table.grid(expand=True)
    root.add_column()

    # Header
    root.add_row(_header(bms_readings, mppt_readings, bms_updated, vic_updated))

    # ── Aggregate row ─────────────────────────────────────────────────────────
    agg_row = Table.grid(expand=True)
    agg_row.add_column(ratio=1)
    agg_row.add_column(ratio=1)
    agg_row.add_column(ratio=1)
    agg_row.add_row(
        _mppt_aggregate_panel(mppt_readings),
        _inverter_aggregate_panel(mppt_readings),
        _battery_aggregate_panel(bms_readings),
    )
    root.add_row(agg_row)

    # ── MPPT individual ───────────────────────────────────────────────────────
    if mppt_solar or mppt_other:
        all_mppt = mppt_solar + mppt_other
        row = Table.grid(expand=True)
        for _ in all_mppt:
            row.add_column(ratio=1)
        row.add_row(*(_victron_device_panel(r) for r in all_mppt))
        root.add_row(row)

    # ── Inverter individual ───────────────────────────────────────────────────
    if mppt_inv:
        row = Table.grid(expand=True)
        for _ in mppt_inv:
            row.add_column(ratio=1)
        row.add_row(*(_victron_device_panel(r) for r in mppt_inv))
        root.add_row(row)

    # ── BMS individual ────────────────────────────────────────────────────────
    if bms_readings:
        # Wrap into rows of up to 3 cards
        PER_ROW = 3
        chunks = [bms_readings[i:i+PER_ROW]
                  for i in range(0, len(bms_readings), PER_ROW)]
        for chunk in chunks:
            row = Table.grid(expand=True)
            for _ in range(PER_ROW):
                row.add_column(ratio=1)
            # Pad short last row with empty cells
            cells = [_bms_device_panel(r) for r in chunk]
            while len(cells) < PER_ROW:
                cells.append("")
            row.add_row(*cells)
            root.add_row(row)

    # Footer
    now = datetime.now().strftime("%H:%M:%S")
    root.add_row(
        Text(f"  {now}  ·  Press Ctrl-C to exit", style=C_MUTED)
    )

    return root


# ─────────────────────────────────────────────────────────────────────────────
# File-change watcher
# ─────────────────────────────────────────────────────────────────────────────

def _mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Solar Monitor — live Rich console dashboard",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python console_monitor.py\n"
            "  python console_monitor.py --state-file /run/solar/state.json\n"
            "  python console_monitor.py --interval 5\n"
        ),
    )
    parser.add_argument("--config",     metavar="FILE",
                        help="Config file (reads state_file path from it)")
    parser.add_argument("--state-file", metavar="FILE",
                        help="Shared state JSON file (overrides config)")
    parser.add_argument("--interval",   type=float, default=2.0, metavar="SECS",
                        help="How often to check for new data (default: 2s)")
    args = parser.parse_args()

    # Resolve state file path
    state_path = args.state_file
    if not state_path and args.config:
        try:
            from solar_monitor.config import load_config
            cfg = load_config(args.config)
            state_path = cfg.state_file
        except Exception:
            pass
    if not state_path:
        state_path = "solar_state.json"

    console = Console()
    interval = max(0.5, args.interval)

    console.print(
        f"\n[bold bright_cyan]Solar Monitor[/] — console dashboard\n"
        f"[bright_black]Watching:[/] {state_path}\n"
        f"[bright_black]Interval:[/] {interval}s    "
        f"[bright_black]Press Ctrl-C to exit[/]\n"
    )

    last_mtime = -1.0
    state = {
        "bms":     {"updated": None, "readings": []},
        "victron": {"updated": None, "readings": []},
    }

    with Live(
        _render(state),
        console=console,
        refresh_per_second=4,
        screen=True,           # full-screen mode — clears on exit
    ) as live:
        try:
            while True:
                mtime = _mtime(state_path)
                if mtime != last_mtime:
                    state      = load_state(state_path)
                    last_mtime = mtime
                    live.update(_render(state))
                time.sleep(interval)
        except KeyboardInterrupt:
            pass

    console.print("\n[bright_black]Console dashboard closed.[/]\n")


if __name__ == "__main__":
    main()
