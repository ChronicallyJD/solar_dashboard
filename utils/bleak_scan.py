#!/usr/bin/python3

print("""Simple Bluetooth scanner for devices in reach""")

import asyncio
from bleak import BleakScanner

async def main():
    print("Scanning for Bluetooth devices...")
    devices = await BleakScanner.discover()
    for d in devices:
        print(d)

asyncio.run(main())
