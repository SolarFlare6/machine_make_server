"""
task_manager.py

Task Manager (device_server_hardware_mapper.txt sections 22, 35;
dcp_protocol_specification.txt sections 18-22).

Long-running tool calls (e.g. walk) return a task_id immediately rather
than blocking the DCP connection, then report progress/completion via
events. This module owns task bookkeeping and cancellation; it does not
know anything about robots or servos -- callers hand it an arbitrary
coroutine plus a progress callback wired to task_progress events.
"""

import asyncio
import itertools
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from event_manager import EventManager
from safety_manager import SafetyManager

_task_id_counter = itertools.count(1)

TASK_STARTED = "started"
TASK_RUNNING = "running"
TASK_COMPLETED = "completed"
TASK_FAILED = "failed"
TASK_CANCELLED = "cancelled"


@dataclass
class Task:
    task_id: str
    coro_factory: Callable[[Callable[[float], None]], Any]
    state: str = TASK_STARTED
    progress: float = 0.0
    error: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    asyncio_task: Optional[asyncio.Task] = None


class TaskManager:
    def __init__(self, event_manager: EventManager, safety_manager: SafetyManager):
        self._events = event_manager
        self._safety = safety_manager
        self._tasks: Dict[str, Task] = {}

    def get_status(self, task_id: str) -> Optional[Dict[str, Any]]:
        task = self._tasks.get(task_id)
        if task is None:
            return None
        return {"task_id": task.task_id, "state": task.state, "progress": task.progress,
                "error": task.error}

    def start(self, coro_factory: Callable[[Callable[[float], None]], Any]) -> str:
        """coro_factory receives a progress_cb(fraction) and returns the
        coroutine to run. Returns the new task_id immediately; the
        coroutine itself is scheduled on the event loop and reports
        progress/completion via events as it runs."""
        task_id = f"task-{next(_task_id_counter)}"
        task = Task(task_id=task_id, coro_factory=coro_factory)
        self._tasks[task_id] = task
        self._safety.mark_busy(True)
        task.asyncio_task = asyncio.create_task(self._run(task))
        return task_id

    async def _run(self, task: Task) -> None:
        await self._events.emit("task_started", {}, task_id=task.task_id)

        def progress_cb(fraction: float) -> None:
            task.progress = fraction
            task.state = TASK_RUNNING
            asyncio.create_task(
                self._events.emit("task_progress", {"progress": fraction}, task_id=task.task_id)
            )

        try:
            await task.coro_factory(progress_cb)
            task.state = TASK_COMPLETED
            task.progress = 1.0
            await self._events.emit("task_completed", {}, task_id=task.task_id)
        except asyncio.CancelledError:
            task.state = TASK_CANCELLED
            await self._events.emit("task_failed", {"error": "CANCELLED"}, task_id=task.task_id)
        except Exception as exc:  # noqa: BLE001
            task.state = TASK_FAILED
            task.error = str(exc)
            await self._events.emit("task_failed", {"error": str(exc)}, task_id=task.task_id)
        finally:
            self._safety.mark_busy(False)

    def cancel(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if task is None or task.asyncio_task is None:
            return False
        task.asyncio_task.cancel()
        return True
