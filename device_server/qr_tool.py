"""
qr_tool.py

CLI tool to generate DCP QR Pairing codes for Machine Make.
Allows the user to input custom client labels, pairing codes, and display/save
the resulting QR code for the mobile app scanner.

Usage:
    python qr_tool.py
    python qr_tool.py --client "Damjan's Phone" --code 123456
"""

import argparse
import json
import os
import sys

from auth import TrustStore
from config import ConfigurationManager
from identity import DeviceIdentity
from pairing import PairingManager, print_qr_terminal


def main():
    parser = argparse.ArgumentParser(description="Generate Machine Make DCP Pairing QR Code")
    parser.add_argument("--client", "-c", default=None, help="Name of client/device being invited (default: prompts)")
    parser.add_argument("--code", default=None, help="Optional 6-digit pairing PIN code (auto-generated if omitted)")
    parser.add_argument("--port", "-p", type=int, default=8765, help="Server port (default: 8765)")
    parser.add_argument("--host", default=None, help="Server IP address (default: auto-detected LAN IP)")
    parser.add_argument("--ttl", type=int, default=300, help="QR code validity duration in seconds (default: 300)")
    parser.add_argument("--output", "-o", default="qr_pairing.png", help="PNG output path (default: qr_pairing.png)")
    args = parser.parse_args()

    # Load configuration & identity
    config = ConfigurationManager()
    identity_cfg = config.identity
    identity = DeviceIdentity(
        name=identity_cfg.get("name", "Machine Make Robot"),
        profile=identity_cfg.get("profile", "generic"),
        device_type=identity_cfg.get("type", "device"),
    )

    trust_store = TrustStore()
    pairing_mgr = PairingManager(trust_store, identity=identity)

    # Interactive input if client not specified and stdin is interactive
    client_name = args.client
    if client_name is None:
        if sys.stdin.isatty():
            try:
                user_in = input("Enter client name to pair [My Mobile Device]: ").strip()
                client_name = user_in if user_in else "My Mobile Device"
            except (EOFError, KeyboardInterrupt):
                client_name = "My Mobile Device"
        else:
            client_name = "My Mobile Device"

    pairing_code = args.code
    if pairing_code is None and sys.stdin.isatty() and args.client is None:
        try:
            custom_code = input("Enter 6-digit pairing PIN (press Enter to auto-generate): ").strip()
            if custom_code:
                pairing_code = custom_code
        except (EOFError, KeyboardInterrupt):
            pass

    result = pairing_mgr.generate_qr_pairing(
        client_name=client_name,
        pairing_code=pairing_code,
        ttl_seconds=args.ttl,
        host=args.host,
        port=args.port,
        save_path=args.output,
        display_terminal=True,
    )

    print("QR Code Payload (JSON):")
    print(json.dumps(result["payload"], indent=2))
    print("\nPairing invitation is active and ready for scanning.")


if __name__ == "__main__":
    main()
