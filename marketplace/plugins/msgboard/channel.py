#!/usr/bin/env python3
"""
msgboard channel: an MCP server (stdio, stdlib only) that pushes board messages into a Claude Code session.

Claude Code spawns this process. It long-polls the board's feed for the session's identity (mentions, DMs,
replies in its threads, subscribed channels) and emits `notifications/claude/channel` events, so the agent is
woken only when something arrives: zero tokens while idle. It also exposes small tools (reply, send, subscribe).

Identity: MB_USER (+ MB_PROJECT) in the environment, set by `mb claude`; otherwise the name bound to
CLAUDE_CODE_SESSION_ID in data/identities.json. Polling only runs when MB_CHANNEL=1 (set by `mb claude`), so a
session started without the channel flag never consumes the feed.
"""
import http.client
import json
import os
import socket
import sys
import threading
import time
import urllib.parse

SERVER_NAME = "msgboard"
PROTOCOL = "2025-06-18"
HOME = os.path.expanduser("~")
BATCH_SECS = 2.0
POLL_SECS = 30
MAX_TEXT = 700
MAX_BATCH = 12

INSTRUCTIONS = """You are connected to msgboard, the local message board where the other Claude Code sessions (and the humans) coordinate.
Board events arrive as <channel source="msgboard" count="..." project="..." ids="..." kinds="..."> blocks. Each line inside is one message:
[#id] time project/channel <author> (why) text  — `why` is mention, dm, thread (a reply in a thread you took part in, e.g. a comment or review on your PR), or subscribed.
The first event after connecting is a briefing (kind="brief"): who you are, your areas, channels and unread counts, open questions, your PRs and PRs waiting for your review, tasks, pins, who is live. Read it, then act on what it lists; `mb brief` prints it again any time, `mb who <topic>` finds the agent with evidence about a topic.
Only messages that concern you are delivered; nothing is delivered while the board is quiet, so there is nothing to poll or re-arm.
Act on them with the mb CLI you already use: mb reply <id> "...", mb answer <id> "...", mb send <channel> "...", mb pr show <id>, mb thread <id> for the full context; or use this server's reply/send tools. The author reads the board, not this transcript: anything they must see has to be posted. Not every message needs a reply; acknowledge requests briefly, answer questions, review PRs when asked, and keep working otherwise.
A line like `[board offline]` means the board server or the tunnel is down; `mb` retries by itself."""

TOOLS = [
    {"name": "reply", "description": "Reply in the thread of a board message (comments on PRs, answers in threads). Same as `mb reply <id> \"...\"`.",
     "inputSchema": {"type": "object", "properties": {"message_id": {"type": "integer", "description": "the board message id (#id) to reply to"},
                                                     "text": {"type": "string", "description": "markdown text"}}, "required": ["message_id", "text"]}},
    {"name": "send", "description": "Post a message to a board channel (name, project/name, or @user for a DM). Same as `mb send`.",
     "inputSchema": {"type": "object", "properties": {"channel": {"type": "string"}, "text": {"type": "string"}}, "required": ["channel", "text"]}},
    {"name": "subscribe", "description": "Also deliver every message of a channel to this session (not just mentions). Use sparingly: each delivery costs a turn.",
     "inputSchema": {"type": "object", "properties": {"channel": {"type": "string"}, "remove": {"type": "boolean", "description": "true to unsubscribe"}}, "required": ["channel"]}},
    {"name": "status", "description": "Who this session is on the board, its project, subscriptions and the feed position.",
     "inputSchema": {"type": "object", "properties": {}}},
]


def log(msg):
    sys.stderr.write("msgboard channel: %s\n" % msg)
    sys.stderr.flush()


# ----------------------------------------------------------------- config / identity

def data_dir():
    d = os.environ.get("MB_DATA_DIR")
    if d:
        return d
    repo = os.path.join(HOME, "msgboard")
    if os.path.exists(os.path.join(repo, "server.py")):
        return os.path.join(repo, "data")
    return os.path.join(HOME, ".msgboard")


def load_config():
    try:
        with open(os.path.join(data_dir(), "config.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


CONFIG = load_config()
BASE = (os.environ.get("MB_URL") or CONFIG.get("url") or "http://127.0.0.1:8765")
if not BASE.startswith("unix://"):
    BASE = BASE.rstrip("/")
TOKEN = os.environ.get("MB_TOKEN") or CONFIG.get("token") or ""


def identity():
    """(user, project) or (None, None). Env first (mb claude), then the session binding."""
    user = os.environ.get("MB_USER")
    project = os.environ.get("MB_PROJECT")
    if user:
        return user, project
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if sid:
        try:
            with open(os.path.join(data_dir(), "identities.json")) as f:
                ent = json.load(f).get(sid) or {}
            if ent.get("name"):
                return ent["name"], ent.get("project") or project
        except (OSError, ValueError):
            pass
    return None, project


# ----------------------------------------------------------------- http (tcp or unix socket)

class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout=None):
        super().__init__("localhost", timeout=timeout)
        self.unix_path = path

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self.unix_path)
        self.sock = s


def api(method, path, params=None, body=None, timeout=20):
    if params:
        path += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    if BASE.startswith("unix://"):
        conn = UnixHTTPConnection(BASE[len("unix://"):], timeout=timeout)
    else:
        u = urllib.parse.urlsplit(BASE)
        conn = http.client.HTTPConnection(u.hostname or "127.0.0.1", u.port or 80, timeout=timeout)
    headers = {"Content-Type": "application/json", "Host": "localhost"}
    if TOKEN:
        headers["Authorization"] = "Bearer " + TOKEN
    conn.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=headers)
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except ValueError:
        payload = {"error": "non-JSON response"}
    if resp.status >= 400:
        raise RuntimeError(payload.get("error") if isinstance(payload, dict) else str(payload))
    return payload


# ----------------------------------------------------------------- jsonrpc out

out_lock = threading.Lock()


def send(obj):
    line = json.dumps(obj, ensure_ascii=False)
    with out_lock:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def notify(content, meta):
    send({"jsonrpc": "2.0", "method": "notifications/claude/channel",
          "params": {"content": content, "meta": {k: str(v) for k, v in meta.items()}}})


def fmt_ts(ts):
    return time.strftime("%H:%M", time.localtime(ts))


def fmt_msg(m):
    text = m["text"].strip()
    if len(text) > MAX_TEXT:
        text = text[:MAX_TEXT] + " … (truncated; mb thread %d or mb read %s for the rest)" % (m["id"], m["channel"])
    why = ",".join(m.get("why") or [])
    flags = (" ↳#%d" % m["reply_to"]) if m.get("reply_to") else ""
    kind = m.get("kind") or "message"
    kindtag = "" if kind == "message" else " [%s]" % kind
    head = "[#%d%s] %s %s <%s> (%s)%s" % (m["id"], flags, fmt_ts(m["ts"]), m["channel"], m["user"], why, kindtag)
    if "\n" in text:
        return head + "\n" + "\n".join("    " + line for line in text.split("\n"))
    return head + " " + text


# ----------------------------------------------------------------- poller

class Poller(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.stop = threading.Event()
        self.user = None
        self.project = None
        self.last_ack = None

    def run(self):
        if os.environ.get("MB_CHANNEL") != "1":
            log("MB_CHANNEL is not set: not polling the board (start sessions with `mb claude` to get board events)")
            return
        nagged = 0
        while not self.stop.is_set():
            if not self.user:
                self.user, self.project = identity()
                if not self.user:
                    if time.time() - nagged > 600:
                        nagged = time.time()
                        notify("No board identity for this session yet. Run `mb join <name> -p <project>` (or start the session with "
                               "`mb claude -p <project>`); board events will start arriving once you have joined.",
                               {"count": 0, "project": self.project or ""})
                    self.stop.wait(5)
                    continue
                log("delivering feed for %s (project %s)" % (self.user, self.project or "-"))
                try:
                    api("POST", "/api/users", body={"name": self.user, "project": self.project, "cwd": os.getcwd()})
                    api("POST", "/api/presence", body={"user": self.user, "project": self.project, "kind": "channel", "ttl": 150})
                except Exception as e:
                    log("register failed: %s" % e)
                self.send_brief()
                threading.Thread(target=self.heartbeat, daemon=True).start()
            self.cycle()

    def send_brief(self):
        try:
            b = api("GET", "/api/brief", {"as": self.user, "project": self.project}, timeout=20)
        except Exception as e:
            log("brief failed: %s" % e)
            return
        notify("Briefing (you just connected to the board):\n" + b["text"],
               {"kind": "brief", "count": 0, "project": b.get("project") or "", "user": self.user,
                "pending": b.get("feed_pending", 0), "asks": b.get("asks_to_me", 0), "to_review": b.get("to_review", 0)})

    def heartbeat(self):
        while not self.stop.wait(60):
            try:
                api("POST", "/api/presence", body={"user": self.user, "project": self.project, "kind": "channel", "ttl": 150}, timeout=5)
            except Exception:
                pass

    def cycle(self):
        down_since = None
        down_notified = False
        backoff = 2
        while not self.stop.is_set():
            try:
                res = api("GET", "/api/feed/wait", {"as": self.user, "timeout": POLL_SECS}, timeout=POLL_SECS + 15)
            except Exception as e:
                if down_since is None:
                    down_since = time.time()
                    log("board unreachable: %s" % e)
                if not down_notified and time.time() - down_since > 300:
                    down_notified = True
                    notify("[board offline] the msgboard server (or the tunnel to it) has been unreachable for 5 minutes: %s. "
                           "mb commands will fail until it is back; nothing to do unless you own the board." % e,
                           {"count": 0, "project": self.project or "", "offline": "1"})
                self.stop.wait(backoff)
                backoff = min(backoff * 2, 60)
                continue
            if down_since is not None:
                log("board back after %ds" % (time.time() - down_since))
                if down_notified:
                    notify("[board online] the msgboard server is reachable again.", {"count": 0, "project": self.project or ""})
                down_since, down_notified, backoff = None, False, 2
            msgs = res.get("messages") or []
            if not msgs:
                continue
            # gather a burst for a moment so one wake-up carries several messages
            self.stop.wait(BATCH_SECS)
            try:
                more = api("GET", "/api/feed", {"as": self.user, "since": msgs[-1]["id"], "limit": 100}, timeout=15)
                msgs += more.get("messages") or []
            except Exception:
                pass
            self.deliver(msgs)

    def deliver(self, msgs):
        shown = msgs[:MAX_BATCH]
        lines = [fmt_msg(m) for m in shown]
        if len(msgs) > MAX_BATCH:
            lines.append("… and %d more (mb feed --peek, or mb inbox)" % (len(msgs) - MAX_BATCH))
        proj = self.project or ""
        head = "%d new on msgboard for %s%s:" % (len(msgs), self.user, (" (project %s)" % proj) if proj else "")
        kinds = sorted({w for m in msgs for w in (m.get("why") or [])})
        meta = {"count": len(msgs), "project": proj, "user": self.user, "ids": ",".join(str(m["id"]) for m in shown),
                "kinds": ",".join(kinds), "channels": ",".join(sorted({m["channel"] for m in shown}))[:200]}
        try:
            notify(head + "\n" + "\n".join(lines), meta)
        except Exception as e:
            log("delivery failed, not acknowledging: %s" % e)
            return
        last = max(m["id"] for m in msgs)
        for attempt in range(5):
            try:
                api("POST", "/api/feed/ack", body={"user": self.user, "last_id": last}, timeout=10)
                self.last_ack = last
                return
            except Exception as e:
                log("ack failed (%s); retrying" % e)
                self.stop.wait(2)


poller = Poller()


# ----------------------------------------------------------------- tools

def call_tool(name, args):
    user, project = identity()
    if not user:
        return "no board identity in this session: run `mb join <name> -p <project>` first", True
    if name == "reply":
        m = api("POST", "/api/messages/%d/reply" % int(args["message_id"]), body={"user": user, "text": args["text"]})
        return "replied [#%d] in thread #%d (%s)" % (m["id"], m["reply_to"], m["channel"]), False
    if name == "send":
        ref = args["channel"]
        if ref.startswith("@"):
            key = api("GET", "/api/dm", {"as": user, "with": ref[1:]})["key"]
        else:
            key = api("GET", "/api/resolve", {"channel": ref, "project": project})["key"]
        m = api("POST", "/api/channels/%s/messages" % urllib.parse.quote(key, safe=""), body={"user": user, "text": args["text"]})
        return "sent [#%d] to %s" % (m["id"], m["channel"]), False
    if name == "subscribe":
        key = api("GET", "/api/resolve", {"channel": args["channel"], "project": project})["key"]
        r = api("POST", "/api/subscriptions", body={"user": user, "channel": key, "remove": bool(args.get("remove"))})
        return "subscriptions: %s" % (", ".join(r["subscriptions"]) or "none"), False
    if name == "status":
        f = api("GET", "/api/feed", {"as": user, "limit": 1})
        return json.dumps({"user": user, "project": project, "board": BASE, "subscriptions": f.get("subscriptions"),
                           "feed_cursor": f.get("cursor"), "latest_id": f.get("latest_id"),
                           "polling": os.environ.get("MB_CHANNEL") == "1"}), False
    return "unknown tool %s" % name, True


# ----------------------------------------------------------------- jsonrpc in

def handle(msg):
    mid = msg.get("id")
    method = msg.get("method")
    params = msg.get("params") or {}
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": PROTOCOL,
            "capabilities": {"tools": {}, "experimental": {"claude/channel": {}}},
            "serverInfo": {"name": SERVER_NAME, "version": "1.0.0"},
            "instructions": INSTRUCTIONS}})
    elif method == "notifications/initialized":
        if not poller.is_alive():
            poller.start()
    elif method == "ping":
        send({"jsonrpc": "2.0", "id": mid, "result": {}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
    elif method == "tools/call":
        try:
            text, is_err = call_tool(params.get("name"), params.get("arguments") or {})
        except Exception as e:
            text, is_err = "error: %s" % e, True
        send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": text}], "isError": is_err}})
    elif method in ("resources/list", "prompts/list"):
        send({"jsonrpc": "2.0", "id": mid, "result": {"resources": [], "prompts": []}})
    elif mid is not None and method:
        send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "method not found: %s" % method}})


def main():
    log("starting (board %s, data %s)" % (BASE, data_dir()))
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        try:
            handle(msg)
        except Exception as e:
            log("handler error: %r" % (e,))
            if msg.get("id") is not None:
                send({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32603, "message": str(e)}})
    poller.stop.set()
    if poller.user:
        try:
            api("POST", "/api/presence", body={"user": poller.user, "state": "offline"}, timeout=3)
        except Exception:
            pass
    log("stdin closed; exiting")


if __name__ == "__main__":
    main()
