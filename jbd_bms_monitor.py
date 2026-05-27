#!/usr/bin/env python3
"""
jbd_bms_monitor.py — backward-compatible launcher
===================================================
This file exists for backward compatibility.  The application has been
refactored into the solar_monitor package.

Usage (unchanged from previous versions):
    python jbd_bms_monitor.py [--config monitor.ini] [options]

Preferred going forward:
    python -m solar_monitor [options]
"""
import sys
import os

# Ensure the solar_monitor package directory is found relative to this script,
# regardless of the working directory the script is invoked from.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from solar_monitor.__main__ import run

if __name__ == "__main__":
    run()
