"""
capability_mapper.py

Capability Mapper (device_server_hardware_mapper.txt, sections 23-25,
42). Answers "what can this device do?", derived from:

    Hardware Map + Physical Configuration + Software Modules

It never talks to hardware directly and never talks DCP directly -- it
is a pure function of the three inputs above, which makes it trivial to
unit test.

Low-level capabilities map fairly directly to detected+configured
resources (servo, imu, camera, audio, ...). Higher-level capabilities
(walking, turning, ...) require a combination of several low-level ones
plus a software module (e.g. a Robot Controller) actually being present.
"""

from typing import Any, Dict, List


class CapabilityMapper:
    def __init__(self, robot_controller=None):
        # Presence of a robot_controller stands in for "software modules"
        # in the pipeline diagram (section 23) -- e.g. a gait engine.
        self._robot_controller = robot_controller

    def build(self, hardware_map: Dict[str, Any], physical_config: Dict[str, Any]) -> List[str]:
        capabilities: List[str] = []

        interfaces = hardware_map.get("interfaces", {})
        devices = hardware_map.get("devices", {})

        has_pca9685 = self._has_configured_controller(physical_config, "pca9685")
        has_servos = bool(physical_config.get("servos"))
        has_imu = "imu" in physical_config or self._i2c_candidate_present(devices, ["MPU6050"])
        has_camera = bool(devices.get("cameras"))
        has_audio_out = bool(devices.get("audio", {}).get("output"))
        has_led = "led" in physical_config

        # Low-level capabilities
        if interfaces.get("gpio"):
            capabilities.append("gpio")
        if interfaces.get("i2c"):
            capabilities.append("i2c")
        if has_pca9685 and has_servos:
            capabilities.append("pwm")
            capabilities.append("servo")
        if has_imu:
            capabilities.append("imu")
        if has_camera:
            capabilities.append("camera")
        if has_audio_out:
            capabilities.append("audio")
        if has_led:
            capabilities.append("led")
        if hardware_map.get("system", {}).get("storage"):
            capabilities.append("storage")
        if interfaces.get("wifi") or interfaces.get("bluetooth"):
            capabilities.append("network")

        # Higher-level capabilities: require a robot controller (or
        # equivalent software module) AND the low-level building blocks.
        if self._robot_controller is not None and "servo" in capabilities:
            capabilities += ["stand", "sit", "walk", "turn", "movement"]
            if has_imu:
                capabilities.append("balance_correction")

        if has_camera:
            capabilities.append("taking_pictures")
        if has_audio_out:
            capabilities.append("playing_audio")

        # Deduplicate while preserving order (Python dict keys preserve
        # insertion order; this is a common, readable idiom for that).
        return list(dict.fromkeys(capabilities))

    @staticmethod
    def _has_configured_controller(physical_config: Dict[str, Any], name: str) -> bool:
        return name in physical_config

    @staticmethod
    def _i2c_candidate_present(devices: Dict[str, Any], part_names: List[str]) -> bool:
        for bus in devices.get("i2c", []):
            for dev in bus.get("devices", []):
                if any(part in dev.get("candidates", []) for part in part_names):
                    return True
        return False
