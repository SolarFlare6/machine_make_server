"""
discovery.py

Discovery / advertising (device_server_hardware_mapper.txt section 5;
dcp_protocol_specification.txt implicit via android app doc section 4).

Advertises the device over mDNS/DNS-SD as `_dcp._tcp.local`, carrying
only public, non-secret information (device_id, profile, protocol
version, pairing state, available transports) -- matching the "do not
advertise: private keys / passwords / secrets / complete hardware maps"
instruction exactly.

Uses `zeroconf` if installed; degrades to a no-op with a clear log
message if it isn't, so the rest of the Device Server still runs (e.g.
during local development or on a platform without mDNS support). BLE
advertising is out of scope for this reference implementation -- see
README.md for what a real BLE peripheral integration would need.
"""

import logging
import socket
from typing import Any, Dict

logger = logging.getLogger("discovery")

SERVICE_TYPE = "_dcp._tcp.local."


class DiscoveryAdvertiser:
    def __init__(self, device_id: str, profile: str, port: int, extra_info: Dict[str, Any] = None):
        self._device_id = device_id
        self._profile = profile
        self._port = port
        self._extra_info = extra_info or {}
        self._zeroconf = None
        self._service_info = None

    def start(self) -> None:
        try:
            from zeroconf import ServiceInfo, Zeroconf
        except ImportError:
            logger.warning(
                "zeroconf package not installed; mDNS advertising disabled. "
                "Install with `pip install zeroconf` to enable Wi-Fi discovery."
            )
            return

        properties = {
            b"device_id": self._device_id.encode("utf-8"),
            b"profile": self._profile.encode("utf-8"),
            b"protocol": b"DCP",
            b"protocol_version": b"1.0",
        }
        for k, v in self._extra_info.items():
            k_bytes = k.encode("utf-8") if isinstance(k, str) else bytes(k)
            v_bytes = str(v).encode("utf-8") if not isinstance(v, bytes) else v
            properties[k_bytes] = v_bytes

        local_ip = self._local_ip()

        # mDNS is a discovery convenience, not something the rest of the
        # Device Server depends on (a client can always connect directly
        # by IP, or pair via QR). Any failure here -- most commonly
        # zeroconf.EventLoopBlocked on Windows, caused by the firewall or
        # a VPN/virtual adapter silently dropping multicast -- is therefore
        # logged and swallowed rather than allowed to crash main.py.
        try:
            self._zeroconf = Zeroconf()
            self._service_info = ServiceInfo(
                SERVICE_TYPE,
                name=f"{self._device_id}.{SERVICE_TYPE}",
                addresses=[socket.inet_aton(local_ip)],
                port=self._port,
                properties=properties,
            )
            self._zeroconf.register_service(self._service_info)
            logger.info("advertising %s on mDNS at %s:%s", self._device_id, local_ip, self._port)
        except Exception as exc:  # noqa: BLE001 - deliberately broad, see comment above
            logger.warning(
                "mDNS advertising failed (%s: %s); continuing without it. "
                "The server is still reachable directly at %s:%s. "
                "On Windows this is usually Windows Defender Firewall, a VPN, "
                "or a virtual adapter blocking multicast -- allow Python "
                "through the firewall on private networks, or disable "
                "conflicting virtual adapters, to enable discovery.",
                type(exc).__name__, exc, local_ip, self._port,
            )
            if self._zeroconf is not None:
                try:
                    self._zeroconf.close()
                except Exception:  # noqa: BLE001
                    pass
            self._zeroconf = None
            self._service_info = None

    def stop(self) -> None:
        if self._zeroconf and self._service_info:
            try:
                self._zeroconf.unregister_service(self._service_info)
            except Exception:  # noqa: BLE001
                pass
            self._zeroconf.close()

    @staticmethod
    def _local_ip() -> str:
        # Doesn't actually send packets; just asks the OS which local
        # interface would be used to reach an external address, which is
        # a common trick for finding the "real" LAN IP.
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"
        finally:
            s.close()
