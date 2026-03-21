"""
Task Queue Manager — FIFO queue with a single worker thread.

Business tasks go through the queue (one at a time).
System tasks (general chat, status queries) bypass the queue and execute immediately.
"""

import logging
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field

logger = logging.getLogger("workiq_assistant")


@dataclass
class TaskItem:
    """A single task in the queue."""

    id: str
    user_input: str
    source: str  # "ui" | "remote"
    skill_name: str | None = None  # pre-routed skill name
    status: str = "queued"  # queued → running → completed | failed
    submitted_at: float = field(default_factory=time.time)
    started_at: float | None = None
    completed_at: float | None = None
    progress_log: list[tuple[float, str, str]] = field(default_factory=list)
    result: str | None = None
    error: str | None = None


class TaskQueue:
    """Thread-safe FIFO task queue with a single background worker."""

    def __init__(self):
        self._queue: deque[TaskItem] = deque()
        self._lock = threading.Lock()
        self._current: TaskItem | None = None
        self._has_work = threading.Event()
        self._on_broadcast = None  # set by caller
        self._on_notify = None  # set by caller
        self._on_show_window = None  # set by caller
        self._on_task_complete = None  # set by caller
        self._run_agent = None  # set by caller

        self._worker = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker.start()
        logger.info("Task queue worker thread started")

    def configure(self, *, run_agent, on_broadcast, on_notify=None,
                  on_show_window=None, on_task_complete=None):
        """Wire up the agent executor and UI broadcast callback."""
        self._run_agent = run_agent
        self._on_broadcast = on_broadcast
        self._on_notify = on_notify
        self._on_show_window = on_show_window
        self._on_task_complete = on_task_complete

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def submit_task(self, user_input: str, source: str = "ui",
                    skill_name: str | None = None) -> TaskItem:
        """Enqueue a business task. Returns the TaskItem."""
        task = TaskItem(
            id=uuid.uuid4().hex[:8],
            user_input=user_input,
            source=source,
            skill_name=skill_name,
        )
        with self._lock:
            self._queue.append(task)
            position = len(self._queue)
        logger.info("Task %s queued (position %d, source=%s): %.80s",
                     task.id, position, source, user_input)
        self._has_work.set()
        return task

    def get_queue_status(self) -> dict:
        """Return current task + queued items for status reporting."""
        with self._lock:
            current = self._current
            queued = list(self._queue)
        return {
            "current": current,
            "queued": queued,
            "queue_depth": len(queued),
        }

    def get_current_task(self) -> TaskItem | None:
        with self._lock:
            return self._current

    def is_busy(self) -> bool:
        with self._lock:
            return self._current is not None

    # ------------------------------------------------------------------
    # Worker loop
    # ------------------------------------------------------------------

    def _worker_loop(self):
        """Continuously process tasks from the queue."""
        while True:
            self._has_work.wait()
            while True:
                with self._lock:
                    if not self._queue:
                        self._has_work.clear()
                        break
                    task = self._queue.popleft()
                    self._current = task
                    task.status = "running"
                    task.started_at = time.time()

                self._execute_task(task)

                with self._lock:
                    self._current = None

    def _execute_task(self, task: TaskItem):
        """Run a single task through the agent."""
        logger.info("Task %s started (source=%s, skill=%s): %.80s",
                     task.id, task.source, task.skill_name, task.user_input)

        # Broadcast task_started with request_id so UI tracks the right bubble
        self._broadcast({
            "type": "task_started",
            "request_id": task.id,
            "task_id": task.id,
            "source": task.source,
        })
        if self._on_notify:
            self._on_notify("WorkIQ Assistant", "Working on your request...")

        def on_progress(kind: str, message: str):
            task.progress_log.append((time.time(), kind, message))
            self._broadcast({"type": "progress", "request_id": task.id,
                             "kind": kind, "message": message})

        try:
            result = self._run_agent(task.user_input, task.skill_name,
                                     on_progress=on_progress)
            task.status = "completed"
            task.result = result
            task.completed_at = time.time()
            logger.info("Task %s completed", task.id)

            self._broadcast({"type": "task_complete", "request_id": task.id,
                             "result": result})
            first_line = result.strip().split("\n")[0]
            summary = first_line[:200] + "\u2026" if len(first_line) > 200 else first_line
            if self._on_notify:
                self._on_notify("Task Complete", summary)
            if self._on_show_window:
                self._on_show_window()

            # If more tasks are queued, notify UI
            with self._lock:
                pending = len(self._queue)
                next_task = self._queue[0] if self._queue else None
            if pending and next_task:
                self._broadcast({
                    "type": "task_started",
                    "request_id": next_task.id,
                    "source": "queued",
                    "task_id": next_task.id,
                    "message": f"Processing next queued request ({pending} remaining)...",
                })

        except Exception as e:
            task.status = "failed"
            task.error = str(e)
            task.completed_at = time.time()
            logger.error("Task %s failed: %s", task.id, e, exc_info=True)
            self._broadcast({"type": "task_error", "request_id": task.id,
                             "error": str(e)[:500]})
            if self._on_notify:
                self._on_notify("Task Failed", str(e)[:200])

        # Notify listeners (e.g. Redis bridge) that a task finished
        if self._on_task_complete:
            try:
                self._on_task_complete(task)
            except Exception as cb_err:
                logger.warning("on_task_complete callback failed: %s", cb_err)

    def _broadcast(self, message: dict):
        if self._on_broadcast:
            self._on_broadcast(message)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

queue = TaskQueue()
