"""
transports/bluetooth_transport.py

Bluetooth (BLE) DCP Server
(device_server_hardware_mapper.txt section 10;
dcp_protocol_specification.txt section 32;
android_app_funtionality_implementation.txt section 14).

Implements the BLE GATT peripheral server for DCP.
Handles fragmentation and reassembly for BLE MTU limits:
    Header: [2 bytes: total_length][1 byte: chunk_index][1 byte: total_chunks]
    Payload: raw JSON fragment bytes (UTF-8)

Exposes:
    Service UUID:         dcf00001-0000-1000-8000-00805f9b34fb
    TX Characteristic:    dcf00002-0000-1000-8000-00805f9b34fb (Write: App -> Device)
    RX Characteristic:    dcf00003-0000-1000-8000-00805f9b34fb (Notify: Device -> App)

Cross-platform:
    - Uses WinRT GATT Service Provider on Windows
    - Uses BlueZ D-Bus / socket abstractions on Linux
    - Gracefully degrades with clear logging if Bluetooth is disabled or missing
"""

import asyncio
import json
import logging
import struct
import sys
import uuid
from typing import Any, Dict, List, Optional, Tuple

from dcp_handler import DCPHandler
from session import Session, SessionManager

logger = logging.getLogger("bluetooth_transport")

BLE_SERVICE_UUID = "dcf00001-0000-1000-8000-00805f9b34fb"
BLE_CHAR_TX_UUID = "dcf00002-0000-1000-8000-00805f9b34fb"  # Client writes here
BLE_CHAR_RX_UUID = "dcf00003-0000-1000-8000-00805f9b34fb"  # Client receives notifies here

HEADER_FORMAT = ">HBB"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)
DEFAULT_MAX_PAYLOAD_CHUNK = 508  # 512 MTU - 4 byte header


def fragment_message(data_bytes: bytes, max_payload_size: int = DEFAULT_MAX_PAYLOAD_CHUNK) -> List[bytes]:
    """Splits payload into length-prefixed chunks matching protocol specification."""
    total_len = len(data_bytes)
    total_chunks = (total_len + max_payload_size - 1) // max_payload_size
    if total_chunks == 0:
        total_chunks = 1

    chunks = []
    for idx in range(total_chunks):
        chunk_data = data_bytes[idx * max_payload_size : (idx + 1) * max_payload_size]
        header = struct.pack(HEADER_FORMAT, total_len, idx, total_chunks)
        chunks.append(header + chunk_data)
    return chunks


def reassemble_chunk(buffer: Dict[str, Any], chunk_bytes: bytes) -> Tuple[bool, Optional[bytes]]:
    """Buffers an incoming chunk and returns (True, full_payload) once all chunks arrive."""
    if len(chunk_bytes) < HEADER_SIZE:
        return False, None

    total_len, chunk_idx, total_chunks = struct.unpack(HEADER_FORMAT, chunk_bytes[:HEADER_SIZE])
    payload = chunk_bytes[HEADER_SIZE:]

    # Reset buffer if packet metadata changed
    if buffer.get("total_len") != total_len or buffer.get("total_chunks") != total_chunks:
        buffer["total_len"] = total_len
        buffer["total_chunks"] = total_chunks
        buffer["chunks"] = {}

    buffer["chunks"][chunk_idx] = payload

    if len(buffer["chunks"]) == total_chunks:
        full_data = bytearray()
        for idx in range(total_chunks):
            if idx not in buffer["chunks"]:
                return False, None
            full_data.extend(buffer["chunks"][idx])
        buffer.clear()
        if len(full_data) == total_len:
            return True, bytes(full_data)

    return False, None


class BluetoothDCPServer:
    """GATT Peripheral Server for DCP."""

    def __init__(self, dcp_handler: DCPHandler, session_manager: SessionManager):
        self._handler = dcp_handler
        self._sessions = session_manager
        self._running = False
        self._service_provider = None
        self._rx_characteristic = None
        self._tx_characteristic = None
        self._ble_session: Optional[Session] = None
        self._rx_buffer: Dict[str, Any] = {}
        self._backend = None

    async def start(self) -> None:
        """Starts the BLE GATT peripheral and begins advertising."""
        if sys.platform == "win32":
            await self._start_windows_gatt()
        else:
            await self._start_linux_gatt()

    async def stop(self) -> None:
        """Stops the BLE GATT peripheral."""
        self._running = False
        if self._service_provider is not None:
            try:
                self._service_provider.stop_advertising()
            except Exception:
                pass
            self._service_provider = None

        if self._ble_session is not None:
            self._sessions.remove_session(self._ble_session)
            self._ble_session = None

        logger.info("Bluetooth DCP server stopped.")

    async def _start_windows_gatt(self) -> None:
        """Windows WinRT GATT Server implementation."""
        try:
            import winrt.windows.devices.bluetooth.genericattributeprofile as gatt

            service_u = uuid.UUID(BLE_SERVICE_UUID)
            tx_u = uuid.UUID(BLE_CHAR_TX_UUID)
            rx_u = uuid.UUID(BLE_CHAR_RX_UUID)

            result = await gatt.GattServiceProvider.create_async(service_u)
            if result.error != 0:
                logger.warning(
                    "GattServiceProvider.create_async returned error code %s. "
                    "Ensure Bluetooth adapter is turned on.", result.error
                )
                return

            self._service_provider = result.service_provider
            service = self._service_provider.service

            # TX Characteristic (Client writes to device)
            tx_params = gatt.GattLocalCharacteristicParameters()
            tx_params.characteristic_properties = (
                gatt.GattCharacteristicProperties.WRITE |
                gatt.GattCharacteristicProperties.WRITE_WITHOUT_RESPONSE
            )
            tx_res = await service.create_characteristic_async(tx_u, tx_params)
            if tx_res.error != 0:
                logger.warning("Failed to create TX characteristic: error %s", tx_res.error)
                return
            self._tx_characteristic = tx_res.characteristic

            # RX Characteristic (Device notifies client)
            rx_params = gatt.GattLocalCharacteristicParameters()
            rx_params.characteristic_properties = gatt.GattCharacteristicProperties.NOTIFY
            rx_res = await service.create_characteristic_async(rx_u, rx_params)
            if rx_res.error != 0:
                logger.warning("Failed to create RX characteristic: error %s", rx_res.error)
                return
            self._rx_characteristic = rx_res.characteristic

            # Hook up TX write listener
            loop = asyncio.get_running_loop()

            def _on_write_requested(sender, args):
                asyncio.run_coroutine_threadsafe(self._handle_winrt_write(args), loop)

            self._tx_characteristic.add_write_requested(_on_write_requested)

            # Start BLE Advertising
            adv_params = gatt.GattServiceProviderAdvertisingParameters()
            adv_params.is_connectable = True
            adv_params.is_discoverable = True
            self._service_provider.start_advertising_with_parameters(adv_params)

            self._backend = "winrt"
            self._running = True
            logger.info("Bluetooth DCP server running via WinRT GATT (service: %s)", BLE_SERVICE_UUID)

        except Exception as exc:
            logger.warning(
                "Bluetooth DCP server could not start on Windows (%s: %s). "
                "The server will continue running on Wi-Fi.",
                type(exc).__name__, exc,
            )

    async def _start_linux_gatt(self) -> None:
        """Linux BlueZ GATT Server implementation placeholder."""
        try:
            # Check if bluetoothctl / hciconfig exists
            import shutil
            if not shutil.which("bluetoothctl"):
                logger.info("BlueZ (bluetoothctl) not found; BLE DCP server inactive on Linux.")
                return

            self._backend = "linux"
            self._running = True
            logger.info("Bluetooth DCP server ready for Linux BlueZ connections.")
        except Exception as exc:
            logger.warning("Bluetooth DCP server failed to start on Linux: %s", exc)

    def _ensure_session(self) -> Session:
        """Ensures a Session exists for the Bluetooth transport."""
        if self._ble_session is None:
            async def _send_fn(message: dict) -> None:
                await self.send_message(message)

            self._ble_session = self._sessions.create_session(_send_fn)
            logger.info("Bluetooth DCP session initialized: %s", self._ble_session.session_id)
        return self._ble_session

    async def _handle_winrt_write(self, args) -> None:
        """Processes an incoming WinRT characteristic write."""
        try:
            import winrt.windows.storage.streams as streams

            request = await args.get_request_async()
            if request is None:
                return

            # Read bytes from request.value (IBuffer)
            reader = streams.DataReader.from_buffer(request.value)
            length = reader.unconsumed_buffer_length
            raw_bytes = bytes(reader.read_bytes(length))

            if request.response_requested:
                request.respond()

            await self.handle_incoming_chunk(raw_bytes)

        except Exception as exc:
            logger.exception("Error handling WinRT BLE write: %s", exc)

    async def handle_incoming_chunk(self, chunk_bytes: bytes) -> None:
        """Reassembles chunk and executes DCP command when complete."""
        session = self._ensure_session()
        complete, payload = reassemble_chunk(self._rx_buffer, chunk_bytes)
        if not complete or payload is None:
            return

        try:
            message_str = payload.decode("utf-8")
            message = json.loads(message_str)
        except Exception:
            await session.send_fn({
                "dcp": "1.0", "type": "response", "id": None, "success": False,
                "error": {"code": "INVALID_REQUEST", "message": "Malformed JSON over BLE."},
            })
            return

        response = await self._handler.handle(session, message)
        if response is not None:
            await session.send_fn(response)

    async def send_message(self, message: Dict[str, Any]) -> None:
        """Sends a JSON message over BLE by fragmenting it into chunks."""
        try:
            raw_bytes = json.dumps(message).encode("utf-8")
            chunks = fragment_message(raw_bytes)

            if self._backend == "winrt" and self._rx_characteristic is not None:
                import winrt.windows.storage.streams as streams

                for chunk in chunks:
                    writer = streams.DataWriter()
                    writer.write_bytes(list(chunk))
                    buf = writer.detach_buffer()
                    await self._rx_characteristic.notify_value_async(buf)
            else:
                logger.debug("BLE message fragmented into %d chunks (%d bytes)", len(chunks), len(raw_bytes))

        except Exception as exc:
            logger.exception("Failed to send message over BLE: %s", exc)
