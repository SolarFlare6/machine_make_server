"""
config.py

Configuration Manager: Physical Configuration
(device_server_hardware_mapper.txt, sections 21-22, 42).

Hardware Mapper answers "what exists?" This module answers "what is each
detected resource physically connected to / used for?" -- e.g. "PCA9685
on I2C bus 1, address 0x40, channel 0 = front_left_shoulder".

This is plain, human-edited data (device_config.json). It deliberately
does NOT try to auto-derive itself from the hardware map: a bus address
alone is never sufficient evidence for what's wired to a robot leg.
"""

import json
import os
from typing import Any, Dict

_CONFIG_FILE_DEFAULT = os.path.join(os.path.dirname(__file__), "config", "device_config.json")


class ConfigurationManager:
    def __init__(self, config_file: str = _CONFIG_FILE_DEFAULT):
        self._config_file = config_file
        self._config: Dict[str, Any] = {}
        self.reload()

    def reload(self) -> None:
        if os.path.isfile(self._config_file):
            with open(self._config_file, "r") as f:
                self._config = json.load(f)
        else:
            self._config = {"configuration": {}}

    def save(self) -> None:
        os.makedirs(os.path.dirname(self._config_file), exist_ok=True)
        with open(self._config_file, "w") as f:
            json.dump(self._config, f, indent=2)

    @property
    def identity(self) -> Dict[str, Any]:
        return self._config.get("identity", {})

    @property
    def physical_configuration(self) -> Dict[str, Any]:
        return self._config.get("configuration", {})

    @property
    def safety_limits(self) -> Dict[str, Any]:
        return self._config.get("safety_limits", {})

    @property
    def connection_loss_policy(self) -> Dict[str, Any]:
        """device_server_hardware_mapper.txt section 36 / protocol spec
        section 44: behavior on connection loss must be explicitly
        configured, never assumed."""
        return self._config.get("connection_loss_policy", {
            "interactive_control": "safe_stop",
            "autonomous_tasks": "continue",
        })

    def update_configuration(self, patch: Dict[str, Any]) -> None:
        """Handler for the `update_configuration` DCP command (ADMIN
        permission only -- enforced by the Device Agent, not here)."""
        self._config.setdefault("configuration", {}).update(patch)
        self.save()
