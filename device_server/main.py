"""
main.py

Device Server entry point. Implements the startup sequence exactly as
listed in device_server_hardware_mapper.txt section 4:

    1. Start Device Server.
    2. Load device identity.
    3. Initialize security keys.
    4. Run Hardware Mapper.
    5. Scan available hardware.
    6. Load physical configuration.
    7. Build Capability Map.
    8. Build Tool Registry.
    9. Start Wi-Fi DCP server.
    10. Start Bluetooth DCP server.        (not implemented -- see README.md)
    11. Start discovery/advertising.
    12. Start optional Needle.             (out of scope for this file)
    13. Wait for clients.

Run with:  python main.py
Stop with: Ctrl+C
"""

import asyncio
import logging
import signal

from auth import Authenticator, TrustStore
from capability_mapper import CapabilityMapper
from config import ConfigurationManager
from device_agent import DeviceAgent
from discovery import DiscoveryAdvertiser
from dcp_handler import DCPHandler
from event_manager import EventManager
from hardware_mapper.mapper import HardwareMapper
from identity import DeviceIdentity
from pairing import PairingManager
from robot_controller import RobotController
from safety_manager import SafetyManager
from session import SessionManager
from task_manager import TaskManager
from tool_registry import ToolRegistry
from transports.websocket_transport import WiFiDCPServer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("main")

WIFI_PORT = 8765


async def run() -> None:
    # 2-3. Device identity + security keys
    config = ConfigurationManager()
    identity_cfg = config.identity
    identity = DeviceIdentity(
        name=identity_cfg.get("name", "Unnamed Device"),
        profile=identity_cfg.get("profile", "generic"),
        device_type=identity_cfg.get("type", "device"),
    )
    logger.info("device identity: %s (%s)", identity.device_id, identity.profile)

    # 4-5. Hardware Mapper: scan available hardware
    hardware_mapper = HardwareMapper()
    hardware_map = hardware_mapper.scan()
    logger.info("hardware scan complete; interfaces=%s", hardware_map["interfaces"])
    if hardware_map["detector_errors"]:
        logger.warning("some detectors were unavailable: %s", hardware_map["detector_errors"])

    # 6. Physical configuration already loaded via ConfigurationManager above.
    physical_config = config.physical_configuration

    # Robot Controller: the "software module" the Capability Mapper needs
    # to unlock movement capabilities (device_server_hardware_mapper.txt
    # section 23). Swap or omit this for non-robot devices.
    robot_controller = RobotController(physical_config)

    # 7. Capability Map
    capability_mapper = CapabilityMapper(robot_controller=robot_controller)
    capabilities = capability_mapper.build(hardware_map, physical_config)
    logger.info("capabilities: %s", capabilities)

    # 8. Tool Registry
    tool_registry = ToolRegistry()
    tool_registry.build_default_tools(capabilities, robot_controller, config.safety_limits)
    logger.info("registered tools: %s", list(tool_registry.all_tools().keys()))

    # Supporting managers used by the DCP Handler / Device Agent pipeline.
    trust_store = TrustStore()
    authenticator = Authenticator(trust_store)
    session_manager = SessionManager()
    safety_manager = SafetyManager()
    event_manager = EventManager(session_manager)
    task_manager = TaskManager(event_manager, safety_manager)
    device_agent = DeviceAgent(tool_registry, safety_manager, task_manager)
    pairing_manager = PairingManager(trust_store)

    dcp_handler = DCPHandler(
        identity=identity,
        hardware_mapper=hardware_mapper,
        config=config,
        tool_registry=tool_registry,
        device_agent=device_agent,
        task_manager=task_manager,
        session_manager=session_manager,
        trust_store=trust_store,
        authenticator=authenticator,
        capabilities=capabilities,
    )

    # 9. Start Wi-Fi DCP server (WebSocket over TCP)
    wifi_server = WiFiDCPServer(dcp_handler, session_manager, port=WIFI_PORT)
    await wifi_server.start()

    # 10. Bluetooth DCP server -- not implemented in this reference
    # implementation. A real deployment would add a BLE GATT peripheral
    # here (e.g. via `bleak`/`bluezero` on Linux) that decodes/reassembles
    # fragmented DCP JSON frames and hands the resulting dict to the same
    # dcp_handler.handle(session, message) used above -- transport
    # independence (protocol spec section 33) means no other code changes.
    logger.info("Bluetooth DCP server not implemented in this reference build (see README.md)")

    # 11. Discovery / advertising
    advertiser = DiscoveryAdvertiser(
        device_id=identity.device_id,
        profile=identity.profile,
        port=WIFI_PORT,
    )
    advertiser.start()

    # 12. Optional Needle -- out of scope here. Needle would connect to
    # this same server as just another authenticated DCP client (either
    # embedded in-process on a capable device, per
    # device_server_hardware_mapper.txt section 31, or running on the
    # phone per section 32), calling execute_tool the same way any other
    # client does.

    # Expose a manual pairing entry point for operators/testers: run
    #   python -c "import asyncio, main; asyncio.run(main.pair_client('phone-1', 'My Phone', 'wifi'))"
    # or wire PairingManager.request_pairing into whatever out-of-band
    # channel (QR, terminal, admin API) your deployment uses to *initiate*
    # a pairing request.

    logger.info("Device Server ready. Waiting for clients on port %s.", WIFI_PORT)

    stop_event = asyncio.Event()

    def _handle_signal():
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except NotImplementedError:
            pass  # e.g. Windows

    await stop_event.wait()

    logger.info("shutting down")
    advertiser.stop()
    await wifi_server.stop()


if __name__ == "__main__":
    asyncio.run(run())
