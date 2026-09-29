"""
device_agent.py

Device Agent: the authoritative execution layer
(device_server_hardware_mapper.txt sections 28-29;
dcp_protocol_specification.txt section 27, 47).

Every protected command flows through, in this exact order:

    Authentication -> Permission check -> Argument validation
        -> Safety validation -> Execution

No other module is allowed to shortcut this pipeline; the DCP Handler
should always call into execute_tool() rather than invoking a tool's
handler directly.
"""

from typing import Any, Dict

from safety_manager import SafetyManager, SafetyViolation
from session import Session
from task_manager import TaskManager
from tool_registry import ToolDefinition, ToolRegistry

_PERMISSION_RANK = {"READ_ONLY": 0, "CONTROL": 1, "ADMIN": 2}


class DCPError(Exception):
    """Carries a DCP standard error code + message
    (dcp_protocol_specification.txt section 25)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class DeviceAgent:
    def __init__(self, tool_registry: ToolRegistry, safety_manager: SafetyManager,
                 task_manager: TaskManager):
        self._tools = tool_registry
        self._safety = safety_manager
        self._tasks = task_manager

    async def execute_tool(self, session: Session, tool_name: str,
                            parameters: Dict[str, Any]) -> Dict[str, Any]:
        """Runs the full security pipeline for one tool call. Returns
        either {"result": ...} for an immediate tool, or {"task_id": ...}
        for a long-running one. Raises DCPError on any pipeline failure,
        with a standard error code the DCP Handler can serialize directly."""

        # 1. Authentication
        if not session.authenticated:
            raise DCPError("UNAUTHORIZED", "Session is not authenticated.")

        # Resolve tool
        tool = self._tools.get(tool_name)
        if tool is None:
            raise DCPError("NOT_FOUND", f"Unknown tool '{tool_name}'.")

        # 2. Permission check (authorization)
        self._check_permission(session, tool)

        # 3. Argument (schema) validation
        self._validate_arguments(tool, parameters)

        # 4. Safety validation
        try:
            self._safety.validate(tool, parameters, session.has_control_ownership)
        except SafetyViolation as exc:
            raise DCPError("SAFETY_VIOLATION", exc.message) from exc

        # 5. Execution
        if tool.is_long_running:
            task_id = self._tasks.start(
                lambda progress_cb: tool.handler(progress_cb=progress_cb, **parameters),
                tool_name=tool_name,
                parameters=parameters,
            )
            return {"task_id": task_id}

        try:
            result = await tool.handler(**parameters)
        except Exception as exc:  # noqa: BLE001
            raise DCPError("INTERNAL_ERROR", str(exc)) from exc

        return {"result": result}

    def _check_permission(self, session: Session, tool: ToolDefinition) -> None:
        if session.permission is None:
            raise DCPError("UNAUTHORIZED", "Session has no assigned permission.")
        if _PERMISSION_RANK.get(session.permission, -1) < _PERMISSION_RANK.get(tool.permission, 99):
            raise DCPError("FORBIDDEN",
                            f"Tool '{tool.name}' requires {tool.permission} permission.")

    def _validate_arguments(self, tool: ToolDefinition, parameters: Dict[str, Any]) -> None:
        _TYPE_MAP = {
            "string": str,
            "str": str,
            "number": (int, float),
            "int": int,
            "float": (int, float),
            "boolean": bool,
            "bool": bool,
            "list": (list, tuple),
            "array": (list, tuple),
        }

        for arg_name, schema in tool.arguments.items():
            if arg_name not in parameters:
                if schema.get("required", True):
                    raise DCPError("MISSING_ARGUMENT", f"Missing required argument '{arg_name}'.")
                continue
            schema_type = schema.get("type")
            if schema_type and schema_type != "any":
                expected_type = _TYPE_MAP.get(schema_type)
                if expected_type and not isinstance(parameters[arg_name], expected_type):
                    raise DCPError("INVALID_ARGUMENT",
                                    f"Argument '{arg_name}' must be of type {schema_type}.")

        unknown = set(parameters) - set(tool.arguments)
        if unknown:
            raise DCPError("INVALID_ARGUMENT", f"Unexpected argument(s): {sorted(unknown)}.")
