"""
setup.py — Solar Monitor package installer
"""
from setuptools import setup, find_packages

setup(
    name="solar-monitor",
    version="1.0.0",
    description="BLE-based solar system monitor for JBD/Vatrer BMS and Victron devices",
    packages=find_packages(),
    python_requires=">=3.10",
    install_requires=[
        "bleak>=0.21",
        "cryptography>=41",
    ],
    entry_points={
        "console_scripts": [
            "solar-monitor=solar_monitor.__main__:run",
        ],
    },
)
