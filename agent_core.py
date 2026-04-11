"""
Core agent logic — Router + skill-based sub-agents, tool execution, auth helpers.

Architecture:
  Router (master agent) → classifies user intent → hands off to the matching skill.
  Skills are loaded dynamically from YAML files in the skills/ folder.
  Adding a new skill requires only a new YAML file — no Python code changes.
"""

import importlib
import json
import logging
import os
import shutil
import sys
import time
from pathlib import Path

import yaml

from azure.identity import (
    AuthenticationRecord,
    InteractiveBrowserCredential,
    TokenCachePersistenceOptions,
)
from dotenv import load_dotenv
from openai import OpenAI

from outlook_helper import set_credential

load_dotenv()

logger = logging.getLogger("hub_se_agent")

ENDPOINT = os.environ["AZURE_OPENAI_ENDPOINT"]
CHAT_MODEL = os.environ["AZURE_OPENAI_CHAT_MODEL"]
CHAT_MODEL_SMALL = os.environ.get("AZURE_OPENAI_CHAT_MODEL_SMALL", CHAT_MODEL)
API_VERSION = os.environ["AZURE_OPENAI_API_VERSION"]

# Persistent token cache + authentication record
_cache_options = TokenCachePersistenceOptions(name="hub_se_agent")
_tenant_id = os.environ.get("AZURE_TENANT_ID")
_AUTH_RECORD_PATH = Path.home() / ".hub-se-agent" / "auth_record.json"
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

def get_credential():
    """Return the shared InteractiveBrowserCredential (for use by Redis bridge etc)."""
    return _credential


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


# ---------------------------------------------------------------------------
# Tool loader — discovers tool modules from tools/ folder
# ---------------------------------------------------------------------------

_TOOLS_DIR = Path(__file__).resolve().parent / "tools"

# Registry of tool name → JSON schema (for the Responses API)
TOOL_SCHEMAS: dict[str, dict] = {}

# Registry of tool name → handler function (module.handle)
_TOOL_HANDLERS: dict[str, callable] = {}


def _load_tools():
    """Discover and load all tool modules from the tools/ folder.

    Each module must export:
      SCHEMA  — dict with the tool's JSON schema for the Responses API
      handle  — callable(arguments: dict, *, on_progress=None, **kwargs) -> str
    """
    if not _TOOLS_DIR.is_dir():
        logger.warning("Tools directory not found: %s", _TOOLS_DIR)
        return
    # Ensure tools/ is importable
    tools_parent = str(_TOOLS_DIR.parent)
    if tools_parent not in sys.path:
        sys.path.insert(0, tools_parent)
    for path in sorted(_TOOLS_DIR.glob("*.py")):
        if path.name.startswith("_"):
            continue
        module_name = f"tools.{path.stem}"
        try:
            mod = importlib.import_module(module_name)
            schema = getattr(mod, "SCHEMA", None)
            handler = getattr(mod, "handle", None)
            if schema is None or handler is None:
                logger.warning("Tool module %s missing SCHEMA or handle — skipping", path.name)
                continue
            tool_name = schema["name"]
            TOOL_SCHEMAS[tool_name] = schema
            _TOOL_HANDLERS[tool_name] = handler
            logger.info("Loaded tool: %s from %s", tool_name, path.name)
        except Exception as e:
            logger.error("Failed to load tool from %s: %s", path.name, e)


_load_tools()
logger.info("Tools loaded: %s", list(TOOL_SCHEMAS.keys()))


class Skill:
    """A loaded skill definition."""

    def __init__(self, data: dict, source_file: str):
        self.name: str = data["name"]
        self.description: str = data["description"].strip()
        self.model_tier: str = data.get("model", "mini")  # "full" or "mini"
        self.conversational: bool = data.get("conversational", False)
        self.queued: bool = data.get("queued", True)  # queue business tasks by default
        self.tool_names: list[str] = data.get("tools", [])
        self.instructions: str = data["instructions"].strip()
        self.reasoning_effort: str | None = data.get("reasoning_effort")  # "low", "medium", or "high"
        self.next_skill: str | None = data.get("next_skill")  # auto-chain to this skill on completion
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
    for path in sorted(_SKILLS_DIR.glob("**/*.yaml")):
        if path.name.startswith("_"):
            continue
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
    """Return a summary of all loaded skills for the UI (excludes internal skills)."""
    return [
        {
            "name": s.name,
            "description": s.description,
            "model": s.model_tier,
            "tools": s.tool_names,
        }
        for s in _skills.values()
        if not s.description.startswith("[INTERNAL")
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
    routable_skills = []
    for skill in _skills.values():
        # Skip internal chained skills — they are not user-facing entry points
        if "[INTERNAL" in skill.description.upper():
            continue
        routable_skills.append(skill)
        lines.append(f'{len(routable_skills)}. "{skill.name}" — {skill.description}')
        lines.append("")

    # Add a catch-all for greetings / small talk that need no skill
    lines.append(f'{len(routable_skills) + 1}. "none" — Greetings, small talk, thanks, goodbyes, '
                 f'or simple conversational messages that do NOT require any data lookup or action '
                 f'(e.g. "hi", "hello", "thanks", "how are you").')
    lines.append("")

    skill_names = [f'"{ s.name}"' for s in routable_skills]
    skill_names.append('"none"')
    examples = " or ".join(f'{{"skill": {n}}}' for n in skill_names)
    lines.append(f"Respond with ONLY a JSON object, no other text:")
    lines.append(examples)
    return "\n".join(lines)


ROUTER_PROMPT = _build_router_prompt()
logger.debug("Router prompt:\n%s", ROUTER_PROMPT)


def _route(user_input: str) -> str:
    """Classify user intent and return the skill name."""
    # If there's an active session awaiting confirmation, inject context
    prompt = ROUTER_PROMPT
    if _active_session:
        active_skill = _active_session["skill_name"]
        prompt += (
            "\n\n"
            "IMPORTANT CONTEXT: There is an active session for the "
            f'"{active_skill}" skill that is currently awaiting user confirmation. '
            "If the user's message is responding to this active session "
            "(e.g. confirming, correcting, providing new information, answering '"
            "a question that was asked), classify it as "
            f'{{"skill": "{active_skill}"}}. '
            "If the message is clearly unrelated (e.g. a greeting, a status check, "
            "a completely different question), classify it normally."
        )
        logger.info("[Router] Active session detected for '%s' — injecting context", active_skill)

    client = get_responses_client()
    response = client.responses.create(
        model=CHAT_MODEL,
        instructions=prompt,
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
        if skill_name == "none":
            return "none"
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

def handle_tool_call(name: str, arguments: str, on_progress=None) -> str:
    """Execute a tool call and return the result string."""
    args = json.loads(arguments)
    handler = _TOOL_HANDLERS.get(name)
    if handler:
        return handler(args, on_progress=on_progress, workiq_cli=WORKIQ_CLI)
    return f"Unknown tool: {name}"


# ---------------------------------------------------------------------------
# Conversation history for conversational skills
# ---------------------------------------------------------------------------

_conversation_histories: dict[str, list[dict]] = {}

# Active session — tracks when a multi-turn skill is awaiting user input
# Structure: {"skill_name": str, "stage": str} or None
_active_session: dict | None = None


def get_active_session() -> dict | None:
    """Return the current active session, if any."""
    return _active_session


def reset_qa_history():
    """Clear all conversational skill histories and active session."""
    global _active_session
    _conversation_histories.clear()
    _active_session = None
    logger.info("Conversation histories and active session cleared.")


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
    api_kwargs: dict = dict(
        model=skill.model,
        instructions=skill.instructions,
        input=input_messages,
        tools=tools if tools else [],
    )
    if skill.reasoning_effort:
        api_kwargs["reasoning"] = {"effort": skill.reasoning_effort}
    response = client.responses.create(**api_kwargs)


    # Tool-call loop (only if skill has tools)
    if tools:
        step = 1
        while True:
            # Log any reasoning/thinking the model produced before tool calls
            for item in response.output:
                if hasattr(item, "type") and item.type == "reasoning":
                    for part in getattr(item, "summary", []):
                        logger.info("[%s] Reasoning: %s", skill.name, getattr(part, "text", ""))

            tool_calls = [item for item in response.output if item.type == "function_call"]
            if not tool_calls:
                break

            # Tools that produce their own visible output or are internal bookkeeping
            _silent_tools = {"log_progress", "engagement_context", "get_hub_config"}

            tool_results = []
            for tc in tool_calls:
                logger.info("[%s Step %d] Calling tool: %s", skill.name, step, tc.name)
                if on_progress and tc.name not in _silent_tools:
                    on_progress("step", f"Step {step}: {tc.name}")
                result = handle_tool_call(tc.name, tc.arguments, on_progress)
                tool_results.append({
                    "type": "function_call_output",
                    "call_id": tc.call_id,
                    "output": result,
                })

            step += 1
            client = get_responses_client()
            loop_kwargs: dict = dict(
                model=skill.model,
                instructions=skill.instructions,
                input=tool_results,
                tools=tools,
                previous_response_id=response.id,
            )
            if skill.reasoning_effort:
                loop_kwargs["reasoning"] = {"effort": skill.reasoning_effort}
            response = client.responses.create(**loop_kwargs)

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

    # --- Active session & chaining logic ---
    global _active_session

    # [AWAITING_CONFIRMATION] — skill is pausing for user input
    if "[AWAITING_CONFIRMATION]" in (final_text or ""):
        _active_session = {"skill_name": skill.name, "stage": "awaiting_confirmation"}
        final_text = final_text.replace("[AWAITING_CONFIRMATION]", "").strip()
        logger.info("[%s] Awaiting user confirmation — session active", skill.name)
        return final_text

    # [STOP_CHAIN] — skill hit an error, stop chaining and clear session
    if "[STOP_CHAIN]" in (final_text or ""):
        _active_session = None
        logger.info("[%s] Stop chain — clearing active session", skill.name)
        # Don't strip [STOP_CHAIN] here — it's already handled by existing code
        return final_text

    # Normal completion — clear active session and chain if configured
    _active_session = None

    # Autonomous skill chaining — run next_skill if configured
    if skill.next_skill:
        next_skill_obj = _skills.get(skill.next_skill)
        if next_skill_obj:
            logger.info("[%s] Chaining to: %s", skill.name, skill.next_skill)
            if on_progress:
                on_progress("step", f"Chaining to {next_skill_obj.name}...")
                on_progress("agent", next_skill_obj.name)
            chain_input = final_text or "Continue with the next phase."
            return _run_skill(next_skill_obj, chain_input, on_progress)
        else:
            logger.warning("[%s] next_skill '%s' not found — stopping chain", skill.name, skill.next_skill)

    return final_text


# ---------------------------------------------------------------------------
# Master entry point — routes to the right skill
# ---------------------------------------------------------------------------

def route(user_input: str) -> str:
    """Public wrapper around the router — returns the skill name."""
    return _route(user_input)


def get_skill(name: str):
    """Return a Skill by name, or None."""
    return _skills.get(name)


def run_skill(skill_name: str, user_input: str, on_progress=None) -> str:
    """Run a specific skill by name — skips routing.

    Use this when the caller already knows which skill to invoke
    (e.g. after calling route() separately).
    """
    # Handle small talk / greetings directly — no skill needed
    if skill_name == "none":
        logger.info("Handling as small talk — direct LLM reply")
        client = get_responses_client()
        resp = client.responses.create(
            model=CHAT_MODEL,
            instructions=(
                "You are Hub SE Agent, a friendly helper for Microsoft 365 data. "
                "Respond briefly and naturally to greetings and small talk. "
                "Let the user know you can help with their M365 data — calendar, emails, "
                "documents, contacts — or create engagement agendas and meeting invites."
            ),
            input=[{"role": "user", "content": user_input}],
            tools=[],
        )
        reply = ""
        for item in resp.output:
            if item.type == "message":
                for part in item.content:
                    if part.type == "output_text":
                        reply += part.text
        return reply or "Hey! How can I help you today?"

    skill = _skills.get(skill_name)
    if not skill:
        logger.warning("Skill '%s' not found — falling back to qa", skill_name)
        skill = _skills.get("qa")

    # If this is a fresh invocation of a conversational skill (not a continuation
    # of an active session), clear its conversation history for a clean start.
    if (skill and skill.conversational
            and not (_active_session and _active_session.get("skill_name") == skill_name)):
        _conversation_histories.pop(skill.name, None)
        logger.info("[%s] Fresh invocation — cleared conversation history", skill.name)

    if skill and on_progress:
        on_progress("agent", skill.name)

    return _run_skill(skill, user_input, on_progress)


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
    return run_skill(skill_name, user_input, on_progress)
