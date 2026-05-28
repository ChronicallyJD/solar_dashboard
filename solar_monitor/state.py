"""
solar_monitor/state.py — Shared inter-process state via JSON file
=================================================================
Both the BMS monitor and the Victron monitor write their latest readings
to a shared JSON file.  The dashboard writer reads both sections and
renders the combined HTML.

File format
-----------
{
  "bms": {
      "updated": "2024-01-01T12:00:00",
      "readings": [ { ...DeviceReading fields... }, ... ]
  },
  "victron": {
      "updated": "2024-01-01T12:00:00",
      "readings": [ { ...DeviceReading fields... }, ... ]
  }
}

Each process owns its own section and never touches the other's.
Writes are atomic (write to .tmp then rename) to prevent a reader
from seeing a partial file.
"""

import json
import logging
import os
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from .models import DeviceReading

log = logging.getLogger(__name__)

# Keys that hold list values in DeviceReading — must not be coerced to None
_LIST_FIELDS = {"temp_c", "faults", "balance_cells"}


def _reading_to_dict(r: DeviceReading) -> dict:
    """Serialise a DeviceReading to a plain dict for JSON."""
    return asdict(r)


def _dict_to_reading(d: dict) -> DeviceReading:
    """Deserialise a plain dict back into a DeviceReading."""
    known = {f.name for f in DeviceReading.__dataclass_fields__.values()}
    filtered = {k: v for k, v in d.items() if k in known}
    # Ensure list fields are lists, not None
    for lf in _LIST_FIELDS:
        if lf in filtered and filtered[lf] is None:
            filtered[lf] = []
    return DeviceReading(**filtered)


def load_state(state_path: str) -> dict:
    """
    Load the shared state file.

    Returns a dict with keys "bms" and "victron", each containing:
        {"updated": ISO-timestamp-or-None, "readings": [DeviceReading, ...]}

    Returns empty dicts for missing sections.  Never raises on a missing or
    corrupt file — returns an empty state instead.
    """
    empty = {
        "bms":     {"updated": None, "readings": []},
        "victron": {"updated": None, "readings": []},
    }
    try:
        text = Path(state_path).read_text(encoding="utf-8")
        raw  = json.loads(text)
    except FileNotFoundError:
        return empty
    except (json.JSONDecodeError, OSError) as exc:
        log.warning(f"Could not read state file {state_path}: {exc}")
        return empty

    result = {}
    for section in ("bms", "victron"):
        sec = raw.get(section, {})
        readings = []
        for item in sec.get("readings", []):
            try:
                readings.append(_dict_to_reading(item))
            except (TypeError, KeyError) as exc:
                log.debug(f"Skipping corrupt reading in state {section}: {exc}")
        result[section] = {
            "updated":  sec.get("updated"),
            "readings": readings,
        }
    return result


def save_section(state_path: str, section: str, readings: list[DeviceReading]) -> None:
    """
    Atomically update one section ("bms" or "victron") of the shared state file.

    Reads the existing file, updates only the named section, writes to a
    temporary file, then renames — so the reader always sees a complete file.
    """
    assert section in ("bms", "victron"), f"Invalid section: {section!r}"

    # Read existing state so we don't clobber the other process's section
    try:
        text = Path(state_path).read_text(encoding="utf-8")
        raw  = json.loads(text)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        raw  = {}

    raw[section] = {
        "updated":  datetime.now().isoformat(timespec="seconds"),
        "readings": [_reading_to_dict(r) for r in readings],
    }

    # Atomic write: temp file → rename
    tmp = state_path + ".tmp"
    try:
        Path(tmp).write_text(json.dumps(raw, default=str), encoding="utf-8")
        os.replace(tmp, state_path)
    except OSError as exc:
        log.error(f"Failed to write state file {state_path}: {exc}")
        try:
            os.unlink(tmp)
        except OSError:
            pass
