"""Claude Code channel for AgentChat (prototype, Claude Code Channels research preview).

An MCP stdio server (newline-delimited JSON-RPC 2.0). It declares the experimental
`claude/channel` capability and pushes unread chat messages into the session as
`notifications/claude/channel`. Claude answers through the chat_* tools. Only protocol
messages go to stdout; logs go to stderr. Stdlib only.

Batching matches skills/join-claude/watch.py: messages from the user or mentioning @claude/@all go
out at once; others after QUIET seconds without news, or HOLD seconds after the first held.
Nothing is acked here: Claude calls chat_ack after handling, so a restart replays unacked messages.
"""
import json
import os
import sys
import threading
import time

from agentchat import store

AGENT = "claude"
QUIET = float(os.environ.get("CHANNEL_QUIET_S", 45))
HOLD = float(os.environ.get("CHANNEL_HOLD_S", 120))
SETTLE = float(os.environ.get("CHANNEL_SETTLE_S", 0.5))   # an urgent message waits this long for the rest of a burst
SETTLE_MAX = 2.0
EVENT_CHARS = 16000                 # whole messages up to this much text share one event; the rest go in the next
SUPPORTED = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
INSTRUCTIONS = (
    'AgentChat messages arrive as <channel source="agentchat" first_id=".." last_id=".." senders="..">: each message '
    'is a "#id sender:" line followed by its exact text (which may span lines), with a blank line between messages. '
    'last_id is the last message carried. Reply with chat_reply only when mentioned (@claude, @all) or genuinely useful. '
    "Before starting a user request, call chat_take with its id; if it fails, leave it to the owner. After handling, "
    "call chat_ack with the last id handled (never ack unhandled ids). Use chat_read for history."
)


def log(text):
    print(f"claude_channel: {text}", file=sys.stderr, flush=True)


def block(message):
    """A message's header line, then its exact text: newlines stay newlines, nothing is cut or replaced."""
    tag = "" if message["kind"] == "msg" else f"/{message['kind']}"
    return f"#{message['id']} {message['sender']}{tag}:\n{message['text']}"


def split(batch):
    """The messages one event carries, whole, and the rest for the next event. A message longer than
    EVENT_CHARS travels alone and whole: an event never carries part of a message or points past itself."""
    used = 0
    for number, message in enumerate(batch):
        used += len(block(message)) + (2 if number else 0)   # the blank line between messages counts too
        if number and used > EVENT_CHARS:
            return batch[:number], batch[number:]
    return batch, []


def urgent(message):
    """The user, or a real @claude/@all in prose: the store's rule, so quoted or code mentions don't wake."""
    return message["sender"] == "user" or AGENT in store.mentioned_agents(message["text"])


TOOLS = [
    {"name": "chat_reply", "description": "Post a message to AgentChat as claude.",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    {"name": "chat_ack", "description": "Acknowledge messages up to this id as handled.",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]}},
    {"name": "chat_take", "description": "Claim a user request (message id) before working on it.",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]}},
    {"name": "chat_read", "description": "Read recent messages (exact text), or those after an id. Does not ack.",
     "inputSchema": {"type": "object", "properties": {"after": {"type": "integer"}}}},
]


class Channel:
    def __init__(self, out=None):
        self.out = out or sys.stdout
        self.write_lock = threading.Lock()
        self.started = False
        self.notified = 0                   # the last message delivered to Claude: acked up to it means caught up
        self.presence = threading.Lock()    # deciding "caught up" and writing presence is one step against delivery
        self.stopping = threading.Event()   # set when the client goes: the watcher ends instead of running on

    def send(self, obj):
        data = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
        with self.write_lock:
            self.out.write(data + "\n")
            self.out.flush()

    def notify(self, batch):
        meta = {"first_id": str(batch[0]["id"]), "last_id": str(batch[-1]["id"]),
                "count": str(len(batch)),
                "senders": ",".join(dict.fromkeys(m["sender"] for m in batch))}
        self.send({"jsonrpc": "2.0", "method": "notifications/claude/channel",
                   "params": {"content": "\n\n".join(block(m) for m in batch), "meta": meta}})

    # --- watcher -------------------------------------------------------------
    def watch(self):
        try:
            delivered = store.get_ack(AGENT)
        except Exception as error:
            log(f"cannot read ack: {error}")
            delivered = 0
        held, first, last, called = [], 0.0, 0.0, None   # called: when the first urgent message was held

        def due():
            soon = min(first + HOLD, last + QUIET)
            return soon if called is None else min(soon, last + SETTLE, called + SETTLE_MAX)

        while not self.stopping.is_set():
            now = time.monotonic()
            wait = 25.0 if not held else max(0.05, due() - now)
            try:                            # status is best effort: a failed write mustn't keep messages back
                with self.presence:
                    if not held and store.get_ack(AGENT) >= self.notified:   # caught up (own messages aren't acked)
                        store.set_presence(AGENT, "listening", ttl=90)
            except Exception as error:
                log(f"presence not updated: {error}")
            try:
                store.wait_for_change(delivered, timeout=min(wait, 25.0))
                rows = store.read_messages(delivered)
            except Exception as error:
                log(f"store error: {error}")
                time.sleep(5)
                continue
            fresh = []
            for message in rows:
                delivered = max(delivered, message["id"])
                if message["sender"] != AGENT and message["kind"] == "msg":
                    fresh.append(message)
            now = time.monotonic()
            if fresh:
                first = first if held else now
                last = now
                held += fresh
                if called is None and any(urgent(m) for m in fresh):
                    called = now            # soon, but a burst arriving together still wakes once
            if held and now >= due():
                batch, held = split(held)   # anything left goes out next, straight away: it was due too
                if not held:
                    called = None
                with self.presence:                 # an ack deciding "caught up" can't land between these
                    self.notified = batch[-1]["id"]
                    try:
                        store.set_presence(AGENT, "working")    # before Claude can see it: until it acks the batch
                    except Exception as error:          # a status write failing mustn't stop delivery
                        log(f"presence not updated: {error}")
                    try:
                        self.notify(batch)
                    except Exception as error:          # stdout is gone: the client closed this server
                        log(f"notify failed: {error}")
                        return

    # --- tools ---------------------------------------------------------------
    def call_tool(self, name, args):
        try:
            if name == "chat_reply":
                text = args.get("text")
                if not isinstance(text, str):
                    return "error: text must be a string", True
                return f"posted #{store.append_message(AGENT, text)['id']}", False
            if name == "chat_ack":
                with self.presence:
                    store.set_ack(AGENT, args.get("id"))
                    if args["id"] >= self.notified:     # caught up now, not at the watcher's next 25 s turn
                        try:
                            store.set_presence(AGENT, "listening", ttl=90)
                        except Exception as error:      # the ack is saved: a status write can't undo that
                            log(f"presence not updated: {error}")
                return f"acked {args['id']}", False
            if name == "chat_take":
                return f"claimed task {args.get('id')} (#{store.take_task(AGENT, args.get('id'))['id']})", False
            if name == "chat_read":
                after = args.get("after")
                rows = store.read_messages(after, limit=50) if after is not None else store.read_tail(20)
                return "\n\n".join(block(m) for m in rows) or "no messages", False
        except (store.GuardBlocked, store.ClaimConflict, ValueError, TypeError) as error:
            return f"refused: {error}", True
        except Exception as error:  # keep the server alive on any store failure
            return f"error: {error}", True
        raise KeyError(name)

    # --- JSON-RPC ------------------------------------------------------------
    def handle(self, message):
        method, ident = message.get("method"), message.get("id")
        params = message.get("params") or {}
        if method is None:  # a response to us; we send no requests
            return
        if ident is None:  # notification
            if method == "notifications/initialized" and not self.started:
                self.started = True
                threading.Thread(target=self.watch, daemon=True).start()
            return
        try:
            if method == "initialize":
                asked = params.get("protocolVersion")
                result = {"protocolVersion": asked if asked in SUPPORTED else SUPPORTED[1],
                          "capabilities": {"tools": {}, "experimental": {"claude/channel": {}}},
                          "serverInfo": {"name": "agentchat", "version": "0.1.0"},
                          "instructions": INSTRUCTIONS}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                try:
                    text, error = self.call_tool(params.get("name"), params.get("arguments") or {})
                except KeyError:
                    return self.send({"jsonrpc": "2.0", "id": ident,
                                      "error": {"code": -32602, "message": f"Unknown tool: {params.get('name')}"}})
                result = {"content": [{"type": "text", "text": text}], "isError": error}
            else:
                return self.send({"jsonrpc": "2.0", "id": ident,
                                  "error": {"code": -32601, "message": f"Method not found: {method}"}})
        except Exception as error:
            return self.send({"jsonrpc": "2.0", "id": ident, "error": {"code": -32603, "message": str(error)}})
        self.send({"jsonrpc": "2.0", "id": ident, "result": result})

    def serve(self, stdin=None):
        for raw in stdin or sys.stdin:
            raw = raw.strip()
            if not raw:
                continue
            try:
                message = json.loads(raw)
                if not isinstance(message, dict):
                    raise ValueError
            except ValueError:
                self.send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
                continue
            self.handle(message)
        self.stopping.set()                 # stdin closed: the client is gone


def main():
    Channel().serve()


if __name__ == "__main__":
    main()
