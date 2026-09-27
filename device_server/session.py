"""
session.py

Session Manager (device_server_hardware_mapper.txt sections 27, 37;
dcp_protocol_specification.txt section 34, 43).

A Session exists per connected client (one per transport connection) and
tracks: authentication state, permission level, event subscriptions, and
whether this session currently holds CONTROL ownership. Only one session
may hold CONTROL ownership at a time -- everyone else is effectively
READ_ONLY for hardware-affecting tools regardless of their stored
permission, matching "Only one session should normally hold CONTROL
ownership for safety" (app spec, section 27).
"""

import itertools
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Set

_id_counter = itertools.count(1)


@dataclass
class Session:
    session_id: int
    send_fn: Any  # async callable(dict) -> None; how to push a message to this client
    client_id: Optional[str] = None
    authenticated: bool = False
    permission: Optional[str] = None  # READ_ONLY | CONTROL | ADMIN, from TrustStore
    subscribed_events: Set[str] = field(default_factory=set)
    has_control_ownership: bool = False

    async def send(self, message: Dict[str, Any]) -> None:
        await self.send_fn(message)


class SessionManager:
    def __init__(self):
        self._sessions: Dict[int, Session] = {}
        self._control_owner_id: Optional[int] = None

    def create_session(self, send_fn) -> Session:
        session = Session(session_id=next(_id_counter), send_fn=send_fn)
        self._sessions[session.session_id] = session
        return session

    def remove_session(self, session: Session) -> None:
        self._sessions.pop(session.session_id, None)
        if self._control_owner_id == session.session_id:
            self._control_owner_id = None

    def request_control(self, session: Session) -> bool:
        """Grants CONTROL ownership if nobody else currently holds it, or
        if this session already holds it (idempotent). Returns whether
        ownership is now held by this session."""
        if self._control_owner_id in (None, session.session_id):
            self._control_owner_id = session.session_id
            session.has_control_ownership = True
            return True
        return False

    def release_control(self, session: Session) -> None:
        if self._control_owner_id == session.session_id:
            self._control_owner_id = None
        session.has_control_ownership = False

    def sessions_subscribed_to(self, event_name: str):
        return [s for s in self._sessions.values() if event_name in s.subscribed_events]

    def all_sessions(self):
        return list(self._sessions.values())
