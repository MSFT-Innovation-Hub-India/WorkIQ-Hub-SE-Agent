"""
WorkIQ Assistant — Single-process launcher.

Runs completely invisibly:
  • WebSocket server runs in a background thread
  • pywebview window starts HIDDEN
  • Press Ctrl+Shift+M (Cmd+Shift+M on Mac) to show/hide the chat UI
  • Toast notifications appear regardless of UI visibility
  • No console window, no taskbar icon until you summon it

Launch:  pythonw meeting_agent.py          (invisible, no console)
   or:   python  meeting_agent.py          (with console for debugging)
"""

import asyncio
import ctypes
import json
import logging
import os
import platform
import subprocess
import sys
import threading
import time
from pathlib import Path

import websockets
from websockets.asyncio.server import serve
import webview

# ---------------------------------------------------------------------------
# Logging — file + (optionally) console
# ---------------------------------------------------------------------------

LOG_DIR = Path.home() / ".workiq-assistant"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / "agent.log"
_TOAST_SCRIPT = LOG_DIR / "_toast.ps1"
_SCRIPT_DIR = Path(__file__).resolve().parent
_HTML_PATH = _SCRIPT_DIR / "chat_ui.html"

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("workiq_assistant")

from agent_core import run_agent, check_azure_auth, run_az_login, _resolve_organizer, reset_qa_history, get_loaded_skills

IS_WIN = platform.system() == "Windows"
IS_MAC = platform.system() == "Darwin"
HOST = "127.0.0.1"
PORT = 18080
HOTKEY_COMBO = "<cmd>+<shift>+m" if IS_MAC else "<ctrl>+<alt>+m"
HOTKEY_LABEL = "Cmd+Shift+M" if IS_MAC else "Ctrl+Alt+M"

# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------

_clients: set = set()
_loop: asyncio.AbstractEventLoop | None = None
_window = None  # pywebview window reference


# ---------------------------------------------------------------------------
# Toast notifications
# ---------------------------------------------------------------------------

def notify(title: str, message: str):
    """Show a native desktop notification."""
    try:
        if IS_MAC:
            safe_t = title.replace("\\", "\\\\").replace('"', '\\"')
            safe_m = message.replace("\\", "\\\\").replace('"', '\\"')
            subprocess.Popen([
                "osascript", "-e",
                f'display notification "{safe_m}" with title "{safe_t}"',
            ])
        elif IS_WIN:
            from winotify import Notification
            icon_path = _SCRIPT_DIR / "agent_icon.png"
            toast = Notification(
                app_id="WorkIQ Assistant",
                title=title,
                msg=message[:300],
                icon=str(icon_path) if icon_path.exists() else "",
                launch=f"http://{HOST}:{HTTP_PORT}/show",
            )
            toast.show()
        else:
            subprocess.Popen(["notify-send", title, message])
    except Exception as e:
        logger.warning("Notification failed: %s", e)


# ---------------------------------------------------------------------------
# Broadcast to connected UI (WebSocket)
# ---------------------------------------------------------------------------

def _broadcast(message: dict):
    data = json.dumps(message)
    if _loop is None:
        return
    for ws in list(_clients):
        asyncio.run_coroutine_threadsafe(_safe_send(ws, data), _loop)


async def _safe_send(ws, data: str):
    try:
        await ws.send(data)
    except Exception:
        _clients.discard(ws)


# ---------------------------------------------------------------------------
# Agent worker
# ---------------------------------------------------------------------------

_task_lock = threading.Lock()


def _run_task(user_input: str):
    _broadcast({"type": "task_started"})
    notify("WorkIQ Assistant", "Working on your request...")

    def on_progress(kind: str, message: str):
        _broadcast({"type": "progress", "kind": kind, "message": message})

    try:
        result = run_agent(user_input, on_progress=on_progress)
        logger.info("Task complete:\n%s", result)
        _broadcast({"type": "task_complete", "result": result})
        first_line = result.strip().split("\n")[0]
        summary = first_line[:200] + "\u2026" if len(first_line) > 200 else first_line
        notify("Task Complete", summary)
        # Show the window so user sees the result
        _show_window()
    except Exception as e:
        logger.error("Task failed: %s", e, exc_info=True)
        _broadcast({"type": "task_error", "error": str(e)[:500]})
        notify("Task Failed", str(e)[:200])


def _start_task(user_input: str):
    if _task_lock.locked():
        _broadcast({"type": "task_error", "error": "A task is already running. Please wait."})
        return
    t = threading.Thread(target=_locked_run, args=(user_input,), daemon=True)
    t.start()


def _locked_run(user_input: str):
    with _task_lock:
        _run_task(user_input)


# ---------------------------------------------------------------------------
# WebSocket handler
# ---------------------------------------------------------------------------

async def _handler(ws):
    _clients.add(ws)
    logger.info("UI connected (%d client(s))", len(_clients))
    try:
        auth_ok, _ = check_azure_auth()
        if auth_ok:
            try:
                name, email = _resolve_organizer()
                await ws.send(json.dumps({
                    "type": "auth_status", "ok": True,
                    "user": f"{name} <{email}>",
                }))
            except Exception:
                await ws.send(json.dumps({
                    "type": "auth_status", "ok": True, "user": "Authenticated",
                }))
        else:
            await ws.send(json.dumps({"type": "auth_status", "ok": False}))

        # Send loaded skills to the UI
        await ws.send(json.dumps({
            "type": "skills_list",
            "skills": get_loaded_skills(),
        }))

        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("type") == "task":
                user_input = msg.get("input", "").strip()
                if user_input:
                    _start_task(user_input)
            elif msg.get("type") == "signin":
                threading.Thread(target=_handle_signin, daemon=True).start()
            elif msg.get("type") == "clear_history":
                reset_qa_history()
                await ws.send(json.dumps({
                    "type": "history_cleared",
                    "message": "Conversation history cleared.",
                }))
    except websockets.ConnectionClosed:
        pass
    finally:
        _clients.discard(ws)
        logger.info("UI disconnected (%d client(s))", len(_clients))


def _handle_signin():
    _broadcast({"type": "progress", "kind": "step",
                "message": "Opening browser for Azure sign-in..."})
    notify("WorkIQ Assistant", "Opening browser for Azure sign-in...")
    ok, msg = run_az_login()
    _broadcast({"type": "signin_status", "ok": ok, "message": msg})
    if ok:
        logger.info("Azure sign-in succeeded: %s", msg)
        notify("Azure Sign-in", msg)
        # Update auth status for all connected clients
        try:
            name, email = _resolve_organizer()
            _broadcast({"type": "auth_status", "ok": True,
                        "user": f"{name} <{email}>"})
        except Exception:
            _broadcast({"type": "auth_status", "ok": True,
                        "user": "Authenticated"})
    else:
        logger.warning("Azure sign-in failed: %s", msg)
        notify("Azure Sign-in Failed", msg)


# ---------------------------------------------------------------------------
# WebSocket server + HTTP handler for toast clicks (runs in background thread)
# ---------------------------------------------------------------------------

HTTP_PORT = PORT + 1  # 18081

def _run_server():
    """Start the asyncio event loop + WebSocket server + HTTP server in this thread."""
    global _loop
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)

    async def _serve():
        async with serve(_handler, HOST, PORT):
            logger.info("WebSocket server listening on ws://%s:%d", HOST, PORT)

            # Tiny HTTP server for toast click-to-open
            from http.server import HTTPServer, BaseHTTPRequestHandler

            class _ShowHandler(BaseHTTPRequestHandler):
                def do_GET(self):
                    _show_window()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.end_headers()
                    self.wfile.write(b"<html><body><script>window.close()</script>Opening WorkIQ Assistant...</body></html>")

                def log_message(self, *args):
                    pass  # suppress HTTP logs

            http_server = HTTPServer((HOST, HTTP_PORT), _ShowHandler)
            http_thread = threading.Thread(target=http_server.serve_forever, daemon=True)
            http_thread.start()
            logger.info("HTTP server for toast clicks on http://%s:%d", HOST, HTTP_PORT)

            await asyncio.Future()  # run forever

    _loop.run_until_complete(_serve())


# ---------------------------------------------------------------------------
# Window show/hide
# ---------------------------------------------------------------------------

def _set_taskbar_icon():
    """Force our custom icon on the pywebview HWND so the taskbar shows it
    instead of the default pythonw.exe Python icon."""
    if not IS_WIN or _window is None:
        return
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        WM_SETICON = 0x0080
        ICON_BIG = 1
        ICON_SMALL = 0
        IMAGE_ICON = 1
        LR_LOADFROMFILE = 0x0010
        LR_DEFAULTSIZE = 0x0040

        ico = str(_SCRIPT_DIR / "agent_icon.ico")

        # Load the .ico as big (32x32) and small (16x16) HICON handles
        hicon_big = user32.LoadImageW(
            None, ico, IMAGE_ICON, 32, 32, LR_LOADFROMFILE
        )
        hicon_small = user32.LoadImageW(
            None, ico, IMAGE_ICON, 16, 16, LR_LOADFROMFILE
        )

        # Find the top-level window by title
        hwnd = user32.FindWindowW(None, "WorkIQ Assistant")
        if hwnd and hicon_big:
            user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, hicon_big)
        if hwnd and hicon_small:
            user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, hicon_small)

        logger.info("Taskbar icon set via Win32 (hwnd=%s)", hwnd)
    except Exception as e:
        logger.warning("Failed to set taskbar icon: %s", e)


def _show_window():
    """Show the pywebview window (thread-safe)."""
    if _window is not None:
        try:
            _window.show()
            _set_taskbar_icon()
        except Exception:
            pass


def _hide_window():
    """Hide the pywebview window (thread-safe)."""
    if _window is not None:
        try:
            _window.hide()
        except Exception:
            pass


def _toggle_window():
    """Toggle the pywebview window visibility."""
    if _window is None:
        return
    try:
        # pywebview doesn't have a reliable .hidden property on all
        # backends, so we track it ourselves
        if getattr(_window, '_agent_hidden', True):
            _window.show()
            _window._agent_hidden = False
        else:
            _window.hide()
            _window._agent_hidden = True
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Global hotkey
# ---------------------------------------------------------------------------

def _setup_hotkey():
    try:
        from pynput import keyboard
        listener = keyboard.GlobalHotKeys({HOTKEY_COMBO: _toggle_window})
        listener.daemon = True
        listener.start()
        logger.info("Global hotkey registered: %s", HOTKEY_LABEL)
    except ImportError:
        logger.warning("pynput not installed — hotkey disabled")
    except Exception as e:
        logger.warning("Could not register hotkey: %s", e)


# ---------------------------------------------------------------------------
# pywebview lifecycle hooks
# ---------------------------------------------------------------------------

def _on_shown():
    """Called when the pywebview window is first shown."""
    global _window
    # Immediately hide — we only want the window visible on demand
    if _window is not None:
        _window.hide()
        _window._agent_hidden = True


def _on_closing():
    """Intercept window close — hide instead of quitting."""
    _hide_window()
    if _window is not None:
        _window._agent_hidden = True
    return False  # prevent actual close


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    global _window

    logger.info("=" * 50)
    logger.info("WorkIQ Assistant starting (single-process mode)")
    logger.info("Hotkey: %s  |  Log: %s", HOTKEY_LABEL, LOG_FILE)
    logger.info("=" * 50)

    # 1. Start WebSocket server in a background thread
    server_thread = threading.Thread(target=_run_server, daemon=True)
    server_thread.start()

    # 2. Register global hotkey
    _setup_hotkey()

    # 3. Toast to let user know it's running
    notify("WorkIQ Assistant", f"Running in background. Press {HOTKEY_LABEL} to open.")

    # 4. Tell Windows this is a distinct app (not generic pythonw.exe)
    #    so the taskbar shows our custom icon instead of the Python icon.
    if IS_WIN:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "Microsoft.WorkIQAssistant"
        )

    # 5. Create the pywebview window (starts hidden)
    _window = webview.create_window(
        title="WorkIQ Assistant",
        url=str(_HTML_PATH),
        width=480,
        height=600,
        resizable=True,
        text_select=True,
        on_top=False,
        hidden=True,
    )
    _window._agent_hidden = True
    _window.events.closing += _on_closing

    def _on_shown():
        _set_taskbar_icon()

    _window.events.shown += _on_shown

    # 6. Start pywebview event loop (blocks until process exits)
    _icon_path = _SCRIPT_DIR / "agent_icon.ico"
    webview.start(debug=False, icon=str(_icon_path) if _icon_path.exists() else None)


if __name__ == "__main__":
    main()
