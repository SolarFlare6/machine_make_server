"""
schema.py

Defines the versioned Hardware Map schema (DCP-HW-1.0) and helpers for
building an empty/skeleton map. Keeping the schema in one place means
every detector agrees on the same shape, and the schema version can be
bumped independently of DCP itself (see dcp_protocol_specification.txt,
section 41: "hardware map access").
"""

from typing import Any, Dict

HARDWARE_MAP_SCHEMA = "DCP-HW-1.0"


def empty_hardware_map() -> Dict[str, Any]:
    """Return a skeleton hardware map with every top-level section present,
    even before any detector has run. This guarantees consumers (Capability
    Mapper, get_hardware_map handler) never have to guard against missing
    keys, only missing/False values inside them."""
    return {
        "schema": HARDWARE_MAP_SCHEMA,

        "device": {
            "platform": None,
            "architecture": None,
            "os": None,
        },

        "system": {
            "cpu": {
                "cores": None,
                "model": None,
            },
            "memory_mb": None,
            "storage": [],
        },

        "interfaces": {
            "gpio": False,
            "i2c": False,
            "spi": False,
            "uart": False,
            "usb": False,
            "wifi": False,
            "bluetooth": False,
        },

        "devices": {
            "i2c": [],
            "usb": [],
            "cameras": [],
            "audio": {"input": [], "output": []},
        },

        # Per-detector diagnostics, so one failed detector is visible instead
        # of silently missing (see device_server_hardware_mapper.txt, #19:
        # "Detector Fault Tolerance").
        "detector_errors": {},
    }
