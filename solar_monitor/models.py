"""
solar_monitor/models.py — Shared data model
============================================
Central DeviceReading dataclass used by all protocol modules and the
dashboard renderer. Having it in a separate module avoids circular imports
between jbd, victron, scanner, and dashboard.
"""
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class DeviceReading:
    """
    Normalised reading from any monitored device (BMS or Victron).

    Fields are populated by the relevant protocol module; fields not
    applicable to a device type are left as None.

    device_type values:
        "bms"      — JBD / Vatrer battery pack
        "mppt"     — Victron Solar Charger
        "inverter" — Victron Inverter (Phoenix, MultiPlus, etc.)
        "monitor"  — Victron Battery Monitor (SmartShunt, BMV)
        "dcdc"     — Victron DC-DC Converter / Orion
        "lithium"  — Victron SmartLithium
        "meter"    — Victron DC Energy Meter
        "victron"  — Unrecognised Victron record type
    """
    address:         str
    name:            str
    device_type:     str
    timestamp:       str

    # Electrical fundamentals (all device types)
    voltage_v:       Optional[float] = None   # DC battery/pack voltage
    current_a:       Optional[float] = None   # DC current (+ = charge, - = discharge)
    power_w:         Optional[float] = None   # DC power; for inverters = AC apparent power

    # BMS / Battery Monitor fields
    capacity_pct:    Optional[int]   = None   # State of charge 0-100 %
    cell_count:      Optional[int]   = None   # Number of cells in series
    temp_c:          list            = field(default_factory=list)  # NTC sensor readings
    ttg_minutes:     Optional[int]   = None   # Time to go (minutes)
    alarm_reason:    Optional[int]   = None   # Alarm bitmask

    # Solar Charger (MPPT) fields
    pv_power_w:      Optional[float] = None   # PV panel input power
    yield_today_wh:  Optional[float] = None   # Energy harvested today
    load_current_a:  Optional[float] = None   # Load output current (models with load terminal)
    charger_state:   Optional[str]   = None   # "Off" / "Bulk" / "Absorption" / "Float" etc.

    # Inverter fields
    ac_out_power_va:  Optional[float] = None  # AC output power (real W for VE.Bus, VA for others)
    ac_out_voltage_v: Optional[float] = None  # AC output voltage
    ac_out_current_a: Optional[float] = None  # AC output current
    inverter_state:   Optional[str]   = None  # "Off" / "Inverting" / "Fault" etc.

    # VE.Bus Smart Dongle exclusive fields (record 0x0C)
    ac_in_power_w:    Optional[float] = None  # AC input real power (W); +ve = from grid
    ac_in_source:     Optional[str]   = None  # "AC1", "AC2", "Not connected"
    vebus_error:      Optional[int]   = None  # VE.Bus error code (0 = no error)
    temperature_c:    Optional[float] = None  # Battery temperature from dongle (°C)

    # MultiPlus-II 0x07 record diagnostic — byte[8] of decrypted payload.
    # Empirically varies with AC load but scale is not yet determined.
    # Exposed in dashboard for user calibration against VictronConnect.
    raw_load_indicator: Optional[int] = None

    # Shared error / diagnostic
    error_code:      Optional[int]   = None
    error:           Optional[str]   = None   # Human-readable error; None = success
