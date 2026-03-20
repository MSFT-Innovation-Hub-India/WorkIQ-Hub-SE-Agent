# WorkIQ Agent

An always-on, background-running AI assistant for Windows 11 that autonomously completes tasks against your Microsoft 365 data — so you can delegate work, walk away, and come back to results.

---

## The Problem

Every customer engagement, every offsite, every internal workshop starts the same way: someone creates an agenda document — a Word file or a spreadsheet — listing sessions, speakers, topics, and time slots. And then the tedious part begins.

Someone has to go through that document row by row, look up each speaker's email address, open Outlook, create a meeting invite with the correct time slot and topic, and send it. Repeat for every speaker. Blocking the entire day for all speakers is not practical — each person needs an invite for just their session. For a 15-session agenda, that is 15 individual meeting invites, each with the right time, the right topic, and the right recipient.

This is not a rare edge case. **This is an everyday problem** across every organization that runs events, workshops, or customer engagements. And today, it is solved entirely by manual effort — even with all the productivity tools available in Microsoft 365.

**WorkIQ Agent solves this in a single sentence.** The user describes the task in natural language — *"Send meeting invites to all speakers for the upcoming Zava engagement based on the agenda"* — and the agent does the rest: retrieves the agenda document, identifies the speakers, looks up their email addresses, and sends each one a calendar invite for their specific session. No manual steps. No human in the loop.

While this meeting invite workflow is the showcase capability, the underlying architecture is designed to be **easily extensible**. Adding a new automation requires only defining a tool and writing natural-language instructions — no workflow code, no state machines. Thanks to the combined capabilities of WorkIQ for enterprise data access and the Azure OpenAI Responses API on Azure AI Foundry for autonomous orchestration, this agent can be extended to solve many such common everyday problems without significant engineering investment.

---

## A Heterogeneous Agentic Solution

This agent bridges two distinct pillars of the Microsoft AI stack:

- **Microsoft 365 Copilot & WorkIQ** (part of the [Microsoft Intelligence](https://www.microsoft.com/en-us/microsoft-365) suite) — the modern work and productivity platform that surfaces enterprise knowledge from calendars, emails, documents, contacts, and SharePoint through natural-language queries.
- **Azure AI Foundry with Azure OpenAI Responses API** — the code-first agentic AI platform that enables developers to build autonomous, tool-calling agents with nothing more than tool definitions and natural-language instructions.

WorkIQ Agent combines these two worlds into a single solution: WorkIQ provides the **data and enterprise context**, while Azure OpenAI Responses API provides the **autonomous reasoning and orchestration**. The result is an agent that can understand a user's intent, retrieve live Microsoft 365 data through WorkIQ, and act on it through multi-step tool-calling workflows — all without custom orchestration code.

This heterogeneous approach unlocks capabilities that neither platform achieves in isolation. WorkIQ alone answers questions but cannot execute multi-step actions. Azure OpenAI alone can reason and orchestrate but has no access to enterprise data. Together, they form an agent that both *knows* and *acts*.

---

## What It Does

WorkIQ Agent is designed to run perpetually on a Windows 11 laptop. It lives in the background — no window, no taskbar clutter — and is summoned with a keyboard shortcut whenever you need it. You assign a task, close the window, and continue with your day. When the task is complete, a Windows toast notification appears. Clicking the toast brings up the results.

The Agent exhibits **autonomous agentic execution**: the user states an intent in natural language, and the agent orchestrates a multi-step workflow end-to-end without any further human intervention. The agent decides what data to fetch, what sequence of actions to take, and how to present the outcome — all in one shot.

### Functional Features

| Feature | Description |
|---|---|
| **Background operation** | Runs invisibly via `pythonw.exe` — no console window, no taskbar icon until you summon it. |
| **Global hotkey** | Press **Ctrl+Alt+M** anywhere to toggle the chat UI. |
| **Toast notifications** | Native Windows 10/11 toast notifications for task progress and completion. Clicking a toast opens the UI directly. |
| **Autonomous task completion** | Assign a task, minimize or close the window — the agent finishes the work in the background. |
| **Intelligent routing** | A master router agent classifies every request and delegates it to the appropriate sub-agent. |
| **Q&A Agent** | Ask natural-language questions about your Microsoft 365 data — calendar, emails, documents, contacts — powered by WorkIQ. Maintains conversation history for follow-up questions. |
| **Meeting Invite Agent** | Given a customer engagement agenda document, the agent autonomously retrieves the full agenda, identifies all speakers, resolves their email addresses, and sends calendar invites — all without user intervention. |
| **Adaptive model selection** | The router and Meeting Invite Agent use a full LLM (`gpt-5.2`) for complex reasoning, while the Q&A Agent and general responses use a smaller, faster model (`gpt-5.4-mini`) for cost-efficient responsiveness. |
| **Markdown-rendered responses** | Agent responses are rendered with full Markdown support — tables, code blocks, lists, headings. |
| **Persistent authentication** | Sign in once through the browser; tokens are cached and silently refreshed across app restarts. |
| **Auto-start at Windows login** | An install script registers the assistant to launch automatically at Windows startup. |

---

## The Meeting Invite Agent — Autonomous Multi-Step Workflow

This task **cannot be accomplished today using Microsoft 365 Copilot Chat or Copilot Cowork directly**. It would require multiple iterations with the available tools to complete it. When a user says something like:

> *"Refer to the Agenda Word document created in the last 5 days for the upcoming Customer Engagement with Zava. Send meeting invites to all speakers for based on their topics and timing"*

The agent executes the following sequence **entirely on its own**, with no further user input:

1. **Retrieve the agenda** — Calls WorkIQ to fetch the complete agenda document from the user's Microsoft 365 environment, extracting every row: time slots, topic names, and speaker names.
2. **Filter speakers** — Parses the agenda, discards breaks, TBD entries, team names, and non-individual entries. Identifies every named speaker.
3. **Resolve email addresses** — Calls WorkIQ again with the full list of speaker names to look up their Microsoft corporate email addresses.
4. **Send calendar invites** — Uses Azure Communication Services to email each speaker a proper `.ics` calendar invite with the correct time slot, topic details, and the user as the organizer. Recipients see it as a standard Outlook meeting request with Accept/Decline buttons.
5. **Report results** — Presents a summary table showing every invite sent, with status.

This is powered by the **Azure OpenAI Responses API** — true agentic AI. The main agent calls the Meeting Invite Agent only once with natural-language instructions. The Responses API autonomously orchestrates the entire tool-call loop: it decides which tool to call next, interprets the results, and chains them into subsequent tool calls until the workflow is complete.

> At the time of this writing, direct access to the WorkIQ Outlook Calendar API is not available, so Azure Communication Services is used to deliver the calendar invites via email instead.

---

## Azure OpenAI Responses API — The Agentic Core

The autonomous behavior of this solution is powered entirely by the **Azure OpenAI Responses API**. This is what makes the agent truly agentic rather than a scripted workflow.

The Responses API accepts a set of **tool definitions** and **natural-language instructions**, then autonomously decides which tools to call, in what order, and how to interpret the results — looping through tool calls until the task is complete. The application code does not contain any orchestration logic, conditional branching, or step sequencing for the multi-step workflows. It simply:

1. Defines the available tools (`query_workiq`, `log_progress`, `create_meeting_invites`)
2. Provides natural-language instructions describing the desired outcome
3. Calls the Responses API **once**
4. Executes whatever tool calls the API requests, feeding results back
5. Repeats until the API produces a final text response

The entire meeting invite workflow — retrieving agenda data, filtering speakers, resolving emails, sending invites — emerges from the instructions alone. **No custom orchestration code was written for any of these steps.**

This architecture has a significant practical benefit: **extending the agent's capabilities requires only adding a new tool definition and updating the instructions**. No workflow code, no state machines, no step graphs. The Responses API figures out how to use the new tool in context with the existing ones.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Windows 11 Desktop                           │
│                                                                     │
│  ┌───────────────────────┐     ┌──────────────────────────────────┐ │
│  │   pywebview Window    │◄────┤  WebSocket Server (ws://18080)   │ │
│  │   (chat_ui.html)      │────►│  HTTP Server     (http://18081)  │ │
│  │                       │     │                                  │ │
│  │  • Markdown rendering │     │  ┌───────────────────────────┐   │ │
│  │  • Auth banner/Sign-In│     │  │    Router (Master Agent)  │   │ │
│  │  • Progress steps     │     │  │    Azure OpenAI gpt-5.2   │   │ │
│  │  • Toast click handler│     │  └────────┬──────────────────┘   │ │
│  └───────────────────────┘     │           │ classifies intent    │ │
│                                │     ┌─────┴─────┬───────────┐    │ │
│  ┌───────────────────────┐     │     ▼           ▼           ▼    │ │
│  │  Global Hotkey Listener│    │  ┌──────┐  ┌────────┐  ┌─────┐   │ │
│  │  (pynput)             │     │  │Q&A   │  │Meeting │  │Gen- │   │ │
│  │  Ctrl+Alt+M           │     │  │Agent │  │Invite  │  │eral │   │ │
│  └───────────────────────┘     │  │Mini  │  │Agent   │  │Mini │   │ │
│                                │  │Model │  │Full LLM│  │Model│   │ │
│  ┌───────────────────────┐     │  └──┬───┘  └──┬─────┘  └─────┘   │ │
│  │  Toast Notifications  │     │     │         │                  │ │
│  │  (winotify)           │     │     ▼         ▼                  │ │
│  └───────────────────────┘     │  ┌────────────────────────────┐  │ │
│                                │  │  Azure OpenAI Responses    │  │ │
│                                │  │  API (Agentic Layer)       │  │ │
│                                │  │  • Autonomous tool-call    │  │ │
│                                │  │    orchestration           │  │ │
│                                │  │  • No custom workflow code │  │ │
│                                │  │  • Instructions-driven     │  │ │
│                                │  └──────────┬─────────────────┘  │ │
│                                │             ▼                    │ │
│                                │  ┌───────────────────────────┐   │ │
│                                │  │   Tool Execution Layer    │   │ │
│                                │  │  • query_workiq (CLI)     │   │ │
│                                │  │  • log_progress           │   │ │
│                                │  │  • create_meeting_invites │   │ │
│                                │  └──────┬────────────┬───────┘   │ │
│                                └─────────┼────────────┼───────────┘ │
│                                          │            │             │
└──────────────────────────────────────────┼────────────┼─────────────┘
                                           │            │
                              ┌────────────▼───┐  ┌─────▼───────────────┐
                              │   WorkIQ CLI   │  │ Azure Communication │
                              │  (M365 data)   │  │ Services (Email)    │
                              └────────────────┘  └─────────────────────┘
                                           │
                              ┌────────────▼───────────────┐
                              │   Microsoft 365 Graph API  │
                              │  Calendar · Email · Files  │
                              │  Contacts · SharePoint     │
                              └────────────────────────────┘
```

### How It All Fits Together

1. **Single-process launcher** (`meeting_agent.py`) — The entry point. Starts a background thread for the WebSocket/HTTP servers, registers the global hotkey, shows a startup toast, and enters the pywebview event loop.

2. **WebSocket server** (port `18080`) — The communication backbone between the HTML-based chat UI and the Python agent backend. All user messages, agent responses, progress updates, and auth status flow over this channel as JSON messages.

3. **HTTP server** (port `18081`) — A minimal server whose sole purpose is to handle toast notification clicks. When the user clicks a toast, Windows opens `http://127.0.0.1:18081/show`, which triggers the pywebview window to appear.

4. **pywebview window** — A lightweight native window that renders `chat_ui.html`. It starts hidden and is toggled on demand. When the user closes the window, it hides instead of quitting — the agent keeps running.

5. **Router (Master Agent)** — Every user message is first classified by an LLM call into one of three categories: `meeting_invites`, `qa`, or `general`. This determines which sub-agent handles the request.

6. **Sub-agents** — Each operates with its own system prompt, tool set, and model:
   - **Meeting Invite Agent** — Uses the full `gpt-5.2` model. Given natural-language instructions, the Responses API autonomously executes a multi-step tool-calling loop until completion.
   - **Q&A Agent** — Uses the smaller `gpt-5.4-mini` model with conversation history (last 20 messages) for follow-up context.
   - **General handler** — Uses `gpt-5.4-mini` for lightweight greetings and small talk without tool calls.

7. **Azure OpenAI Responses API (Agentic Layer)** — The orchestration engine beneath the sub-agents. The application provides tool definitions and natural-language instructions; the Responses API autonomously determines the sequence of tool calls, interprets results, and loops until the task is complete. There is no custom workflow code — the multi-step behavior emerges entirely from the instructions.

8. **Tool execution layer** — Bridges LLM tool calls to real actions:
   - `query_workiq` — Runs the WorkIQ CLI as a subprocess to query Microsoft 365 data.
   - `log_progress` — Sends structured progress updates to the UI in real time.
   - `create_meeting_invites` — Constructs `.ics` calendar invites and delivers them via Azure Communication Services.

---

## Technical Details

### Authentication Flow

The app uses `InteractiveBrowserCredential` from the Azure Identity SDK with persistent token caching:

1. **First launch** — The UI shows a "Not signed in" banner. The user clicks **Sign In**, which opens a browser for Azure AD authentication.
2. **Token caching** — Upon successful authentication, the `AuthenticationRecord` is serialized to `~/.workiq-assistant/auth_record.json`. The token cache is persisted under the name `workiq_assistant` using the OS credential store (Windows Credential Manager).
3. **Subsequent launches** — The saved `AuthenticationRecord` is loaded at startup. The credential silently refreshes tokens using the cached refresh token — no browser prompt needed.
4. **Token refresh** — The OpenAI client checks token expiry before each API call (with a 5-minute buffer). If the silent refresh fails (e.g., after a password change), it falls back to interactive browser login.
5. **Shared credential** — A single credential instance is shared between `agent_core.py` and `outlook_helper.py` via `set_credential()` to avoid duplicate browser prompts.

### WebSocket Communication Protocol

The UI and backend communicate over WebSocket (`ws://127.0.0.1:18080`) using JSON messages:

| Direction | Message Type | Purpose |
|---|---|---|
| Server → Client | `auth_status` | Reports sign-in state and user identity |
| Client → Server | `task` | User submits a request |
| Server → Client | `task_started` | Indicates processing has begun |
| Server → Client | `progress` | Real-time step/tool updates (kind: `step`, `tool`, `progress`, `agent`) |
| Server → Client | `task_complete` | Final agent response with Markdown content |
| Server → Client | `task_error` | Error message |
| Client → Server | `signin` | User clicks Sign In |
| Server → Client | `signin_status` | Result of the sign-in attempt |
| Client → Server | `clear_history` | User resets Q&A conversation history |

### Window Management

- The pywebview window starts **hidden** (`hidden=True`). Window close is intercepted — it hides rather than quits, so the agent keeps running.
- **Global hotkey** (`Ctrl+Alt+M`) uses `pynput.keyboard.GlobalHotKeys` to toggle visibility from any application.
- **Toast click** — Clicking a toast notification opens `http://127.0.0.1:18081/show` via the `winotify` `launch` parameter. The HTTP handler calls `_show_window()` and returns a self-closing HTML page.

### Subprocess Handling

Since the app runs under `pythonw.exe` (no console), all subprocess calls use `subprocess.CREATE_NO_WINDOW` on Windows to prevent `cmd.exe` windows from flashing on screen during WorkIQ CLI invocations.

### Logging

All logs are written to `~/.workiq-assistant/agent.log`, including agent routing decisions, tool calls, WorkIQ responses, and authentication events.

---

## Project Structure

```
workiq-assistant/
├── meeting_agent.py       # Main entry point — launcher, WebSocket/HTTP servers,
│                          #   pywebview window, hotkey, toast notifications
├── agent_core.py          # Core agent logic — router, sub-agents (Meeting Invite,
│                          #   Q&A, General), tool execution, auth helpers
├── agent.py               # Console entry point — terminal-based interaction for
│                          #   development and debugging (no UI, no background mode)
├── outlook_helper.py      # Azure Communication Services integration — builds .ics
│                          #   calendar invites and sends them via email
├── chat_ui.html           # Chat UI — Markdown rendering, auth banner, progress
│                          #   indicators, WebSocket client
├── favicon.svg            # App icon (SVG) — used inline in the HTML UI
├── agent_icon.png         # App icon (128×128 PNG) — used for toast notifications
├── agent_icon.ico         # App icon (ICO) — used for pywebview taskbar icon
├── .env                   # Environment configuration (Azure endpoints, models,
│                          #   tenant ID, ACS settings) — not committed to git
├── .env.example           # Template for .env with placeholder values
├── .gitignore             # Git ignore rules
├── requirements.txt       # Python dependencies
├── experimental/
│   └── test_graph_calendar.py  # Test script for Microsoft Graph calendar API
│                               #   integration (delegated permissions, creates
│                               #   test events to verify API access)
└── scripts/
    ├── start.ps1          # Start the assistant (detached, via pythonw.exe)
    ├── stop.ps1           # Stop all running pythonw.exe instances
    └── autostart.ps1      # Install/uninstall auto-start at Windows login
```

---

## Getting Started

### Prerequisites

- **Windows 11** laptop
- **Python 3.12+** with a virtual environment
- **WorkIQ CLI** installed and on PATH (or path set in `.env`)
- **Azure OpenAI** resource with `gpt-5.2` and `gpt-5.4-mini` model deployments
- **Azure Communication Services** resource for sending email invites

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

## Configuration

All configuration is in the `.env` file:

| Variable | Description |
|---|---|
| `AZURE_OPENAI_ENDPOINT` | Azure OpenAI resource endpoint |
| `AZURE_OPENAI_CHAT_MODEL` | Full model for router + Meeting Invite Agent (e.g., `gpt-5.2`) |
| `AZURE_OPENAI_CHAT_MODEL_SMALL` | Mini model for Q&A + general responses (e.g., `gpt-5.4-mini`) |
| `AZURE_OPENAI_API_VERSION` | API version (e.g., `2025-03-01-preview`) |
| `AZURE_TENANT_ID` | Azure AD tenant ID |
| `AZURE_SUBSCRIPTION_ID` | Azure subscription ID |
| `ACS_ENDPOINT` | Azure Communication Services endpoint |
| `ACS_SENDER_ADDRESS` | Verified sender email address for ACS |
| `AGENT_TIMEZONE` | (Optional) IANA timezone override (auto-detected if omitted) |
| `WORKIQ_PATH` | (Optional) Full path to WorkIQ CLI if not on PATH |

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
| `tzlocal` | Auto-detection of the system timezone |
