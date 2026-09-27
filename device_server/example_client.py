"""
example_client.py

Minimal DCP client for manually exercising the Device Server over
Wi-Fi/WebSocket -- useful for development before the real Flutter app
(or Needle) is wired up. Demonstrates the full session flow from
dcp_protocol_specification.txt section 34:

    connect -> authenticate -> get_device_info -> get_capabilities
    -> get_tools -> subscribe -> request_control -> execute_tool

Usage:
    1. Start the server:      python main.py
    2. Pair this client once: python example_client.py pair
       (approve the [Y] prompt in the server's terminal)
    3. Then run:               python example_client.py demo-client-1
"""

import asyncio
import hashlib
import hmac
import json
import sys

import websockets

SERVER_URL = "ws://localhost:8765"


async def pair(client_id: str = "demo-client-1", name: str = "Example Client") -> None:
    """Pairing normally goes through PairingManager on the server side,
    triggered by some out-of-band flow (QR scan, admin action, etc). This
    helper drives that flow directly against a locally-imported
    PairingManager/TrustStore for convenience during development, then
    prints the secret the real client would need to store."""
    from auth import TrustStore
    from pairing import PairingManager

    trust_store = TrustStore()

    async def auto_confirm(request):
        print(f"[pair] auto-approving pairing request {request.request_id} for {request.client_name}")
        return True

    pairing_manager = PairingManager(trust_store, confirm_fn=auto_confirm)
    result = await pairing_manager.request_pairing(client_id, name, method="wifi")
    print(json.dumps(result, indent=2))


async def demo(client_id: str) -> None:
    from auth import TrustStore
    trust_store = TrustStore()
    secret = trust_store.secret_for(client_id)
    if secret is None:
        print(f"'{client_id}' is not paired yet. Run: python example_client.py pair")
        return

    async with websockets.connect(SERVER_URL) as ws:
        req_id = 0

        async def call(command: str, arguments: dict = None) -> dict:
            nonlocal req_id
            req_id += 1
            await ws.send(json.dumps({
                "dcp": "1.0", "type": "request", "id": req_id,
                "command": command, "arguments": arguments or {},
            }))
            return json.loads(await ws.recv())

        # Authenticate: step 1 (get nonce)
        resp = await call("authenticate", {"client_id": client_id})
        nonce = resp["data"]["nonce"]
        response_hex = hmac.new(bytes.fromhex(secret), nonce.encode(), hashlib.sha256).hexdigest()

        # Authenticate: step 2 (prove knowledge of the shared secret)
        resp = await call("authenticate", {"client_id": client_id, "response": response_hex})
        print("authenticate:", resp)

        print("device_info:", await call("get_device_info"))
        print("capabilities:", await call("get_capabilities"))
        print("tools:", await call("get_tools"))
        print("subscribe:", await call("subscribe", {"events": ["task_progress", "task_completed"]}))
        print("request_control:", await call("request_control"))

        resp = await call("execute_tool", {"tool": "stand", "parameters": {}})
        print("execute_tool(stand):", resp)

        resp = await call("execute_tool", {
            "tool": "walk", "parameters": {"direction": "forward", "distance": 0.5},
        })
        print("execute_tool(walk):", resp)

        # Long-running: drain a few task_progress / task_completed events.
        for _ in range(15):
            try:
                event = json.loads(await asyncio.wait_for(ws.recv(), timeout=3))
                print("event:", event)
                if event.get("event") == "task_completed":
                    break
            except asyncio.TimeoutError:
                break


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "pair":
        asyncio.run(pair())
    else:
        client_id = sys.argv[1] if len(sys.argv) > 1 else "demo-client-1"
        asyncio.run(demo(client_id))
