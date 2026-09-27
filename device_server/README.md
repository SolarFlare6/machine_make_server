# Device Server — Reference Implementation

This is a working Python implementation of the **Device Server** and
**Hardware Mapper** described in `device_server_hardware_mapper.txt`,
speaking the **DCP** protocol described in `dcp_protocol_specification.txt`,
for the Flutter app described in `android_app_funtionality_implementation.txt`.

It targets Raspberry Pi / PC-class devices running Python 3.9+. The
example configuration and Robot Controller model a quadruped, but every
layer above the Robot Controller is generic — swap `robot_controller.py`
and `config/device_config.json` to support a different device.

## Layout

```
device_server/
├── main.py                 # startup sequence (spec section 4) and entry point
├── identity.py              # device_id + long-term identity (persisted)
├── auth.py                  # TrustStore + nonce challenge-response auth
├── pairing.py               # PairingManager (terminal Y/N confirmation)
├── session.py                # per-connection Session + SessionManager (control ownership)
├── config.py                  # ConfigurationManager (physical configuration, safety limits)
├── capability_mapper.py        # hardware + config + software -> capability list
├── tool_registry.py             # capability list -> DCP tool definitions
├── safety_manager.py             # argument ranges, control ownership, busy-state checks
├── device_agent.py                # auth -> permission -> schema -> safety -> execute pipeline
├── task_manager.py                 # long-running tool calls -> task_id + progress events
├── event_manager.py                 # broadcasts DCP events to subscribed sessions
├── dcp_handler.py                    # JSON message <-> internal calls (transport-agnostic)
├── discovery.py                       # mDNS advertising (_dcp._tcp.local)
├── robot_controller.py                 # example hardware translation layer (simulated I/O)
├── hardware_mapper/
│   ├── schema.py                        # DCP-HW-1.0 schema
│   ├── mapper.py                         # orchestrates detectors, fault-tolerant
│   └── detectors/                         # system, interfaces, i2c, usb, camera, audio
├── transports/
│   └── websocket_transport.py             # Wi-Fi DCP server (WebSocket over TCP)
├── config/
│   ├── device_config.json                  # identity + physical configuration + safety limits
│   └── trusted_devices.json                 # paired clients (created empty)
└── example_client.py                         # manual test client (pairing + full session flow)
```

## Running it

```bash
pip install -r requirements.txt
python main.py
```

This runs the full startup sequence from the spec: load identity, scan
hardware, load physical configuration, build the capability map, build
the tool registry, start the Wi-Fi (WebSocket) DCP server on port 8765,
and start mDNS advertising (if `zeroconf` is installed).

To exercise it end-to-end without the real Flutter app:

```bash
# One-time pairing (auto-approves; edit example_client.py to use the
# real terminal Y/N prompt via PairingManager's default confirm_fn)
python example_client.py pair

# Full session: authenticate, discover capabilities/tools, subscribe to
# events, take control, stand up, then walk forward with progress events
python example_client.py demo-client-1
```

## Design notes / where each spec requirement lives

- **device_id stability** (`identity.py`): generated once, persisted to
  `config/device_identity.json`, never derived from IP/MAC.
- **Detector fault tolerance** (`hardware_mapper/mapper.py`): each
  detector runs in isolation; failures land in
  `hardware_map["detector_errors"]` instead of aborting the scan.
- **Hardware vs. Physical Configuration vs. Capability** (spec section
  42): kept as three separate modules (`hardware_mapper/`, `config.py`,
  `capability_mapper.py`) that only communicate through plain dicts —
  the I2C detector, for example, only ever reports address *candidates*,
  never a confirmed part.
- **Security pipeline** (`device_agent.py`): every `execute_tool` call
  goes through authentication → permission → schema validation → safety
  validation → execution, in that order, matching protocol spec section
  27 exactly.
- **Control ownership** (`session.py`): only one session can hold
  `CONTROL` ownership at a time; `SafetyManager` rejects CONTROL-tool
  calls from anyone else, regardless of their stored permission level.
- **Long-running tasks** (`task_manager.py`): `walk`/`turn` return a
  `task_id` immediately and report `task_started` / `task_progress` /
  `task_completed` / `task_failed` events, per protocol spec sections
  18–21.
- **Transport independence** (protocol spec section 33): `dcp_handler.py`
  operates purely on parsed dicts. `transports/websocket_transport.py`
  is the only transport-specific code in the request path.

## Known limitations / intentional stubs

- **Bluetooth (BLE) DCP server is not implemented.** `main.py` documents
  exactly where it plugs in: a BLE GATT peripheral (e.g. via `bleak` or
  `bluezero`) that handles fragmentation/reassembly and then calls the
  same `dcp_handler.handle(session, message)` used by the WebSocket
  transport. No other layer needs to change to add it.
- **Robot Controller hardware I/O is simulated** (`asyncio.sleep` stands
  in for real servo motion) so this runs on any machine. Real PCA9685 /
  IMU access should replace `_drive_servos()` and `get_orientation()`.
- **Camera capability querying** (`hardware_mapper/detectors/camera.py`)
  detects `/dev/video*` nodes but does not perform a full V4L2
  resolution/format enumeration; the hook for that is left in place.
- **Authentication uses a symmetric HMAC challenge-response**, not
  asymmetric keys. This satisfies "don't rely on IP/MAC/name alone" and
  needs no extra dependency, but a production deployment may prefer
  per-device asymmetric keys (e.g. Ed25519) — `auth.py`'s docstring notes
  exactly where that swap would go without touching DCP itself.
- **QR pairing** and **WebRTC video streaming** (spec sections 10 and 28
  respectively) are referenced in comments but not implemented — both
  are additive and don't change any of the modules above them.
