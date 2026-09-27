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
    def __init__(self, session_manager: SessionManager):
        self._sessions = session_manager

    async def emit(self, event_name: str, data: Dict[str, Any] = None,
                    task_id: Optional[str] = None) -> None:
        message: Dict[str, Any] = {
            "dcp": DCP_VERSION,
            "type": "event",
            "event": event_name,
            "data": data or {},
        }
        if task_id is not None:
            message["task_id"] = task_id

        for session in self._sessions.sessions_subscribed_to(event_name):
            try:
                await session.send(message)
            except Exception:  # noqa: BLE001
                logger.exception("failed to deliver event %s to session %s",
                                  event_name, session.session_id)
