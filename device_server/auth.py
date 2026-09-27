"""
auth.py

Authentication and trusted-device storage
(device_server_hardware_mapper.txt, sections 6-9; dcp_protocol_specification.txt,
sections 26-29).

Design:
  - Pairing (pairing.py) establishes a per-client shared secret after an
    explicit terminal confirmation, and hands it to TrustStore.add_trusted().
  - Authentication is a nonce challenge-response over that shared secret
    (HMAC-SHA256), so nothing that looks like a password or key ever goes
    over the wire in the clear, and replays of an old response fail
    because the nonce changes every session.
  - IP address, Bluetooth MAC, hostname and device name are never used
    as authentication evidence, only as display/debug labels -- per
    section 29 of the protocol spec.

Swapping this for asymmetric crypto (e.g. Ed25519 client certificates)
later is possible without changing DCP itself: authentication is a
transport/session concern, not a protocol-message concern beyond the
`authenticate` command carrying whatever the scheme needs.
"""

import hmac
import hashlib
import json
import os
import secrets
import time
from typing import Any, Dict, Optional

_TRUSTED_FILE_DEFAULT = os.path.join(os.path.dirname(__file__), "config", "trusted_devices.json")


class TrustStore:
    """Persists trusted clients: client_id -> {secret, name, permission}."""

    def __init__(self, trusted_file: str = _TRUSTED_FILE_DEFAULT):
        self._trusted_file = trusted_file
        self._trusted: Dict[str, Dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if os.path.isfile(self._trusted_file):
            with open(self._trusted_file, "r") as f:
                self._trusted = json.load(f)

    def _persist(self) -> None:
        os.makedirs(os.path.dirname(self._trusted_file), exist_ok=True)
        with open(self._trusted_file, "w") as f:
            json.dump(self._trusted, f, indent=2)

    def is_trusted(self, client_id: str) -> bool:
        return client_id in self._trusted

    def add_trusted(self, client_id: str, name: str, permission: str = "CONTROL") -> str:
        """Called after a pairing request is approved. Returns the newly
        generated shared secret (hex) -- the caller is responsible for
        getting it to the client out-of-band, e.g. embedded in a QR code
        per android_app_funtionality_implementation.txt section 10, or
        returned once as part of the pairing response."""
        secret = secrets.token_hex(32)
        self._trusted[client_id] = {"secret": secret, "name": name, "permission": permission}
        self._persist()
        return secret

    def revoke(self, client_id: str) -> None:
        self._trusted.pop(client_id, None)
        self._persist()

    def permission_for(self, client_id: str) -> Optional[str]:
        entry = self._trusted.get(client_id)
        return entry["permission"] if entry else None

    def secret_for(self, client_id: str) -> Optional[str]:
        entry = self._trusted.get(client_id)
        return entry["secret"] if entry else None


class Authenticator:
    """Runs the challenge-response handshake for one connection at a time.
    A fresh Challenge should be created per connection attempt -- never
    reused -- to prevent replay."""

    NONCE_TTL_SECONDS = 30

    def __init__(self, trust_store: TrustStore):
        self._trust_store = trust_store
        self._pending_nonces: Dict[str, Dict[str, Any]] = {}  # client_id -> {nonce, expires_at}

    def create_challenge(self, client_id: str) -> Optional[str]:
        """Returns a nonce (hex) to send to the client, or None if the
        client_id is not a trusted/paired client at all."""
        if not self._trust_store.is_trusted(client_id):
            return None
        nonce = secrets.token_hex(16)
        self._pending_nonces[client_id] = {
            "nonce": nonce,
            "expires_at": time.time() + self.NONCE_TTL_SECONDS,
        }
        return nonce

    def verify_response(self, client_id: str, response_hex: str) -> bool:
        """Verifies HMAC-SHA256(shared_secret, nonce) == response_hex,
        using a constant-time comparison to avoid timing side-channels."""
        pending = self._pending_nonces.pop(client_id, None)
        if pending is None:
            return False
        if time.time() > pending["expires_at"]:
            return False

        secret = self._trust_store.secret_for(client_id)
        if secret is None:
            return False

        expected = hmac.new(bytes.fromhex(secret), pending["nonce"].encode("utf-8"),
                             hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, response_hex)
