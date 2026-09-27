"""
detectors/system.py

System Detector (device_server_hardware_mapper.txt, section 11).
Collects host system information: platform, architecture, OS, CPU, RAM,
storage, hostname. Read-only, best-effort, never raises -- callers get
{"available": False, "error": ...} on failure instead (section 19).
"""

import os
import platform
import socket
from typing import Any, Dict


def _detect_platform() -> str:
    """Best-effort guess of the board/platform we're running on. This is
    intentionally heuristic: precise board identification (e.g. exactly
    which Raspberry Pi model) is a nice-to-have, not something the rest of
    the framework depends on."""
    machine = platform.machine().lower()
    try:
        with open("/proc/device-tree/model", "r") as f:
            model = f.read().strip("\x00").strip()
            if "raspberry pi" in model.lower():
                return "raspberry_pi"
    except (FileNotFoundError, PermissionError, OSError):
        pass

    if machine.startswith("esp32"):
        return "esp32"
    if os.environ.get("DCP_PLATFORM_OVERRIDE"):
        return os.environ["DCP_PLATFORM_OVERRIDE"]
    if machine in ("x86_64", "amd64", "i386", "i686"):
        return "pc"
    if machine.startswith("arm") or machine.startswith("aarch64"):
        return "arm_device"
    return "unknown"


def _total_memory_mb() -> int:
    """Linux-specific /proc/meminfo read, falling back to None on other
    platforms rather than pulling in psutil as a hard dependency."""
    try:
        with open("/proc/meminfo", "r") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    kb = int(line.split()[1])
                    return kb // 1024
    except (FileNotFoundError, PermissionError, ValueError, OSError):
        pass
    return None


def _storage_info() -> list:
    """Best-effort disk usage for the root filesystem only. A full
    multi-mount storage inventory is out of scope for the initial
    implementation."""
    try:
        usage = os.statvfs("/")
        total_mb = (usage.f_frsize * usage.f_blocks) // (1024 * 1024)
        free_mb = (usage.f_frsize * usage.f_bavail) // (1024 * 1024)
        return [{"mount": "/", "total_mb": total_mb, "free_mb": free_mb}]
    except (AttributeError, OSError):
        return []


def detect() -> Dict[str, Any]:
    try:
        cpu_count = os.cpu_count()
        cpu_model = None
        try:
            with open("/proc/cpuinfo", "r") as f:
                for line in f:
                    if line.lower().startswith("model name"):
                        cpu_model = line.split(":", 1)[1].strip()
                        break
        except (FileNotFoundError, PermissionError, OSError):
            pass

        return {
            "available": True,
            "device": {
                "platform": _detect_platform(),
                "architecture": platform.machine(),
                "os": platform.system().lower(),
            },
            "system": {
                "cpu": {
                    "cores": cpu_count,
                    "model": cpu_model,
                },
                "memory_mb": _total_memory_mb(),
                "storage": _storage_info(),
                "hostname": socket.gethostname(),
            },
        }
    except Exception as exc:  # noqa: BLE001 - detectors must never crash the mapper
        return {"available": False, "error": str(exc)}
