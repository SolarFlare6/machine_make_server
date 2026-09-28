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

import argparse
import asyncio
import logging
import signal
import sys

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
from transports.bluetooth_transport import BluetoothDCPServer
from transports.websocket_transport import WiFiDCPServer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("main")

WIFI_PORT = 8765


async def console_loop(
    pairing_manager: PairingManager,
    stop_event: asyncio.Event,
    trust_store: TrustStore,
    session_manager: SessionManager,
    identity: DeviceIdentity,
) -> None:
    """Non-blocking interactive console reader so the user can input commands
    (like 'qr' to generate a pairing QR code) while the server is running."""
    loop = asyncio.get_running_loop()

    # If stdin is not an interactive terminal (e.g. background service), do not block
    if not sys.stdin.isatty():
        return

    banner = (
        "\n" + "=" * 68 + "\n"
        "  MACHINE MAKE DEVICE SERVER CONSOLE\n"
        "  Type 'qr' to generate a pairing QR code for the mobile app.\n"
        "  Commands:\n"
        "    qr [name]    - Generate & display QR pairing code (ASCII + PNG)\n"
        "    devices      - List paired devices and active sessions\n"
        "    status       - Show device status and server info\n"
        "    help         - Show available commands\n"
        "    exit / quit  - Stop the server\n"
        + "=" * 68 + "\n"
    )
    print(banner)

    while not stop_event.is_set():
        try:
            line = await loop.run_in_executor(None, lambda: input("[server]> "))
        except (EOFError, KeyboardInterrupt):
            stop_event.set()
            break
        except Exception:
            break

        if stop_event.is_set():
            break

        cmd_parts = line.strip().split(maxsplit=1)
        if not cmd_parts:
            continue
        cmd = cmd_parts[0].lower()
        arg = cmd_parts[1] if len(cmd_parts) > 1 else ""

        if cmd in ("exit", "quit", "q"):
            stop_event.set()
            break
        elif cmd == "qr":
            client_name = arg or "Mobile App"
            pairing_manager.generate_qr_pairing(client_name=client_name)
        elif cmd in ("devices", "paired"):
            print("\n--- Paired Devices (TrustStore) ---")
            trusted = trust_store._trusted
            if not trusted:
                print("  No devices paired yet. Run 'qr' to pair a mobile app.")
            for cid, data in trusted.items():
                print(f"  - {cid}: {data.get('name')} (permission: {data.get('permission')})")
            print(f"--- Active Sessions: {len(session_manager.all_sessions())} ---")
            for s in session_manager.all_sessions():
                print(f"  - Session {s.session_id}: client={s.client_id} auth={s.authenticated} control={s.has_control}")
            print()
        elif cmd == "status":
            print(f"\nDevice ID: {identity.device_id}")
            print(f"Name:      {identity.name}")
            print(f"Profile:   {identity.profile}")
            print(f"Active sessions: {len(session_manager.all_sessions())}\n")
        elif cmd == "help":
            print("\nAvailable commands:")
            print("  qr [name]    - Generate pairing QR code and display in terminal")
            print("  devices      - List paired devices in TrustStore and active sessions")
            print("  status       - Show device identity & status")
            print("  exit / quit  - Stop the server\n")
        else:
            print(f"Unknown command '{cmd}'. Type 'qr' to generate QR code, or 'help' for options.")


async def telemetry_loop(
    event_manager: EventManager,
    session_manager: SessionManager,
    stop_event: asyncio.Event,
    interval_s: float = 2.5,
) -> None:
    """Periodically emits telemetry events to connected sessions subscribed to 'telemetry'."""
    while not stop_event.is_set():
        try:
            await asyncio.sleep(interval_s)
            if not session_manager.sessions_subscribed_to("telemetry"):
                continue

            cpu_val = 15.0
            ram_val = 35.0
            try:
                import psutil
                cpu_val = float(psutil.cpu_percent(interval=None))
                ram_val = float(psutil.virtual_memory().percent)
            except Exception:
                pass

            telemetry_data = {
                "cpu": cpu_val,
                "ram": ram_val,
                "gpu": 0.0,
                "temp": 42.5,
                "battery": 88.0,
            }
            await event_manager.emit("telemetry", telemetry_data)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.debug("telemetry loop error: %s", exc)


async def run(
    auto_qr: bool = False,
    qr_name: str = "Mobile App",
    port: int = WIFI_PORT,
    pairing_manager: PairingManager = None,
    trust_store: TrustStore = None,
) -> None:
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
    # to unlock movement capabilities (device_server_hardware_mapper.txt section 23).
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
    trust_store = trust_store or TrustStore()
    authenticator = Authenticator(trust_store)
    session_manager = SessionManager()
    safety_manager = SafetyManager()
    event_manager = EventManager(session_manager, identity=identity)
    task_manager = TaskManager(event_manager, safety_manager)
    device_agent = DeviceAgent(tool_registry, safety_manager, task_manager)
    if pairing_manager is None:
        pairing_manager = PairingManager(trust_store, identity=identity)
    else:
        pairing_manager.set_identity(identity)

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
        pairing_manager=pairing_manager,
    )

    # 9. Start Wi-Fi DCP server (WebSocket over TCP)
    wifi_server = WiFiDCPServer(dcp_handler, session_manager, port=port)
    await wifi_server.start()

    # 10. Start Bluetooth DCP server (GATT peripheral with fragmentation/reassembly)
    bluetooth_server = BluetoothDCPServer(dcp_handler, session_manager)
    await bluetooth_server.start()

    # 11. Discovery / advertising (mDNS + BLE)
    advertiser = DiscoveryAdvertiser(
        device_id=identity.device_id,
        profile=identity.profile,
        port=port,
    )
    advertiser.start()

    logger.info("Device Server ready. Waiting for clients on port %s.", port)

    # Optional immediate QR code generation if requested via CLI flag
    if auto_qr:
        pairing_manager.generate_qr_pairing(client_name=qr_name, port=port)

    stop_event = asyncio.Event()

    def _handle_signal():
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except NotImplementedError:
            pass  # e.g. Windows

    # Start non-blocking interactive console reader
    console_task = asyncio.create_task(
        console_loop(pairing_manager, stop_event, trust_store, session_manager, identity)
    )

    # Start periodic telemetry emitter
    telemetry_task = asyncio.create_task(
        telemetry_loop(event_manager, session_manager, stop_event)
    )

    await stop_event.wait()

    logger.info("shutting down")
    telemetry_task.cancel()
    console_task.cancel()
    advertiser.stop()
    await bluetooth_server.stop()
    await wifi_server.stop()


def main():
    parser = argparse.ArgumentParser(description="Machine Make Device Server")
    parser.add_argument("--qr", action="store_true", help="Generate and display a pairing QR code on startup")
    parser.add_argument("--qr-name", default="Mobile App", help="Client name for QR pairing invitation")
    parser.add_argument("--port", type=int, default=WIFI_PORT, help="Wi-Fi WebSocket port (default: 8765)")
    args = parser.parse_args()

    try:
        asyncio.run(run(auto_qr=args.qr, qr_name=args.qr_name, port=args.port))
    except (KeyboardInterrupt, SystemExit):
        pass


if __name__ == "__main__":
    main()

