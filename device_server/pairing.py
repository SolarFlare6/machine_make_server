"""
pairing.py

PairingManager (device_server_hardware_mapper.txt, sections 6-7;
android_app_funtionality_implementation.txt, sections 8-10).

Pairing establishes trust; it is distinct from authentication (which
proves identity on every subsequent connection) and from transport
selection (section 11 of the app doc).

Every pairing request:
  - has a request ID
  - carries the claimed client identity
  - shows the pairing method (wifi / bluetooth / qr)
  - expires after a limited time (default 60s, per the app spec example)
  - requires explicit confirmation -- here, via the server's terminal,
    matching the exact prompt shown in both spec documents.
"""

import asyncio
import secrets
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

from auth import TrustStore

REQUEST_TTL_SECONDS = 60


@dataclass
class PairingRequest:
    request_id: str
    client_id: str
    client_name: str
    method: str  # "wifi" | "bluetooth" | "qr"
    created_at: float = field(default_factory=time.time)
    decided: Optional[bool] = None

    def expired(self) -> bool:
        return (time.time() - self.created_at) > REQUEST_TTL_SECONDS


class PairingManager:
    def __init__(self, trust_store: TrustStore, confirm_fn=None):
        """confirm_fn: optional callable(PairingRequest) -> bool, used
        instead of the terminal prompt (e.g. for tests, or a future GUI
        confirmation dialog). Defaults to the terminal Y/N prompt shown in
        the spec."""
        self._trust_store = trust_store
        self._confirm_fn = confirm_fn or self._terminal_confirm
        self._requests: Dict[str, PairingRequest] = {}

    async def request_pairing(self, client_id: str, client_name: str, method: str) -> Dict:
        """Runs the full pairing flow for one client and returns either
        {"approved": True, "secret": "..."} or {"approved": False,
        "reason": "..."}. The generated secret must be delivered to the
        client over the SAME channel as this pairing exchange -- never
        logged or advertised."""
        request = PairingRequest(
            request_id=secrets.token_hex(4),
            client_id=client_id,
            client_name=client_name,
            method=method,
        )
        self._requests[request.request_id] = request

        try:
            approved = await asyncio.wait_for(
                self._confirm_fn(request), timeout=REQUEST_TTL_SECONDS
            )
        except asyncio.TimeoutError:
            approved = False

        if request.expired():
            approved = False

        if not approved:
            return {"approved": False, "reason": "denied_or_expired"}

        secret = self._trust_store.add_trusted(client_id, client_name, permission="CONTROL")
        return {"approved": True, "secret": secret, "device_id_field": "client_id"}

    async def _terminal_confirm(self, request: PairingRequest) -> bool:
        """Blocking input() run in a thread pool so it never stalls the
        asyncio event loop, matching the exact prompt format specified in
        both source documents."""
        loop = asyncio.get_event_loop()

        banner = (
            "\n================================\n"
            "       DCP PAIRING REQUEST\n"
            "================================\n"
            f"Device: {request.client_name}\n"
            f"Method: {request.method}\n"
            f"Request ID: {request.request_id}\n\n"
            "Allow this device?\n\n"
            "[Y] Yes\n"
            "[N] No\n"
            "> "
        )

        def _prompt() -> bool:
            answer = input(banner).strip().lower()
            return answer in ("y", "yes")

        return await loop.run_in_executor(None, _prompt)
