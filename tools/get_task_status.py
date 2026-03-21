"""
Tool: get_task_status — returns current task and queue status.
"""

import json
import time

SCHEMA = {
    "type": "function",
    "name": "get_task_status",
    "description": (
        "Get the current task queue status: what task is running (with progress "
        "milestones), how many tasks are queued, and a summary of each queued item."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}


def handle(arguments: dict, **kwargs) -> str:
    """Return current task + queue status as JSON."""
    from task_queue import queue

    status = queue.get_queue_status()
    current = status["current"]
    queued = status["queued"]

    result = {"running": None, "queue_depth": status["queue_depth"], "queued": []}

    if current:
        elapsed = time.time() - (current.started_at or current.submitted_at)
        progress_steps = [
            {"time": round(t - current.started_at, 1), "kind": k, "message": m}
            for t, k, m in current.progress_log
        ] if current.started_at else []
        result["running"] = {
            "task_id": current.id,
            "user_input": current.user_input[:200],
            "source": current.source,
            "status": current.status,
            "elapsed_seconds": round(elapsed, 1),
            "progress_steps_completed": len(progress_steps),
            "progress": progress_steps[-5:],  # last 5 steps
        }

    for item in queued:
        wait_time = time.time() - item.submitted_at
        result["queued"].append({
            "task_id": item.id,
            "user_input": item.user_input[:100],
            "source": item.source,
            "waiting_seconds": round(wait_time, 1),
        })

    return json.dumps(result, indent=2)
