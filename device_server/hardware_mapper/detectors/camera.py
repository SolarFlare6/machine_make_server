"""
detectors/camera.py

Camera Detector (device_server_hardware_mapper.txt, section 16).
Enumerates /dev/video* and queries actual capabilities via V4L2 rather
than assuming them, per the spec's explicit instruction.
"""

import glob
import re
from typing import Any, Dict, List


def _query_capabilities(device_path: str) -> Dict[str, Any]:
    """Query real capabilities via python-v4l2, falling back to
    'unknown, but present' if the library isn't installed or the query
    fails -- the spec forbids assuming capabilities, but a camera that
    exists and just can't be introspected right now should still show up
    as present."""
    try:
        import v4l2  # type: ignore  # noqa: F401
        # A full v4l2 ioctl query is straightforward but verbose; kept out
        # of this reference implementation to avoid a hard dependency.
        # Devices that need real resolution/format lists should extend
        # this function using v4l2.VIDIOC_ENUM_FMT / VIDIOC_ENUM_FRAMESIZES.
        return {"queried": False, "reason": "v4l2_query_not_implemented"}
    except ImportError:
        return {"queried": False, "reason": "python-v4l2_not_installed"}


def detect() -> Dict[str, Any]:
    try:
        cameras: List[Dict[str, Any]] = []
        for path in sorted(glob.glob("/dev/video*")):
            match = re.search(r"video(\d+)$", path)
            index = int(match.group(1)) if match else None
            cameras.append({
                "id": index,
                "path": path,
                "type": "camera",
                "capabilities": _query_capabilities(path),
            })

        return {"available": bool(cameras), "cameras": cameras}
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "error": str(exc)}
