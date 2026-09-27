"""
detectors/usb.py

USB Detector (device_server_hardware_mapper.txt, section 15).
Reads sysfs directly, so no external dependency (e.g. pyusb) is required
just to enumerate devices.
"""

import os
from typing import Any, Dict, List

_SYSFS_USB = "/sys/bus/usb/devices"


def _read(path: str) -> str:
    try:
        with open(path, "r") as f:
            return f.read().strip()
    except (FileNotFoundError, PermissionError, OSError):
        return None


def detect() -> Dict[str, Any]:
    try:
        if not os.path.isdir(_SYSFS_USB):
            return {"available": False, "error": "usb_sysfs_not_found"}

        devices: List[Dict[str, Any]] = []
        for entry in os.listdir(_SYSFS_USB):
            base = os.path.join(_SYSFS_USB, entry)
            id_vendor = _read(os.path.join(base, "idVendor"))
            id_product = _read(os.path.join(base, "idProduct"))
            if id_vendor is None or id_product is None:
                continue  # not an actual device node (e.g. an interface entry)

            devices.append({
                "vendor_id": id_vendor,
                "product_id": id_product,
                "manufacturer": _read(os.path.join(base, "manufacturer")),
                "product": _read(os.path.join(base, "product")),
                "device_class": _read(os.path.join(base, "bDeviceClass")),
                "serial_number": _read(os.path.join(base, "serial")),
            })

        return {"available": True, "devices": devices}
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "error": str(exc)}
