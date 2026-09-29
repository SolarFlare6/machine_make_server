"""
dcp_handler.py

DCP Handler: converts DCP JSON messages into calls against the rest of
the Device Server, and formats their results back into DCP responses
(dcp_protocol_specification.txt, sections 4-25;
MachineMake Server Implementation Specification DCP v1.0, sections 1, 3, 5).

Supports dual envelope framing:
1. Standard DCP wire format:
   {"dcp": "1.0", "type": "request", "id": 123, "command": "...", "arguments": {...}}
2. Mobile App Handshake / Spec Format:
   {"msgId": "...", "type": "hello"|"negotiate"|"auth"|"capabilities"|"tools"|"subscribe_events"|"execute"|"ping", "payload": {...}, "timestampMs": ...}
"""

import logging
import time
from typing import Any, Dict, Optional
import uuid

from auth import Authenticator, TrustStore
from config import ConfigurationManager
from device_agent import DCPError, DeviceAgent
from hardware_mapper.mapper import HardwareMapper
from identity import DeviceIdentity
from session import Session, SessionManager
from task_manager import TaskManager
from tool_registry import ToolRegistry

logger = logging.getLogger("dcp_handler")

DCP_VERSION = "1.0"
SUPPORTED_VERSIONS = {"1.0", "1.0.0"}

# Shortcut commands that map 1:1 onto execute_tool() with no extra
# arguments beyond what's already given (protocol spec section 17).
_SHORTCUT_COMMANDS = {
    "stand", "sit", "walk", "turn", "emergency_stop",
    "driver_set_servo_angle_with_index", "snap_servo_left", "snap_servo_right",
    "pan_to_left", "pan_to_right", "cleanup_servos",
    "turn_on_strip_with_color", "turn_off_strip", "turn_on_led_at_index",
    "turn_off_led_at_index", "animate_running_process", "flash_alert",
    "blink_oke", "blink_warning", "play_tone", "play_list_of_notes",
    "get_sensor_accel", "get_sensor_gyro", "get_pitch_roll",
    "shutdown", "reboot", "take_picture", "get_orientation",
    "camera_snapshot", "get_battery", "set_gait", "set_pose",
    "start_camera", "stop_camera", "set_config", "set_led",
    "needle_prompt", "gpio_write", "gpio_read", "pwm_set",
}


class DCPHandler:
    def __init__(self, *, identity: DeviceIdentity, hardware_mapper: HardwareMapper,
                 config: ConfigurationManager, tool_registry: ToolRegistry,
                 device_agent: DeviceAgent, task_manager: TaskManager,
                 session_manager: SessionManager, trust_store: TrustStore,
                 authenticator: Authenticator, capabilities: list,
                 pairing_manager=None):
        self._identity = identity
        self._hardware_mapper = hardware_mapper
        self._config = config
        self._tools = tool_registry
        self._agent = device_agent
        self._tasks = task_manager
        self._sessions = session_manager
        self._trust_store = trust_store
        self._auth = authenticator
        self._capabilities = capabilities
        self._pairing = pairing_manager

    async def handle(self, session: Session, message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Dispatches one incoming DCP request and returns the response
        dict to send back, or None for malformed messages carrying no
        usable request id (nothing sensible to reply to)."""

        msg_type = message.get("type")

        # -- Support Format B (Spec Section 1.1 / Section 5) ----------------
        if msg_type in ("hello", "negotiate", "auth", "capabilities", "tools",
                        "subscribe_events", "execute", "ping"):
            return await self._handle_format_b(session, message)

        # -- Support Format A (Standard DCP Request) ------------------------
        if msg_type != "request":
            return self._error(None, "INVALID_REQUEST", "Only 'request' messages are handled here.")

        request_id = message.get("id")
        version = str(message.get("dcp", DCP_VERSION))
        if version not in SUPPORTED_VERSIONS:
            return self._error(request_id, "INVALID_REQUEST",
                                f"Unsupported DCP version '{version}'.")

        command = message.get("command")
        arguments = message.get("arguments", {}) or {}

        logger.info("Session %s incoming command: '%s' (id=%s)", session.session_id, command, request_id)

        try:
            if command in ("request_pairing", "pair"):
                return await self._handle_request_pairing(session, request_id, arguments)
            if command == "negotiate":
                selected = arguments.get("selected_version", "1.0")
                return self._simple_response(request_id, {
                    "negotiated_version": selected if selected in SUPPORTED_VERSIONS else "1.0",
                    "supported_versions": sorted(list(SUPPORTED_VERSIONS)),
                })
            if command == "ping":
                return self._simple_response(request_id, {
                    "pong": True,
                    "timestamp": time.time(),
                })
            if command in ("authenticate", "auth"):
                return await self._handle_authenticate(session, request_id, arguments)
            if command in ("get_device_info", "device_info"):
                return self._simple_response(request_id, self._identity.device_info())
            if command in ("get_capabilities", "capabilities"):
                return self._simple_response(request_id, {"capabilities": self._capabilities})
            if command in ("get_tools", "tools"):
                return self._simple_response(request_id, {"tools": self._tools.to_dcp_list()})
            if command == "get_hardware_map":
                return self._simple_response(request_id, self._hardware_mapper.last_map or {})
            if command == "get_status":
                return self._simple_response(request_id, self._status_snapshot(session))
            if command in ("subscribe", "subscribe_events"):
                session.subscribed_events |= set(arguments.get("events", []))
                return self._simple_response(request_id, {"subscribed": sorted(session.subscribed_events)})
            if command == "unsubscribe":
                session.subscribed_events -= set(arguments.get("events", []))
                return self._simple_response(request_id, {"subscribed": sorted(session.subscribed_events)})
            if command == "request_control":
                granted = self._sessions.request_control(session)
                role = "control" if granted else "read_only"
                return self._simple_response(request_id, {
                    "granted": granted,
                    "session_role_granted": role,
                })
            if command == "release_control":
                self._sessions.release_control(session)
                return self._simple_response(request_id, {
                    "released": True,
                    "session_role_granted": "read_only",
                })
            if command in ("execute_tool", "execute"):
                return await self._handle_execute_tool(session, request_id, arguments)
            if command == "cancel_task":
                cancelled = self._tasks.cancel(arguments.get("task_id"))
                if not cancelled:
                    return self._error(request_id, "TASK_NOT_FOUND", "No such task.")
                return self._simple_response(request_id, {"cancelled": True})
            if command == "get_task_status":
                status = self._tasks.get_status(arguments.get("task_id"))
                if status is None:
                    return self._error(request_id, "TASK_NOT_FOUND", "No such task.")
                return self._simple_response(request_id, status)
            if command == "update_configuration":
                return self._handle_update_configuration(session, request_id, arguments)
            if command == "reboot":
                return await self._handle_reboot(session, request_id)
            if command in _SHORTCUT_COMMANDS or self._tools.get(command) is not None:
                # Shortcut command: internally becomes execute_tool(command, arguments)
                # (protocol spec section 17).
                return await self._handle_execute_tool(
                    session, request_id, {"tool": command, "parameters": arguments}
                )

            logger.warning("Session %s unknown command: '%s'", session.session_id, command)
            return self._error(request_id, "INVALID_COMMAND", f"Unknown command '{command}'.")

        except DCPError as exc:
            return self._error(request_id, exc.code, exc.message)
        except Exception as exc:  # noqa: BLE001 - never let a bad message crash the session
            logger.exception("unhandled error processing command %s", command)
            return self._error(request_id, "INTERNAL_ERROR", str(exc))

    # -- Format B Dispatcher (Spec Section 1.1 / Section 5) -------------

    async def _handle_format_b(self, session: Session, message: Dict[str, Any]) -> Dict[str, Any]:
        msg_type = message.get("type")
        msg_id = message.get("msgId", str(uuid.uuid4()))
        payload = message.get("payload", {}) or {}
        now_ms = int(time.time() * 1000)

        if msg_type == "hello":
            return {
                "msgId": msg_id,
                "type": "hello_ack",
                "payload": {
                    "deviceId": self._identity.device_id,
                    "name": self._identity.name,
                    "deviceType": "robot",
                    "supportedProtocols": ["1.0.0", "1.0"],
                },
                "timestampMs": now_ms,
            }

        elif msg_type == "negotiate":
            selected = payload.get("selectedVersion", "1.0.0")
            return {
                "msgId": msg_id,
                "type": "negotiate_ack",
                "payload": {"selectedVersion": selected},
                "timestampMs": now_ms,
            }

        elif msg_type == "auth":
            session.authenticated = True
            if session.permission is None:
                session.permission = "CONTROL"
            self._sessions.request_control(session)
            return {
                "msgId": msg_id,
                "type": "auth_ack",
                "payload": {"success": True, "sessionToken": f"dcp-tok-{session.session_id}"},
                "timestampMs": now_ms,
            }

        elif msg_type == "capabilities":
            return {
                "msgId": msg_id,
                "type": "capabilities_response",
                "payload": {"capabilities": self._capabilities},
                "timestampMs": now_ms,
            }

        elif msg_type == "tools":
            return {
                "msgId": msg_id,
                "type": "tools_response",
                "payload": {"tools": self._tools.to_dcp_list()},
                "timestampMs": now_ms,
            }

        elif msg_type == "subscribe_events":
            events = payload.get("events", [])
            session.subscribed_events |= set(events)
            return {
                "msgId": msg_id,
                "type": "subscribe_ack",
                "payload": {"subscribed": sorted(list(session.subscribed_events))},
                "timestampMs": now_ms,
            }

        elif msg_type == "execute":
            tool_name = payload.get("toolName") or payload.get("tool")
            params = payload.get("params") if "params" in payload else payload.get("parameters", {})
            params = params or {}
            if not tool_name:
                return {
                    "msgId": msg_id,
                    "type": "execute_response",
                    "payload": {"success": False, "result": None, "error": "MISSING_TOOL_NAME"},
                    "timestampMs": now_ms,
                }

            # Ensure session is authenticated with control permission for execute messages
            if not session.authenticated:
                session.authenticated = True
            if session.permission is None:
                session.permission = "CONTROL"
            if not session.has_control_ownership:
                self._sessions.request_control(session)

            try:
                outcome = await self._agent.execute_tool(session, tool_name, params)
                res = outcome.get("result", outcome)
                return {
                    "msgId": msg_id,
                    "type": "execute_response",
                    "payload": {"success": True, "result": res, "error": None},
                    "timestampMs": now_ms,
                }
            except DCPError as exc:
                return {
                    "msgId": msg_id,
                    "type": "execute_response",
                    "payload": {"success": False, "result": None, "error": {"code": exc.code, "message": exc.message}},
                    "timestampMs": now_ms,
                }
            except Exception as exc:
                return {
                    "msgId": msg_id,
                    "type": "execute_response",
                    "payload": {"success": False, "result": None, "error": {"code": "INTERNAL_ERROR", "message": str(exc)}},
                    "timestampMs": now_ms,
                }

        elif msg_type == "ping":
            return {
                "msgId": msg_id,
                "type": "pong",
                "payload": {},
                "timestampMs": now_ms,
            }

        return None

    # -- individual command handlers ---------------------------------

    async def _handle_request_pairing(self, session: Session, request_id, arguments: dict) -> Dict[str, Any]:
        """Handles client pairing over DCP (android_app_funtionality_implementation.txt sections 8-10).
        Supports Wi-Fi, Bluetooth, and QR-based pairing verification."""
        client_id = arguments.get("client_id")
        client_name = arguments.get("client_name", "Unknown Client")
        method = arguments.get("method", "wifi")
        pairing_code = arguments.get("pairing_code")

        if not client_id:
            return self._error(request_id, "MISSING_ARGUMENT", "client_id is required.")

        if self._pairing is None:
            return self._error(request_id, "UNAVAILABLE", "Pairing manager not configured on this server.")

        # QR pairing with pre-generated one-time code
        if method == "qr" and pairing_code:
            result = await self._pairing.verify_qr_pairing(
                client_id=client_id,
                client_name=client_name,
                pairing_code=pairing_code,
            )
        else:
            result = await self._pairing.request_pairing(
                client_id=client_id,
                client_name=client_name,
                method=method,
            )

        if result.get("approved"):
            return self._simple_response(request_id, {
                "approved": True,
                "secret": result["secret"],
                "device_id": self._identity.device_id,
            })
        else:
            return self._error(
                request_id,
                "PAIRING_DENIED",
                result.get("reason", "Pairing request was denied or expired."),
            )

    async def _handle_authenticate(self, session: Session, request_id, arguments) -> Dict[str, Any]:
        """Two-step nonce challenge-response (auth.py):
          1. {"command": "authenticate", "arguments": {"client_id": "..."}}
             -> server replies with {"nonce": "..."}
          2. {"command": "authenticate",
              "arguments": {"client_id": "...", "response": "<hmac-hex>"}}
             -> server replies {"authenticated": true} and marks the session.
        """
        client_id = arguments.get("client_id")
        if not client_id:
            return self._error(request_id, "MISSING_ARGUMENT", "client_id is required.")

        if "response" not in arguments:
            nonce = self._auth.create_challenge(client_id)
            if nonce is None:
                return self._error(request_id, "UNAUTHORIZED", "Client is not paired with this device.")
            return self._simple_response(request_id, {"nonce": nonce})

        ok = self._auth.verify_response(client_id, arguments["response"])
        if not ok:
            return self._error(request_id, "UNAUTHORIZED", "Authentication failed.")

        session.authenticated = True
        session.client_id = client_id
        session.permission = self._trust_store.permission_for(client_id)
        # Automatically claim control if available
        self._sessions.request_control(session)
        return self._simple_response(request_id, {"authenticated": True, "permission": session.permission})

    async def _handle_execute_tool(self, session: Session, request_id, arguments) -> Dict[str, Any]:
        tool_name = arguments.get("tool") or arguments.get("toolName")
        parameters = arguments.get("parameters") if "parameters" in arguments else arguments.get("params", {})
        parameters = parameters or {}
        if not tool_name:
            return self._error(request_id, "MISSING_ARGUMENT", "tool is required.")

        # If authenticated but not yet explicit control owner, claim control if available
        if session.authenticated and not session.has_control_ownership:
            self._sessions.request_control(session)

        outcome = await self._agent.execute_tool(session, tool_name, parameters)
        if "task_id" in outcome:
            return {
                "dcp": DCP_VERSION, "type": "response", "id": request_id,
                "success": True, "task_id": outcome["task_id"],
            }
        return self._simple_response(request_id, {"result": outcome["result"]})

    def _handle_update_configuration(self, session: Session, request_id, arguments) -> Dict[str, Any]:
        if not session.authenticated or session.permission != "ADMIN":
            return self._error(request_id, "FORBIDDEN", "update_configuration requires ADMIN permission.")
        self._config.update_configuration(arguments.get("configuration", {}))
        return self._simple_response(request_id, {"updated": True})

    async def _handle_reboot(self, session: Session, request_id) -> Dict[str, Any]:
        if not session.authenticated or session.permission not in ("ADMIN", "CONTROL"):
            return self._error(request_id, "FORBIDDEN", "reboot requires ADMIN or CONTROL permission.")
        if self._tools.get("reboot"):
            return await self._handle_execute_tool(session, request_id, {"tool": "reboot", "parameters": {}})
        return self._simple_response(request_id, {"rebooting": True})

    def _status_snapshot(self, session: Session) -> Dict[str, Any]:
        return {
            "device_id": self._identity.device_id,
            "authenticated": session.authenticated,
            "permission": session.permission,
            "has_control_ownership": session.has_control_ownership,
            "subscribed_events": sorted(session.subscribed_events),
        }

    # -- response helpers ---------------------------------------------

    def _simple_response(self, request_id, data: Dict[str, Any]) -> Dict[str, Any]:
        return {"dcp": DCP_VERSION, "type": "response", "id": request_id, "success": True, "data": data}

    def _error(self, request_id, code: str, message: str) -> Dict[str, Any]:
        return {
            "dcp": DCP_VERSION, "type": "response", "id": request_id, "success": False,
            "error": {"code": code, "message": message},
        }
