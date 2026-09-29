"""
identity.py

Device Identity (device_server_hardware_mapper.txt, section 3).

The device_id is the permanent logical identity of the device and must
remain stable across restarts. It is generated once and persisted to
disk; transport addresses (IP, Bluetooth MAC) are never used as identity.

This module also owns the device's long-term signing key, used by
auth.py to prove the *server's* identity to already-paired clients
(mutual authentication), independent of pairing secrets which prove the
*client's* identity to the server.
"""

import json
import os
import secrets
from typing import Any, Dict

_IDENTITY_FILE_DEFAULT = os.path.join(os.path.dirname(__file__), "config", "device_identity.json")


class DeviceIdentity:
    def __init__(self, name: str, profile: str, device_type: str = "robot",
                 identity_file: str = _IDENTITY_FILE_DEFAULT,
                 device_id: str = None):
        self._identity_file = identity_file
        self.name = name
        self.profile = profile
        self.device_type = device_type
        self.device_id: str = device_id
        self.server_key: str = None  # hex-encoded long-term secret
        self._load_or_create()

    def _load_or_create(self) -> None:
        if os.path.isfile(self._identity_file):
            with open(self._identity_file, "r") as f:
                data = json.load(f)
            stored_id = data.get("device_id")
            self.server_key = data.get("server_key") or secrets.token_hex(32)
            if self.device_id and self.device_id != stored_id:
                # Custom device_id provided, persist update
                self._persist()
            else:
                self.device_id = stored_id
        else:
            if not self.device_id:
                self.device_id = self._generate_device_id()
            self.server_key = secrets.token_hex(32)
            self._persist()

    def _generate_device_id(self) -> str:
        # Human-recognizable but effectively unique: profile prefix + random
        # suffix, e.g. "quadruped-4f9a12".
        return f"{self.profile}-{secrets.token_hex(3)}"

    def _persist(self) -> None:
        os.makedirs(os.path.dirname(self._identity_file), exist_ok=True)
        with open(self._identity_file, "w") as f:
            json.dump({"device_id": self.device_id, "server_key": self.server_key}, f, indent=2)

    def device_info(self) -> Dict[str, Any]:
        """Matches the get_device_info response payload in
        dcp_protocol_specification.txt, section 12 and Needle AI metadata."""
        return {
            "device_id": self.device_id,
            "name": self.name,
            "type": self.device_type,
            "profile": self.profile,
            "firmware_version": "2.1.0",
            "supported_versions": ["1.0", "1.1"],
            "ai": {
                "needle": {
                    "supported": True,
                    "execution": ["device", "client"],
                    "preferred": "device",
                }
            },
        }

    def advertisement_info(self) -> Dict[str, Any]:
        """Minimal public info suitable for mDNS/BLE advertising
        (device_server_hardware_mapper.txt, section 5). Never includes
        server_key or any other secret."""
        return {
            "device_id": self.device_id,
            "name": self.name,
            "profile": self.profile,
            "protocol": "DCP",
            "protocol_version": "1.0",
        }
