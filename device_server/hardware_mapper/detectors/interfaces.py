"""
detectors/interfaces.py

Interface Detector (device_server_hardware_mapper.txt, section 12).
Answers only "is this interface available at all?" -- never assigns
meaning to it (that is Physical Configuration's job, section 13/21).
"""

import glob
import os
import shutil
from typing import Any, Dict


def _gpio_available() -> bool:
    # Modern Linux exposes GPIO chips under /dev/gpiochip*; sysfs GPIO
    # (/sys/class/gpio) is legacy but still checked as a fallback.
    return bool(glob.glob("/dev/gpiochip*")) or os.path.isdir("/sys/class/gpio")


def _i2c_available() -> bool:
    return bool(glob.glob("/dev/i2c-*"))


def _spi_available() -> bool:
    return bool(glob.glob("/dev/spidev*"))


def _uart_available() -> bool:
    return bool(glob.glob("/dev/ttyAMA*") or glob.glob("/dev/ttyS*") or glob.glob("/dev/ttyUSB*"))


def _usb_available() -> bool:
    return os.path.isdir("/sys/bus/usb/devices") and bool(os.listdir("/sys/bus/usb/devices"))


def _wifi_available() -> bool:
    if shutil.which("iw") or shutil.which("nmcli"):
        return True
    return bool(glob.glob("/sys/class/net/wl*"))


def _bluetooth_available() -> bool:
    return bool(glob.glob("/sys/class/bluetooth/hci*")) or shutil.which("bluetoothctl") is not None


def detect() -> Dict[str, Any]:
    try:
        return {
            "available": True,
            "interfaces": {
                "gpio": _gpio_available(),
                "i2c": _i2c_available(),
                "spi": _spi_available(),
                "uart": _uart_available(),
                "usb": _usb_available(),
                "wifi": _wifi_available(),
                "bluetooth": _bluetooth_available(),
            },
        }
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "error": str(exc)}
