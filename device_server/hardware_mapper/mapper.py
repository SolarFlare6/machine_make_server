"""
hardware_mapper/mapper.py

HardwareMapper: answers "what hardware and resources exist on this
device?" (device_server_hardware_mapper.txt, section 10). It does NOT
decide what that hardware is *for* -- that is Physical Configuration
(config.py) and, above it, the Capability Mapper.

One failed detector must never stop the others (section 19): every
detector call is isolated and its error recorded under
hardware_map["detector_errors"] instead of raising.
"""

import logging
from typing import Any, Callable, Dict

from hardware_mapper.schema import empty_hardware_map
from hardware_mapper.detectors import system, interfaces, i2c, usb, camera, audio

logger = logging.getLogger("hardware_mapper")


class HardwareMapper:
    """Runs every detector and assembles a single standardized hardware map
    matching the DCP-HW-1.0 schema."""

    def __init__(self):
        self._last_map: Dict[str, Any] = None

    def scan(self) -> Dict[str, Any]:
        hw_map = empty_hardware_map()

        system_result = self._run(hw_map, "system", system.detect)
        if system_result.get("available"):
            hw_map["device"].update(system_result["device"])
            hw_map["system"].update(system_result["system"])

        iface_result = self._run(hw_map, "interfaces", interfaces.detect)
        if iface_result.get("available"):
            hw_map["interfaces"].update(iface_result["interfaces"])

        i2c_result = self._run(hw_map, "i2c", i2c.detect)
        if i2c_result.get("available"):
            hw_map["devices"]["i2c"] = i2c_result["buses"]

        usb_result = self._run(hw_map, "usb", usb.detect)
        if usb_result.get("available"):
            hw_map["devices"]["usb"] = usb_result["devices"]

        camera_result = self._run(hw_map, "camera", camera.detect)
        if camera_result.get("available"):
            hw_map["devices"]["cameras"] = camera_result["cameras"]

        audio_result = self._run(hw_map, "audio", audio.detect)
        if audio_result.get("available"):
            hw_map["devices"]["audio"] = audio_result["audio"]

        self._last_map = hw_map
        return hw_map

    @property
    def last_map(self) -> Dict[str, Any]:
        """The most recent scan result, or None if scan() has not run yet."""
        return self._last_map

    def _run(self, hw_map: Dict[str, Any], name: str, detect_fn: Callable[[], Dict[str, Any]]) -> Dict[str, Any]:
        """Run one detector in isolation.

        "available": False has two different meanings a caller must not
        conflate: the detector ran fine and simply found no hardware (e.g.
        no /dev/video* nodes), versus the detector itself could not run
        (e.g. permission denied, missing sysfs path, an exception). Only
        the latter -- indicated by an explicit "error" key -- is recorded
        under detector_errors; a clean "nothing found" is not a fault."""
        try:
            result = detect_fn()
        except Exception as exc:  # noqa: BLE001 - absolute last line of defense
            result = {"available": False, "error": str(exc)}

        if "error" in result:
            logger.warning("detector %s unavailable: %s", name, result["error"])
            hw_map["detector_errors"][name] = result["error"]

        return result
