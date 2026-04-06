"""
Redis Bridge — connects the local agent to Azure Managed Redis for remote
task delivery (e.g. from a Teams relay service).

Inbox stream:  workiq:inbox:{email}   — remote senders push messages here
Outbox stream: workiq:outbox:{email}  — agent pushes results here
Agent key:     workiq:agents:{email}  — presence registration with TTL

Optional — only active when AZ_REDIS_CACHE_ENDPOINT is configured.
"""

import json
import logging
import threading
import time

import redis
from redis_entraid.cred_provider import EntraIdCredentialsProvider
from redis_entraid.identity_provider import DefaultAzureCredentialProvider
from redis.auth.token_manager import TokenManagerConfig, RetryPolicy

logger = logging.getLogger("hub_se_agent")


class RedisBridge:
    """Polls a Redis inbox stream for remote tasks and writes results to an outbox stream."""

    def __init__(self, user_email: str, user_name: str,
                 endpoint: str, credential, ttl: int = 86400):
        self._credential = credential  # shared InteractiveBrowserCredential
        self._user_email = user_email.lower()
        self._user_name = user_name
        self._ttl = ttl
        self._stopping = threading.Event()

        # Parse host:port from endpoint
        parts = endpoint.rsplit(":", 1)
        self._host = parts[0]
        self._port = int(parts[1]) if len(parts) > 1 else 10000

        # Stream / key names
        self._inbox_key = f"workiq:inbox:{self._user_email}"
        self._outbox_key = f"workiq:outbox:{self._user_email}"
        self._agent_key = f"workiq:agents:{self._user_email}"

        # Track remote task_id → inbox msg_id for reply correlation
        self._pending_replies: dict[str, str] = {}  # task.id → msg_id
        self._pending_lock = threading.Lock()

        self._client: redis.RedisCluster | None = None
        self._on_broadcast = None  # set by start()
        self._poller_thread: threading.Thread | None = None
        self._heartbeat_thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def _connect(self):
        """Create the Redis cluster connection using the shared credential."""
        # Wrap the shared InteractiveBrowserCredential (silent refresh via cached auth record)
        # instead of DefaultAzureCredential (which spawns az CLI processes → cmd windows)
        idp = DefaultAzureCredentialProvider(
            app=self._credential,
            scopes=("https://redis.azure.com/.default",),
        )
        token_mgr_config = TokenManagerConfig(
            0.7, 0, 100, RetryPolicy(3, 3)
        )
        credential_provider = EntraIdCredentialsProvider(idp, token_mgr_config)

        self._client = redis.RedisCluster(
            host=self._host,
            port=self._port,
            ssl=True,
            ssl_cert_reqs=None,
            decode_responses=True,
            credential_provider=credential_provider,
            socket_timeout=10,
            socket_connect_timeout=10,
        )
        self._client.ping()
        self._connected_at = time.time()
        logger.info("Redis bridge connected to %s:%d (credential_provider)",
                     self._host, self._port)

    _MAX_CONNECTION_AGE = 1800  # force reconnect every 30 minutes

    def _ensure_connected(self):
        """Reconnect if the client is missing or the connection is stale."""
        age = time.time() - getattr(self, '_connected_at', 0)
        if self._client is None or age > self._MAX_CONNECTION_AGE:
            if self._client is not None:
                logger.info("Redis connection stale (%.0fs) — forcing reconnect", age)
                try:
                    self._client.close()
                except Exception:
                    pass
                self._client = None
            self._connect()

    def _ping_or_reconnect(self):
        """Verify the connection is alive; reconnect if not."""
        try:
            self._ensure_connected()
            self._client.ping()
        except Exception as e:
            logger.warning("Redis PING failed (%s) — reconnecting", e)
            self._client = None
            self._connect()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self, task_queue, on_broadcast=None):
        """Start the bridge: connect, register, begin polling."""
        self._task_queue = task_queue
        self._on_broadcast = on_broadcast
        try:
            self._connect()
        except Exception as e:
            logger.error("Redis bridge failed to connect: %s", e)
            return

        self._register_agent()

        self._poller_thread = threading.Thread(
            target=self._poll_inbox, daemon=True, name="redis-inbox-poller"
        )
        self._poller_thread.start()

        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, daemon=True, name="redis-heartbeat"
        )
        self._heartbeat_thread.start()

        logger.info("Redis bridge started (inbox=%s)", self._inbox_key)

    def stop(self):
        """Signal the bridge to stop."""
        self._stopping.set()
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass
        logger.info("Redis bridge stopped")

    # ------------------------------------------------------------------
    # Agent registration
    # ------------------------------------------------------------------

    def _register_agent(self):
        """Register / refresh this agent's presence in Redis."""
        try:
            self._ensure_connected()
            info = json.dumps({
                "name": self._user_name,
                "email": self._user_email,
                "started_at": time.time(),
                "version": "1.0",
            })
            self._client.set(self._agent_key, info, ex=self._ttl)
            logger.info("Agent registered: %s (TTL=%ds)", self._agent_key, self._ttl)
        except Exception as e:
            logger.error("Agent registration failed: %s", e)

    def _heartbeat_loop(self):
        """Refresh the agent registration key every 30 minutes."""
        while not self._stopping.wait(timeout=1800):
            try:
                self._register_agent()
            except Exception as e:
                logger.warning("Heartbeat refresh failed: %s", e)

    # ------------------------------------------------------------------
    # Inbox poller
    # ------------------------------------------------------------------

    def _poll_inbox(self):
        """Block-read from the inbox stream, dispatching messages to the task queue."""
        last_id = "$"  # only new messages from this point forward
        while not self._stopping.is_set():
            try:
                self._ensure_connected()
                # XREAD with 5-second block timeout
                result = self._client.xread(
                    {self._inbox_key: last_id}, block=5000, count=10
                )
                if not result:
                    continue

                for _stream, messages in result:
                    for msg_id, fields in messages:
                        last_id = msg_id
                        self._handle_inbox_message(msg_id, fields)

            except redis.ConnectionError as e:
                logger.warning("Redis connection lost: %s — reconnecting in 5s", e)
                self._client = None
                self._stopping.wait(timeout=5)
            except Exception as e:
                logger.error("Inbox poll error: %s", e, exc_info=True)
                self._stopping.wait(timeout=5)

    def _handle_inbox_message(self, stream_id: str, fields: dict):
        """Process a single inbox message.

        Routes the message first — system skills (queued=False) run inline
        so they don't appear as "running tasks" in their own status output.
        """
        text = fields.get("text", "").strip()
        sender = fields.get("sender", "remote")
        msg_id = fields.get("msg_id", stream_id)

        if not text:
            logger.warning("Empty inbox message %s — skipping", stream_id)
            return

        logger.info("Remote message from %s (msg_id=%s): %.80s", sender, msg_id, text)

        # Show the remote user's message in the local chat UI
        if self._on_broadcast:
            self._on_broadcast({
                "type": "remote_message",
                "sender": sender,
                "text": text,
            })

        # Route first to check if this is a system (non-queued) skill
        from agent_core import route, get_skill, run_skill

        try:
            skill_name = route(text)
            skill = get_skill(skill_name)
        except Exception as e:
            logger.error("Router failed for remote message: %s", e, exc_info=True)
            skill_name = None
            skill = None

        if skill and not skill.queued:
            # System skill — run inline and write reply directly to outbox
            logger.info("Remote system task (skill=%s): %.80s", skill_name, text)
            threading.Thread(
                target=self._run_system_task_remote,
                args=(skill_name, text, msg_id),
                daemon=True,
            ).start()
        else:
            # Business task — enqueue as before
            task = self._task_queue.submit_task(text, source="remote",
                                                skill_name=skill_name)

            # Remember the correlation for the outbox reply
            with self._pending_lock:
                self._pending_replies[task.id] = msg_id

    def _run_system_task_remote(self, skill_name: str, text: str, msg_id: str):
        """Run a non-queued skill for a remote message and write the reply to outbox."""
        import uuid
        from agent_core import run_skill
        from task_queue import TaskItem

        task_id = uuid.uuid4().hex[:8]

        # Pre-register the correlation so on_task_done can find the msg_id
        with self._pending_lock:
            self._pending_replies[task_id] = msg_id

        # Broadcast task_started so the local UI shows activity
        if self._on_broadcast:
            self._on_broadcast({"type": "task_started", "request_id": task_id,
                                "source": "remote"})

        def on_progress(kind: str, message: str):
            if self._on_broadcast:
                self._on_broadcast({"type": "progress", "request_id": task_id,
                                    "kind": kind, "message": message})

        try:
            result = run_skill(skill_name, text, on_progress=on_progress)
            status = "completed"
            logger.info("Remote system task complete (skill=%s): %.100s", skill_name, result)
            if self._on_broadcast:
                self._on_broadcast({"type": "task_complete", "request_id": task_id,
                                    "result": result})
        except Exception as e:
            logger.error("Remote system task failed (skill=%s): %s", skill_name, e, exc_info=True)
            result = str(e)
            status = "failed"
            if self._on_broadcast:
                self._on_broadcast({"type": "task_error", "request_id": task_id,
                                    "error": str(e)[:500]})

        # Build a synthetic task to reuse on_task_done's outbox write logic
        task = TaskItem(
            id=task_id,
            user_input=text,
            source="remote",
            status=status,
            result=result if status == "completed" else None,
            error=result if status == "failed" else None,
        )
        self.on_task_done(task)

    # ------------------------------------------------------------------
    # Outbox writer (called by task_queue.on_task_complete)
    # ------------------------------------------------------------------

    def on_task_done(self, task):
        """Write the task result to the outbox stream (only for remote tasks).

        Uses PING-before-write and retry-with-reconnect to guard against
        stale connections that silently drop writes after long idle periods.
        """
        if task.source != "remote":
            return

        # Look up the correlation msg_id
        with self._pending_lock:
            in_reply_to = self._pending_replies.pop(task.id, "")

        payload = {
            "task_id": task.id,
            "status": task.status,
            "text": (task.result or task.error or "")[:4000],
            "ts": str(time.time()),
            "in_reply_to": in_reply_to,
        }

        for attempt in range(2):
            try:
                self._ping_or_reconnect()
                self._client.xadd(self._outbox_key, payload)
                # Keep the outbox trimmed
                self._client.xtrim(self._outbox_key, maxlen=100, approximate=True)
                logger.info("Outbox reply for task %s (in_reply_to=%s, attempt=%d)",
                            task.id, in_reply_to, attempt + 1)
                return  # success
            except Exception as e:
                logger.warning("Outbox write attempt %d failed for task %s: %s",
                               attempt + 1, task.id, e)
                self._client = None  # force full reconnect on retry

        logger.error("Failed to write outbox for task %s after 2 attempts", task.id)
