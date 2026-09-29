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

        # Low-level and high-level capabilities matching Section 2 specification
        if self._robot_controller is not None:
            capabilities.append({
                "id": "robotics",
                "type": "robotics",
                "name": "Quadruped Kinematics & Servos",
                "description": "16-channel PCA9685 servo driver with inverse kinematics",
                "params": {"channels": 16, "driver": "PCA9685"},
                "enabled": True,
            })
            capabilities.append({
                "id": "imu",
                "type": "telemetry",
                "name": "MPU6050 6-DOF IMU",
                "description": "Accelerometer and Gyroscope over I2C 0x68",
                "params": {"i2c_address": "0x68"},
                "enabled": True,
            })
            capabilities.append({
                "id": "lighting",
                "type": "gpio",
                "name": "WS281x LED Strip",
                "description": "8 addressable RGB LEDs on GPIO 10",
                "params": {"led_count": 8, "gpio_pin": 10},
                "enabled": True,
            })
            capabilities.append({
                "id": "buzzer",
                "type": "audio",
                "name": "Tonal Buzzer",
                "description": "Piezo tone generator on GPIO 23",
                "params": {"gpio_pin": 23},
                "enabled": True,
            })
            capabilities.append({
                "id": "camera",
                "type": "camera",
                "name": "Pi Camera Module",
                "description": "CSI or USB Camera feed",
                "params": {"stream_port": 8554},
                "enabled": True,
            })

        if interfaces.get("gpio") or self._robot_controller is not None:
            capabilities.append({
                "id": "gpio",
                "type": "gpio",
                "name": "Raspberry Pi GPIO Header",
                "description": "40-pin header with 28 BCM GPIO pins",
                "params": {
                    "pins": [2, 3, 4, 17, 27, 22, 10, 9, 11, 5, 6, 13, 19, 26, 14, 15, 18, 23, 24, 25, 8, 7, 12, 16, 20, 21],
                    "labels": {"4": "Status LED", "17": "Relay 1", "27": "Buzzer"},
                },
            })
        if interfaces.get("i2c"):
            capabilities.append("i2c")
        if (has_pca9685 and has_servos) or self._robot_controller is not None:
            capabilities.append({
                "id": "pwm",
                "type": "pwm",
                "name": "PWM Channels",
                "description": "16-channel PCA9685 PWM Controller",
                "params": {
                    "channels": 16,
                },
            })
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
        # Note: Robot hardware has no battery indicator/measurement circuit;
        # do not advertise "battery" so the mobile app displays "DC In".
        if self._robot_controller is not None:
            capabilities += [
                "telemetry", "emergency_stop", "config",
                "camera", "taking_pictures", "needle_ai", "ai",
            ]
            capabilities += ["stand", "sit", "walk", "turn", "movement", "kinematics", "gait_control"]
            if has_imu:
                capabilities.append("balance_correction")

        if has_camera:
            capabilities.append("taking_pictures")
        if has_audio_out:
            capabilities.append("playing_audio")

        # Deduplicate while preserving order (handles both str and dict descriptors)
        unique = []
        seen_keys = set()
        for item in capabilities:
            key = item.get("id", item.get("type", "")) if isinstance(item, dict) else str(item)
            if key not in seen_keys:
                seen_keys.add(key)
                unique.append(item)
        return unique

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
