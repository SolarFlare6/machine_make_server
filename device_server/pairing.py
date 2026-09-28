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
import json
import logging
import os
import secrets
import socket
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from auth import TrustStore

logger = logging.getLogger("pairing")

REQUEST_TTL_SECONDS = 60
QR_TTL_SECONDS_DEFAULT = 300
BLE_DCP_SERVICE_UUID = "dcf00001-0000-1000-8000-00805f9b34fb"


@dataclass
class PairingRequest:
    request_id: str
    client_id: str
    client_name: str
    method: str  # "wifi" | "bluetooth" | "qr"
    created_at: float = field(default_factory=time.time)
    decided: Optional[bool] = None
    pairing_code: Optional[str] = None

    def expired(self) -> bool:
        return (time.time() - self.created_at) > REQUEST_TTL_SECONDS


def print_qr_terminal(qr_data: str) -> None:
    """Renders QR code directly in the terminal using ASCII/Unicode blocks."""
    try:
        import qrcode

        # Ensure utf-8 encoding on stdout for unicode block characters if possible
        if hasattr(sys.stdout, "reconfigure"):
            try:
                sys.stdout.reconfigure(encoding="utf-8")
            except Exception:
                pass

        qr = qrcode.QRCode(border=1)
        qr.add_data(qr_data)
        qr.make(fit=True)
        try:
            qr.print_ascii(invert=True)
        except Exception:
            qr.print_ascii(out=sys.stdout, tty=False)
    except Exception as exc:
        logger.warning("Could not print QR code to terminal: %s", exc)


_ACTIVE_QR_FILE_DEFAULT = os.path.join(os.path.dirname(__file__), "config", "active_qr_pairings.json")


class PairingManager:
    def __init__(self, trust_store: TrustStore, confirm_fn=None, identity=None,
                 active_qr_file: str = _ACTIVE_QR_FILE_DEFAULT):
        """confirm_fn: optional callable(PairingRequest) -> bool, used
        instead of the terminal prompt (e.g. for tests, or a future GUI
        confirmation dialog). Defaults to the terminal Y/N prompt shown in
        the spec."""
        self._trust_store = trust_store
        self._confirm_fn = confirm_fn or self._terminal_confirm
        self._identity = identity
        self._active_qr_file = active_qr_file
        self._requests: Dict[str, PairingRequest] = {}
        self._active_qr_pairings: Dict[str, Dict[str, Any]] = {}
        self._load_active_qr()

    def set_identity(self, identity) -> None:
        self._identity = identity

    def _load_active_qr(self) -> None:
        """Loads non-expired active QR codes from disk so CLI tools and server share them."""
        now = time.time()
        if os.path.isfile(self._active_qr_file):
            try:
                with open(self._active_qr_file, "r") as f:
                    data = json.load(f)
                # Keep only valid non-expired codes
                self._active_qr_pairings = {
                    k: v for k, v in data.items() if v.get("expires_at", 0) > now
                }
            except Exception as exc:
                logger.warning("Could not read active QR file: %s", exc)
                self._active_qr_pairings = {}
        else:
            self._active_qr_pairings = {}

    def _persist_active_qr(self) -> None:
        """Persists active QR pairing codes to disk."""
        now = time.time()
        # Clean expired before saving
        valid = {k: v for k, v in self._active_qr_pairings.items() if v.get("expires_at", 0) > now}
        self._active_qr_pairings = valid
        try:
            os.makedirs(os.path.dirname(self._active_qr_file), exist_ok=True)
            with open(self._active_qr_file, "w") as f:
                json.dump(valid, f, indent=2)
        except Exception as exc:
            logger.warning("Could not persist active QR file: %s", exc)

    def generate_qr_pairing(
        self,
        client_name: str = "Mobile App",
        client_id: Optional[str] = None,
        pairing_code: Optional[str] = None,
        ttl_seconds: int = QR_TTL_SECONDS_DEFAULT,
        host: Optional[str] = None,
        port: int = 8765,
        save_path: str = "qr_pairing.png",
        display_terminal: bool = True,
    ) -> Dict[str, Any]:
        """Generates a QR pairing invitation containing public discovery and
        one-time pairing verification data (android_app_funtionality_implementation.txt section 10).
        Renders ASCII QR in console and saves PNG image."""
        self._load_active_qr()

        code = str(pairing_code).strip() if pairing_code else f"{secrets.randbelow(900000) + 100000}"
        local_host = host or self._get_local_ip()

        device_id = self._identity.device_id if self._identity else "unknown"
        device_name = self._identity.name if self._identity else "Machine Make Device"
        profile = self._identity.profile if self._identity else "generic"

        # Public QR payload per Section 10 specification aligned with Flutter PairingManager.parseQrPayload
        payload = {
            "protocol": "DCP",
            "version": "1.0",
            "device_id": device_id,
            "name": device_name,
            "device_name": device_name,
            "profile": profile,
            "pairing": "qr",
            "action": "pair",
            "pairing_code": code,
            "host": local_host,
            "address": local_host,
            "ip": local_host,
            "port": port,
            "transport": "wifi",
            "ble_service_uuid": BLE_DCP_SERVICE_UUID,
        }

        payload_json = json.dumps(payload, separators=(",", ":"))

        # Save image file
        abs_save_path = os.path.abspath(save_path)
        try:
            import qrcode
            img = qrcode.make(payload_json)
            img.save(abs_save_path)
        except Exception as exc:
            logger.warning("Could not save QR code image to %s: %s", abs_save_path, exc)
            abs_save_path = None

        expires_at = time.time() + ttl_seconds
        self._active_qr_pairings[code] = {
            "pairing_code": code,
            "client_name": client_name,
            "client_id": client_id,
            "created_at": time.time(),
            "expires_at": expires_at,
            "claimed_by": None,
        }
        self._persist_active_qr()

        if display_terminal:
            banner = (
                "\n" + "=" * 50 + "\n"
                "         MACHINE MAKE - DCP QR PAIRING\n"
                + "=" * 50 + "\n"
                f"Device:       {device_name} ({device_id})\n"
                f"Profile:      {profile}\n"
                f"Pairing Code: {code}\n"
                f"Target:       {local_host}:{port}\n"
                f"BLE Service:  {BLE_DCP_SERVICE_UUID}\n"
                f"Expires In:   {ttl_seconds} seconds\n"
                + (f"Saved Image:  {abs_save_path}\n" if abs_save_path else "")
                + "-" * 50 + "\n"
                "Scan the QR code below using the Machine Make app:\n"
            )
            print(banner)
            print_qr_terminal(payload_json)
            print("=" * 50 + "\n")

        return {
            "approved": True,
            "pairing_code": code,
            "payload": payload,
            "payload_json": payload_json,
            "image_path": abs_save_path,
            "expires_at": expires_at,
        }

    async def verify_qr_pairing(
        self,
        client_id: str,
        client_name: str,
        pairing_code: str,
    ) -> Dict[str, Any]:
        """Validates a client attempting to complete pairing using a pre-generated QR code.
        Idempotent: If client is already paired in TrustStore, re-returns the shared secret."""
        # 1. Idempotence check: If this client was already paired (e.g. fast repeated camera scans)
        if self._trust_store.is_trusted(client_id):
            existing_secret = self._trust_store.secret_for(client_id)
            logger.info("Client '%s' is already trusted in TrustStore; returning existing pairing credentials.", client_id)
            return {
                "approved": True,
                "secret": existing_secret,
                "client_id": client_id,
                "device_id": self._identity.device_id if self._identity else None,
            }

        # 2. Check active QR pairing codes (reload from disk to see codes from qr_tool.py or other processes)
        self._load_active_qr()
        code_str = str(pairing_code).strip()
        entry = self._active_qr_pairings.get(code_str)

        if not entry:
            logger.warning("QR pairing attempt with unknown code: %s", code_str)
            return {"approved": False, "reason": "invalid_pairing_code"}

        if time.time() > entry.get("expires_at", 0):
            self._active_qr_pairings.pop(code_str, None)
            self._persist_active_qr()
            logger.warning("QR pairing attempt with expired code: %s", code_str)
            return {"approved": False, "reason": "expired_pairing_code"}

        # 3. Add to TrustStore
        secret = self._trust_store.add_trusted(client_id, client_name, permission="CONTROL")
        logger.info("QR pairing successful for client '%s' (%s) with code %s", client_id, client_name, code_str)

        # Mark as claimed by this client and keep for a 60s grace period for camera retries
        entry["claimed_by"] = client_id
        entry["expires_at"] = min(entry["expires_at"], time.time() + 60)
        self._persist_active_qr()

        return {
            "approved": True,
            "secret": secret,
            "client_id": client_id,
            "device_id": self._identity.device_id if self._identity else None,
        }

    async def request_pairing(self, client_id: str, client_name: str, method: str) -> Dict:
        """Runs the interactive pairing flow for one client (wifi / bluetooth) and returns
        either {"approved": True, "secret": "..."} or {"approved": False, "reason": "..."}."""
        request = PairingRequest(
            request_id=f"{secrets.randbelow(900000) + 100000}",
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
        return {
            "approved": True,
            "secret": secret,
            "client_id": client_id,
            "device_id": self._identity.device_id if self._identity else None,
        }

    async def _terminal_confirm(self, request: PairingRequest) -> bool:
        """Blocking input() run in a thread pool so it never stalls the asyncio event loop."""
        loop = asyncio.get_running_loop()

        banner = (
            "\n" + "=" * 32 + "\n"
            "       DCP PAIRING REQUEST\n"
            + "=" * 32 + "\n"
            f"Device: {request.client_name}\n"
            f"Method: {request.method}\n"
            f"Request ID: {request.request_id}\n\n"
            "Allow this device?\n\n"
            "[Y] Yes\n"
            "[N] No\n"
            "> "
        )

        def _prompt() -> bool:
            try:
                answer = input(banner).strip().lower()
                return answer in ("y", "yes")
            except (EOFError, KeyboardInterrupt):
                return False

        return await loop.run_in_executor(None, _prompt)

    @staticmethod
    def _get_local_ip() -> str:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"
        finally:
            s.close()

