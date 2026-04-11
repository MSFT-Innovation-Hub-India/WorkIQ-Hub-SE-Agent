# Project Guidelines

## Architecture

Hub SE Agent is a **single-process, multi-threaded Windows desktop agent** built with Python 3.12+. It combines a WebSocket server, pywebview UI, system tray icon, task queue, and optional Redis bridge for remote messaging.

| Component | File | Role |
|---|---|---|
| Agent core | `agent_core.py` | LLM router, skill loader, tool loader, Azure OpenAI Responses API client |
| Desktop host | `meeting_agent.py` | WebSocket server (port 18080), pywebview, tray icon, toast notifications |
| Task queue | `task_queue.py` | In-memory FIFO queue with single worker thread for business tasks |
| Email/calendar | `outlook_helper.py` | ACS email + `.ics` invite builder |
| Word doc gen | `tools/create_word_doc.py` | Create Word documents from agenda markdown using python-docx |
| Remote bridge | `redis_bridge.py` | Azure Managed Redis (Entra ID auth), stream-based inbox/outbox |
| Tray icon | `tray_icon.py` | Raw Win32 ctypes system tray with message pump |

See [README.md](../README.md) for the full architecture diagram and feature overview.

## Build and Run

```powershell
python -m venv .venv; .venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env   # fill in values
python meeting_agent.py # debug (with console)
pythonw meeting_agent.py # production (headless)
python agent.py         # console REPL, no UI
```

Required env vars: `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_CHAT_MODEL`, `AZURE_OPENAI_API_VERSION`, `ACS_ENDPOINT`, `ACS_SENDER_ADDRESS`, `AZURE_TENANT_ID`.

## Code Style

- Python 3.12+ type hints (`str | None`, `dict[str, str]`)
- Module-level private globals prefixed with `_`
- Logging via `logging.getLogger("hub_se_agent")`
- No linter or formatter configured — keep consistent with existing files

## Adding Skills and Tools

**New tool** — create `tools/<name>.py` exporting:
- `SCHEMA: dict` — OpenAI function JSON schema with `name`, `description`, `parameters`
- `handle(arguments: dict, *, on_progress=None, workiq_cli=None, **kwargs) -> str`

Tools are auto-discovered from `tools/*.py` (files starting with `_` are skipped).

**New skill** — create `skills/<name>.yaml` (or `skills/<group>/<name>.yaml` for grouped chains) with fields: `name`, `description`, `model` (`"full"` | `"mini"`), `conversational` (bool), `queued` (bool), `tools` (list), `instructions` (str). Optional: `next_skill` (str) for chaining.

Mark chained internal skills with `[INTERNAL` in `description` to exclude from routing.

Skills are auto-discovered recursively from `skills/**/*.yaml`. The router prompt is rebuilt automatically from all non-internal skill descriptions. Greetings and small talk are handled directly by the router (classified as `"none"`) without invoking a skill.

No restart needed when editing YAML skill instructions — but new files require a restart.

### Conversational skills

Set `conversational: true` when the skill needs multi-turn context (follow-up Q&A, human-in-the-loop confirmation). Conversation history is stored in `_conversation_histories[skill.name]`, bounded to 20 messages, and automatically cleared on fresh invocations (prevents stale context across different engagements).

### Human-in-the-loop confirmation pattern

To add a user confirmation checkpoint to a skill:

1. Set `conversational: true` — needed for turn detection via conversation history
2. Structure instructions as multi-turn: Turn 1 presents candidates + emits `[AWAITING_CONFIRMATION]`; Turn 2+ handles confirmation, corrections, or re-asks
3. `[AWAITING_CONFIRMATION]` in final text → `agent_core` sets `_active_session`, strips marker, returns to user without chaining. Router routes the user's next message back to the same skill.
4. Normal completion (no markers) → clears `_active_session`, chains to `next_skill` if configured
5. See `skills/hub-agenda-creation/engagement_briefing.yaml` for the reference implementation

### Skill chaining

Set `next_skill: <skill_name>` to auto-chain to the next phase on completion. Control flow markers:
- `[STOP_CHAIN]` — halt chain on errors, clear active session
- `[AWAITING_CONFIRMATION]` — pause for user input, do NOT chain until user confirms

## Conventions

- **OpenAI Responses API** — not Chat Completions. Tool-call loop uses `previous_response_id`.
- **Single shared credential** — `InteractiveBrowserCredential` in `agent_core.py`, shared via `set_credential()` / `get_credential()`.
- **WebSocket messages** — JSON with `type` field. Client sends `task`, `signin`, `clear_history`. Server sends `task_started`, `progress`, `task_complete`, `task_error`, `auth_status`, `skills_list`.
- **Request IDs** — every request gets `uuid.uuid4().hex[:8]`, used across WebSocket, UI, Redis.
- **Progress callback chain** — `on_progress(kind, message)` flows from `meeting_agent` → `agent_core` → tools.
- **Skill chaining gates** — If a skill's final text contains `[STOP_CHAIN]`, `agent_core` skips chaining to `next_skill` and clears any active session. Skills use this to gate on errors (e.g., no briefing calls found).
- **Human-in-the-loop confirmation** — If a skill's final text contains `[AWAITING_CONFIRMATION]`, `agent_core` sets `_active_session` with the skill name, strips the marker, and returns the text to the user without chaining. The router detects the active session and routes the user's next message back to the same skill. The skill (which must be `conversational: true`) checks its conversation history to determine it is on Turn 2+ and handles the confirmation. Once the skill completes normally (no marker), the active session is cleared and chaining proceeds.

## Pitfalls

- Azure auth must complete (user clicks Sign In) before any LLM or tool calls work
- `query_workiq` tool shells out to the `workiq` CLI binary — must be on PATH or set `WORKIQ_PATH`
- Windows-specific: `pythonw.exe`, `winotify`, Win32 ctypes tray. Mac support exists but is untested
- `scripts\stop.ps1` kills **all** `pythonw` processes, not just this agent
- Ports 18080 (WebSocket) and 18081 (HTTP) are hardcoded
- No automated tests — verification is manual via UI or `test-client/chat.py`
