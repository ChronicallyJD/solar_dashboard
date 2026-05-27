"""
solar_monitor — BLE-based solar system monitor
===============================================
Polls JBD/Vatrer BMS devices over GATT and reads Victron Instant Readout
advertisements, then renders a self-contained HTML dashboard.

Package structure:
    models.py   — DeviceReading dataclass (shared by all modules)
    config.py   — AppConfig, DeviceConfig, INI/CLI parsing
    jbd.py      — JBD BMS GATT protocol
    victron.py  — Victron Instant Readout BLE advertisement parsing
    scanner.py  — BLE scanner, device resolution, poll orchestration
    dashboard.py— HTML template, card renderers, build_html()
    __main__.py — CLI entry point (python -m solar_monitor)
"""
from .models import DeviceReading
from .config import AppConfig, DeviceConfig, load_config, apply_cli_overrides

__all__ = ["DeviceReading", "AppConfig", "DeviceConfig",
           "load_config", "apply_cli_overrides"]
