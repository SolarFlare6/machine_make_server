"""
robot_controller.py

Example Robot Controller for a quadruped
(device_server_hardware_mapper.txt, sections 33-34;
dcp_protocol_specification.txt, section 37).

This is where DCP tool calls actually touch (simulated) hardware. Needle
and the Flutter app never see any of this file -- they only ever see
tool names like walk()/stand(). This class is intentionally the ONLY
place that would talk to a real PCA9685/servo driver; everything above
it (Device Agent, Tool Registry, DCP Handler) is hardware-agnostic.

Servo I/O is simulated here (asyncio.sleep standing in for real motion
time) so the reference implementation runs on any machine without a
physical robot attached. Swap `_drive_servos()` for real PCA9685 calls
(e.g. via `adafruit-circuitpython-pca9685`) to run this on real hardware.
"""

import asyncio
from typing import Any, Callable, Dict, Optional


class RobotController:
    def __init__(self, physical_config: Dict[str, Any]):
        self._servos = physical_config.get("servos", {})
        self._pca9685_cfg = physical_config.get("pca9685", {})
        self.standing = False
        self.orientation = {"roll": 0.0, "pitch": 0.0, "yaw": 0.0}
        self.height = 0.0
        self.led_state = False
        self.gait = "walk"
        self.camera_streaming = False
        self.dynamic_config: Dict[str, Any] = {}

    # -- simulated low-level hardware I/O --------------------------------

    async def _drive_servos(self, positions: Dict[str, float], duration_s: float) -> None:
        """Stand-in for real servo motion. `positions` maps servo name ->
        target angle; a real implementation would compute inverse
        kinematics and stream PWM updates to the PCA9685 here."""
        await asyncio.sleep(duration_s)

    # -- tool-facing operations -------------------------------------------
    # Each of these is what tool_registry.py wires a DCP tool call to.
    # progress_cb(fraction: float) is called for tools that report
    # task_progress events; it may be None for instantaneous operations.

    async def emergency_stop(self) -> Dict[str, Any]:
        """Immediately halts all motion, disables servo drive, and sits down."""
        self.standing = False
        await self._drive_servos({name: 0 for name in self._servos}, duration_s=0.05)
        return {"stopped": True, "standing": False, "message": "Emergency stop triggered"}

    async def set_gait(self, mode: str = "walk") -> Dict[str, Any]:
        """Sets the walking gait mode (walk, trot, bound, gallop)."""
        valid_modes = ["walk", "trot", "bound", "gallop"]
        selected = str(mode).lower() if str(mode).lower() in valid_modes else "walk"
        self.gait = selected
        return {"gait": selected, "supported_gaits": valid_modes}

    async def set_pose(self, pitch: float = 0.0, roll: float = 0.0,
                       yaw: float = 0.0, height: float = 0.0) -> Dict[str, Any]:
        """Sets body orientation (kinematic pose) and body height."""
        self.orientation = {"roll": float(roll), "pitch": float(pitch), "yaw": float(yaw)}
        self.height = float(height)
        return {"orientation": dict(self.orientation), "height": self.height}

    async def start_camera(self, resolution: str = "640x480", fps: int = 30) -> Dict[str, Any]:
        """Starts camera stream endpoint."""
        self.camera_streaming = True
        return {"streaming": True, "resolution": resolution, "fps": fps, "stream_path": "/camera/stream"}

    async def stop_camera(self) -> Dict[str, Any]:
        """Stops camera stream endpoint."""
        self.camera_streaming = False
        return {"streaming": False}

    async def camera_snapshot(self) -> Dict[str, Any]:
        """Alias for take_picture used by the mobile app."""
        return await self.take_picture()

    async def get_battery(self) -> Dict[str, Any]:
        """Returns battery telemetry."""
        return {
            "percentage": 88,
            "voltage": 8.1,
            "charging": False,
            "level": 88,
        }

    async def set_config(self, param: str, value: Any = None) -> Dict[str, Any]:
        """Updates a dynamic runtime parameter."""
        self.dynamic_config[param] = value
        return {"updated": True, "param": param, "value": value}

    async def stand(self, progress_cb: Optional[Callable[[float], None]] = None) -> None:
        await self._drive_servos({name: 90 for name in self._servos}, duration_s=1.0)
        self.standing = True

    async def sit(self, progress_cb: Optional[Callable[[float], None]] = None) -> None:
        await self._drive_servos({name: 0 for name in self._servos}, duration_s=1.0)
        self.standing = False

    async def walk(self, direction: str, distance: float,
                    progress_cb: Optional[Callable[[float], None]] = None) -> None:
        """Simulated gait: this is where a real implementation would run
        gait generation -> foot trajectory -> inverse kinematics -> joint
        angles -> servo updates, per section 34 of the hardware mapper
        spec. Reports progress in ~10 steps so callers can surface
        task_progress events."""
        steps = 10
        for step in range(1, steps + 1):
            await self._drive_servos({}, duration_s=0.1)
            if progress_cb:
                progress_cb(step / steps)

    async def turn(self, direction: str, angle: float,
                    progress_cb: Optional[Callable[[float], None]] = None) -> None:
        steps = 5
        for step in range(1, steps + 1):
            await self._drive_servos({}, duration_s=0.1)
            if progress_cb:
                progress_cb(step / steps)

    async def get_orientation(self) -> Dict[str, float]:
        """Would read the IMU (e.g. MPU6050 over I2C) in a real
        implementation; returns the simulated resting orientation here."""
        return dict(self.orientation)

    async def set_led(self, on: bool) -> None:
        self.led_state = bool(on)

    async def take_picture(self) -> Dict[str, Any]:
        """Would grab a frame from the configured camera device; returns a
        placeholder result here."""
        return {"format": "jpeg", "bytes": 0, "note": "simulated capture"}
