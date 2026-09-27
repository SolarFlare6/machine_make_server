"""
detectors/i2c.py

I2C Detector (device_server_hardware_mapper.txt, section 14).

Steps required by the spec:
    1. Detect available I2C buses.
    2. Scan the buses.
    3. Record responding addresses.
    4. Identify known devices only when sufficient evidence exists.

Identification is deliberately conservative: an address match alone
(e.g. 0x40) is only ever reported as a "candidate" list, never as a
confirmed device. Confirming a candidate is what Physical Configuration
is for (a human/config file says "0x40 on bus 1 is a PCA9685"); the
detector must not silently assume it.
"""

import glob
import re
from typing import Any, Dict, List

# Well-known I2C addresses that MIGHT correspond to these parts. This is
# advisory metadata only -- see the docstring above.
_KNOWN_ADDRESS_HINTS = {
    0x40: ["PCA9685"],
    0x68: ["MPU6050", "DS3231"],
    0x69: ["MPU6050 (AD0 high)"],
    0x76: ["BMP280", "BME280"],
    0x77: ["BMP280", "BME280"],
    0x3C: ["SSD1306 OLED"],
    0x27: ["PCF8574 LCD backpack"],
}


def _list_buses() -> List[int]:
    buses = []
    for path in glob.glob("/dev/i2c-*"):
        match = re.search(r"i2c-(\d+)$", path)
        if match:
            buses.append(int(match.group(1)))
    return sorted(buses)


def _scan_bus(bus_number: int) -> Dict[str, Any]:
    """Probe every valid 7-bit I2C address on a bus using a zero-length
    read, which is the same non-intrusive technique `i2cdetect` uses.
    Requires the `smbus2` package and read/write permission on the bus
    device node; both failures are reported, not raised."""
    try:
        import smbus2  # type: ignore
    except ImportError:
        return {"bus": bus_number, "devices": [], "error": "smbus2_not_installed"}

    devices = []
    try:
        bus = smbus2.SMBus(bus_number)
        try:
            for address in range(0x03, 0x78):
                try:
                    bus.read_byte(address)
                    hints = _KNOWN_ADDRESS_HINTS.get(address, [])
                    devices.append({
                        "address": "0x{:02x}".format(address),
                        "candidates": hints,  # empty list == unidentified
                    })
                except OSError:
                    continue  # no device answered at this address
        finally:
            bus.close()
        return {"bus": bus_number, "devices": devices}
    except PermissionError:
        return {"bus": bus_number, "devices": [], "error": "permission_denied"}
    except Exception as exc:  # noqa: BLE001
        return {"bus": bus_number, "devices": [], "error": str(exc)}


def detect() -> Dict[str, Any]:
    try:
        buses = _list_buses()
        if not buses:
            return {"available": False, "error": "no_i2c_buses_found"}
        results = [_scan_bus(bus) for bus in buses]
        return {"available": True, "buses": results}
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "error": str(exc)}
