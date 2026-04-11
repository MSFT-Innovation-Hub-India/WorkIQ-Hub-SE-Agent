"""
Tool: query_workiq
Query the user's Microsoft 365 data via WorkIQ CLI.
"""

import logging
import subprocess
import sys

logger = logging.getLogger("hub_se_agent")

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


SCHEMA = {
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
}


def handle(arguments: dict, *, on_progress=None, workiq_cli=None, **kwargs) -> str:
    """Run WorkIQ CLI and return the output."""
    question = arguments["question"]
    if not workiq_cli:
        return "Error: workiq CLI not found. Install it or set WORKIQ_PATH in .env"
    logger.info("[WorkIQ] Querying: %s", question[:200])
    if on_progress:
        on_progress("tool", f"Querying WorkIQ: {question[:200]}")
    try:
        # Windows command line limit is ~8191 chars. For long questions,
        # pipe via stdin in interactive mode instead of using -q argument.
        if len(question) > 7000:
            logger.info("[WorkIQ] Question too long for CLI arg (%d chars), using stdin", len(question))
            result = subprocess.run(
                [workiq_cli, "ask"],
                input=question + "\n",
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=180,
                creationflags=_NO_WINDOW,
            )
        else:
            result = subprocess.run(
                [workiq_cli, "ask", "-q", question],
                capture_output=True,
                text=True,
                encoding="utf-8",
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
