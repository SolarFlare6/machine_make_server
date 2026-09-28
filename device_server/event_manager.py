"""
event_manager.py

Event Manager (device_server_hardware_mapper.txt section 23-ish via the
Flutter doc's equivalent; dcp_protocol_specification.txt sections 9-10,
19-20). Broadcasts asynchronous DCP events to every session subscribed to
that event name, in the exact envelope shape the protocol defines.
"""

import logging
from typing import Any, Dict, Optional

from session import SessionManager

logger = logging.getLogger("event_manager")

DCP_VERSION = "1.0"


class EventManager:
    def __init__(self, session_manager: SessionManager, identity=None):
        self._sessions = session_manager
        self._identity = identity

    def set_identity(self, identity) -> None:
        self._identity = identity

    async def emit(self, event_name: str, data: Dict[str, Any] = None,
                    task_id: Optional[str] = None) -> None:
        payload_data = dict(data or {})
        if task_id is not None:
            payload_data.setdefault("task_id", task_id)

        device_id = self._identity.device_id if self._identity else ""

        message: Dict[str, Any] = {
            "dcp": DCP_VERSION,
            "type": "event",
            "event": event_name,
            "event_type": event_name,
            "device_id": device_id,
            "data": payload_data,
        }
        if task_id is not None:
            message["task_id"] = task_id

        target_sessions: Dict[int, Session] = {}
        for s in self._sessions.sessions_subscribed_to(event_name):
            target_sessions[s.session_id] = s
        if event_name in ("task_update", "task_progress", "task_started", "task_completed", "task_failed"):
            for alt in ("task_update", "task_progress", "task_completed", "task_started"):
                for s in self._sessions.sessions_subscribed_to(alt):
                    target_sessions[s.session_id] = s

        for session in target_sessions.values():
            try:
                await session.send(message)
            except Exception:  # noqa: BLE001
                logger.exception("failed to deliver event %s to session %s",
                                  event_name, session.session_id)
