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
from redis_entraid.cred_provider import create_from_default_azure_credential

logger = logging.getLogger("workiq_assistant")


class RedisBridge:
    """Polls a Redis inbox stream for remote tasks and writes results to an outbox stream."""

    def __init__(self, user_email: str, user_name: str,
                 endpoint: str, ttl: int = 86400):
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
        self._poller_thread: threading.Thread | None = None
        self._heartbeat_thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def _connect(self):
        """Create the Redis cluster connection using redis-entraid credential provider."""
        credential_provider = create_from_default_azure_credential(
            ("https://redis.azure.com/.default",)
        )

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
        logger.info("Redis bridge connected to %s:%d (credential_provider)",
                     self._host, self._port)

    def _ensure_connected(self):
        """Reconnect if the client isn't set."""
        if self._client is None:
            self._connect()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self, task_queue):
        """Start the bridge: connect, register, begin polling."""
        self._task_queue = task_queue
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
        """Process a single inbox message."""
        text = fields.get("text", "").strip()
        sender = fields.get("sender", "remote")
        msg_id = fields.get("msg_id", stream_id)

        if not text:
            logger.warning("Empty inbox message %s — skipping", stream_id)
            return

        logger.info("Remote message from %s (msg_id=%s): %.80s", sender, msg_id, text)

        task = self._task_queue.submit_task(text, source="remote")

        # Remember the correlation for the outbox reply
        with self._pending_lock:
            self._pending_replies[task.id] = msg_id

    # ------------------------------------------------------------------
    # Outbox writer (called by task_queue.on_task_complete)
    # ------------------------------------------------------------------

    def on_task_done(self, task):
        """Write the task result to the outbox stream (only for remote tasks)."""
        if task.source != "remote":
            return

        # Look up the correlation msg_id
        with self._pending_lock:
            in_reply_to = self._pending_replies.pop(task.id, "")

        try:
            self._ensure_connected()
            self._client.xadd(self._outbox_key, {
                "task_id": task.id,
                "status": task.status,
                "text": (task.result or task.error or "")[:4000],
                "ts": str(time.time()),
                "in_reply_to": in_reply_to,
            })
            # Keep the outbox trimmed
            self._client.xtrim(self._outbox_key, maxlen=100, approximate=True)
            logger.info("Outbox reply for task %s (in_reply_to=%s)", task.id, in_reply_to)
        except Exception as e:
            logger.error("Failed to write outbox for task %s: %s", task.id, e)
