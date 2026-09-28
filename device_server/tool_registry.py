"""
tool_registry.py

Tool Registry (device_server_hardware_mapper.txt, sections 26-27;
dcp_protocol_specification.txt, sections 14-17, 42): "What high-level
operations can the client request?"

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
        """Serialize exactly as dcp_protocol_specification.txt section 15
        shows (no handler, no internal safety_constraints -- those are
        server-internal)."""
        return {
            "name": self.name,
            "description": self.description,
            "arguments": self.arguments,
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

        if "stand" in capabilities:
            self.register(ToolDefinition(
                name="stand",
                description="Make the robot stand up.",
                arguments={},
                permission="CONTROL",
                handler=lambda **kw: robot_controller.stand(),
            ))

        if "sit" in capabilities:
            self.register(ToolDefinition(
                name="sit",
                description="Make the robot sit down.",
                arguments={},
                permission="CONTROL",
                handler=lambda **kw: robot_controller.sit(),
            ))

        if "walk" in capabilities:
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

        if "turn" in capabilities:
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

        if "imu" in capabilities or robot_controller is not None:
            self.register(ToolDefinition(
                name="get_orientation",
                description="Read the current IMU orientation.",
                arguments={},
                permission="READ_ONLY",
                handler=lambda **kw: robot_controller.get_orientation(),
            ))

        if "led" in capabilities or robot_controller is not None:
            self.register(ToolDefinition(
                name="set_led",
                description="Turn the status LED on or off.",
                arguments={"on": {"type": "boolean"}},
                permission="CONTROL",
                handler=lambda **kw: robot_controller.set_led(**kw),
            ))

        if robot_controller is not None:
            # 1. emergency_stop tool
            self.register(ToolDefinition(
                name="emergency_stop",
                description="Immediately halt all motors and stop the robot.",
                arguments={},
                permission="CONTROL",
                handler=lambda **kw: robot_controller.emergency_stop(),
            ))

            # 2. set_gait tool
            self.register(ToolDefinition(
                name="set_gait",
                description="Set locomotion gait mode (walk, trot, bound, gallop).",
                arguments={"mode": {"type": "string"}},
                permission="CONTROL",
                handler=lambda mode="walk", **kw: robot_controller.set_gait(mode=mode),
            ))

            # 3. set_pose tool
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

            # 4. camera tools (start_camera, stop_camera, camera_snapshot, take_picture)
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

            # 5. battery tool
            self.register(ToolDefinition(
                name="get_battery",
                description="Get device battery level and charging status.",
                arguments={},
                permission="READ_ONLY",
                handler=lambda **kw: robot_controller.get_battery(),
            ))

            # 6. dynamic config tool
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
