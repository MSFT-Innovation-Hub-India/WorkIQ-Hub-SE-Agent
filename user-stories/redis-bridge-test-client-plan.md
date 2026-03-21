# Plan: Redis Bridge + Console Test Client

## TL;DR
Implement the agent-side Redis bridge (`redis_bridge.py` — Phase 2 of the task queue plan) and build a **console test client** in `test-client/` that simulates a remote sender (like Teams) by pushing messages to the agent's Redis inbox stream and reading responses from the outbox stream. This validates the full remote-task pipeline before building the real Teams integration.

## Prerequisites
- Phase 1 (task queue) — already implemented
- redis 7.3.0 — already installed
- .env Redis vars — already set

---

## Part A — Agent-side Redis Bridge (`redis_bridge.py`)

### Step A1: Add `on_task_complete` callback to `TaskQueue`
- Add `on_task_complete` callback to `TaskQueue`
- In `_execute_task()`, after task completes or fails, call the callback
- This lets the Redis bridge know when a remote-sourced task finishes

### Step A2: Create `redis_bridge.py`
- `RedisBridge` class with `start()` / `stop()` lifecycle
- **Connection**: `redis.Redis` with SSL, Entra ID token as password, OID as username
  - Token: `credential.get_token("https://redis.azure.com/.default").token`
  - Username: extract `oid` claim from the token JWT (Azure Redis Entra ID requires OID as username)
  - Reconnect on auth expiry (token refresh)
- **Agent registration**: `SET workiq:agents:{email}` with JSON payload `{name, email, started_at, version}` + TTL
  - Background timer refreshes TTL every 30 min
- **Inbox poller** (background thread):
  - `XREAD(streams={"workiq:inbox:{email}": last_id}, block=5000)` — 5s blocking read
  - For each message: extract `text`, `sender` fields → `task_queue.submit_task(text, source="remote")`
  - Track `task_id → stream_message_id` mapping
- **Outbox writer** (via `on_task_complete` callback):
  - When a task with `source="remote"` completes: `XADD workiq:outbox:{email}` with `{task_id, status, result_or_error, timestamp}`
  - Trim outbox stream to last 100 entries (`XTRIM ... MAXLEN ~ 100`)

### Step A3: Wire `redis_bridge` into `meeting_agent.py`
- After `task_queue.configure(...)`, check if `AZ_REDIS_CACHE_ENDPOINT` is set
- If set: resolve user email via `_resolve_organizer()`, create `RedisBridge(credential, email, endpoint, ttl)`, call `bridge.start()`
- Register `bridge.on_task_done` as `task_queue.on_task_complete` callback
- If not set: log "Redis bridge disabled — running in local-only mode" and skip

### Step A4: Stream message schema
- **Inbox** (`workiq:inbox:{email}`) fields: `sender` (str), `text` (str), `ts` (epoch str), `msg_id` (uuid str)
- **Outbox** (`workiq:outbox:{email}`) fields: `task_id` (str), `status` ("completed"|"failed"), `text` (str — result or error), `ts` (epoch str), `in_reply_to` (msg_id from inbox)

---

## Part B — Console Test Client (`test-client/`)

### Step B1: Folder structure
```
test-client/
  chat.py           — main console REPL
  requirements.txt  — redis, azure-identity, python-dotenv
```

### Step B2: Implement `chat.py`
- **Auth**: `InteractiveBrowserCredential` with same tenant ID from `../.env`, scope `https://redis.azure.com/.default`
  - Reuse auth record from `~/.workiq-assistant/auth_record.json` if it exists (same user, already signed in from the main agent)
  - Extract user email from Redis token JWT or ACS token
- **Redis connect**: Parse `AZ_REDIS_CACHE_ENDPOINT` from `../.env` (parent dir), connect with SSL + token
- **Startup check**: `GET workiq:agents:{email}` — verify the agent is online, print agent info
- **REPL loop**:
  1. Print prompt `You > `
  2. Read line from stdin
  3. Generate a `msg_id` (uuid)
  4. `XADD workiq:inbox:{email}` with `{sender: "test-client", text: input, ts: now, msg_id: msg_id}`
  5. Print "Waiting for response..."
  6. Poll `XREAD` on `workiq:outbox:{email}` (blocking, 60s timeout) filtering for `in_reply_to == msg_id`
  7. Print response: `Agent > {result text}`
  8. Loop back to prompt
- **Graceful exit**: Ctrl+C → close Redis, exit
- **Token refresh**: Reauth if token expires mid-session

### Step B3: `test-client/requirements.txt`
```
redis>=5.0
azure-identity>=1.15.0
python-dotenv>=1.0.0
```
Can reuse the main agent's `.venv` — no separate venv needed.

---

## Relevant files

| File | Action |
|------|--------|
| `redis_bridge.py` (NEW) | Agent-side Redis inbox/outbox bridge |
| `test-client/chat.py` (NEW) | Console REPL test client |
| `test-client/requirements.txt` (NEW) | Dependencies |
| `task_queue.py` | Add `on_task_complete` callback |
| `meeting_agent.py` | Wire Redis bridge in `main()` |

## Verification
1. Start agent → logs show "Redis bridge started" + agent registered
2. Run `python test-client/chat.py` → "Agent online" check passes
3. Type "hello" → immediate response (general skill, non-queued)
4. Type business query → queued → result appears in console
5. While task runs, send another → verify it queues then completes
6. Ctrl+C → clean exit
7. Stop agent → run test client → "Agent offline" warning

## Decisions
- Console REPL (no UI) — simplest possible test harness
- Reads Redis config from `../.env` (parent dir)
- Shares the main `.venv` (no separate venv)
- `msg_id` + `in_reply_to` for request-response correlation
- XREAD with 60s blocking timeout (no spin-wait)
- Stream message schema defined explicitly so agent bridge and test client agree
