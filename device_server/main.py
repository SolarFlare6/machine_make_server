import argparse
import asyncio
import logging
import os
import signal
import sys
from typing import Optional

from auto_config import run_setup_wizard
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

# params
#python main.py --auto-detect (enables auto-detection for this run)
#python main.py --setup (launches the interactive setup wizard)
#python main.py --robot (forces quadruped robot mode)
#python main.py --no-robot (disables forced quadruped mode)


# setup vars
automatic_detection = True # Set to True to run the new hardware auto-detection implementation; False runs the old implementation
is_robot_project = False # Set to True to configure the server for your Quadruped Robot project

# Uppercase aliases for convenience:
AUTOMATIC_DETECTION = automatic_detection
IS_ROBOT_PROJECT = is_robot_project


def configure_robot_project_config(config_file: Optional[str] = None, custom_name: Optional[str] = None) -> ConfigurationManager:
    """Configures and writes device_config.json with the full Quadruped Robot configuration."""
    from auto_config import build_config_for_profile
    mgr = ConfigurationManager(config_file) if config_file else ConfigurationManager()
    quad_name = custom_name or mgr.identity.get("name") or "MachineMake Quadruped Dog"
    robot_cfg = build_config_for_profile("quadruped", custom_name=quad_name)
    existing_id = mgr.identity.get("device_id")
    if existing_id:
        robot_cfg["identity"]["device_id"] = existing_id
    mgr._config = robot_cfg
    mgr.save()
    logger.info("Configured %s for Quadruped Robot.", mgr._config_file)
    return mgr


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
    interval_s: float = 2.0,
    robot_controller: Optional[RobotController] = None,
) -> None:
    """Periodically emits telemetry events to connected sessions subscribed to 'telemetry'."""
    while not stop_event.is_set():
        try:
            await asyncio.sleep(interval_s)
            if not session_manager.sessions_subscribed_to("telemetry"):
                continue

            cpu_val = 0.0
            ram_val = 0.0
            temp_val = 45.0
            gpu_val = 0.0

            try:
                import psutil
                cpu_val = float(psutil.cpu_percent(interval=None))
                ram_val = float(psutil.virtual_memory().percent)
            except Exception:
                pass

            # Read Raspberry Pi CPU temperature if available
            try:
                with open("/sys/class/thermal/thermal_zone0/temp", "r") as f:
                    temp_val = float(f.read().strip()) / 1000.0
            except Exception:
                pass

            imu_data = robot_controller.get_mpu6050_telemetry() if robot_controller else {
                "pitch": 0.42,
                "roll": -1.15,
                "yaw": 0.0,
                "accel": {"x": 0.01, "y": -0.04, "z": 0.99},
                "gyro": {"x": 0.2, "y": -0.5, "z": 0.1},
            }

            servo_data = robot_controller.get_servo_angles_dict() if robot_controller else {
                "2": 110, "3": 70, "4": 50,
                "5": 110, "6": 110, "7": 150,
                "8": 130, "9": 70, "10": 40,
                "11": 80, "12": 90, "13": 160,
            }

            telemetry_data = {
                "cpu": round(cpu_val, 1),
                "ram": round(ram_val, 1),
                "temp": round(temp_val, 1),
                "gpu": round(gpu_val, 1),
                "power_source": "dc_in",
                "battery": None,
                "voltage": None,
                "imu": imu_data,
                "servos": servo_data,
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
    device_id: Optional[str] = None,
    device_name: Optional[str] = None,
    setup_mode: bool = False,
    auto_detect: Optional[bool] = None,
    robot_project: Optional[bool] = None,
) -> None:
    # Resolve operating mode: explicit argument overrides top-level variable
    should_be_robot = is_robot_project if robot_project is None else robot_project
    should_auto_detect = automatic_detection if auto_detect is None else auto_detect

    # 1. Hardware Mapper: scan available hardware
    hardware_mapper = HardwareMapper()
    hardware_map = hardware_mapper.scan()
    logger.info("hardware scan complete; interfaces=%s", hardware_map["interfaces"])
    if hardware_map["detector_errors"]:
        logger.warning("some detectors were unavailable: %s", hardware_map["detector_errors"])

    config = ConfigurationManager()

    # 2. Operating Mode Routing:
    if should_be_robot:
        # User's Robot Quadruped Project Mode:
        logger.info("Mode: is_robot_project=True -> setting up server specifically for Quadruped Robot")
        identity_cfg = config.identity
        quad_name = device_name or identity_cfg.get("name") or "MachineMake Quadruped Dog"
        identity = DeviceIdentity(
            name=quad_name,
            profile="quadruped",
            device_type="robot",
            device_id=device_id or identity_cfg.get("device_id"),
        )

        # Configure and persist device_config.json for the Quadruped Robot
        from auto_config import build_config_for_profile
        robot_cfg = build_config_for_profile("quadruped", custom_name=quad_name)
        if identity.device_id:
            robot_cfg["identity"]["device_id"] = identity.device_id
        config._config = robot_cfg
        config.save()
        logger.info("Configured %s with Quadruped Robot configuration.", config._config_file)

        physical_config = dict(config.physical_configuration)

    elif should_auto_detect or setup_mode or not os.path.isfile(config._config_file):
        # New implementation: Hardware-scan auto-detection / Setup wizard
        is_interactive = setup_mode or (not should_auto_detect and sys.stdin.isatty())
        logger.info("Mode: automatic_detection=True -> running new hardware auto-detection (interactive=%s)", is_interactive)
        run_setup_wizard(hardware_map, interactive=is_interactive, save_path=config._config_file)
        config.reload()

        identity_cfg = config.identity
        identity = DeviceIdentity(
            name=device_name or identity_cfg.get("name", "Unnamed Device"),
            profile=identity_cfg.get("profile", "generic"),
            device_type=identity_cfg.get("type", "device"),
            device_id=device_id or identity_cfg.get("device_id"),
        )
        physical_config = config.physical_configuration

    else:
        # Old implementation: Load static device_config.json directly without auto-detection
        logger.info("Mode: automatic_detection=False -> running standard/old implementation from device_config.json")
        identity_cfg = config.identity
        identity = DeviceIdentity(
            name=device_name or identity_cfg.get("name", "Unnamed Device"),
            profile=identity_cfg.get("profile", "generic"),
            device_type=identity_cfg.get("type", "device"),
            device_id=device_id or identity_cfg.get("device_id"),
        )
        physical_config = config.physical_configuration

    logger.info("device identity: %s (profile=%s, type=%s, name='%s')", identity.device_id, identity.profile, identity.device_type, identity.name)

    # 4. Physical configuration
    physical_config = physical_config or {}

    # Robot Controller: the "software module" the Capability Mapper needs
    # to unlock movement capabilities (device_server_hardware_mapper.txt section 23).
    robot_controller = RobotController(physical_config, hardware_map=hardware_map)

    # 5. Capability Map
    capability_mapper = CapabilityMapper(robot_controller=robot_controller)
    capabilities = capability_mapper.build(hardware_map, physical_config)
    logger.info("capabilities: %s", capabilities)

    # 6. Tool Registry
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
        telemetry_loop(event_manager, session_manager, stop_event, robot_controller=robot_controller)
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
    parser.add_argument("--device-id", default=None, help="Explicit device ID (e.g. quadruped-pi-01)")
    parser.add_argument("--name", default=None, help="Device display name (e.g. MachineMake Quadruped Dog)")
    parser.add_argument("--setup", action="store_true", help="Run interactive initial setup wizard to detect & configure device")
    parser.add_argument("--auto-detect", action="store_true", default=None, help="Automatically infer and save device configuration from hardware scan")
    parser.add_argument("--robot", action="store_true", default=None, help="Force server setup for quadruped robot project")
    parser.add_argument("--no-robot", action="store_false", dest="robot", help="Disable forced robot quadruped setup")
    args = parser.parse_args()

    try:
        asyncio.run(run(
            auto_qr=args.qr,
            qr_name=args.qr_name,
            port=args.port,
            device_id=args.device_id,
            device_name=args.name,
            setup_mode=args.setup,
            auto_detect=args.auto_detect,
            robot_project=args.robot,
        ))
    except (KeyboardInterrupt, SystemExit):
        pass


if __name__ == "__main__":
    main()

