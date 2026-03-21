# WorkIQ Agent

**Part 1 of 2** — This is the always-on desktop agent. It works in tandem with a companion cloud application (Part 2) that lets users interact with this agent **remotely from their mobile phones via Microsoft Teams**. Users can leave their computer, send requests from Teams, and receive completed results — including multi-step agentic workflows — without being at their desk.

Platforms like Claude CoWork and OpenClaw are defining the next wave of AI — autonomous agents that live on the user's local computer, act on their behalf, and integrate deeply with the tools they already use. The WorkIQ Agent  exhibits a few of their key design traits — **always-on local execution, skills-driven autonomy, and remote reachability** — applied to a specific class of Microsoft 365 workflow tasks that are commonly performed today and remain painstaking and cumbersome to do manually (resolving contacts across documents, cross-referencing agendas with email directories, sending batches of calendar invites). Like those platforms, the agent runs on the user's computer but can be reached from other channels: a user can message it from **Microsoft Teams on their phone**, ask questions, and have multi-step workflows executed autonomously — no manual intervention, no need to be at the desk.

---

## Functional Features

| Feature | Description |
|---|---|
| **Autonomous agentic execution** | State your intent in plain language. The agent orchestrates multi-step workflows end-to-end — deciding what data to fetch, what actions to take, and how to present the outcome — without further human input. |
| **Remote access via Microsoft Teams** | Send requests and receive responses from your phone through Teams. The agent processes the work locally on your machine and delivers the result back through Azure Managed Redis. |
| **FIFO task queue** | All business tasks are queued and processed one at a time. Queue multiple requests — they execute sequentially without interrupting each other. System queries (status checks, greetings) bypass the queue and respond instantly. |
| **Real-time task status** | Ask "what's the status of my request?" at any time — even while a long-running task is executing. The agent summarizes progress milestones from the live execution log. |
| **Concurrent request isolation** | Multiple requests (local + remote) are tracked independently. Each gets its own UI bubble and progress stream — no cross-talk. |
| **Skills-driven extensibility** | Each capability is a declarative YAML file. Add a new skill by dropping a YAML file into `skills/` — no code changes, no redeployment. |
| **Background operation** | Runs invisibly via `pythonw.exe` — no console window, no taskbar clutter until you summon it. |
| **Global hotkey** | Press **Ctrl+Alt+M** anywhere to toggle the chat UI. |
| **Toast notifications** | Native Windows 10/11 toasts for task progress and completion. Click a toast to open the UI. |
| **Intelligent routing** | A master router classifies every request and delegates to the appropriate skill-specific sub-agent. |
| **Adaptive model selection** | Complex workflows use a full LLM (`gpt-5.2`); Q&A and general responses use a smaller, faster model (`gpt-5.4-mini`) for cost-efficient responsiveness. |
| **Markdown-rendered responses** | Tables, code blocks, lists, and headings rendered natively in the chat UI. Progress updates render as formatted markdown with structured step indicators. |
| **Persistent authentication** | Sign in once; tokens are cached and silently refreshed across restarts via Azure Identity with persistent token cache. |
| **Auto-start at Windows login** | An install script registers the assistant to launch at startup. |

---

## Key Technical Capabilities

| Capability | Implementation |
|---|---|
| **Azure OpenAI Responses API** | The agentic core — tool definitions and natural-language instructions drive autonomous tool-call orchestration. No custom workflow code or state machines. |
| **Azure Managed Redis (cluster mode)** | Inbox/outbox streams keyed by user email. Passwordless Entra ID authentication via `redis-entraid` credential provider with automatic token refresh. |
| **Task queue with request classification** | Skills declare `queued: true/false`. Business tasks queue in FIFO; system tasks (status, greetings) execute immediately. Each task carries full progress logs for status reporting. |
| **Composable tool system** | Tools are self-contained Python modules in `tools/` — discovered and registered at startup via `importlib`. Add a tool by dropping a `.py` file. |
| **Composable skill system** | Skills are YAML files in `skills/` — discovered at startup. The router prompt is auto-generated from skill descriptions. Add a skill by dropping a `.yaml` file. |
| **Request-ID based concurrency** | Every request gets a unique ID. All WebSocket messages, UI bubbles, and Redis correlation use this ID for complete task isolation. |
| **Shared credential architecture** | A single `InteractiveBrowserCredential` instance (with cached `AuthenticationRecord`) is shared across OpenAI, WorkIQ, ACS, and Redis — one sign-in, zero command prompts. |

---

## The Two-Part Architecture

```
  ┌──────────────────────────────────┐       ┌──────────────────────────────────┐
  │         Part 2 (Cloud)           │       │        Part 1 (Desktop)          │
  │                                  │       │   ← THIS REPOSITORY →            │
  │   Microsoft Teams                │       │                                  │
  │     ↕                            │       │   WorkIQ Agent                   │
  │   Teams Relay Service            │       │     • Skills-driven sub-agents   │
  │     ↕                            │       │     • FIFO task queue            │
  │   Azure Managed Redis            │◄─────►│     • Redis bridge (inbox/outbox)│
  │     inbox:{email}                │       │     • Tool execution layer       │
  │     outbox:{email}               │       │     • Local chat UI (pywebview)  │
  │     agents:{email}               │       │     • Toast notifications        │
  └──────────────────────────────────┘       └──────────────────────────────────┘
```

**Part 1** (this repo) is the agent itself — running on a Windows 11 laptop, processing tasks locally with full access to the user's Microsoft 365 data via WorkIQ. It registers its presence in Azure Managed Redis and polls an inbox stream for remote requests.

**Part 2** (separate repo) is a cloud service that bridges Microsoft Teams to the Redis streams. When a user sends a message in Teams, the relay service pushes it to the agent's Redis inbox. When the agent writes a result to the outbox, the relay delivers it back to the Teams conversation.

The user experience: send a message from your phone in Teams → the agent on your laptop picks it up, executes the full agentic workflow (retrieving M365 data, calling tools, orchestrating multi-step actions) → the result appears in your Teams chat.

---

## A Heterogeneous Agentic Solution

This agent bridges two distinct pillars of the Microsoft AI stack:

- **Microsoft 365 Copilot & WorkIQ** (part of the [Microsoft Intelligence](https://www.microsoft.com/en-us/microsoft-365) suite) — the productivity platform that surfaces enterprise knowledge from calendars, emails, documents, contacts, and SharePoint.
- **Azure AI Foundry with Azure OpenAI Responses API** — the code-first agentic platform that builds autonomous, tool-calling agents with nothing more than tool definitions and natural-language instructions.

WorkIQ provides the **data and enterprise context**. Azure OpenAI Responses API provides the **autonomous reasoning and orchestration**. The result is an agent that understands intent, retrieves live Microsoft 365 data, and acts on it through multi-step tool-calling workflows — without custom orchestration code.

WorkIQ alone answers questions but cannot execute multi-step actions. Azure OpenAI alone can reason but has no access to enterprise data. Together, they form an agent that both *knows* and *acts*.

---

## Built-in Skills

WorkIQ Agent is **skills-driven** — each capability is a declarative YAML file rather than hardcoded logic. Skills are discovered at startup; the router prompt is auto-built from their descriptions.

| Skill | Model | Queued | Tools | What it does |
|---|---|---|---|---|
| **Meeting Invites** | full (`gpt-5.2`) | Yes | `query_workiq`, `log_progress`, `create_meeting_invites` | Autonomous workflow: retrieve agenda → filter speakers → resolve emails → send calendar invites |
| **Q&A** | mini (`gpt-5.4-mini`) | Yes | `query_workiq`, `log_progress` | Conversational Q&A about M365 data with session history |
| **Email Summary** | mini (`gpt-5.4-mini`) | Yes | `query_workiq`, `log_progress` | Summarize unread/recent emails, highlight items needing attention |
| **Task Status** | mini (`gpt-5.4-mini`) | No | `get_task_status` | Report current task progress and queue depth — responds instantly even while a task is running |
| **General** | mini (`gpt-5.4-mini`) | No | *(none)* | Greetings and small talk — no data lookup |

**Queued = Yes**: task enters the FIFO queue and executes when its turn comes.
**Queued = No**: task executes immediately, bypassing the queue.

### Adding a new skill

For skills using existing tools — **no Python code required**:

1. Create a `.yaml` file in `skills/`
2. Define `name`, `description`, `model`, `queued`, `tools`, and `instructions`
3. Restart the agent — auto-discovered, router starts routing matching requests

For skills needing a new tool:

1. Create a `.py` file in `tools/` with `SCHEMA` dict and `handle()` function
2. Reference the tool by name in the skill's `tools:` list
3. Restart — both are auto-discovered

### Skills-Driven Architecture

The meeting invites skill illustrates how a complex multi-step autonomous workflow is defined entirely in YAML — the full five-step sequence (retrieve agenda → filter speakers → resolve emails → send invites → report results) is expressed as natural-language instructions with zero Python orchestration code:

```yaml
# skills/meeting_invites.yaml
name: meeting_invites
description: >
  Send or create calendar invites and meeting invitations to speakers or
  presenters from an agenda document or event. Keywords: invite, calendar,
  schedule speakers, send invites, agenda, engagement.
model: full              # "full" → gpt-5.2 (complex reasoning)
conversational: false    # no follow-up context needed

tools:
  - query_workiq
  - log_progress
  - create_meeting_invites

instructions: |
  You are an autonomous Hub Engagement Speaker Schedule Management Agent.

  Given a user request about a customer engagement event, you MUST complete
  ALL of the following steps using tool calls — do NOT stop or return text
  to the user until every step is done.

  STEP 1: Call query_workiq to retrieve the COMPLETE agenda document. Ask
  for: EVERY row in the agenda table including topic names, speaker names,
  and time slots for each session. ...

  STEP 2: From the COMPLETE list of rows, identify ALL Microsoft employee
  speakers. Apply these rules:
  DISCARD rows that are:
  - Lunch breaks, tea breaks, coffee breaks, or any kind of break
  - Rows with no topic or no speaker assigned
  - Rows where the speaker is ONLY a team name or company name
  KEEP rows where:
  - The speaker is a clearly identifiable individual person's name
  ...

  STEP 3: Call query_workiq ONCE to look up the Microsoft corporate email
  addresses of ALL the individual speakers identified in Step 2.
  ...

  STEP 4: Call create_meeting_invites with the curated list of sessions,
  including each speaker's email address.
  ...

  STEP 5: After the invites are created, present the user with a final
  summary table showing: Topic, Speaker, Time Slot, Email, and Status.
  ...

  IMPORTANT:
  - Complete ALL steps autonomously in a single turn.
  - Always call log_progress after each query_workiq call.
  - If a speaker appears in multiple sessions, create a separate invite
    for each session.
```

The **entire five-step workflow is expressed as natural-language instructions**. No Python code for step sequencing, conditional logic, or state management. The Responses API reads these instructions and autonomously orchestrates the tool calls.

| Field | Purpose |
|---|---|
| `name` | Unique identifier — what the router returns when it classifies a request |
| `description` | Natural-language description used by the router to match user intent |
| `model` | `full` for complex reasoning (e.g., meeting invites), `mini` for Q&A and summarization |
| `queued` | `true` → enters FIFO task queue; `false` → executes immediately (system tasks) |
| `conversational` | Whether to maintain session history for follow-up questions |
| `tools` | List of tool names this skill can use (must exist in the tool registry) |
| `instructions` | The complete system prompt — all the Responses API needs to orchestrate the workflow |

> **Note on calendar invite delivery:** This sample uses **Azure Communication Services (ACS)** to send meeting invites via email with `.ics` attachments. Replacing the ACS-based delivery with the **WorkIQ Outlook MCP Server** (for creating events directly in Outlook) would require only swapping the `create_meeting_invites` tool implementation — no changes to agent instructions or orchestration logic.

---

## Architecture

```
┌────────────────────────────────────────────────────────────────────────────┐
│                           Windows 11 Desktop                               │
│                                                                            │
│  ┌─────────────────────┐    ┌────────────────────────────────────────────┐ │
│  │  pywebview Window   │◄──►│  WebSocket Server (ws://18080)             │ │
│  │  (chat_ui.html)     │    │  HTTP Server     (http://18081)            │ │
│  │                     │    │                                            │ │
│  │ • Markdown rendering│    │  ┌──────────────────┐  ┌────────────────┐  │ │
│  │ • Progress steps    │    │  │  Tool Loader     │  │  Skill Loader  │  │ │
│  │ • Remote msg bubbles│    │  │  tools/*.py      │  │  skills/*.yaml │  │ │
│  │ • Queue status      │    │  └────────┬─────────┘  └───────┬────────┘  │ │
│  │ • Auth banner       │    │           │                    │           │ │
│  └─────────────────────┘    │  ┌────────▼────────────────────▼─────────┐ │ │
│                             │  │          Router (Master Agent)        │ │ │
│  ┌────────────────────┐     │  │          Azure OpenAI gpt-5.2         │ │ │
│  │ Global Hotkey      │     │  │   (prompt auto-built from skill       │ │ │
│  │ (Ctrl+Alt+M)       │     │  │    descriptions)                      │ │ │
│  └────────────────────┘     │  └───────────────┬───────────────────────┘ │ │
│                             │                  │ classifies intent       │ │
│  ┌─────────────────────┐    │         ┌────────▼────────┐                │ │
│  │ Toast Notifications │    │         │   Request       │                │ │
│  │ (winotify)          │    │         │   Classifier    │                │ │
│  └─────────────────────┘    │         │  queued: true?  │                │ │
│                             │         └───┬─────────┬───┘                │ │
│                             │             │         │                    │ │
│                             │     ┌───────▼──┐  ┌───▼──────────────┐     │ │
│                             │     │   FIFO   │  │ Immediate exec   │     │ │
│                             │     │   Task   │  │ (general, status)│     │ │
│                             │     │   Queue  │  └──────────────────┘     │ │
│                             │     └───┬──────┘                           │ │
│                             │         │ one at a time                    │ │
│                             │  ┌──────▼───────────────────────────────┐  │ │
│                             │  │  Skill Sub-Agent Execution           │  │ │
│                             │  │  (model + tools + instructions)      │  │ │
│                             │  │  Azure OpenAI Responses API          │  │ │
│                             │  │  • Autonomous tool-call orchestration│  │ │
│                             │  │  • No custom workflow code           │  │ │
│                             │  └──────┬───────────────┬───────────────┘  │ │
│                             │         │               │                  │ │
│                             │  ┌──────▼──────┐ ┌──────▼──────────────┐   │ │
│                             │  │ Tool Layer  │ │ Progress Broadcast  │   │ │
│                             │  │ query_workiq│ │ → UI (WebSocket)    │   │ │
│                             │  │ log_progress│ │ → Toast notification│   │ │
│                             │  │ create_mtg  │ │ → Progress log      │   │ │
│                             │  │ get_status  │ │                     │   │ │
│                             │  └──────┬──────┘ └─────────────────────┘   │ │
│                             └─────────┼──────────────────────────────────┘ │
│                                       │                                    │
│  ┌────────────────────────────────────▼──────────────────────────────────┐ │
│  │                      Redis Bridge (optional)                          │ │
│  │  • Polls workiq:inbox:{email} for remote messages                     │ │
│  │  • Writes results to workiq:outbox:{email}                            │ │
│  │  • Registers workiq:agents:{email} with TTL heartbeat                 │ │
│  │  • Entra ID auth via shared InteractiveBrowserCredential              │ │
│  │  • RedisCluster with redis-entraid credential_provider                │ │
│  └───────────────────────────┬───────────────────────────────────────────┘ │
└──────────────────────────────┼─────────────────────────────────────────────┘
                               │
              ┌────────────────▼─────────────────┐
              │    Azure Managed Redis           │
              │    (cluster mode, Entra ID)      │
              │    inbox / outbox / agents keys  │
              └────────────────┬─────────────────┘
                               │
              ┌────────────────▼─────────────────┐
              │    Part 2: Teams Relay Service   │
              │    (companion cloud app)         │
              └──────────────────────────────────┘

              ┌──────────────────────────────────┐
              │    WorkIQ CLI → M365 Graph API   │
              │    Calendar · Email · Files ·    │
              │    Contacts · SharePoint         │
              └──────────────────────────────────┘

              ┌─────────────────────────────────┐
              │    Azure Communication Services │
              │    (calendar invite email)      │
              └─────────────────────────────────┘
```

### How It All Fits Together

1. **Single-process launcher** (`meeting_agent.py`) — Entry point. Starts WebSocket/HTTP servers, registers the global hotkey, configures the task queue, optionally starts the Redis bridge, shows a startup toast, and enters the pywebview event loop.

2. **WebSocket server** (port `18080`) — Communication backbone between the chat UI and the Python backend. User messages, agent responses, progress updates, auth status, queue notifications, and remote message alerts all flow over this channel as JSON.

3. **HTTP server** (port `18081`) — Handles toast notification clicks. When the user clicks a toast, Windows opens `http://127.0.0.1:18081/show`, which brings up the pywebview window.

4. **pywebview window** — Renders `chat_ui.html`. Starts hidden; close hides rather than quits. The `activeBubbles` Map tracks each concurrent request by `request_id` for complete isolation.

5. **Tool Loader** — Discovers all `.py` files in `tools/` via `importlib` at startup. Each module exports a `SCHEMA` dict and `handle()` function. Adding a tool requires only dropping a Python file.

6. **Skill Loader** — Discovers all `.yaml` files in `skills/` at startup. Parses each into a runtime `Skill` object and auto-builds the router prompt from their descriptions.

7. **Router (Master Agent)** — Classifies every request into a skill name via LLM call. Also resolves the `queued` flag to determine whether the request enters the task queue or executes immediately.

8. **Task Queue** — In-memory FIFO queue with a dedicated worker thread. Business tasks (`queued: true`) execute one at a time. System tasks (`queued: false` — status queries, greetings) bypass the queue and respond instantly. Each task carries a full progress log for status reporting.

9. **Skill Sub-Agents** — Each skill operates with its own system prompt, tool set, and model tier:
   - **Meeting Invites** — `gpt-5.2`. Autonomous five-step workflow.
   - **Q&A** — `gpt-5.4-mini` with conversation history.
   - **Email Summary** — `gpt-5.4-mini`. Email triage and prioritization.
   - **Task Status** — `gpt-5.4-mini`. Reports live progress from execution logs.
   - **General** — `gpt-5.4-mini`. Greetings and small talk.

10. **Azure OpenAI Responses API** — The agentic core. Tool definitions and natural-language instructions drive autonomous tool-call orchestration. No custom workflow code — multi-step behavior emerges from the instructions alone.

11. **Tool execution layer** — Self-contained Python modules in `tools/`:
    - `query_workiq` — Runs the WorkIQ CLI to query Microsoft 365 data.
    - `log_progress` — Sends structured progress updates (rendered as markdown in the UI).
    - `create_meeting_invites` — Constructs `.ics` calendar invites, delivers via ACS.
    - `get_task_status` — Returns current task progress and queue depth.

12. **Redis Bridge** (optional) — Connects the desktop agent to Azure Managed Redis for remote task delivery:
    - **Inbox poller** — Background thread polls `workiq:inbox:{email}` via `XREAD` (5s blocking). Remote messages are submitted to the task queue and shown in the UI as purple "remote" bubbles.
    - **Outbox writer** — On task completion, writes results to `workiq:outbox:{email}` with `in_reply_to` correlation for request-response matching.
    - **Agent registration** — Sets `workiq:agents:{email}` with TTL, refreshed by a heartbeat every 30 minutes. Remote clients check this key to verify the agent is online.
    - **Authentication** — Shares the agent's `InteractiveBrowserCredential` (with cached auth record for silent refresh), wrapped in `redis-entraid`'s `EntraIdCredentialsProvider`. No `DefaultAzureCredential` chain — no command windows on Windows.

---

## Technical Details

### Authentication Flow

A single `InteractiveBrowserCredential` from Azure Identity SDK is shared across all components — OpenAI, WorkIQ, ACS, and Redis:

1. **First launch** — The UI shows a "Not signed in" banner. Click **Sign In** to open a browser for Entra ID authentication.
2. **Token caching** — The `AuthenticationRecord` is serialized to `~/.workiq-assistant/auth_record.json`. The token cache is persisted via Windows Credential Manager.
3. **Subsequent launches** — The saved record enables silent token refresh — no browser prompt.
4. **Token refresh** — The OpenAI client checks expiry with a 5-minute buffer. If silent refresh fails, it falls back to interactive browser login.
5. **Shared credential** — The same credential instance is shared with `outlook_helper.py` (via `set_credential()`) and with the Redis bridge (via `get_credential()`). This avoids duplicate browser prompts and prevents `DefaultAzureCredential` from spawning `az` CLI subprocesses under `pythonw.exe`.

### WebSocket Communication Protocol

| Direction | Message Type | Purpose |
|---|---|---|
| Server → Client | `auth_status` | Sign-in state and user identity |
| Client → Server | `task` | User submits a request |
| Server → Client | `task_queued` | Request added to queue (includes position) |
| Server → Client | `task_started` | Processing has begun (includes `request_id` and `source`) |
| Server → Client | `progress` | Real-time updates (kind: `step`, `tool`, `progress`, `agent`) |
| Server → Client | `task_complete` | Final agent response with Markdown content |
| Server → Client | `task_error` | Error message |
| Server → Client | `remote_message` | Remote message arrived (sender + text, shown as purple bubble) |
| Client → Server | `signin` | User clicks Sign In |
| Server → Client | `signin_status` | Result of sign-in attempt |
| Client → Server | `clear_history` | Reset Q&A conversation history |
| Server → Client | `skills_list` | Loaded skills for the UI skills panel |

All messages include a `request_id` field for concurrent task isolation.

### Redis Streams Schema

| Stream | Direction | Fields |
|---|---|---|
| `workiq:inbox:{email}` | Remote → Agent | `sender`, `text`, `ts`, `msg_id` |
| `workiq:outbox:{email}` | Agent → Remote | `task_id`, `status`, `text`, `ts`, `in_reply_to` |
| `workiq:agents:{email}` | Agent → Cloud | JSON: `{name, email, started_at, version}` with TTL |

The `in_reply_to` field correlates outbox responses to inbox `msg_id` values, enabling request-response matching for remote clients.

### Window Management

- pywebview window starts **hidden**. Close hides rather than quits.
- **Global hotkey** (`Ctrl+Alt+M`) via `pynput.keyboard.GlobalHotKeys`.
- **Toast click** opens `http://127.0.0.1:18081/show` to bring up the window.
- **Custom taskbar icon** via `SetCurrentProcessExplicitAppUserModelID` + `WM_SETICON` to override default `pythonw.exe` grouping.

### Subprocess Handling

All subprocess calls use `subprocess.CREATE_NO_WINDOW` on Windows to prevent `cmd.exe` windows from flashing during WorkIQ CLI invocations.

### Logging

All logs are written to `~/.workiq-assistant/agent.log` — routing decisions, tool calls, task queue operations, Redis bridge events, and authentication.

---

## Project Structure

```
workiq-assistant/
├── meeting_agent.py       # Main entry point — launcher, WebSocket/HTTP servers,
│                          #   pywebview window, hotkey, toast, task queue + Redis wiring
├── agent_core.py          # Core agent logic — router, skill loader, tool loader,
│                          #   auth helpers, shared credential (no hardcoded tools/skills)
├── task_queue.py           # FIFO task queue — worker thread, request classification,
│                          #   progress capture, status API, on_task_complete callback
├── redis_bridge.py        # Azure Managed Redis bridge — inbox poller, outbox writer,
│                          #   agent presence registration, Entra ID credential_provider
├── agent.py               # Console entry point — terminal-based interaction for
│                          #   development and debugging (no UI, no background mode)
├── outlook_helper.py      # Azure Communication Services — .ics calendar invite
│                          #   construction, email delivery, organizer resolution
├── chat_ui.html           # Chat UI — Markdown rendering, progress steps, remote
│                          #   message bubbles, queue indicators, concurrent task isolation
├── .env / .env.example    # Environment configuration (Azure endpoints, models, Redis)
├── requirements.txt       # Python dependencies
├── tools/                 # Tool modules (Python) — loaded dynamically at startup
│   ├── query_workiq.py       # Query M365 data via WorkIQ CLI
│   ├── log_progress.py       # Real-time progress updates (rendered as markdown)
│   ├── create_meeting_invites.py  # Build .ics invites, send via ACS
│   └── get_task_status.py    # Report current task progress and queue depth
├── skills/                # Skill definitions (YAML) — loaded dynamically at startup
│   ├── meeting_invites.yaml  # Autonomous meeting invite workflow (full model, queued)
│   ├── qa.yaml               # Conversational Q&A via WorkIQ (mini model, queued)
│   ├── email_summary.yaml    # Email summarization (mini model, queued)
│   ├── task_status.yaml      # Task/queue status reporting (mini model, immediate)
│   └── general.yaml          # Greetings and small talk (mini model, immediate)
├── test-client/           # Console REPL test client — simulates remote sender via Redis
│   ├── chat.py               # Push to inbox, read from outbox, request-response correlation
│   └── requirements.txt      # redis, redis-entraid, azure-identity, python-dotenv
├── scripts/
│   ├── start.ps1          # Start the assistant (detached, via pythonw.exe)
│   ├── stop.ps1           # Stop all running instances
│   └── autostart.ps1      # Install/uninstall auto-start at Windows login
├── experimental/
│   └── test_graph_calendar.py  # Microsoft Graph calendar API test script
├── user-stories/          # Planning documents for task queue and Redis bridge features
├── favicon.svg            # App icon (SVG) — inline in HTML
├── agent_icon.png         # App icon (PNG) — toast notifications
└── agent_icon.ico         # App icon (ICO) — taskbar
```

---

## Getting Started

### Prerequisites

- **Windows 11** laptop
- **Python 3.12+** with a virtual environment
- **WorkIQ CLI** installed and on PATH (or path set in `.env`)
- **Azure OpenAI** resource with `gpt-5.2` and `gpt-5.4-mini` model deployments
- **Azure Communication Services** resource for sending email invites
- **Azure Managed Redis** (optional) — for remote task delivery via Teams. Requires Entra ID authentication (passwordless, no API keys).

### Installation

```powershell
# Clone the repository
git clone <repo-url>
cd workiq-assistant

# Create virtual environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# Install dependencies
pip install -r requirements.txt

# Configure environment
copy .env.example .env
# Edit .env with your Azure endpoints, model names, tenant ID, and ACS settings
```

### Running the App

#### From PowerShell (recommended)

```powershell
# Start (runs invisibly in the background)
.\scripts\start.ps1

# Stop
.\scripts\stop.ps1
```

#### Without VS Code

The app does not require VS Code. To run it directly from any PowerShell or Command Prompt:

```powershell
# Start invisibly (no console window)
Start-Process -FilePath .\.venv\Scripts\pythonw.exe -ArgumentList "meeting_agent.py" -WorkingDirectory (Get-Location) -WindowStyle Hidden

# Or for debugging (with console output)
.\.venv\Scripts\python.exe meeting_agent.py
```

#### Auto-Start at Windows Login

```powershell
# Install auto-start (creates a VBScript in the Windows Startup folder)
.\scripts\autostart.ps1 install

# Remove auto-start
.\scripts\autostart.ps1 uninstall
```

This places a `WorkIQAssistant.vbs` launcher in `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup`, which starts the assistant silently at every Windows login.

---

## Testing Remote Task Delivery with the Test Client

The `test-client/` folder contains a **console REPL** that simulates a remote sender (like a Teams relay service) by talking to the agent through the same Azure Managed Redis streams. This lets you validate the full remote-task pipeline — inbox delivery, task queue processing, outbox response — without deploying the companion cloud application.

### Prerequisites

- The agent must be **running** (via `.\scripts\start.ps1`)
- `AZ_REDIS_CACHE_ENDPOINT` must be set in `.env`
- The test client reuses the agent's `.env` (loaded from the parent directory) and its saved auth record from `~/.workiq-assistant/auth_record.json`

### Running the test client

```powershell
# From the project root (uses the same .venv as the agent)
.\.venv\Scripts\Activate.ps1
python test-client\chat.py
```

On startup, the test client:

1. **Authenticates** — Reuses the agent's cached Entra ID auth record for silent token acquisition
2. **Connects to Redis** — Same Azure Managed Redis cluster as the agent, with `redis-entraid` credential provider
3. **Checks agent status** — Reads `workiq:agents:{email}` to verify the agent is online and shows agent info
4. **Enters the REPL** — Prompts `You >` for input

### What to test

| Test | What happens |
|---|---|
| Type `hello` | Message pushed to `workiq:inbox:{email}` → agent picks it up → routes to `general` skill (non-queued) → response appears in the test client console AND the agent's local chat UI shows a purple "remote" bubble |
| Type a business query (e.g., `summarize my recent emails`) | Message queued as a business task → agent processes it → response written to `workiq:outbox:{email}` → test client displays the result |
| Send a second request while the first is running | The second task queues at position 2. The test client blocks waiting for its specific `in_reply_to` correlation match. |
| Ask `what is the status of my request?` from the **local chat UI** while a remote task runs | Responds immediately with progress milestones (bypasses queue via `task_status` skill) |

### How it works

```
  test-client (console)              Azure Managed Redis              WorkIQ Agent
  ──────────────────────             ────────────────────             ──────────────
        │                                    │                              │
        │── XADD inbox:{email} ─────────────►│                              │
        │   {sender, text, msg_id}           │                              │
        │                                    │◄──── XREAD inbox:{email} ────│
        │                                    │      (5s blocking poll)      │
        │                                    │                              │
        │                                    │      task_queue.submit()     │
        │                                    │      skill execution...      │
        │                                    │                              │
        │                                    │◄──── XADD outbox:{email} ────│
        │                                    │      {task_id, status, text, │
        │◄── XREAD outbox:{email} ────────── │       in_reply_to: msg_id}   │
        │    match in_reply_to == msg_id     │                              │
        │                                    │                              │
        │    print response                  │                              │
```

The `msg_id` → `in_reply_to` correlation ensures the test client matches each response to its original request, even when multiple messages are in flight.

### Exiting

Press **Ctrl+C** or type `exit` to disconnect cleanly.

---

## Configuration

All configuration is in the `.env` file:

| Variable | Description |
|---|---|
| `AZURE_OPENAI_ENDPOINT` | Azure OpenAI resource endpoint |
| `AZURE_OPENAI_CHAT_MODEL` | Full model for router + complex workflows (e.g., `gpt-5.2`) |
| `AZURE_OPENAI_CHAT_MODEL_SMALL` | Mini model for Q&A + general responses (e.g., `gpt-5.4-mini`) |
| `AZURE_OPENAI_API_VERSION` | API version (e.g., `2025-03-01-preview`) |
| `AZURE_TENANT_ID` | Azure AD tenant ID |
| `AZURE_SUBSCRIPTION_ID` | Azure subscription ID |
| `ACS_ENDPOINT` | Azure Communication Services endpoint |
| `ACS_SENDER_ADDRESS` | Verified sender email address for ACS |
| `AZ_REDIS_CACHE_ENDPOINT` | (Optional) Azure Managed Redis endpoint (`host:port`). Enables remote task delivery. |
| `REDIS_SESSION_TTL_SECONDS` | (Optional) Agent presence TTL in seconds (default: `86400`) |
| `AGENT_TIMEZONE` | (Optional) IANA timezone override (auto-detected if omitted) |
| `WORKIQ_PATH` | (Optional) Full path to WorkIQ CLI if not on PATH |

**Redis is optional.** If `AZ_REDIS_CACHE_ENDPOINT` is not set, the agent runs in local-only mode — all features work except remote task delivery.

---

## Dependencies

| Package | Purpose |
|---|---|
| `openai` | Azure OpenAI Responses API client |
| `azure-identity` | Azure AD authentication with persistent token cache |
| `azure-communication-email` | Sending calendar invites via ACS |
| `python-dotenv` | Loading `.env` configuration |
| `pywebview` | Native desktop window for the chat UI |
| `websockets` | WebSocket server for UI ↔ backend communication |
| `pynput` | Global keyboard hotkey listener |
| `winotify` | Windows 10/11 native toast notifications |
| `pyyaml` | YAML parsing for skill definitions |
| `tzlocal` | Auto-detection of the system timezone |
| `redis` | Redis client (cluster mode support) |
| `redis-entraid` | Entra ID credential provider for passwordless Redis authentication |
