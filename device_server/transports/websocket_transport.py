"""
transports/websocket_transport.py

Wi-Fi DCP Server: WebSocket over TCP
(device_server_hardware_mapper.txt section 13-ish equivalent;
dcp_protocol_specification.txt section 31).

Purely a framing/delivery layer: it JSON-decodes incoming frames, hands
them to DCPHandler.handle(), JSON-encodes the response, and sends it
back. It also gives EventManager a way to push messages to this specific
session asynchronously (via session.send_fn). No DCP semantics live here
-- the exact same message dict this module parses is what a BLE
transport would parse too (protocol spec section 33, "transport
independence").
"""

import asyncio
import json
import logging

import websockets

from dcp_handler import DCPHandler
from session import Session, SessionManager

logger = logging.getLogger("websocket_transport")


class ProbeHandshakeFilter(logging.Filter):
    """
    Suppresses noisy 'opening handshake failed' traceback logs caused by port scans
    or TCP probes (such as the mobile app scanning the local subnet).
    These probes open a raw TCP socket to check if port 8765 is listening and
    immediately close it without completing an HTTP/WebSocket upgrade handshake.
    """
    def filter(self, record: logging.LogRecord) -> bool:
        if "opening handshake failed" in record.getMessage():
            logger.debug(
                "Suppressed probe connection handshake error: %s",
                record.exc_info[1] if record.exc_info and record.exc_info[1] else record.getMessage()
            )
            return False
        return True


def apply_probe_filter() -> None:
    ws_logger = logging.getLogger("websockets.server")
    if not any(isinstance(f, ProbeHandshakeFilter) for f in ws_logger.filters):
        ws_logger.addFilter(ProbeHandshakeFilter())


apply_probe_filter()


class WiFiDCPServer:
    def __init__(self, dcp_handler: DCPHandler, session_manager: SessionManager,
                 host: str = "0.0.0.0", port: int = 8765):
        self._handler = dcp_handler
        self._sessions = session_manager
        self._host = host
        self._port = port
        self._server = None

    async def start(self) -> None:
        apply_probe_filter()
        self._server = await websockets.serve(self._on_connect, self._host, self._port)
        logger.info("Wi-Fi DCP server listening on ws://%s:%s", self._host, self._port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _on_connect(self, websocket) -> None:
        async def send_fn(message: dict) -> None:
            await websocket.send(json.dumps(message))

        session: Session = self._sessions.create_session(send_fn)
        logger.info("client connected: session %s", session.session_id)

        try:
            async for raw in websocket:
                try:
                    message = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    await send_fn({
                        "dcp": "1.0", "type": "response", "id": None, "success": False,
                        "error": {"code": "INVALID_REQUEST", "message": "Malformed JSON."},
                    })
                    continue

                response = await self._handler.handle(session, message)
                if response is not None:
                    await send_fn(response)
        except websockets.exceptions.ConnectionClosed as exc:
            logger.debug("session %s connection closed: code=%s, reason=%s", session.session_id, exc.code, exc.reason)
        except Exception as exc:
            logger.warning("session %s unexpected error: %s", session.session_id, exc)
        finally:
            logger.info("client disconnected: session %s", session.session_id)
            self._sessions.remove_session(session)
            # Interactive-control safety behavior on disconnect
            # (protocol spec section 44) belongs at the caller/main level,
            # since it needs the RobotController/SafetyManager -- see
            # main.py's on_disconnect hook.
