"""
test_pairing_and_bluetooth.py

Automated test suite verifying:
1. BLE packet fragmentation and reassembly matching protocol specification.
2. QR pairing generation (ASCII rendering, image creation, payload structure).
3. QR pairing verification and trust establishment.
4. Bluetooth / Wi-Fi pairing request dispatch in DCPHandler.
5. End-to-end authentication handshake using the pairing secret.
"""

import asyncio
import hashlib
import hmac
import json
import os
import unittest

from auth import Authenticator, TrustStore
from config import ConfigurationManager
from dcp_handler import DCPHandler
from hardware_mapper.mapper import HardwareMapper
from identity import DeviceIdentity
from pairing import PairingManager
from device_agent import DeviceAgent
from event_manager import EventManager
from robot_controller import RobotController
from safety_manager import SafetyManager
from session import Session, SessionManager
from task_manager import TaskManager
from tool_registry import ToolRegistry
from transports.bluetooth_transport import (
    BLE_SERVICE_UUID,
    BluetoothDCPServer,
    fragment_message,
    reassemble_chunk,
)


class TestPairingAndBluetooth(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = ConfigurationManager()
        self.identity = DeviceIdentity(name="Test Robot", profile="quadruped")
        self.test_trust_file = "config/test_trusted.json"
        if os.path.exists(self.test_trust_file):
            os.remove(self.test_trust_file)

        self.trust_store = TrustStore(trusted_file=self.test_trust_file)
        self.pairing_manager = PairingManager(self.trust_store, identity=self.identity)
        self.authenticator = Authenticator(self.trust_store)
        self.session_manager = SessionManager()
        self.safety_manager = SafetyManager()
        self.event_manager = EventManager(self.session_manager, identity=self.identity)
        self.task_manager = TaskManager(self.event_manager, self.safety_manager)
        self.tool_registry = ToolRegistry()
        self.robot_controller = RobotController({"servos": {"servo_1": 0}, "pca9685": {}})
        self.tool_registry.build_default_tools(
            ["stand", "sit", "walk", "turn", "imu", "led", "movement", "servo", "camera"],
            self.robot_controller,
            {},
        )
        self.device_agent = DeviceAgent(self.tool_registry, self.safety_manager, self.task_manager)

        self.dcp_handler = DCPHandler(
            identity=self.identity,
            hardware_mapper=HardwareMapper(),
            config=self.config,
            tool_registry=self.tool_registry,
            device_agent=self.device_agent,
            task_manager=self.task_manager,
            session_manager=self.session_manager,
            trust_store=self.trust_store,
            authenticator=self.authenticator,
            capabilities=["robot_movement", "camera"],
            pairing_manager=self.pairing_manager,
        )

    def tearDown(self):
        if os.path.exists(self.test_trust_file):
            os.remove(self.test_trust_file)
        if os.path.exists("test_qr.png"):
            os.remove("test_qr.png")

    def test_ble_fragmentation_and_reassembly(self):
        """Tests that messages exceeding BLE MTU are fragmented with >HBB header and correctly reassembled."""
        large_dict = {
            "dcp": "1.0",
            "type": "request",
            "id": 42,
            "command": "get_hardware_map",
            "padding": "x" * 1200,
        }
        data = json.dumps(large_dict).encode("utf-8")
        max_chunk = 128
        chunks = fragment_message(data, max_payload_size=max_chunk)

        self.assertGreater(len(chunks), 1)

        buffer = {}
        reassembled = None
        for chunk in chunks:
            complete, payload = reassemble_chunk(buffer, chunk)
            if complete:
                reassembled = payload

        self.assertIsNotNone(reassembled)
        self.assertEqual(data, reassembled)
        parsed = json.loads(reassembled.decode("utf-8"))
        self.assertEqual(parsed["id"], 42)

    def test_qr_generation(self):
        """Tests QR pairing generation, payload structure, and image creation."""
        result = self.pairing_manager.generate_qr_pairing(
            client_name="Test Mobile",
            pairing_code="123456",
            ttl_seconds=60,
            save_path="test_qr.png",
            display_terminal=False,
        )

        self.assertTrue(result["approved"])
        self.assertEqual(result["pairing_code"], "123456")
        self.assertTrue(os.path.exists("test_qr.png"))

        payload = result["payload"]
        self.assertEqual(payload["protocol"], "DCP")
        self.assertEqual(payload["version"], "1.0")
        self.assertEqual(payload["pairing"], "qr")
        self.assertEqual(payload["pairing_code"], "123456")
        self.assertEqual(payload["ble_service_uuid"], BLE_SERVICE_UUID)

    async def test_qr_pairing_flow_via_dcp(self):
        """Tests that a client presenting a valid QR pairing code over DCP is approved and given a secret."""
        # 1. Server generates QR code
        qr_info = self.pairing_manager.generate_qr_pairing(
            client_name="Damjan's Phone",
            pairing_code="839214",
            display_terminal=False,
        )
        pairing_code = qr_info["pairing_code"]

        # 2. Client connects and sends request_pairing command with method=qr
        session = self.session_manager.create_session(lambda m: None)
        pair_req = {
            "dcp": "1.0",
            "type": "request",
            "id": 1,
            "command": "request_pairing",
            "arguments": {
                "client_id": "phone-damjan",
                "client_name": "Damjan's Phone",
                "method": "qr",
                "pairing_code": pairing_code,
            },
        }

        resp = await self.dcp_handler.handle(session, pair_req)
        self.assertTrue(resp["success"])
        self.assertTrue(resp["data"]["approved"])
        secret = resp["data"]["secret"]
        self.assertIsNotNone(secret)

        # 3. Client authenticates using the received secret
        auth_req_1 = {
            "dcp": "1.0",
            "type": "request",
            "id": 2,
            "command": "authenticate",
            "arguments": {"client_id": "phone-damjan"},
        }
        resp1 = await self.dcp_handler.handle(session, auth_req_1)
        self.assertTrue(resp1["success"])
        nonce = resp1["data"]["nonce"]

        # Client computes HMAC-SHA256(secret, nonce)
        signature = hmac.new(bytes.fromhex(secret), nonce.encode(), hashlib.sha256).hexdigest()

        auth_req_2 = {
            "dcp": "1.0",
            "type": "request",
            "id": 3,
            "command": "authenticate",
            "arguments": {"client_id": "phone-damjan", "response": signature},
        }
        resp2 = await self.dcp_handler.handle(session, auth_req_2)
        self.assertTrue(resp2["success"])
        self.assertTrue(resp2["data"]["authenticated"])
        self.assertTrue(session.authenticated)

    async def test_bluetooth_pairing_interactive(self):
        """Tests pairing over Bluetooth method with auto-approved confirm function."""
        # Set auto-approve confirmation
        self.pairing_manager._confirm_fn = lambda req: asyncio.sleep(0.01, result=True)

        session = self.session_manager.create_session(lambda m: None)
        pair_req = {
            "dcp": "1.0",
            "type": "request",
            "id": 10,
            "command": "request_pairing",
            "arguments": {
                "client_id": "ble-client-01",
                "client_name": "Bluetooth Remote",
                "method": "bluetooth",
            },
        }

        resp = await self.dcp_handler.handle(session, pair_req)
        self.assertTrue(resp["success"])
        self.assertTrue(resp["data"]["approved"])
        self.assertTrue(self.trust_store.is_trusted("ble-client-01"))

    async def test_negotiate_and_ping(self):
        """Tests that negotiate and ping commands are supported and respond properly."""
        session = self.session_manager.create_session(lambda m: None)

        neg_req = {
            "dcp": "1.0",
            "type": "request",
            "id": 1,
            "command": "negotiate",
            "arguments": {"selected_version": "1.0"},
        }
        resp = await self.dcp_handler.handle(session, neg_req)
        self.assertTrue(resp["success"])
        self.assertEqual(resp["data"]["negotiated_version"], "1.0")
        self.assertIn("1.0", resp["data"]["supported_versions"])

        ping_req = {
            "dcp": "1.0",
            "type": "request",
            "id": 2,
            "command": "ping",
            "arguments": {},
        }
        resp_ping = await self.dcp_handler.handle(session, ping_req)
        self.assertTrue(resp_ping["success"])
        self.assertTrue(resp_ping["data"]["pong"])
        self.assertIn("timestamp", resp_ping["data"])

    async def test_qr_pairing_idempotency(self):
        """Tests that repeated pairing attempts (e.g. rapid camera scanner frames) succeed."""
        qr_info = self.pairing_manager.generate_qr_pairing(
            client_name="Test Phone",
            pairing_code="777888",
            display_terminal=False,
        )
        session = self.session_manager.create_session(lambda m: None)
        pair_req = {
            "dcp": "1.0",
            "type": "request",
            "id": 1,
            "command": "request_pairing",
            "arguments": {
                "client_id": "test-phone-repeat",
                "client_name": "Test Phone",
                "method": "qr",
                "pairing_code": "777888",
            },
        }

        # First scan
        resp1 = await self.dcp_handler.handle(session, pair_req)
        self.assertTrue(resp1["success"])
        self.assertTrue(resp1["data"]["approved"])
        secret1 = resp1["data"]["secret"]

        # Second scan frame (within grace period / already trusted client)
        pair_req["id"] = 2
        resp2 = await self.dcp_handler.handle(session, pair_req)
        self.assertTrue(resp2["success"])
        self.assertTrue(resp2["data"]["approved"])
        self.assertEqual(resp2["data"]["secret"], secret1)

    async def test_session_role_granted(self):
        """Tests that request_control and release_control include session_role_granted."""
        session = self.session_manager.create_session(lambda m: None)

        req_ctrl = {
            "dcp": "1.0",
            "type": "request",
            "id": 1,
            "command": "request_control",
            "arguments": {},
        }
        resp = await self.dcp_handler.handle(session, req_ctrl)
        self.assertTrue(resp["success"])
        self.assertTrue(resp["data"]["granted"])
        self.assertEqual(resp["data"]["session_role_granted"], "control")

        rel_ctrl = {
            "dcp": "1.0",
            "type": "request",
            "id": 2,
            "command": "release_control",
            "arguments": {},
        }
        resp_rel = await self.dcp_handler.handle(session, rel_ctrl)
        self.assertTrue(resp_rel["success"])
        self.assertTrue(resp_rel["data"]["released"])
        self.assertEqual(resp_rel["data"]["session_role_granted"], "read_only")

    async def test_new_tools_and_shortcuts(self):
        """Tests emergency_stop, set_gait, set_pose, camera tools, get_battery, set_config."""
        session = self.session_manager.create_session(lambda m: None)
        session.authenticated = True
        session.permission = "CONTROL"
        self.session_manager.request_control(session)

        # 1. emergency_stop
        resp = await self.dcp_handler.handle(session, {
            "dcp": "1.0", "type": "request", "id": 10,
            "command": "emergency_stop", "arguments": {},
        })
        self.assertTrue(resp["success"])
        self.assertTrue(resp["data"]["result"]["stopped"])

        # 2. set_gait
        resp = await self.dcp_handler.handle(session, {
            "dcp": "1.0", "type": "request", "id": 11,
            "command": "execute_tool", "arguments": {"tool": "set_gait", "parameters": {"mode": "trot"}},
        })
        self.assertTrue(resp["success"])
        self.assertEqual(resp["data"]["result"]["gait"], "trot")

        # 3. set_pose
        resp = await self.dcp_handler.handle(session, {
            "dcp": "1.0", "type": "request", "id": 12,
            "command": "execute_tool",
            "arguments": {"tool": "set_pose", "parameters": {"pitch": 10.5, "roll": -5.0, "height": 12.0}},
        })
        self.assertTrue(resp["success"])
        self.assertEqual(resp["data"]["result"]["orientation"]["pitch"], 10.5)

        # 4. camera tools
        resp = await self.dcp_handler.handle(session, {
            "dcp": "1.0", "type": "request", "id": 13,
            "command": "execute_tool", "arguments": {"tool": "start_camera", "parameters": {}},
        })
        self.assertTrue(resp["success"])
        self.assertTrue(resp["data"]["result"]["streaming"])

        resp = await self.dcp_handler.handle(session, {
            "dcp": "1.0", "type": "request", "id": 14,
            "command": "camera_snapshot", "arguments": {},
        })
        self.assertTrue(resp["success"])
        self.assertEqual(resp["data"]["result"]["format"], "jpeg")

        resp = await self.dcp_handler.handle(session, {
            "dcp": "1.0", "type": "request", "id": 15,
            "command": "execute_tool", "arguments": {"tool": "stop_camera", "parameters": {}},
        })
        self.assertTrue(resp["success"])
        self.assertFalse(resp["data"]["result"]["streaming"])

        # 5. get_battery
        resp = await self.dcp_handler.handle(session, {
            "dcp": "1.0", "type": "request", "id": 16,
            "command": "get_battery", "arguments": {},
        })
        self.assertTrue(resp["success"])
        self.assertEqual(resp["data"]["result"]["percentage"], 88)

        # 6. set_config
        resp = await self.dcp_handler.handle(session, {
            "dcp": "1.0", "type": "request", "id": 17,
            "command": "execute_tool",
            "arguments": {"tool": "set_config", "parameters": {"param": "speed_limit", "value": 1.5}},
        })
        self.assertTrue(resp["success"])
        self.assertTrue(resp["data"]["result"]["updated"])

    async def test_task_update_events(self):
        """Tests that long-running tasks emit task_update events with full status."""
        received_events = []

        async def capture_event(msg):
            received_events.append(msg)

        session = self.session_manager.create_session(capture_event)
        session.authenticated = True
        session.permission = "CONTROL"
        self.session_manager.request_control(session)
        session.subscribed_events.add("task_update")

        walk_req = {
            "dcp": "1.0",
            "type": "request",
            "id": 20,
            "command": "execute_tool",
            "arguments": {
                "tool": "walk",
                "parameters": {"direction": "forward", "distance": 1.0},
            },
        }

        resp = await self.dcp_handler.handle(session, walk_req)
        self.assertTrue(resp["success"])
        task_id = resp["task_id"]
        self.assertIsNotNone(task_id)

        # Wait for task to finish execution
        for _ in range(30):
            if any(e.get("data", {}).get("state") == "completed" for e in received_events):
                break
            await asyncio.sleep(0.1)

        # Check that task_update events were emitted
        task_updates = [e for e in received_events if e.get("event") == "task_update"]
        self.assertGreater(len(task_updates), 0)
        # Check event envelope structure
        last_update = [e for e in task_updates if e.get("data", {}).get("state") == "completed"][-1]
        self.assertEqual(last_update.get("event_type"), "task_update")
        self.assertEqual(last_update.get("data", {}).get("state"), "completed")
        self.assertEqual(last_update.get("data", {}).get("progress"), 1.0)
        self.assertEqual(last_update.get("data", {}).get("task_id"), task_id)

    def test_aligned_qr_payload_format(self):
        """Tests that the QR payload contains all keys expected by Flutter app."""
        result = self.pairing_manager.generate_qr_pairing(
            client_name="Flutter App",
            pairing_code="654321",
            display_terminal=False,
        )
        payload = result["payload"]
        self.assertEqual(payload["protocol"], "DCP")
        self.assertEqual(payload["version"], "1.0")
        self.assertEqual(payload["pairing"], "qr")
        self.assertEqual(payload["action"], "pair")
        self.assertEqual(payload["pairing_code"], "654321")
        self.assertIn("device_id", payload)
        self.assertIn("name", payload)
        self.assertIn("device_name", payload)
        self.assertIn("host", payload)
        self.assertIn("address", payload)
        self.assertIn("port", payload)
        self.assertIn("transport", payload)

    async def test_telemetry_loop(self):
        """Tests that telemetry events are emitted with cpu, ram, gpu, temp, battery."""
        from main import telemetry_loop

        received = []

        async def capture_event(msg):
            received.append(msg)

        session = self.session_manager.create_session(capture_event)
        session.subscribed_events.add("telemetry")

        stop_event = asyncio.Event()
        task = asyncio.create_task(
            telemetry_loop(self.event_manager, self.session_manager, stop_event, interval_s=0.05)
        )

        await asyncio.sleep(0.15)
        stop_event.set()
        await task

        telemetry_events = [e for e in received if e.get("event") == "telemetry"]
        self.assertGreater(len(telemetry_events), 0)
        data = telemetry_events[0].get("data", {})
        self.assertIn("cpu", data)
        self.assertIn("ram", data)
        self.assertIn("temp", data)
        self.assertIn("battery", data)


if __name__ == "__main__":
    unittest.main()
