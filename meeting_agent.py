"""
Hub SE Agent — Single-process launcher.

Runs completely invisibly:
  • WebSocket server runs in a background thread
  • pywebview window starts HIDDEN
  • System tray icon: left-click to show/hide, right-click for menu
  • Toast notifications appear regardless of UI visibility
  • No console window until you summon it via tray icon or toast click

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

LOG_DIR = Path.home() / ".hub-se-agent"
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
logger = logging.getLogger("hub_se_agent")

from agent_core import run_agent, run_skill, check_azure_auth, run_az_login, reset_qa_history, get_loaded_skills, route, get_skill, get_credential
from outlook_helper import _resolve_organizer
from task_queue import queue as task_queue
import hub_config

import uuid

IS_WIN = platform.system() == "Windows"
IS_MAC = platform.system() == "Darwin"
HOST = "127.0.0.1"
PORT = 18080

# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------

_clients: set = set()
_loop: asyncio.AbstractEventLoop | None = None

# ---------------------------------------------------------------------------
# WebSocket log handler — streams log lines to the UI in real-time
# ---------------------------------------------------------------------------

_LOG_RING_SIZE = 500
_log_ring: list[dict] = []          # ring buffer of recent log entries
_log_ring_lock = threading.Lock()


class _WebSocketLogHandler(logging.Handler):
    """Logging handler that broadcasts every log record to connected UI clients."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            entry = {
                "ts": self.format(record).split("]")[0].lstrip("[") if "]" in self.format(record) else record.created,
                "level": record.levelname,
                "msg": record.getMessage(),
            }
            with _log_ring_lock:
                _log_ring.append(entry)
                if len(_log_ring) > _LOG_RING_SIZE:
                    del _log_ring[: len(_log_ring) - _LOG_RING_SIZE]
            # Broadcast to UI
            if _loop and _clients:
                _broadcast({"type": "log_entry", "entry": entry})
        except Exception:
            pass  # never break the app because of log streaming


_ws_log_handler = _WebSocketLogHandler()
_ws_log_handler.setLevel(logging.INFO)
_ws_log_handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s %(message)s", datefmt="%H:%M:%S"))
logging.getLogger("hub_se_agent").addHandler(_ws_log_handler)
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
                app_id="Hub SE Agent",
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
# Agent worker — uses task queue for business tasks, inline for system tasks
# ---------------------------------------------------------------------------

def _submit_or_execute(user_input: str, source: str = "ui"):
    """Route a request: queue it if business, execute inline if system.

    Every request gets a unique request_id so the UI can track concurrent
    messages in separate bubbles.
    """
    request_id = uuid.uuid4().hex[:8]

    try:
        skill_name = route(user_input)
        skill = get_skill(skill_name)
    except Exception as e:
        logger.error("Router failed: %s", e, exc_info=True)
        _broadcast({"type": "task_error", "request_id": request_id,
                     "error": f"Router error: {e}"})
        return

    if skill and not skill.queued:
        # System request — execute immediately in a new thread
        t = threading.Thread(
            target=_run_system_task,
            args=(request_id, skill_name, user_input),
            daemon=True,
        )
        t.start()
    else:
        # Business request — enqueue (skill_name stored on task)
        task = task_queue.submit_task(user_input, source=source,
                                      skill_name=skill_name)
        if task_queue.is_busy():
            position = task_queue.get_queue_status()["queue_depth"]
            _broadcast({
                "type": "task_queued",
                "request_id": task.id,
                "position": position,
            })
        # else: worker picks it up immediately


def _run_system_task(request_id: str, skill_name: str, user_input: str):
    """Run a non-queued (system) skill inline — uses run_skill to skip re-routing."""
    _broadcast({"type": "task_started", "request_id": request_id,
                "source": "system"})

    def on_progress(kind: str, message: str):
        _broadcast({"type": "progress", "request_id": request_id,
                     "kind": kind, "message": message})

    try:
        result = run_skill(skill_name, user_input, on_progress=on_progress)
        logger.info("System task [%s] complete: %.100s", request_id, result)
        _broadcast({"type": "task_complete", "request_id": request_id,
                     "result": result})
    except Exception as e:
        logger.error("System task [%s] failed: %s", request_id, e, exc_info=True)
        _broadcast({"type": "task_error", "request_id": request_id,
                     "error": str(e)[:500]})


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
                    _submit_or_execute(user_input, source="ui")
            elif msg.get("type") == "signin":
                threading.Thread(target=_handle_signin, daemon=True).start()
            elif msg.get("type") == "clear_history":
                reset_qa_history()
                await ws.send(json.dumps({
                    "type": "history_cleared",
                    "message": "Conversation history cleared.",
                }))
            elif msg.get("type") == "get_logs":
                with _log_ring_lock:
                    snapshot = list(_log_ring)
                await ws.send(json.dumps({
                    "type": "log_history",
                    "entries": snapshot,
                }))
            elif msg.get("type") == "get_config":
                config = hub_config.load()
                await ws.send(json.dumps({
                    "type": "config_data",
                    "config": config,
                }))
            elif msg.get("type") == "save_config":
                try:
                    hub_config.save(msg.get("config", {}))
                    await ws.send(json.dumps({
                        "type": "config_saved",
                        "ok": True,
                    }))
                except Exception as e:
                    await ws.send(json.dumps({
                        "type": "config_saved",
                        "ok": False,
                        "error": str(e),
                    }))
    except websockets.ConnectionClosed:
        pass
    finally:
        _clients.discard(ws)
        logger.info("UI disconnected (%d client(s))", len(_clients))


def _handle_signin():
    _broadcast({"type": "progress", "kind": "step",
                "message": "Opening browser for Azure sign-in..."})
    notify("Hub SE Agent", "Opening browser for Azure sign-in...")
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
                    self.wfile.write(b"<html><body><script>window.close()</script>Opening Hub SE Agent...</body></html>")

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
        hwnd = user32.FindWindowW(None, "Hub SE Agent")
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
# System tray icon
# ---------------------------------------------------------------------------

_tray: "TrayIcon | None" = None


def _setup_tray():
    """Start the Win32 system tray icon (Windows only)."""
    global _tray
    if not IS_WIN:
        return
    try:
        from tray_icon import TrayIcon
        icon_path = str(_SCRIPT_DIR / "agent_icon.ico")
        _tray = TrayIcon(
            on_show=_toggle_window,
            on_quit=_quit_app,
            icon_path=icon_path,
            tooltip="Hub SE Agent",
        )
        _tray.start()
        logger.info("System tray icon started")
    except Exception as e:
        logger.warning("Could not start tray icon: %s", e)


def _quit_app():
    """Cleanly shut down the agent."""
    logger.info("Quit requested from tray menu")
    if _tray:
        _tray.stop()
    if _window:
        _window.destroy()
    os._exit(0)


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
    logger.info("Hub SE Agent starting (single-process mode)")
    logger.info("Log: %s", LOG_FILE)
    logger.info("=" * 50)

    # 1. Configure the task queue
    def _queue_runner(user_input, skill_name=None, on_progress=None):
        """Bridge between task queue and agent_core — uses run_skill if routed."""
        if skill_name:
            return run_skill(skill_name, user_input, on_progress=on_progress)
        return run_agent(user_input, on_progress=on_progress)

    task_queue.configure(
        run_agent=_queue_runner,
        on_broadcast=_broadcast,
        on_notify=notify,
        on_show_window=_show_window,
    )

    # 1b. Start Redis bridge if configured (optional — remote task delivery)
    _redis_bridge = None
    redis_endpoint = os.environ.get("AZ_REDIS_CACHE_ENDPOINT")
    if redis_endpoint:
        try:
            from redis_bridge import RedisBridge
            name, email = _resolve_organizer()
            ttl = int(os.environ.get("REDIS_SESSION_TTL_SECONDS", "86400"))
            _redis_bridge = RedisBridge(
                user_email=email,
                user_name=name,
                endpoint=redis_endpoint,
                credential=get_credential(),
                ttl=ttl,
            )
            task_queue.configure(
                run_agent=_queue_runner,
                on_broadcast=_broadcast,
                on_notify=notify,
                on_show_window=_show_window,
                on_task_complete=_redis_bridge.on_task_done,
            )
            _redis_bridge.start(task_queue, on_broadcast=_broadcast)
        except Exception as e:
            logger.warning("Redis bridge failed to start: %s — running in local-only mode", e)
    else:
        logger.info("Redis bridge disabled — AZ_REDIS_CACHE_ENDPOINT not set")

    # 2. Start WebSocket server in a background thread
    server_thread = threading.Thread(target=_run_server, daemon=True)
    server_thread.start()

    # 3. Start system tray icon (left-click to show/hide, right-click for menu)
    _setup_tray()

    # 4. Toast to let user know it's running
    notify("Hub SE Agent", "Running in background. Click the tray icon to open.")

    # 5. Tell Windows this is a distinct app (not generic pythonw.exe)
    #    so the taskbar shows our custom icon instead of the Python icon.
    if IS_WIN:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "Microsoft.HubSEAgent"
        )

    # 6. Disable Chromium GPU acceleration to reduce memory and heat.
    #    WebView2 respects this env var for additional Chromium flags.
    if IS_WIN:
        os.environ.setdefault(
            "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS",
            "--disable-gpu --disable-gpu-compositing",
        )

    # 7. Create the pywebview window (starts hidden)
    _window = webview.create_window(
        title="Hub SE Agent",
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

    # 8. Start pywebview event loop (blocks until process exits)
    _icon_path = _SCRIPT_DIR / "agent_icon.ico"
    webview.start(debug=False, private_mode=True,
                  icon=str(_icon_path) if _icon_path.exists() else None)


if __name__ == "__main__":
    main()
