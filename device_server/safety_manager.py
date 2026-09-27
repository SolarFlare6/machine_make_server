"""
safety_manager.py

Safety Manager (device_server_hardware_mapper.txt, sections 29, 36-37;
dcp_protocol_specification.txt, sections 26-27, 43-44).

Answers: "Is this physically allowed, right now?" -- distinct from
Authorization ("is this client allowed to ask?"), which the Device Agent
checks separately via permissions.

Checks performed here:
  - argument range/enum validation against each tool's safety_constraints
  - control ownership (only the CONTROL-owning session may run CONTROL
    tools that affect hardware, section 27/37 of the app+server specs)
  - basic device-state sanity (e.g. don't "walk" while already mid-task)
"""

from typing import Any, Dict

from tool_registry import ToolDefinition


class SafetyViolation(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class SafetyManager:
    def __init__(self):
        self._busy = False  # true while a CONTROL long-running task is active

    def mark_busy(self, busy: bool) -> None:
        self._busy = busy

    def validate(self, tool: ToolDefinition, parameters: Dict[str, Any],
                 has_control_ownership: bool) -> None:
        # Control ownership: only the single CONTROL owner may invoke
        # CONTROL-permission tools that move hardware (protocol spec #43).
        if tool.permission == "CONTROL" and not has_control_ownership:
            raise SafetyViolation("Session does not hold control ownership.")

        # Don't stack movement commands: an in-flight long-running CONTROL
        # task must finish or be cancelled first.
        if tool.is_long_running and self._busy:
            raise SafetyViolation("Device is busy with another task.")

        self._validate_constraints(tool, parameters)

    def _validate_constraints(self, tool: ToolDefinition, parameters: Dict[str, Any]) -> None:
        for arg_name, rule in tool.safety_constraints.items():
            if arg_name not in parameters:
                continue  # missing-argument errors are caught earlier, at schema validation
            value = parameters[arg_name]

            if "enum" in rule and value not in rule["enum"]:
                raise SafetyViolation(
                    f"'{arg_name}' must be one of {rule['enum']}, got {value!r}."
                )

            if "min" in rule and value < rule["min"]:
                raise SafetyViolation(f"'{arg_name}' below minimum allowed value {rule['min']}.")

            if "max" in rule and value > rule["max"]:
                raise SafetyViolation(f"'{arg_name}' exceeds configured limit {rule['max']}.")
