# Plan: Task Queue Manager + Remote Message Bridge

## TL;DR
Add a **Task Queue Manager** layer between all message sources (UI, remote/Redis) and the agent executor. All business tasks go through a FIFO queue processed one-at-a-time; status queries and system requests bypass the queue and execute immediately. Add an **Azure Managed Redis** bridge with inbox/outbox streams keyed by user email for remote message delivery (Teams integration consumed separately).

## Scope
- **In scope**: Task queue manager, request classification (business vs system), status tracking, Redis inbox/outbox polling, progress log capture, UI updates for queued/remote tasks
- **Out of scope**: Teams bot / cloud relay service that puts messages into Redis (handled separately)

---

## Phase 1 — Task Queue Manager (in-process, no cloud dependency)

### Step 1: Create `task_queue.py` — the queue manager module
- In-memory FIFO queue using `collections.deque` protected by `threading.Lock`
- Each queue entry: `TaskItem(id, user_input, source, status, submitted_at, progress_log, result)`
- States: `queued → running → completed | failed`
- `progress_log`: list of `(timestamp, kind, message)` tuples captured from `on_progress` callbacks
- Public API:
  - `submit_task(user_input, source="ui"|"remote") → task_id` — enqueue a business task
  - `get_queue_status() → list[TaskItem]` — return all items (running + queued)
  - `get_current_task() → TaskItem | None` — the currently executing task
  - `is_busy() → bool` — True if a task is running
- Internal: a **worker thread** (started once) that loops forever: pop from queue → execute → mark done → pop next
- The worker calls `run_agent(user_input, on_progress=...)` with an `on_progress` that **both** broadcasts to UI **and** appends to `task.progress_log`

### Step 2: Create `skills/task_status.yaml` — status query skill
- A new skill YAML: `name: task_status`, `model: mini`, `tools: [get_task_status]`, `conversational: false`
- Description targets phrases like "what is the status", "is any task running", "where is my request"
- Instructions: call `get_task_status` tool, then summarize the progress milestones and current state

### Step 3: Create `tools/get_task_status.py` — status tool
- `SCHEMA`: function `get_task_status` with no required params (returns status of current + queued tasks)
- `handle()`: imports `task_queue.get_queue_status()` and `get_current_task()`, formats as JSON string
- Returns: current task (input, source, status, progress steps completed, elapsed time), queue depth, summary of queued items

### Step 4: Classify requests — business vs system (*depends on step 1*)
- In `task_queue.py`, add `classify_request(user_input) → "business" | "system"`
- Uses the existing router (`_route()` from agent_core) to get the skill name
- Skill-level flag: add `queued: true/false` field to skill YAML (default `true`)
  - `meeting_invites.yaml`: `queued: true`
  - `qa.yaml`: `queued: true`
  - `email_summary.yaml`: `queued: true`
  - `general.yaml`: `queued: false`
  - `task_status.yaml`: `queued: false`
- If the resolved skill has `queued: false` → execute immediately (bypass queue)
- If `queued: true` → submit to queue

### Step 5: Refactor `meeting_agent.py` to use the queue (*depends on steps 1, 4*)
- Remove `_task_lock`, `_start_task`, `_locked_run`
- Replace with: on receiving a `task` message → call `task_queue.submit_or_execute(user_input, source="ui", on_broadcast=_broadcast)`
  - If system request: execute inline, return result directly
  - If business request: enqueue, broadcast `{"type": "task_queued", "position": N, "task_id": id}`
- The queue worker thread broadcasts progress/completion to all UI clients via `_broadcast`
- When a task completes, if next task is in queue → auto-start, broadcast `{"type": "task_started", "source": "queued"}`

### Step 6: Update `chat_ui.html` for queue awareness (*depends on step 5*)
- Handle new message type `task_queued` — show "Your request has been queued (position N)"
- Handle `task_started` with `source` field — show "Processing queued request..." or "Processing your request..."
- Show queue indicator in UI (e.g. badge showing queue depth)

---

## Phase 2 — Azure Managed Redis Bridge (cloud dependency)

### Step 7: Add `redis_bridge.py` — Redis inbox/outbox poller (*parallel with phase 1*)
- Connect to Azure Managed Redis at `ocvp-cache.southindia.redis.azure.net:10000` (SSL, port 10000)
- Auth via Managed Identity — reuse existing InteractiveBrowserCredential, get token for `https://redis.azure.com/.default`, pass as password
- **Inbox** stream: `workiq:inbox:{user_email}` — messages from Teams → agent
- **Outbox** stream: `workiq:outbox:{user_email}` — agent responses → Teams relay
- Polling loop (threaded): XREAD with block timeout on inbox, for each message → `task_queue.submit_or_execute(source="teams")`
- On task completion, XADD result to outbox stream
- Progress to remote: final summary only (not every step — avoids mobile spam)
- Connection: `redis.Redis` with `ssl=True`

### Step 8: Register/update agent on startup (*depends on step 7*)
- After auth, SET key `workiq:agents:{user_email}` with agent info + TTL = 86400s (upsert — overwrites if exists)
- Background timer refreshes TTL periodically (e.g. every hour)
- Teams relay checks this key to know if user's agent is online

### Step 9: Configuration (*parallel*)
- Add to `.env.example`:
  - `AZ_REDIS_CACHE_ENDPOINT=ocvp-cache.southindia.redis.azure.net:10000`
  - `REDIS_SESSION_TTL_SECONDS=86400`
- Add `redis>=5.0` to requirements.txt (plain redis — no hiredis needed for this volume)
- Redis bridge is **optional** — if `AZ_REDIS_CACHE_ENDPOINT` is not set, agent runs in local-only mode

---

## Relevant files

- `task_queue.py` (NEW) — queue manager, worker thread, task classification, status API
- `tools/get_task_status.py` (NEW) — tool for the LLM to query task status
- `skills/task_status.yaml` (NEW) — skill definition for status queries
- `redis_bridge.py` (NEW) — Azure Redis inbox/outbox poller
- `meeting_agent.py` — refactor `_start_task`/`_locked_run` → use `task_queue`, add Redis startup
- `agent_core.py` — add `queued` field to `Skill` class, expose `_route()` for external classification
- `chat_ui.html` — handle `task_queued` message, queue depth indicator
- `skills/meeting_invites.yaml`, `qa.yaml`, `email_summary.yaml` — add `queued: true`
- `skills/general.yaml` — add `queued: false`

## Verification
1. Start app, send a meeting invite task from UI → verify it enters queue and executes
2. While task is running, send another business request → verify it queues at position 2
3. While task is running, ask "what is the status?" → verify immediate response with progress summary
4. Send a "hello" → verify it responds immediately (bypasses queue)
5. After task completes, verify queued task auto-starts
6. (Phase 2) Set Redis env vars, start app → verify registration in Redis
7. (Phase 2) Push a message to inbox stream → verify agent picks it up and processes it
8. (Phase 2) Verify response appears in outbox stream

## Decisions
- **Queue is in-memory, not persistent** — if the agent restarts, queued tasks are lost (acceptable for a desktop agent)
- **Router is called first** to classify, then queue decision is made — this means one LLM call for classification before queuing
- **`queued` field on skills** — cleaner than a hardcoded list; new skills declare their own queue behavior
- **Redis Streams** (not pub/sub) — messages persist until consumed, survives brief disconnections
- **Redis is optional** — agent works fully in local mode without Redis config

## Further Considerations
1. **Queue persistence**: If the agent crashes mid-task, the in-memory queue is lost. Acceptable for v1? Or should we persist to a local SQLite/JSON file?
2. **Progress forwarding to Teams**: Should every `on_progress` step be forwarded to the Teams user, or only a final summary? Recommendation: final summary only, to avoid notification spam on mobile.
3. **Concurrent status queries**: Multiple "what's the status?" queries while a task runs should all be handled immediately. The current design supports this since `task_status` skill has `queued: false`.
