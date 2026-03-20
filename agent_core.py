"""
Core agent logic — Router + skill-based sub-agents, tool execution, auth helpers.

Architecture:
  Router (master agent) → classifies user intent → hands off to the matching skill.
  Skills are loaded dynamically from YAML files in the skills/ folder.
  Adding a new skill requires only a new YAML file — no Python code changes.
"""

import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yaml

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
# Skill loader — reads YAML files from skills/ folder
# ---------------------------------------------------------------------------

_SKILLS_DIR = Path(__file__).resolve().parent / "skills"


# Registry of tool name → JSON schema (for the Responses API)
TOOL_SCHEMAS: dict[str, dict] = {
    "query_workiq": {
        "type": "function",
        "name": "query_workiq",
        "description": (
            "Query the user's Microsoft 365 data via WorkIQ CLI. Use this to "
            "retrieve agenda details, speakers, topics, time slots, email "
            "addresses, calendar events, documents, emails, contacts, and "
            "any other M365 data."
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
    "log_progress": {
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
    "create_meeting_invites": {
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
}


class Skill:
    """A loaded skill definition."""

    def __init__(self, data: dict, source_file: str):
        self.name: str = data["name"]
        self.description: str = data["description"].strip()
        self.model_tier: str = data.get("model", "mini")  # "full" or "mini"
        self.conversational: bool = data.get("conversational", False)
        self.tool_names: list[str] = data.get("tools", [])
        self.instructions: str = data["instructions"].strip()
        self.source_file: str = source_file

    @property
    def model(self) -> str:
        return CHAT_MODEL if self.model_tier == "full" else CHAT_MODEL_SMALL

    @property
    def tools(self) -> list[dict]:
        return [TOOL_SCHEMAS[t] for t in self.tool_names if t in TOOL_SCHEMAS]


def _load_skills() -> dict[str, Skill]:
    """Load all skill YAML files from the skills/ folder."""
    skills: dict[str, Skill] = {}
    if not _SKILLS_DIR.is_dir():
        logger.warning("Skills directory not found: %s", _SKILLS_DIR)
        return skills
    for path in sorted(_SKILLS_DIR.glob("*.yaml")):
        try:
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f)
            skill = Skill(data, str(path))
            skills[skill.name] = skill
            logger.info("Loaded skill: %s (%s model, %d tools) from %s",
                        skill.name, skill.model_tier, len(skill.tool_names), path.name)
        except Exception as e:
            logger.error("Failed to load skill from %s: %s", path, e)
    return skills


# Load skills at import time
_skills = _load_skills()
logger.info("Skills loaded: %s", list(_skills.keys()))


def get_loaded_skills() -> list[dict]:
    """Return a summary of all loaded skills for the UI."""
    return [
        {
            "name": s.name,
            "description": s.description,
            "model": s.model_tier,
            "tools": s.tool_names,
        }
        for s in _skills.values()
    ]


# ---------------------------------------------------------------------------
# Router (master agent) — classifies intent dynamically from loaded skills
# ---------------------------------------------------------------------------

def _build_router_prompt() -> str:
    """Build the router system prompt from all loaded skills."""
    lines = [
        "You are a routing agent. Your ONLY job is to classify the user's request "
        "and return a JSON object.",
        "",
        "Classify into one of these categories:",
        "",
    ]
    for i, skill in enumerate(_skills.values(), 1):
        lines.append(f'{i}. "{skill.name}" — {skill.description}')
        lines.append("")

    skill_names = [f'"{s}"' for s in _skills]
    examples = " or ".join(f'{{"skill": {n}}}' for n in skill_names)
    lines.append(f"Respond with ONLY a JSON object, no other text:")
    lines.append(examples)
    return "\n".join(lines)


ROUTER_PROMPT = _build_router_prompt()
logger.debug("Router prompt:\n%s", ROUTER_PROMPT)


def _route(user_input: str) -> str:
    """Classify user intent and return the skill name."""
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
        skill_name = result.get("skill") or result.get("agent", "qa")
        logger.info("[Router] Classified as: %s", skill_name)
        if skill_name in _skills:
            return skill_name
        logger.warning("[Router] Unknown skill '%s' — defaulting to qa", skill_name)
        return "qa"
    except (json.JSONDecodeError, AttributeError):
        logger.warning("[Router] Could not parse response: %s — defaulting to qa", text)
        return "qa"


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


# Tool name → handler function
_TOOL_HANDLERS = {
    "query_workiq": lambda args, on_progress: execute_query_workiq(args["question"], on_progress),
    "log_progress": lambda args, on_progress: execute_log_progress(args["step_title"], args["details"], on_progress),
    "create_meeting_invites": lambda args, on_progress: execute_create_meeting_invites(args["customer_name"], args["sessions"], on_progress),
}


def handle_tool_call(name: str, arguments: str, on_progress=None) -> str:
    """Execute a tool call and return the result string."""
    args = json.loads(arguments)
    handler = _TOOL_HANDLERS.get(name)
    if handler:
        return handler(args, on_progress)
    return f"Unknown tool: {name}"


# ---------------------------------------------------------------------------
# Conversation history for conversational skills
# ---------------------------------------------------------------------------

_conversation_histories: dict[str, list[dict]] = {}


def reset_qa_history():
    """Clear all conversational skill histories."""
    _conversation_histories.clear()
    logger.info("Conversation histories cleared.")


# ---------------------------------------------------------------------------
# Generic skill runner
# ---------------------------------------------------------------------------

def _run_skill(skill: Skill, user_input: str, on_progress=None) -> str:
    """
    Run a skill against the user's input.

    - Conversational skills maintain a session history for follow-up context.
    - Non-conversational skills run single-turn (no history).
    - Skills with no tools get a direct LLM response (no tool-call loop).
    """
    client = get_responses_client()

    # Build input messages
    if skill.conversational:
        history = _conversation_histories.setdefault(skill.name, [])
        history.append({"role": "user", "content": user_input})
        input_messages = list(history)
        logger.info("[%s] Query: %s (history: %d messages)", skill.name, user_input, len(history))
    else:
        input_messages = [{"role": "user", "content": user_input}]
        logger.info("[%s] Starting execution...", skill.name)

    if on_progress and skill.tool_names:
        on_progress("step", f"{skill.name}: starting...")

    # Initial API call
    tools = skill.tools or []
    response = client.responses.create(
        model=skill.model,
        instructions=skill.instructions,
        input=input_messages,
        tools=tools if tools else [],
    )

    # Tool-call loop (only if skill has tools)
    if tools:
        step = 1
        while True:
            tool_calls = [item for item in response.output if item.type == "function_call"]
            if not tool_calls:
                break

            tool_results = []
            for tc in tool_calls:
                logger.info("[%s Step %d] Calling tool: %s", skill.name, step, tc.name)
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
                model=skill.model,
                instructions=skill.instructions,
                input=tool_results,
                tools=tools,
                previous_response_id=response.id,
            )

    # Extract final text
    final_text = ""
    for item in response.output:
        if item.type == "message":
            for part in item.content:
                if part.type == "output_text":
                    final_text += part.text

    # Save to history for conversational skills
    if skill.conversational and final_text:
        history = _conversation_histories.setdefault(skill.name, [])
        history.append({"role": "assistant", "content": final_text})
        # Keep history manageable (last 20 messages)
        if len(history) > 20:
            _conversation_histories[skill.name] = history[-20:]

    return final_text


# ---------------------------------------------------------------------------
# Master entry point — routes to the right skill
# ---------------------------------------------------------------------------

def run_agent(user_input: str, on_progress=None) -> str:
    """
    Master entry point. Routes user input to the appropriate skill.

    on_progress(kind, message) is called with live updates:
      kind="step"     — agent step started
      kind="tool"     — tool execution update
      kind="progress" — structured progress from log_progress tool
      kind="agent"    — sub-agent activated
    """
    skill_name = _route(user_input)
    skill = _skills.get(skill_name)

    if not skill:
        logger.warning("Skill '%s' not found — falling back to qa", skill_name)
        skill = _skills.get("qa")

    # Don't show "agent" badge for lightweight general responses
    if skill and skill.name != "general" and on_progress:
        on_progress("agent", skill.name)

    return _run_skill(skill, user_input, on_progress)
