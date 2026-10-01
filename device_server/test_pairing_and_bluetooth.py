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
from discovery import DiscoveryAdvertiser
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
        self.assertIn(data.get("power_source"), ("dc_in", "dc_external"))
        self.assertIn("imu", data)

    def test_gpio_and_pwm_capabilities_in_manifest(self):
        """Tests that CapabilityMapper provides rich GPIO and PWM descriptors without battery."""
        from capability_mapper import CapabilityMapper
        mapper = CapabilityMapper(self.robot_controller)
        caps = mapper.build({"interfaces": {"gpio": True}}, {})
        gpio_cap = next((c for c in caps if isinstance(c, dict) and c.get("id") == "gpio"), None)
        self.assertIsNotNone(gpio_cap)
        self.assertIn("pins", gpio_cap["params"])
        self.assertIn(17, gpio_cap["params"]["pins"])

        pwm_cap = next((c for c in caps if isinstance(c, dict) and c.get("id") == "pwm"), None)
        self.assertIsNotNone(pwm_cap)
        self.assertEqual(pwm_cap["params"]["channels"], 16)

        # Battery must NOT be present
        battery_cap = next((c for c in caps if (c == "battery" or (isinstance(c, dict) and c.get("id") == "battery"))), None)
        self.assertIsNone(battery_cap)

    async def test_ai_needle_metadata_in_device_info(self):
        """Tests that get_device_info advertises firmware_version and ai.needle metadata."""
        session = self.session_manager.create_session(lambda m: None)
        resp = await self.dcp_handler.handle(session, {
            "dcp": "1.0", "type": "request", "id": 50,
            "command": "get_device_info", "arguments": {},
        })
        self.assertTrue(resp["success"])
        info = resp["data"]
        self.assertEqual(info.get("firmware_version"), "2.1.0")
        self.assertIn("1.0", info.get("supported_versions", []))
        self.assertIn("ai", info)
        needle = info["ai"].get("needle", {})
        self.assertTrue(needle.get("supported"))
        self.assertIn("device", needle.get("execution", []))
        self.assertEqual(needle.get("preferred"), "device")

    async def test_needle_prompt_execution(self):
        """Tests that needle_prompt interprets natural language instructions into actions."""
        session = self.session_manager.create_session(lambda m: None)
        session.authenticated = True
        session.permission = "CONTROL"
        self.session_manager.request_control(session)

        # Stand command
        resp = await self.dcp_handler.handle(session, {
            "dcp": "1.0", "type": "request", "id": 51,
            "command": "execute_tool",
            "arguments": {"tool": "needle_prompt", "parameters": {"prompt": "robot please stand up"}},
        })
        self.assertTrue(resp["success"])
        self.assertEqual(resp["data"]["result"]["action"], "stand")
        self.assertTrue(self.robot_controller.standing)

        # Sit command
        resp = await self.dcp_handler.handle(session, {
            "dcp": "1.0", "type": "request", "id": 52,
            "command": "execute_tool",
            "arguments": {"tool": "needle_prompt", "parameters": {"prompt": "sit down now"}},
        })
        self.assertTrue(resp["success"])
        self.assertEqual(resp["data"]["result"]["action"], "sit")
        self.assertFalse(self.robot_controller.standing)

        # Light command
        resp = await self.dcp_handler.handle(session, {
            "dcp": "1.0", "type": "request", "id": 53,
            "command": "execute_tool",
            "arguments": {"tool": "needle_prompt", "parameters": {"prompt": "turn on flashlight"}},
        })
        self.assertTrue(resp["success"])
        self.assertEqual(resp["data"]["result"]["action"], "set_led")
        self.assertTrue(self.robot_controller.led_state)

        # Stop command
        resp = await self.dcp_handler.handle(session, {
            "dcp": "1.0", "type": "request", "id": 54,
            "command": "execute_tool",
            "arguments": {"tool": "needle_prompt", "parameters": {"prompt": "emergency stop halt"}},
        })
        self.assertTrue(resp["success"])
        self.assertTrue(resp["data"]["result"]["stopped"])

        # Hardware inquiry command
        self.robot_controller.set_hardware_map({
            "device": {"platform": "raspberry_pi", "os": "linux", "architecture": "aarch64"},
            "system": {"cpu": {"cores": 4}},
            "interfaces": {"gpio": True, "i2c": True, "wifi": True},
        })
        resp = await self.dcp_handler.handle(session, {
            "dcp": "1.0", "type": "request", "id": 55,
            "command": "execute_tool",
            "arguments": {"tool": "needle_prompt", "parameters": {"prompt": "what hardware do you have?"}},
        })
        self.assertTrue(resp["success"])
        self.assertEqual(resp["data"]["result"]["action"], "hardware_info")
        self.assertIn("raspberry_pi", resp["data"]["result"]["message"])

    async def test_generic_hardware_gpio_and_pwm(self):
        """Tests gpio_write, gpio_read, and pwm_set tools."""
        session = self.session_manager.create_session(lambda m: None)
        session.authenticated = True
        session.permission = "CONTROL"
        self.session_manager.request_control(session)

        # Write GPIO 17 HIGH
        resp = await self.dcp_handler.handle(session, {
            "dcp": "1.0", "type": "request", "id": 60,
            "command": "execute_tool",
            "arguments": {"tool": "gpio_write", "parameters": {"pin": 17, "state": True}},
        })
        self.assertTrue(resp["success"])
        self.assertEqual(resp["data"]["result"]["pin"], 17)
        self.assertTrue(resp["data"]["result"]["state"])

        # Read GPIO 17
        resp = await self.dcp_handler.handle(session, {
            "dcp": "1.0", "type": "request", "id": 61,
            "command": "execute_tool",
            "arguments": {"tool": "gpio_read", "parameters": {"pin": 17}},
        })
        self.assertTrue(resp["success"])
        self.assertTrue(resp["data"]["result"]["state"])

        # Write GPIO 17 LOW
        resp = await self.dcp_handler.handle(session, {
            "dcp": "1.0", "type": "request", "id": 62,
            "command": "execute_tool",
            "arguments": {"tool": "gpio_write", "parameters": {"pin": 17, "state": False}},
        })
        self.assertTrue(resp["success"])
        self.assertFalse(resp["data"]["result"]["state"])

        # Set PWM channel 2
        resp = await self.dcp_handler.handle(session, {
            "dcp": "1.0", "type": "request", "id": 63,
            "command": "execute_tool",
            "arguments": {"tool": "pwm_set", "parameters": {"channel": 2, "value": 0.65}},
        })
        self.assertTrue(resp["success"])
        self.assertEqual(resp["data"]["result"]["channel"], 2)
        self.assertAlmostEqual(resp["data"]["result"]["value"], 0.65)

    def test_mdns_discovery_byte_properties(self):
        """Tests that DiscoveryAdvertiser encodes properties as bytes for zeroconf compatibility."""
        advertiser = DiscoveryAdvertiser(
            device_id="quadruped-test",
            profile="quadruped",
            port=8765,
            extra_info={"name": "TestBot", "version": "1.0"},
        )
        # Check that start() doesn't throw and properties encoding handles bytes cleanly
        advertiser.start()
        if advertiser._service_info is not None:
            props = advertiser._service_info.properties
            for k, v in props.items():
                self.assertIsInstance(k, bytes)
                self.assertIsInstance(v, bytes)
        advertiser.stop()

    def test_section2_capabilities_registration(self):
        """Tests that CapabilityMapper advertises the 5 core capabilities from Section 2."""
        from capability_mapper import CapabilityMapper
        mapper = CapabilityMapper(self.robot_controller)
        hw_map = {"interfaces": {"gpio": True, "i2c": True}, "devices": {}, "system": {}}
        caps = mapper.build(hw_map, {})
        cap_ids = {c["id"] if isinstance(c, dict) else c for c in caps}

        self.assertIn("robotics", cap_ids)
        self.assertIn("imu", cap_ids)
        self.assertIn("lighting", cap_ids)
        self.assertIn("buzzer", cap_ids)
        self.assertIn("camera", cap_ids)

    async def test_pca9685_servo_tools(self):
        """Tests Section 3.2 direct PCA9685 servo control tools."""
        session = self.session_manager.create_session(lambda msg: None)
        session.authenticated = True
        session.permission = "CONTROL"

        # 1. driver_set_servo_angle_with_index
        resp = await self.dcp_handler.handle(session, {
            "dcp": "1.0", "type": "request", "id": 101,
            "command": "driver_set_servo_angle_with_index",
            "arguments": {"index": 3, "angle": 90},
        })
        self.assertTrue(resp["success"])
        self.assertEqual(resp["data"]["result"]["index"], 3)
        self.assertEqual(resp["data"]["result"]["angle"], 90)

        # 2. snap_servo_left
        resp = await self.dcp_handler.handle(session, {
            "dcp": "1.0", "type": "request", "id": 102,
            "command": "snap_servo_left",
            "arguments": {"index": 4},
        })
        self.assertTrue(resp["success"])
        self.assertEqual(resp["data"]["result"]["angle"], 180.0)

        # 3. snap_servo_right
        resp = await self.dcp_handler.handle(session, {
            "dcp": "1.0", "type": "request", "id": 103,
            "command": "snap_servo_right",
            "arguments": {"index": 4},
        })
        self.assertTrue(resp["success"])
        self.assertEqual(resp["data"]["result"]["angle"], 0.0)

        # 4. pan_to_left
        resp = await self.dcp_handler.handle(session, {
            "dcp": "1.0", "type": "request", "id": 104,
            "command": "pan_to_left",
            "arguments": {"index": 6},
        })
        self.assertTrue(resp["success"])
        self.assertEqual(resp["data"]["result"]["angle"], 180.0)

        # 5. pan_to_right
        resp = await self.dcp_handler.handle(session, {
            "dcp": "1.0", "type": "request", "id": 105,
            "command": "pan_to_right",
            "arguments": {"index": 6},
        })
        self.assertTrue(resp["success"])
        self.assertEqual(resp["data"]["result"]["angle"], 0.0)

        # 6. cleanup_servos
        resp = await self.dcp_handler.handle(session, {
            "dcp": "1.0", "type": "request", "id": 106,
            "command": "cleanup_servos",
            "arguments": {},
        })
        self.assertTrue(resp["success"])
        self.assertEqual(resp["data"]["result"]["status"], "servos_released")

    async def test_ws281x_led_strip_tools(self):
        """Tests Section 3.3 WS281x 8-LED strip tools."""
        session = self.session_manager.create_session(lambda msg: None)
        session.authenticated = True
        session.permission = "CONTROL"

        # 1. turn_on_strip_with_color
        resp = await self.dcp_handler.handle(session, {
            "dcp": "1.0", "type": "request", "id": 201,
            "command": "turn_on_strip_with_color",
            "arguments": {"r": 0, "g": 255, "b": 128},
        })
        self.assertTrue(resp["success"])
        self.assertEqual(resp["data"]["result"]["color"], [0, 255, 128])

        # 2. turn_off_strip
        resp = await self.dcp_handler.handle(session, {
            "dcp": "1.0", "type": "request", "id": 202,
            "command": "turn_off_strip",
            "arguments": {},
        })
        self.assertTrue(resp["success"])
        self.assertEqual(resp["data"]["result"]["status"], "off")

        # 3. turn_on_led_at_index
        resp = await self.dcp_handler.handle(session, {
            "dcp": "1.0", "type": "request", "id": 203,
            "command": "turn_on_led_at_index",
            "arguments": {"index": 2, "r": 255, "g": 0, "b": 0},
        })
        self.assertTrue(resp["success"])
        self.assertEqual(resp["data"]["result"]["index"], 2)
        self.assertEqual(resp["data"]["result"]["color"], [255, 0, 0])

        # 4. turn_off_led_at_index
        resp = await self.dcp_handler.handle(session, {
            "dcp": "1.0", "type": "request", "id": 204,
            "command": "turn_off_led_at_index",
            "arguments": {"index": 2},
        })
        self.assertTrue(resp["success"])
        self.assertEqual(resp["data"]["result"]["status"], "off")

        # 5. animations and alerts
        for anim in ("animate_running_process", "flash_alert", "blink_oke", "blink_warning"):
            resp = await self.dcp_handler.handle(session, {
                "dcp": "1.0", "type": "request", "id": 205,
                "command": anim,
                "arguments": {},
            })
            self.assertTrue(resp["success"], f"{anim} failed")

    async def test_buzzer_and_imu_and_power_tools(self):
        """Tests Section 3.4 buzzer, 3.5 IMU, and 3.6 power tools."""
        session = self.session_manager.create_session(lambda msg: None)
        session.authenticated = True
        session.permission = "CONTROL"

        # Buzzer: play_tone
        resp = await self.dcp_handler.handle(session, {
            "dcp": "1.0", "type": "request", "id": 301,
            "command": "play_tone",
            "arguments": {"tone": "A4"},
        })
        self.assertTrue(resp["success"])
        self.assertEqual(resp["data"]["result"]["tone"], "A4")

        # Buzzer: play_list_of_notes
        resp = await self.dcp_handler.handle(session, {
            "dcp": "1.0", "type": "request", "id": 302,
            "command": "play_list_of_notes",
            "arguments": {"notes": ["C4", "E4", "G4"]},
        })
        self.assertTrue(resp["success"])
        self.assertEqual(resp["data"]["result"]["notes"], ["C4", "E4", "G4"])

        # IMU: get_sensor_accel
        resp = await self.dcp_handler.handle(session, {
            "dcp": "1.0", "type": "request", "id": 303,
            "command": "get_sensor_accel",
            "arguments": {},
        })
        self.assertTrue(resp["success"])
        self.assertIn("z", resp["data"]["result"])

        # IMU: get_sensor_gyro
        resp = await self.dcp_handler.handle(session, {
            "dcp": "1.0", "type": "request", "id": 304,
            "command": "get_sensor_gyro",
            "arguments": {},
        })
        self.assertTrue(resp["success"])
        self.assertIn("x", resp["data"]["result"])

        # IMU: get_pitch_roll
        resp = await self.dcp_handler.handle(session, {
            "dcp": "1.0", "type": "request", "id": 305,
            "command": "get_pitch_roll",
            "arguments": {},
        })
        self.assertTrue(resp["success"])
        self.assertIn("pitch", resp["data"]["result"])
        self.assertIn("roll", resp["data"]["result"])

        # Power: shutdown & reboot
        resp = await self.dcp_handler.handle(session, {
            "dcp": "1.0", "type": "request", "id": 306,
            "command": "shutdown",
            "arguments": {},
        })
        self.assertTrue(resp["success"])
        self.assertEqual(resp["data"]["result"]["status"], "shutting_down")

        resp = await self.dcp_handler.handle(session, {
            "dcp": "1.0", "type": "request", "id": 307,
            "command": "reboot",
            "arguments": {},
        })
        self.assertTrue(resp["success"])
        self.assertEqual(resp["data"]["result"]["status"], "rebooting")

    async def test_wire_format_b_handshake_and_execution(self):
        """Tests Section 1.1 & Section 5 mobile app handshake and execute messages."""
        session = self.session_manager.create_session(lambda msg: None)

        # 1. hello -> hello_ack
        resp = await self.dcp_handler.handle(session, {
            "msgId": "req-1",
            "type": "hello",
            "payload": {
                "clientId": "machmake-app-uuid",
                "clientVersion": "2.0.0",
                "protocolVersion": "1.0.0",
            },
            "timestampMs": 1727570000000,
        })
        self.assertEqual(resp["type"], "hello_ack")
        self.assertEqual(resp["msgId"], "req-1")
        self.assertIn("deviceId", resp["payload"])

        # 2. negotiate -> negotiate_ack
        resp = await self.dcp_handler.handle(session, {
            "msgId": "req-2",
            "type": "negotiate",
            "payload": {"selectedVersion": "1.0.0"},
            "timestampMs": 1727570000050,
        })
        self.assertEqual(resp["type"], "negotiate_ack")
        self.assertEqual(resp["payload"]["selectedVersion"], "1.0.0")

        # 3. auth -> auth_ack
        resp = await self.dcp_handler.handle(session, {
            "msgId": "req-3",
            "type": "auth",
            "payload": {"token": "sample-tok"},
            "timestampMs": 1727570000100,
        })
        self.assertEqual(resp["type"], "auth_ack")
        self.assertTrue(resp["payload"]["success"])
        self.assertTrue(session.authenticated)

        # 4. capabilities -> capabilities_response
        resp = await self.dcp_handler.handle(session, {
            "msgId": "req-4",
            "type": "capabilities",
            "payload": {},
        })
        self.assertEqual(resp["type"], "capabilities_response")
        self.assertIn("capabilities", resp["payload"])

        # 5. tools -> tools_response
        resp = await self.dcp_handler.handle(session, {
            "msgId": "req-5",
            "type": "tools",
            "payload": {},
        })
        self.assertEqual(resp["type"], "tools_response")
        self.assertIn("tools", resp["payload"])

        # 6. subscribe_events -> subscribe_ack
        resp = await self.dcp_handler.handle(session, {
            "msgId": "req-6",
            "type": "subscribe_events",
            "payload": {"events": ["telemetry"]},
        })
        self.assertEqual(resp["type"], "subscribe_ack")
        self.assertIn("telemetry", resp["payload"]["subscribed"])

        # 7. execute -> execute_response
        resp = await self.dcp_handler.handle(session, {
            "msgId": "req-7",
            "type": "execute",
            "payload": {
                "toolName": "driver_set_servo_angle_with_index",
                "params": {"index": 7, "angle": 45},
            },
            "timestampMs": 1727570000200,
        })
        self.assertEqual(resp["type"], "execute_response")
        self.assertTrue(resp["payload"]["success"])
        self.assertEqual(resp["payload"]["result"]["index"], 7)
        self.assertEqual(resp["payload"]["result"]["angle"], 45)

        # 8. ping -> pong
        resp = await self.dcp_handler.handle(session, {
            "msgId": "req-8",
            "type": "ping",
            "payload": {},
        })
        self.assertEqual(resp["type"], "pong")

    def test_auto_config_heuristics(self):
        """Tests auto-configuration inference from different hardware map profiles."""
        import auto_config

        # 1. Pi with PCA9685 and MPU6050 -> quadruped
        map_quadruped = {
            "device": {"platform": "raspberry_pi", "os": "linux"},
            "interfaces": {"gpio": True, "i2c": True},
            "devices": {"i2c": [{"bus": 1, "devices": [
                {"address": "0x40", "candidates": ["PCA9685"]},
                {"address": "0x68", "candidates": ["MPU6050"]},
            ]}]},
        }
        self.assertEqual(auto_config.infer_profile_from_hardware(map_quadruped), "quadruped")

        # 2. Pi with PCA9685 only -> robotic_arm
        map_arm = {
            "device": {"platform": "raspberry_pi", "os": "linux"},
            "interfaces": {"gpio": True, "i2c": True},
            "devices": {"i2c": [{"bus": 1, "devices": [
                {"address": "0x40", "candidates": ["PCA9685"]},
            ]}]},
        }
        self.assertEqual(auto_config.infer_profile_from_hardware(map_arm), "robotic_arm")

        # 3. Pi with camera and no servos -> camera_node
        map_cam = {
            "device": {"platform": "raspberry_pi", "os": "linux"},
            "interfaces": {"gpio": True, "i2c": False},
            "devices": {"cameras": [{"device": "/dev/video0"}], "i2c": []},
        }
        self.assertEqual(auto_config.infer_profile_from_hardware(map_cam), "camera_node")

        # 4. Config building
        cfg = auto_config.build_config_for_profile("quadruped", custom_name="Custom Dog")
        self.assertEqual(cfg["identity"]["profile"], "quadruped")
        self.assertEqual(cfg["identity"]["name"], "Custom Dog")
        self.assertIn("pca9685", cfg["configuration"])

    def test_main_configuration_flags(self):
        """Tests that automatic_detection and is_robot_project variables are present in main.py."""
        import main
        self.assertIsInstance(main.automatic_detection, bool)
        self.assertIsInstance(main.is_robot_project, bool)
        self.assertTrue(main.is_robot_project)
        self.assertFalse(main.automatic_detection)

    def test_robot_project_configures_device_config(self):
        """Tests that when is_robot_project is True, device_config.json is configured for the quadruped."""
        import main
        test_cfg_path = "config/test_quadruped_config.json"
        if os.path.exists(test_cfg_path):
            os.remove(test_cfg_path)

        mgr = main.configure_robot_project_config(config_file=test_cfg_path, custom_name="Test Quadruped")
        self.assertTrue(os.path.exists(test_cfg_path))
        self.assertEqual(mgr.identity["profile"], "quadruped")
        self.assertEqual(mgr.identity["type"], "robot")
        self.assertEqual(mgr.identity["name"], "Test Quadruped")

        # Check physical configuration
        cfg = mgr.physical_configuration
        self.assertIn("pca9685", cfg)
        self.assertIn("servos", cfg)
        self.assertEqual(cfg["servos"]["fl_hip"]["channel"], 2)
        self.assertEqual(cfg["servos"]["br_lower"]["channel"], 13)
        self.assertEqual(cfg["imu"]["address"], "0x68")
        self.assertEqual(cfg["lighting"]["gpio_pin"], 10)
        self.assertEqual(cfg["audio"]["buzzer_pin"], 23)

        if os.path.exists(test_cfg_path):
            os.remove(test_cfg_path)

    async def test_audio_subsystem_tools(self):
        """Tests Section 3.1 Audio Subsystem (Buzzer & Speaker) tool implementations."""
        session = self.session_manager.create_session(lambda msg: None)
        session.authenticated = True
        session.permission = "CONTROL"

        # 1. play_tone
        resp = await self.dcp_handler.handle(session, {
            "dcp": "1.0", "type": "request", "id": 301,
            "command": "play_tone", "arguments": {"tone": "A4"},
        })
        self.assertTrue(resp["success"])
        self.assertEqual(resp["data"]["result"]["tone"], "A4")

        # 2. stop_tone
        resp = await self.dcp_handler.handle(session, {
            "dcp": "1.0", "type": "request", "id": 302,
            "command": "stop_tone", "arguments": {},
        })
        self.assertTrue(resp["success"])
        self.assertEqual(resp["data"]["result"]["status"], "stopped")

        # 3. play_audio
        resp = await self.dcp_handler.handle(session, {
            "dcp": "1.0", "type": "request", "id": 303,
            "command": "play_audio", "arguments": {"file_path": "/home/pi/audio/bark.wav"},
        })
        self.assertTrue(resp["success"])
        self.assertEqual(resp["data"]["result"]["file"], "/home/pi/audio/bark.wav")

        # 4. stop_audio
        resp = await self.dcp_handler.handle(session, {
            "dcp": "1.0", "type": "request", "id": 304,
            "command": "stop_audio", "arguments": {},
        })
        self.assertTrue(resp["success"])
        self.assertEqual(resp["data"]["result"]["status"], "stopped")

        # 5. set_volume
        resp = await self.dcp_handler.handle(session, {
            "dcp": "1.0", "type": "request", "id": 305,
            "command": "set_volume", "arguments": {"volume": 0.75},
        })
        self.assertTrue(resp["success"])
        self.assertEqual(resp["data"]["result"]["volume"], 0.75)

    async def test_format_b_audio_and_resilience(self):
        """Tests Format B execute calls for audio tools and fallback resilience."""
        session = self.session_manager.create_session(lambda msg: None)

        # Format B execute play_audio
        resp = await self.dcp_handler.handle(session, {
            "msgId": "audio-exec-1",
            "type": "execute",
            "payload": {
                "toolName": "play_audio",
                "params": {"file_path": "/home/pi/audio/hello.wav"},
            },
        })
        self.assertEqual(resp["type"], "execute_response")
        self.assertTrue(resp["payload"]["success"])
        self.assertEqual(resp["payload"]["result"]["file"], "/home/pi/audio/hello.wav")

        # Format B execute stop_tone
        resp = await self.dcp_handler.handle(session, {
            "msgId": "audio-exec-2",
            "type": "execute",
            "payload": {
                "toolName": "stop_tone",
                "params": {},
            },
        })
        self.assertEqual(resp["type"], "execute_response")
        self.assertTrue(resp["payload"]["success"])
        self.assertEqual(resp["payload"]["result"]["status"], "stopped")

        # Format B execute fallback for unmapped tool
        resp = await self.dcp_handler.handle(session, {
            "msgId": "unknown-tool-1",
            "type": "execute",
            "payload": {
                "toolName": "custom_extension_tool",
                "params": {"foo": "bar"},
            },
        })
        self.assertEqual(resp["type"], "execute_response")
        self.assertTrue(resp["payload"]["success"])
        self.assertEqual(resp["payload"]["result"]["executed"], "custom_extension_tool")

    async def test_realtime_mirroring_get_servo_angles(self):
        """Tests Section 3.1 get_servo_angles endpoint for Realtime Mirroring."""
        session = self.session_manager.create_session(lambda msg: None)
        session.authenticated = True
        session.permission = "CONTROL"

        # 1. Format A request
        resp = await self.dcp_handler.handle(session, {
            "dcp": "1.0", "type": "request", "id": 401,
            "command": "get_servo_angles", "arguments": {},
        })
        self.assertTrue(resp["success"])
        servos = resp["data"]["result"]["servos"]
        self.assertEqual(servos["2"], 110)
        self.assertEqual(servos["3"], 70)
        self.assertEqual(servos["4"], 50)
        self.assertEqual(servos["13"], 160)

        # 2. Format B execute request
        resp_b = await self.dcp_handler.handle(session, {
            "msgId": "mirror-req-1",
            "type": "execute",
            "payload": {
                "toolName": "get_servo_angles",
                "params": {},
            },
        })
        self.assertEqual(resp_b["type"], "execute_response")
        self.assertTrue(resp_b["payload"]["success"])
        servos_b = resp_b["payload"]["result"]["servos"]
        self.assertEqual(servos_b["2"], 110)
        self.assertEqual(servos_b["7"], 150)


if __name__ == "__main__":
    unittest.main()
