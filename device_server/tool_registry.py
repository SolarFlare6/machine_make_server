"""
tool_registry.py

Tool Registry (device_server_hardware_mapper.txt, sections 26-27;
dcp_protocol_specification.txt, sections 14-17, 42;
MachineMake Server Implementation Specification DCP v1.0, sections 3.1-3.6).

A ToolDefinition bundles everything the Device Agent's security pipeline
needs to check a call, plus the callable that actually performs it. The
DCP-visible shape (name/description/arguments/permission) matches the
protocol spec's tool definition exactly so get_tools() can serialize it
directly.
"""

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

Handler = Callable[..., Any]  # async callable; exact signature is tool-specific


@dataclass
class ToolDefinition:
    name: str
    description: str
    arguments: Dict[str, Dict[str, str]]  # {"direction": {"type": "string"}, ...}
    permission: str  # READ_ONLY | CONTROL | ADMIN
    handler: Handler
    is_long_running: bool = False
    safety_constraints: Dict[str, Any] = field(default_factory=dict)

    def to_dcp(self) -> Dict[str, Any]:
        """Serialize as dcp_protocol_specification.txt section 15 shows,
        with toolName/parameters aliases for mobile app client compatibility."""
        return {
            "name": self.name,
            "toolName": self.name,
            "description": self.description,
            "arguments": self.arguments,
            "parameters": self.arguments,
            "params": self.arguments,
            "permission": self.permission,
        }


class ToolRegistry:
    def __init__(self):
        self._tools: Dict[str, ToolDefinition] = {}

    def register(self, tool: ToolDefinition) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[ToolDefinition]:
        return self._tools.get(name)

    def all_tools(self) -> Dict[str, ToolDefinition]:
        return dict(self._tools)

    def to_dcp_list(self) -> list:
        return [tool.to_dcp() for tool in self._tools.values()]

    def build_default_tools(self, capabilities: list, robot_controller,
                             safety_limits: Dict[str, Any]) -> None:
        """Registers the standard tool set conditioned on which
        capabilities the Capability Mapper actually found -- an unknown /
        minimal device simply gets fewer tools rather than the app calling
        into hardware that doesn't exist."""

        cap_ids = set()
        for c in capabilities:
            if isinstance(c, dict):
                cap_ids.add(c.get("id"))
                cap_ids.add(c.get("type"))
            else:
                cap_ids.add(str(c))

        has_robotics = "robotics" in cap_ids or "stand" in cap_ids or robot_controller is not None

        # -- Section 3.1: Locomotion & High-Level Actions ----------------
        if has_robotics:
            self.register(ToolDefinition(
                name="stand",
                description="Make the robot stand up.",
                arguments={},
                permission="CONTROL",
                handler=lambda **kw: robot_controller.stand(),
            ))

            self.register(ToolDefinition(
                name="sit",
                description="Make the robot sit down.",
                arguments={},
                permission="CONTROL",
                handler=lambda **kw: robot_controller.sit(),
            ))

            max_distance = safety_limits.get("max_walk_distance_m", 5.0)
            self.register(ToolDefinition(
                name="walk",
                description="Move the robot in a specified direction.",
                arguments={
                    "direction": {"type": "string"},
                    "distance": {"type": "number"},
                },
                permission="CONTROL",
                handler=lambda progress_cb=None, **kw: robot_controller.walk(progress_cb=progress_cb, **kw),
                is_long_running=True,
                safety_constraints={
                    "direction": {"enum": ["forward", "backward", "left", "right"]},
                    "distance": {"min": 0.0, "max": max_distance},
                },
            ))

            max_angle = safety_limits.get("max_turn_angle_deg", 180.0)
            self.register(ToolDefinition(
                name="turn",
                description="Turn the robot in place.",
                arguments={
                    "direction": {"type": "string"},
                    "angle": {"type": "number"},
                },
                permission="CONTROL",
                handler=lambda progress_cb=None, **kw: robot_controller.turn(progress_cb=progress_cb, **kw),
                is_long_running=True,
                safety_constraints={
                    "direction": {"enum": ["left", "right"]},
                    "angle": {"min": 0.0, "max": max_angle},
                },
            ))

            self.register(ToolDefinition(
                name="emergency_stop",
                description="Immediately halt all motors, locomotion loops, and release servo power.",
                arguments={},
                permission="CONTROL",
                handler=lambda **kw: robot_controller.emergency_stop(),
            ))

        # -- Section 3.1: Servo Control & Realtime Mirroring (PCA9685) ------
        if "robotics" in cap_ids or "pwm" in cap_ids or robot_controller is not None:
            self.register(ToolDefinition(
                name="get_servo_angles",
                description="Returns current angles across quadruped servo channels for realtime 3D simulation mirroring.",
                arguments={},
                permission="READ_ONLY",
                handler=lambda **kw: robot_controller.get_servo_angles(),
            ))

            self.register(ToolDefinition(
                name="driver_set_servo_angle_with_index",
                description="Sets angle of single servo channel (0-15, 0-180 deg).",
                arguments={
                    "index": {"type": "int", "required": True},
                    "angle": {"type": "number", "required": True},
                },
                permission="CONTROL",
                handler=lambda index=0, angle=90, **kw: robot_controller.driver_set_servo_angle_with_index(index=index, angle=angle),
            ))

            self.register(ToolDefinition(
                name="snap_servo_left",
                description="Sets channel angle to 180 deg immediately.",
                arguments={
                    "index": {"type": "int", "required": True},
                },
                permission="CONTROL",
                handler=lambda index=0, **kw: robot_controller.snap_servo_left(index=index),
            ))

            self.register(ToolDefinition(
                name="snap_servo_right",
                description="Sets channel angle to 0 deg immediately.",
                arguments={
                    "index": {"type": "int", "required": True},
                },
                permission="CONTROL",
                handler=lambda index=0, **kw: robot_controller.snap_servo_right(index=index),
            ))

            self.register(ToolDefinition(
                name="pan_to_left",
                description="Sweeps channel smoothly towards 180 deg.",
                arguments={
                    "index": {"type": "int", "required": True},
                },
                permission="CONTROL",
                handler=lambda index=0, **kw: robot_controller.pan_to_left(index=index),
            ))

            self.register(ToolDefinition(
                name="pan_to_right",
                description="Sweeps channel smoothly towards 0 deg.",
                arguments={
                    "index": {"type": "int", "required": True},
                },
                permission="CONTROL",
                handler=lambda index=0, **kw: robot_controller.pan_to_right(index=index),
            ))

            self.register(ToolDefinition(
                name="cleanup_servos",
                description="De-energizes all 16 servo channels to release torque and prevent overheating.",
                arguments={},
                permission="CONTROL",
                handler=lambda **kw: robot_controller.cleanup_servos(),
            ))

        # -- Section 3.3: WS281x 8-LED Strip Controls (GPIO 10) ----------
        if "lighting" in cap_ids or "led" in cap_ids or robot_controller is not None:
            self.register(ToolDefinition(
                name="turn_on_strip_with_color",
                description="Fills all 8 LEDs with RGB color.",
                arguments={
                    "r": {"type": "int", "required": True},
                    "g": {"type": "int", "required": True},
                    "b": {"type": "int", "required": True},
                },
                permission="CONTROL",
                handler=lambda r=255, g=255, b=255, **kw: robot_controller.turn_on_strip_with_color(r=r, g=g, b=b),
            ))

            self.register(ToolDefinition(
                name="turn_off_strip",
                description="Turns off all 8 LEDs.",
                arguments={},
                permission="CONTROL",
                handler=lambda **kw: robot_controller.turn_off_strip(),
            ))

            self.register(ToolDefinition(
                name="turn_on_led_at_index",
                description="Sets single LED color on 8-LED strip.",
                arguments={
                    "index": {"type": "int", "required": True},
                    "r": {"type": "int", "required": True},
                    "g": {"type": "int", "required": True},
                    "b": {"type": "int", "required": True},
                },
                permission="CONTROL",
                handler=lambda index=0, r=255, g=255, b=255, **kw: robot_controller.turn_on_led_at_index(index=index, r=r, g=g, b=b),
            ))

            self.register(ToolDefinition(
                name="turn_off_led_at_index",
                description="Turns off single LED on 8-LED strip.",
                arguments={
                    "index": {"type": "int", "required": True},
                },
                permission="CONTROL",
                handler=lambda index=0, **kw: robot_controller.turn_off_led_at_index(index=index),
            ))

            self.register(ToolDefinition(
                name="animate_running_process",
                description="Runs animated LED chase / cycle sequence.",
                arguments={},
                permission="CONTROL",
                handler=lambda **kw: robot_controller.animate_running_process(),
            ))

            self.register(ToolDefinition(
                name="flash_alert",
                description="Rapid red blinking alert sequence.",
                arguments={},
                permission="CONTROL",
                handler=lambda **kw: robot_controller.flash_alert(),
            ))

            self.register(ToolDefinition(
                name="blink_oke",
                description="Double green flash confirming command success.",
                arguments={},
                permission="CONTROL",
                handler=lambda **kw: robot_controller.blink_oke(),
            ))

            self.register(ToolDefinition(
                name="blink_warning",
                description="Amber pulsing warning sequence.",
                arguments={},
                permission="CONTROL",
                handler=lambda **kw: robot_controller.blink_warning(),
            ))

        # -- Section 3.1: Audio Subsystem (Buzzer & Speaker) ------------
        if "buzzer" in cap_ids or "audio" in cap_ids or robot_controller is not None:
            self.register(ToolDefinition(
                name="play_tone",
                description="Plays frequency/note on piezo buzzer (e.g. 'C4', 'A4').",
                arguments={
                    "tone": {"type": "string", "required": False},
                },
                permission="CONTROL",
                handler=lambda tone="C4", **kw: robot_controller.play_tone(tone=tone),
            ))

            self.register(ToolDefinition(
                name="play_list_of_notes",
                description="Plays melody sequence of notes on tonal buzzer.",
                arguments={
                    "notes": {"type": "list", "required": False},
                },
                permission="CONTROL",
                handler=lambda notes=None, **kw: robot_controller.play_list_of_notes(notes=notes or ["C4", "E4", "G4", "C5"]),
            ))

            self.register(ToolDefinition(
                name="stop_tone",
                description="Silences the tonal buzzer immediately.",
                arguments={},
                permission="CONTROL",
                handler=lambda **kw: robot_controller.stop_tone(),
            ))

            self.register(ToolDefinition(
                name="play_audio",
                description="Plays audio file on the speaker.",
                arguments={
                    "file_path": {"type": "string", "required": False},
                },
                permission="CONTROL",
                handler=lambda file_path="", **kw: robot_controller.play_audio(file_path=file_path),
            ))

            self.register(ToolDefinition(
                name="stop_audio",
                description="Halts speaker audio playback immediately.",
                arguments={},
                permission="CONTROL",
                handler=lambda **kw: robot_controller.stop_audio(),
            ))

            self.register(ToolDefinition(
                name="set_volume",
                description="Sets speaker playback volume (0.0 to 1.0).",
                arguments={
                    "volume": {"type": "number", "required": False},
                },
                permission="CONTROL",
                handler=lambda volume=0.8, **kw: robot_controller.set_volume(volume=volume),
            ))

        # -- Section 3.5: MPU6050 IMU Telemetry (I2C 0x68) ---------------
        if "imu" in cap_ids or robot_controller is not None:
            self.register(ToolDefinition(
                name="get_sensor_accel",
                description="Read 3-axis accelerometer values (x, y, z in g).",
                arguments={},
                permission="READ_ONLY",
                handler=lambda **kw: robot_controller.get_sensor_accel(),
            ))

            self.register(ToolDefinition(
                name="get_sensor_gyro",
                description="Read 3-axis gyroscope values (x, y, z in deg/s).",
                arguments={},
                permission="READ_ONLY",
                handler=lambda **kw: robot_controller.get_sensor_gyro(),
            ))

            self.register(ToolDefinition(
                name="get_pitch_roll",
                description="Read IMU pitch and roll angles in degrees.",
                arguments={},
                permission="READ_ONLY",
                handler=lambda **kw: robot_controller.get_pitch_roll(),
            ))

            self.register(ToolDefinition(
                name="get_orientation",
                description="Read the current IMU orientation.",
                arguments={},
                permission="READ_ONLY",
                handler=lambda **kw: robot_controller.get_orientation(),
            ))

        # -- Section 3.6: System & Power Options -------------------------
        if robot_controller is not None:
            self.register(ToolDefinition(
                name="shutdown",
                description="Executes system shutdown (sudo shutdown -h now).",
                arguments={},
                permission="CONTROL",
                handler=lambda **kw: robot_controller.shutdown(),
            ))

            self.register(ToolDefinition(
                name="reboot",
                description="Executes system reboot (sudo reboot).",
                arguments={},
                permission="CONTROL",
                handler=lambda **kw: robot_controller.reboot(),
            ))

        # -- Kinematics, Gait, Camera, Config & Telemetry ----------------
        if robot_controller is not None:
            self.register(ToolDefinition(
                name="set_gait",
                description="Set locomotion gait mode (walk, trot, bound, gallop).",
                arguments={"mode": {"type": "string"}},
                permission="CONTROL",
                handler=lambda mode="walk", **kw: robot_controller.set_gait(mode=mode),
            ))

            self.register(ToolDefinition(
                name="set_pose",
                description="Set body kinematic orientation (pitch, roll, yaw) and height.",
                arguments={
                    "pitch": {"type": "number", "required": False},
                    "roll": {"type": "number", "required": False},
                    "yaw": {"type": "number", "required": False},
                    "height": {"type": "number", "required": False},
                },
                permission="CONTROL",
                handler=lambda **kw: robot_controller.set_pose(**kw),
            ))

            self.register(ToolDefinition(
                name="start_camera",
                description="Start camera video streaming.",
                arguments={
                    "resolution": {"type": "string", "required": False},
                    "fps": {"type": "number", "required": False},
                },
                permission="CONTROL",
                handler=lambda **kw: robot_controller.start_camera(**kw),
            ))

            self.register(ToolDefinition(
                name="stop_camera",
                description="Stop camera video streaming.",
                arguments={},
                permission="CONTROL",
                handler=lambda **kw: robot_controller.stop_camera(),
            ))

            self.register(ToolDefinition(
                name="camera_snapshot",
                description="Capture a still snapshot from the camera (alias for take_picture).",
                arguments={},
                permission="READ_ONLY",
                handler=lambda **kw: robot_controller.camera_snapshot(),
            ))

            self.register(ToolDefinition(
                name="take_picture",
                description="Capture a still image from the camera.",
                arguments={},
                permission="READ_ONLY",
                handler=lambda **kw: robot_controller.take_picture(),
            ))

            self.register(ToolDefinition(
                name="get_battery",
                description="Get device battery level and charging status.",
                arguments={},
                permission="READ_ONLY",
                handler=lambda **kw: robot_controller.get_battery(),
            ))

            self.register(ToolDefinition(
                name="set_config",
                description="Set dynamic robot runtime parameter.",
                arguments={
                    "param": {"type": "string"},
                    "value": {"type": "any", "required": False},
                },
                permission="CONTROL",
                handler=lambda param="", value=None, **kw: robot_controller.set_config(param, value),
            ))

            self.register(ToolDefinition(
                name="set_led",
                description="Turn the status LED on or off.",
                arguments={"on": {"type": "boolean"}},
                permission="CONTROL",
                handler=lambda on=True, **kw: robot_controller.set_led(on=on),
            ))

            self.register(ToolDefinition(
                name="needle_prompt",
                description="Processes high-level natural language instructions using on-device Needle AI.",
                arguments={
                    "prompt": {"type": "string", "required": True}
                },
                permission="CONTROL",
                handler=lambda prompt="", **kw: robot_controller.needle_prompt(prompt=prompt),
                is_long_running=False,
            ))

            self.register(ToolDefinition(
                name="gpio_write",
                description="Set digital state (HIGH/LOW) of a GPIO pin.",
                arguments={
                    "pin": {"type": "int", "required": True},
                    "state": {"type": "bool", "required": True},
                },
                permission="CONTROL",
                handler=lambda pin=0, state=False, **kw: robot_controller.gpio_write(pin=pin, state=state),
            ))

            self.register(ToolDefinition(
                name="gpio_read",
                description="Read digital state of a GPIO pin.",
                arguments={
                    "pin": {"type": "int", "required": True},
                },
                permission="READ_ONLY",
                handler=lambda pin=0, **kw: robot_controller.gpio_read(pin=pin),
            ))

            self.register(ToolDefinition(
                name="pwm_set",
                description="Configure PWM channel duty cycle (0.0 to 1.0).",
                arguments={
                    "channel": {"type": "int", "required": True},
                    "value": {"type": "float", "required": True},
                },
                permission="CONTROL",
                handler=lambda channel=0, value=0.0, **kw: robot_controller.pwm_set(channel=channel, value=value),
            ))
