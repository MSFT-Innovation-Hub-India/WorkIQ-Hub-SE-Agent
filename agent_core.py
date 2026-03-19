"""
Core agent logic — Router + sub-agents, tool execution, auth helpers.

Architecture:
  Router (master agent) → classifies user intent → hands off to:
    • MeetingInviteAgent — autonomous multi-step calendar invite workflow
    • QAAgent — conversational Q&A via WorkIQ (with session history)
"""

import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# Prevent visible cmd.exe windows when spawning subprocesses under pythonw
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

from azure.identity import (
    AuthenticationRecord,
    InteractiveBrowserCredential,
    TokenCachePersistenceOptions,
)
from dotenv import load_dotenv
from openai import OpenAI

from outlook_helper import create_outlook_meeting, _resolve_organizer, set_credential

load_dotenv()

logger = logging.getLogger("workiq_assistant")

ENDPOINT = os.environ["AZURE_OPENAI_ENDPOINT"]
CHAT_MODEL = os.environ["AZURE_OPENAI_CHAT_MODEL"]
CHAT_MODEL_SMALL = os.environ.get("AZURE_OPENAI_CHAT_MODEL_SMALL", CHAT_MODEL)
API_VERSION = os.environ["AZURE_OPENAI_API_VERSION"]

# Persistent token cache + authentication record
_cache_options = TokenCachePersistenceOptions(name="workiq_assistant")
_tenant_id = os.environ.get("AZURE_TENANT_ID")
_AUTH_RECORD_PATH = Path.home() / ".workiq-assistant" / "auth_record.json"
_AUTH_RECORD_PATH.parent.mkdir(exist_ok=True)


def _create_credential(record=None):
    """Create credential, optionally with a saved AuthenticationRecord for silent refresh."""
    return InteractiveBrowserCredential(
        tenant_id=_tenant_id,
        cache_persistence_options=_cache_options,
        authentication_record=record,
    )


# Load saved authentication record if it exists (enables silent token refresh)
_auth_record = None
if _AUTH_RECORD_PATH.exists():
    try:
        _auth_record = AuthenticationRecord.deserialize(_AUTH_RECORD_PATH.read_text())
        logger.info("Loaded saved authentication record")
    except Exception:
        logger.warning("Failed to load auth record — will require sign-in")

_credential = _create_credential(_auth_record)
set_credential(_credential)

_responses_client: OpenAI | None = None
_responses_client_token_expires: float = 0


# ---------------------------------------------------------------------------
# WorkIQ CLI resolution
# ---------------------------------------------------------------------------

def _find_workiq() -> str | None:
    """Resolve the full path to the workiq CLI."""
    # 1. Same venv as the agent
    venv_dir = Path(sys.executable).parent
    for name in ("workiq", "workiq.exe"):
        candidate = venv_dir / name
        if candidate.exists():
            return str(candidate)
    # 2. Explicit env var
    env_path = os.environ.get("WORKIQ_PATH")
    if env_path and Path(env_path).exists():
        return env_path
    # 3. System PATH
    found = shutil.which("workiq")
    if found:
        return found
    return None


WORKIQ_CLI = _find_workiq()
if WORKIQ_CLI:
    logger.info("workiq CLI found: %s", WORKIQ_CLI)
else:
    logger.warning("workiq CLI not found. Install it or set WORKIQ_PATH in .env")


# ---------------------------------------------------------------------------
# Azure auth helpers
# ---------------------------------------------------------------------------

def check_azure_auth() -> tuple[bool, str]:
    """Check if Azure credentials are cached (non-interactive — never opens browser)."""
    if _auth_record is None:
        return False, "Not signed in"
    try:
        _credential.get_token("https://cognitiveservices.azure.com/.default")
        return True, "Authenticated"
    except Exception as e:
        return False, str(e)


def run_az_login(tenant_id: str | None = None,
                 subscription_id: str | None = None) -> tuple[bool, str]:
    """Trigger interactive browser login, save record for future silent refresh."""
    global _auth_record, _credential
    try:
        record = _credential.authenticate(
            scopes=["https://cognitiveservices.azure.com/.default"]
        )
        # Save the authentication record so future launches can silently refresh
        _AUTH_RECORD_PATH.write_text(record.serialize())
        _auth_record = record
        # Recreate credential with the record for silent refresh
        _credential = _create_credential(_auth_record)
        set_credential(_credential)
        logger.info("Auth record saved to %s", _AUTH_RECORD_PATH)
        return True, "Signed in successfully"
    except Exception as e:
        return False, str(e)


# ---------------------------------------------------------------------------
# OpenAI client (token-refreshing)
# ---------------------------------------------------------------------------

def get_responses_client() -> OpenAI:
    """Return a cached OpenAI client for Azure OpenAI Responses API.
    
    Silently refreshes tokens via cached refresh token.
    Falls back to interactive browser login if refresh fails.
    """
    global _responses_client, _responses_client_token_expires
    now = time.time()
    if _responses_client is None or now >= _responses_client_token_expires - 300:
        base_url = ENDPOINT.rstrip("/")
        if not base_url.endswith("/openai/v1"):
            base_url = f"{base_url}/openai/v1"
        try:
            token_obj = _credential.get_token(
                "https://cognitiveservices.azure.com/.default"
            )
        except Exception:
            logger.warning("Token refresh failed — attempting interactive login...")
            ok, msg = run_az_login()
            if not ok:
                raise RuntimeError(
                    f"Azure authentication expired. Please sign in again. ({msg})"
                )
            logger.info("Interactive login succeeded: %s", msg)
            token_obj = _credential.get_token(
                "https://cognitiveservices.azure.com/.default"
            )
        _responses_client_token_expires = token_obj.expires_on
        _responses_client = OpenAI(
            base_url=base_url,
            api_key=token_obj.token,
        )
    return _responses_client


# ---------------------------------------------------------------------------
# Router (master agent) — classifies intent, no history
# ---------------------------------------------------------------------------

ROUTER_PROMPT = """You are a routing agent. Your ONLY job is to classify the user's request and return a JSON object.

Classify into one of these categories:

1. "meeting_invites" — The user wants to send/create calendar invites or meeting invitations to speakers/presenters from an agenda document or event. Keywords: invite, calendar, schedule speakers, send invites, agenda, engagement.

2. "qa" — The user is asking a question that requires looking up data from Microsoft 365: calendar events, documents, emails, contacts, etc.

3. "general" — Greetings, small talk, thanks, goodbyes, or simple conversational messages that do NOT require any data lookup or action (e.g. "hi", "hello", "thanks", "how are you", "hey there").

Respond with ONLY a JSON object, no other text:
{"agent": "meeting_invites"} or {"agent": "qa"} or {"agent": "general"}"""


def _route(user_input: str) -> str:
    """Classify user intent and return the sub-agent name."""
    client = get_responses_client()
    response = client.responses.create(
        model=CHAT_MODEL,
        instructions=ROUTER_PROMPT,
        input=[{"role": "user", "content": user_input}],
        tools=[],
    )
    text = ""
    for item in response.output:
        if item.type == "message":
            for part in item.content:
                if part.type == "output_text":
                    text += part.text
    try:
        result = json.loads(text.strip())
        agent = result.get("agent", "qa")
        logger.info("[Router] Classified as: %s", agent)
        return agent
    except (json.JSONDecodeError, AttributeError):
        logger.warning("[Router] Could not parse response: %s — defaulting to qa", text)
        return "qa"


# ---------------------------------------------------------------------------
# Sub-agent 1: Meeting Invite Agent (single-turn, autonomous)
# ---------------------------------------------------------------------------

MEETING_INVITE_PROMPT = """You are an autonomous Hub Engagement Speaker Schedule Management Agent.

Given a user request about a customer engagement event, you MUST complete ALL of the following steps using tool calls — do NOT stop or return text to the user until every step is done.

STEP 1: Call query_workiq to retrieve the COMPLETE agenda document. Ask for: EVERY row in the agenda table including topic names, speaker names, and time slots for each session. Emphasize that you need ALL rows — do not summarize or abbreviate.
After receiving the response, call log_progress with a markdown summary containing:
- Document name retrieved
- Engagement date
- A markdown table of ALL rows exactly as returned, with columns: Time Slot | Topic | Speaker
- A count of total rows retrieved

STEP 2: From the COMPLETE list of rows, identify ALL Microsoft employee speakers. Apply these rules:
DISCARD rows that are:
- Lunch breaks, tea breaks, coffee breaks, or any kind of break
- Rows with no topic or no speaker assigned
- Rows where the speaker field is empty, "-", or "TBD"
- Rows where the speaker is ONLY a team name (e.g. "Honeywell Team", "Customer Team"), a company name, or a generic role like "Moderator" with no individual name
KEEP rows where:
- The speaker is a clearly identifiable individual person's name (first name + last name)
- This includes ALL Microsoft employees listed as speakers, even if they appear only once
- If a row has multiple speakers listed (e.g. "John Smith / Jane Doe" or "John Smith & Jane Doe"), treat EACH person as a separate speaker and create entries for each
After filtering, call log_progress listing:
- Every kept session with speaker name and topic
- Every discarded row with the reason for discarding
- Total speakers kept vs total rows discarded

STEP 3: Call query_workiq ONCE to look up the Microsoft corporate email addresses of ALL the individual speakers identified in Step 2. List every speaker name explicitly in your query — do not abbreviate. Ask for all of them in a single query.
After receiving the response, call log_progress with a markdown table showing: Speaker | Email

STEP 4: Call create_meeting_invites with the curated list of sessions, including each speaker's email address. Use the event date from the user's request to build full YYYY-MM-DD HH:MM datetime strings for start_time and end_time. Use 24-hour format.

CRITICAL TIME CONVERSION RULES:
- Copy AM/PM designations EXACTLY as they appear in the source agenda. Do NOT change AM to PM or vice versa.
- Business events run during daytime hours (typically 8 AM to 7 PM). If your converted time falls outside this range, you have made an error — go back and re-read the source times.
- When converting 12-hour to 24-hour: 10:15 AM = 10:15, 11:15 AM = 11:15, 12:15 PM = 12:15, 1:30 PM = 13:30, 2:30 PM = 14:30, 5:30 PM = 17:30, 5:45 PM = 17:45.
- Double-check EVERY time value before calling create_meeting_invites.

STEP 5: After the invites are created, present the user with a final summary table showing: Topic, Speaker, Time Slot, Email, and Status (created / failed). Also note any sessions that were skipped and why.

IMPORTANT:
- Complete ALL steps autonomously in a single turn. Do NOT stop after any intermediate step to ask the user for input.
- Always call log_progress after each query_workiq call and after filtering, BEFORE moving to the next step.
- If a speaker appears in multiple sessions, create a separate invite for each session.
- If WorkIQ cannot find an email for a speaker, use "unknown@unknown.com" as a placeholder and flag it in the summary.
- NEVER include team names, company names, or generic roles as speakers in the invite list."""


MEETING_INVITE_TOOLS = [
    {
        "type": "function",
        "name": "query_workiq",
        "description": (
            "Query the user's Microsoft 365 data via WorkIQ CLI. Use this to "
            "retrieve agenda details, speakers, topics, time slots, and email "
            "addresses from documents, calendar, or email."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": (
                        "The natural language question to ask WorkIQ about "
                        "the user's M365 data."
                    ),
                }
            },
            "required": ["question"],
        },
    },
    {
        "type": "function",
        "name": "log_progress",
        "description": (
            "Log a formatted progress update for the user to see. Call this "
            "after each major step to show what was retrieved or decided. "
            "Use markdown formatting (tables, lists, bold)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "step_title": {
                    "type": "string",
                    "description": (
                        "Short title for this step, e.g. 'Agenda Retrieved', "
                        "'Speakers Filtered', 'Emails Resolved'."
                    ),
                },
                "details": {
                    "type": "string",
                    "description": (
                        "Formatted markdown summary of what was found or "
                        "decided in this step."
                    ),
                },
            },
            "required": ["step_title", "details"],
        },
    },
    {
        "type": "function",
        "name": "create_meeting_invites",
        "description": (
            "Create draft (unsent) meeting invites in Outlook for each "
            "speaker session. The invites will appear in the user's Outlook "
            "calendar for review before sending."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "customer_name": {
                    "type": "string",
                    "description": "Customer or event name for the meeting subject.",
                },
                "sessions": {
                    "type": "array",
                    "description": "Array of session objects to create invites for.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "speaker_name": {"type": "string"},
                            "speaker_email": {"type": "string"},
                            "topic": {"type": "string"},
                            "start_time": {
                                "type": "string",
                                "description": "Start time in YYYY-MM-DD HH:MM format (24h).",
                            },
                            "end_time": {
                                "type": "string",
                                "description": "End time in YYYY-MM-DD HH:MM format (24h).",
                            },
                        },
                        "required": [
                            "speaker_name",
                            "speaker_email",
                            "topic",
                            "start_time",
                            "end_time",
                        ],
                    },
                },
            },
            "required": ["customer_name", "sessions"],
        },
    },
]


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------

def execute_query_workiq(question: str, on_progress=None) -> str:
    """Run WorkIQ CLI and return the output."""
    if not WORKIQ_CLI:
        return "Error: workiq CLI not found. Install it or set WORKIQ_PATH in .env"
    logger.info("[WorkIQ] Querying: %s", question)
    if on_progress:
        on_progress("tool", f"Querying WorkIQ: {question}")
    try:
        result = subprocess.run(
            [WORKIQ_CLI, "ask", "-q", question],
            capture_output=True,
            text=True,
            timeout=120,
            creationflags=_NO_WINDOW,
        )
        if result.returncode != 0:
            return f"WorkIQ error (exit code {result.returncode}): {result.stderr.strip()}"
        output = result.stdout.strip()
        logger.info("[WorkIQ] Response received (%d chars)", len(output))
        if on_progress:
            on_progress("tool", f"WorkIQ responded ({len(output)} chars)")
        return output
    except subprocess.TimeoutExpired:
        return "WorkIQ timed out after 120 seconds."
    except Exception as e:
        return f"Failed to call WorkIQ: {e}"


def execute_log_progress(step_title: str, details: str, on_progress=None) -> str:
    """Log a formatted progress update."""
    header = f"┌─ {step_title}"
    separator = "│"
    footer = f"└{'─' * 60}"
    body = "\n".join(f"│  {line}" for line in details.splitlines())
    msg = f"{header}\n{separator}\n{body}\n{separator}\n{footer}"
    logger.info("\n%s\n", msg)
    if on_progress:
        on_progress("progress", step_title)
    return "Logged."


def execute_create_meeting_invites(customer_name: str, sessions: list[dict], on_progress=None) -> str:
    """Create Outlook meetings and return a summary."""
    if on_progress:
        on_progress("tool", f"Creating {len(sessions)} meeting invite(s)...")
    results = []
    for s in sessions:
        try:
            subject = f"{customer_name} — {s['topic']}"
            body = (
                f"Customer Engagement: {customer_name}\n"
                f"Speaker: {s['speaker_name']}\n"
                f"Topic: {s['topic']}\n\n"
                f"This is an auto-generated invite. Please review before sending."
            )
            create_outlook_meeting(
                subject=subject,
                start=s["start_time"],
                end=s["end_time"],
                recipients=[s["speaker_email"]],
                body=body,
            )
            results.append(f"OK: {subject} -> {s['speaker_email']}")
        except Exception as e:
            results.append(f"FAILED: {s['speaker_name']} — {e}")
    return "\n".join(results)


def handle_tool_call(name: str, arguments: str, on_progress=None) -> str:
    """Execute a tool call and return the result string."""
    args = json.loads(arguments)
    if name == "query_workiq":
        return execute_query_workiq(args["question"], on_progress)
    elif name == "log_progress":
        return execute_log_progress(args["step_title"], args["details"], on_progress)
    elif name == "create_meeting_invites":
        return execute_create_meeting_invites(args["customer_name"], args["sessions"], on_progress)
    else:
        return f"Unknown tool: {name}"


# ---------------------------------------------------------------------------
# Agent loop — Meeting Invite Agent (single-turn autonomous)
# ---------------------------------------------------------------------------

def _run_meeting_invite_agent(user_input: str, on_progress=None) -> str:
    """
    Single-turn autonomous agent for meeting invite creation.
    No conversation history — runs all steps to completion.
    """
    input_messages = [{"role": "user", "content": user_input}]

    client = get_responses_client()

    logger.info("[MeetingInviteAgent] Starting autonomous execution...")
    if on_progress:
        on_progress("step", "Meeting Invite Agent: starting...")

    response = client.responses.create(
        model=CHAT_MODEL,
        instructions=MEETING_INVITE_PROMPT,
        input=input_messages,
        tools=MEETING_INVITE_TOOLS,
    )

    step = 1
    while True:
        tool_calls = [item for item in response.output if item.type == "function_call"]

        if not tool_calls:
            break

        tool_results = []
        for tc in tool_calls:
            logger.info("[Step %d] Calling tool: %s", step, tc.name)
            if on_progress:
                on_progress("step", f"Step {step}: {tc.name}")
            result = handle_tool_call(tc.name, tc.arguments, on_progress)
            tool_results.append({
                "type": "function_call_output",
                "call_id": tc.call_id,
                "output": result,
            })

        step += 1

        client = get_responses_client()
        response = client.responses.create(
            model=CHAT_MODEL,
            instructions=MEETING_INVITE_PROMPT,
            input=tool_results,
            tools=MEETING_INVITE_TOOLS,
            previous_response_id=response.id,
        )

    # Extract the final text
    final_text = ""
    for item in response.output:
        if item.type == "message":
            for part in item.content:
                if part.type == "output_text":
                    final_text += part.text
    return final_text


# ---------------------------------------------------------------------------
# Sub-agent 2: Q&A Agent (conversational, with session history)
# ---------------------------------------------------------------------------

QA_PROMPT = """You are a helpful assistant that answers questions about the user's Microsoft 365 data — calendar events, documents, emails, contacts, and more.

You have access to WorkIQ, which can search and retrieve information from the user's M365 environment.

Rules:
- Use query_workiq to look up real data when the user asks about their calendar, files, emails, meetings, contacts, etc.
- Call log_progress to show the user what you found, using markdown formatting.
- Give concise, well-structured answers. Use markdown tables where appropriate.
- If the user asks a follow-up question, use context from the conversation history to understand what they mean.
- If WorkIQ cannot find the answer, say so clearly."""

QA_TOOLS = [
    {
        "type": "function",
        "name": "query_workiq",
        "description": (
            "Query the user's Microsoft 365 data via WorkIQ CLI. Use this to "
            "search calendars, documents, emails, contacts, and any other M365 data."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The natural language question to ask WorkIQ.",
                }
            },
            "required": ["question"],
        },
    },
    {
        "type": "function",
        "name": "log_progress",
        "description": (
            "Log a formatted progress update for the user to see. "
            "Use markdown formatting (tables, lists, bold)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "step_title": {
                    "type": "string",
                    "description": "Short title, e.g. 'Calendar Events', 'Search Results'.",
                },
                "details": {
                    "type": "string",
                    "description": "Formatted markdown summary of what was found.",
                },
            },
            "required": ["step_title", "details"],
        },
    },
]

# Session history for the Q&A agent (list of message dicts)
_qa_history: list[dict] = []


def reset_qa_history():
    """Clear the Q&A conversation history."""
    global _qa_history
    _qa_history = []
    logger.info("[QAAgent] Conversation history cleared.")


def _run_qa_agent(user_input: str, on_progress=None) -> str:
    """
    Conversational Q&A agent with session history.
    Each call appends to _qa_history for follow-up context.
    """
    global _qa_history

    _qa_history.append({"role": "user", "content": user_input})

    client = get_responses_client()

    logger.info("[QAAgent] Query: %s (history: %d messages)", user_input, len(_qa_history))
    if on_progress:
        on_progress("step", "Q&A Agent: looking up your data...")

    response = client.responses.create(
        model=CHAT_MODEL_SMALL,
        instructions=QA_PROMPT,
        input=_qa_history,
        tools=QA_TOOLS,
    )

    step = 1
    while True:
        tool_calls = [item for item in response.output if item.type == "function_call"]

        if not tool_calls:
            break

        tool_results = []
        for tc in tool_calls:
            logger.info("[QAAgent Step %d] Calling tool: %s", step, tc.name)
            if on_progress:
                on_progress("step", f"Looking up: {tc.name}")
            result = handle_tool_call(tc.name, tc.arguments, on_progress)
            tool_results.append({
                "type": "function_call_output",
                "call_id": tc.call_id,
                "output": result,
            })

        step += 1

        client = get_responses_client()
        response = client.responses.create(
            model=CHAT_MODEL_SMALL,
            instructions=QA_PROMPT,
            input=tool_results,
            tools=QA_TOOLS,
            previous_response_id=response.id,
        )

    # Extract the final text
    final_text = ""
    for item in response.output:
        if item.type == "message":
            for part in item.content:
                if part.type == "output_text":
                    final_text += part.text

    # Save assistant response to history for follow-ups
    if final_text:
        _qa_history.append({"role": "assistant", "content": final_text})

    # Keep history manageable (last 20 messages)
    if len(_qa_history) > 20:
        _qa_history = _qa_history[-20:]

    return final_text


# ---------------------------------------------------------------------------
# General response handler (greetings, small talk — no sub-agent needed)
# ---------------------------------------------------------------------------

def _run_general_response(user_input: str, on_progress=None) -> str:
    """Handle greetings and simple conversational messages directly."""
    client = get_responses_client()
    response = client.responses.create(
        model=CHAT_MODEL_SMALL,
        instructions=(
            "You are WorkIQ Assistant, a friendly helper for Microsoft 365 data. "
            "Respond briefly and naturally to greetings and small talk. "
            "Let the user know you can help with their M365 data — calendar, emails, "
            "documents, contacts — or create meeting invites from agenda documents."
        ),
        input=[{"role": "user", "content": user_input}],
    )
    final_text = ""
    for item in response.output:
        if item.type == "message":
            for part in item.content:
                if part.type == "output_text":
                    final_text += part.text
    return final_text


# ---------------------------------------------------------------------------
# Master entry point — routes to the right sub-agent
# ---------------------------------------------------------------------------

def run_agent(user_input: str, on_progress=None) -> str:
    """
    Master entry point. Routes user input to the appropriate sub-agent.

    on_progress(kind, message) is called with live updates:
      kind="step"     — agent step started
      kind="tool"     — tool execution update
      kind="progress" — structured progress from log_progress tool
    """
    agent_name = _route(user_input)

    if agent_name == "meeting_invites":
        if on_progress:
            on_progress("agent", "Meeting Invite Agent")
        return _run_meeting_invite_agent(user_input, on_progress)
    elif agent_name == "general":
        return _run_general_response(user_input, on_progress)
    else:
        if on_progress:
            on_progress("agent", "Q&A Agent")
        return _run_qa_agent(user_input, on_progress)
