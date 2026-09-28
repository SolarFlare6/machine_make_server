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
    tool_name: str = "unknown"
    parameters: Dict[str, Any] = field(default_factory=dict)
    state: str = TASK_STARTED
    progress: float = 0.0
    result: Any = None
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
        return {
            "task_id": task.task_id,
            "tool": task.tool_name,
            "state": task.state,
            "progress": task.progress,
            "result": task.result,
            "error": task.error,
        }

    def start(self, coro_factory: Callable[[Callable[[float], None]], Any],
              tool_name: str = "unknown",
              parameters: Optional[Dict[str, Any]] = None) -> str:
        """coro_factory receives a progress_cb(fraction) and returns the
        coroutine to run. Returns the new task_id immediately; the
        coroutine itself is scheduled on the event loop and reports
        progress/completion via events as it runs."""
        task_id = f"task-{next(_task_id_counter)}"
        task = Task(
            task_id=task_id,
            coro_factory=coro_factory,
            tool_name=tool_name,
            parameters=parameters or {},
        )
        self._tasks[task_id] = task
        self._safety.mark_busy(True)
        task.asyncio_task = asyncio.create_task(self._run(task))
        return task_id

    async def _run(self, task: Task) -> None:
        start_data = {
            "task_id": task.task_id,
            "tool": task.tool_name,
            "state": TASK_STARTED,
            "progress": 0.0,
        }
        await self._events.emit("task_started", start_data, task_id=task.task_id)
        await self._events.emit("task_update", {
            "task_id": task.task_id,
            "tool": task.tool_name,
            "state": TASK_RUNNING,
            "progress": 0.0,
        }, task_id=task.task_id)

        def progress_cb(fraction: float) -> None:
            task.progress = fraction
            task.state = TASK_RUNNING
            data = {
                "task_id": task.task_id,
                "tool": task.tool_name,
                "state": TASK_RUNNING,
                "progress": fraction,
            }
            asyncio.create_task(
                self._events.emit("task_progress", data, task_id=task.task_id)
            )
            asyncio.create_task(
                self._events.emit("task_update", data, task_id=task.task_id)
            )

        try:
            res = await task.coro_factory(progress_cb)
            task.state = TASK_COMPLETED
            task.progress = 1.0
            task.result = res
            done_data = {
                "task_id": task.task_id,
                "tool": task.tool_name,
                "state": TASK_COMPLETED,
                "progress": 1.0,
                "result": res,
            }
            await self._events.emit("task_completed", done_data, task_id=task.task_id)
            await self._events.emit("task_update", done_data, task_id=task.task_id)
        except asyncio.CancelledError:
            task.state = TASK_CANCELLED
            cancel_data = {
                "task_id": task.task_id,
                "tool": task.tool_name,
                "state": TASK_CANCELLED,
                "progress": task.progress,
                "error": "CANCELLED",
            }
            await self._events.emit("task_failed", cancel_data, task_id=task.task_id)
            await self._events.emit("task_update", cancel_data, task_id=task.task_id)
        except Exception as exc:  # noqa: BLE001
            task.state = TASK_FAILED
            task.error = str(exc)
            fail_data = {
                "task_id": task.task_id,
                "tool": task.tool_name,
                "state": TASK_FAILED,
                "progress": task.progress,
                "error": str(exc),
            }
            await self._events.emit("task_failed", fail_data, task_id=task.task_id)
            await self._events.emit("task_update", fail_data, task_id=task.task_id)
        finally:
            self._safety.mark_busy(False)

    def cancel(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if task is None or task.asyncio_task is None:
            return False
        task.asyncio_task.cancel()
        return True
