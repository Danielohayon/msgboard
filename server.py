#!/usr/bin/env python3
"""
msgboard server: a zero-dependency local message board for humans and Claude Code agents.

    python3 server.py [--host 127.0.0.1] [--port 8765] [--data-dir DIR] [--token T]

GUI:  http://127.0.0.1:8765/
API:  README.md (humans) and AGENTS.md (agents). Everything is JSON.

Model: projects > channels > messages (threads via reply_to). Tasks, pins, pastes, asks and
search are scoped by project. Storage is one SQLite file; live delivery is long-polling for
the CLI (/api/wait) and Server-Sent Events for the GUI (/api/events). Stdlib only.
"""
import argparse
import collections
import getpass
import json
import os
import re
import secrets
import select
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(HERE, "static")
MAX_TEXT = 64 * 1024
MAX_PASTE = 8 * 1024 * 1024
LONGPOLL_MAX = 30.0
SSE_PING = 15.0
DEFAULT_PROJECT = "main"
DEFAULT_CHANNEL = "general"
DM_PROJECT = "dm"
BROADCAST_MENTIONS = ("all", "here", "everyone")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")          # projects and channels
USER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$")
MSG_KINDS = ("message", "ask", "answer", "system", "paste", "pr", "bundle", "review")
REVIEW_VERDICTS = ("approved", "changes", "rejected")
REVIEW_ICON = {"approved": "✅", "changes": "🔁", "rejected": "⛔"}
PR_STATUSES = ("open", "draft", "merged", "closed")
PRIORITY_DEFAULT = 50  # priority is 1..100: 1 = merge first, 100 = whenever. The old P0..P3 levels map to 1/10/50/90.
PRIORITY_NAMES = {"p0": 1, "critical": 1, "urgent": 1, "p1": 10, "high": 10, "p2": 50, "normal": 50, "medium": 50,
                  "p3": 90, "low": 90}
TAG_RE = re.compile(r"^[a-z0-9][a-z0-9_.:/-]{0,31}$")
PR_URL_RE = re.compile(r"https?://github\.com/([^/\s]+/[^/\s]+)/pull/(\d+)")
TASK_STATUSES = ("open", "taken", "done")
EMOJI_POOL = ("🦊 🐼 🦉 🐙 🦄 🐝 🦋 🐢 🐬 🦜 🐺 🦁 🐯 🐸 🐧 🦔 🦥 🦦 🐳 🦩 🐲 🦖 🦕 🐌 🦀 🐠 🦎 🦓 🦒 "
              "🐘 🦘 🐇 🚀 🔧 🔬 🧪 🧬 🧭 🎯 🧩 🪐 🌋 🌵 🍄 🍀 ⚡ 🔥 💎").split()


class ApiError(Exception):
    def __init__(self, status, message, **extra):
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra


def now():
    return time.time()


MENTION_ANY_RE = re.compile(r"(?<![\w.-])@([A-Za-z0-9][\w.-]*)")


def mentions_in(text):
    return {m.lower() for m in MENTION_ANY_RE.findall(text or "")}


def valid_name(name, what):
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise ApiError(400, "invalid %s name %r (use a-z 0-9 - _, max 64 chars)" % (what, name))
    return name


def valid_user(name):
    if not isinstance(name, str) or not USER_RE.match(name):
        raise ApiError(400, "invalid user name %r (use letters, digits, . _ -, max 32 chars)" % (name,))
    return name


def valid_emoji(e):
    e = (e or "").strip()
    if not e:
        return ""
    if len(e) > 8 or any(c.isspace() for c in e):
        raise ApiError(400, "emoji must be 1-8 characters without spaces")
    return e


def split_key(key):
    """'proj/chan' -> ('proj', 'chan')."""
    if not isinstance(key, str) or key.count("/") != 1:
        raise ApiError(400, "channel must be given as project/channel, got %r" % (key,))
    return key.split("/", 1)


def dm_key(a, b):
    a, b = sorted([a, b])
    return "%s/%s+%s" % (DM_PROJECT, a, b)


def truthy(v):
    return str(v).lower() in ("1", "true", "yes", "on")


# --------------------------------------------------------------------------- events

class Bus:
    """Monotonic event sequence shared by long-pollers and SSE clients."""

    def __init__(self):
        self.cond = threading.Condition()
        self.seq = 0
        self.events = collections.deque(maxlen=4000)  # (seq, type, payload)

    def publish(self, type_, payload):
        with self.cond:
            self.seq += 1
            self.events.append((self.seq, type_, payload))
            self.cond.notify_all()

    def wait_for_change(self, seen, timeout):
        with self.cond:
            return self.cond.wait_for(lambda: self.seq != seen, timeout=timeout)

    def since(self, seen):
        with self.cond:
            events = [e for e in self.events if e[0] > seen]
            lost = bool(self.events) and seen < self.events[0][0] - 1
            return events, self.seq, lost


# --------------------------------------------------------------------------- storage

MSG_SELECT = ("SELECT m.*, (SELECT COUNT(*) FROM messages r WHERE r.reply_to = m.id) AS reply_count, "
              "(SELECT MAX(r.ts) FROM messages r WHERE r.reply_to = m.id) AS last_reply_ts FROM messages m ")

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS users(
    name TEXT PRIMARY KEY, created REAL NOT NULL, last_seen REAL NOT NULL,
    emoji TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT '', status_ts REAL,
    project TEXT NOT NULL DEFAULT '', cwd TEXT NOT NULL DEFAULT '', human INTEGER NOT NULL DEFAULT 0,
    live_until REAL, live_kind TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS projects(
    name TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '',
    created REAL NOT NULL, created_by TEXT NOT NULL DEFAULT '', archived INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS channels(
    key TEXT PRIMARY KEY, project TEXT NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'channel',
    topic TEXT NOT NULL DEFAULT '', members TEXT NOT NULL DEFAULT '[]',
    created REAL NOT NULL, created_by TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT '', state_by TEXT NOT NULL DEFAULT '', state_ts REAL);
CREATE TABLE IF NOT EXISTS messages(
    id INTEGER PRIMARY KEY AUTOINCREMENT, channel TEXT NOT NULL, user TEXT NOT NULL,
    text TEXT NOT NULL, ts REAL NOT NULL, reply_to INTEGER, kind TEXT NOT NULL DEFAULT 'message',
    meta TEXT NOT NULL DEFAULT '{}', pinned_by TEXT, pinned_ts REAL, resolved_by TEXT, resolved_ts REAL);
CREATE INDEX IF NOT EXISTS messages_channel_id ON messages(channel, id);
CREATE INDEX IF NOT EXISTS messages_reply_to ON messages(reply_to);
CREATE TABLE IF NOT EXISTS cursors(
    user TEXT NOT NULL, channel TEXT NOT NULL, last_id INTEGER NOT NULL, PRIMARY KEY(user, channel));
CREATE TABLE IF NOT EXISTS tasks(
    id INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT NOT NULL, channel TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL, body TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'open',
    owner TEXT NOT NULL DEFAULT '', created_by TEXT NOT NULL DEFAULT '',
    created REAL NOT NULL, updated REAL NOT NULL, done_ts REAL, tags TEXT NOT NULL DEFAULT '[]');
CREATE TABLE IF NOT EXISTS bundles(
    id INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT NOT NULL, title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'open', priority INTEGER NOT NULL DEFAULT 50,
    tags TEXT NOT NULL DEFAULT '[]', merge_order TEXT NOT NULL DEFAULT '[]', created_by TEXT NOT NULL DEFAULT '',
    created REAL NOT NULL, updated REAL NOT NULL, message_id INTEGER);
CREATE TABLE IF NOT EXISTS prs(
    id INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT NOT NULL, title TEXT NOT NULL,
    url TEXT NOT NULL DEFAULT '', repo TEXT NOT NULL DEFAULT '', number INTEGER,
    branch TEXT NOT NULL DEFAULT '', base TEXT NOT NULL DEFAULT '', body TEXT NOT NULL DEFAULT '',
    tldr TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open', priority INTEGER NOT NULL DEFAULT 50, tags TEXT NOT NULL DEFAULT '[]',
    bundle_id INTEGER, author TEXT NOT NULL DEFAULT '', created REAL NOT NULL, updated REAL NOT NULL,
    merged_ts REAL, closed_ts REAL, message_id INTEGER);
CREATE INDEX IF NOT EXISTS prs_project_status ON prs(project, status);
CREATE TABLE IF NOT EXISTS pr_reviews(
    pr_id INTEGER NOT NULL, reviewer TEXT NOT NULL, verdict TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
    ts REAL NOT NULL, stale INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(pr_id, reviewer));
CREATE TABLE IF NOT EXISTS areas(
    user TEXT NOT NULL, area TEXT NOT NULL, score REAL NOT NULL DEFAULT 0, manual INTEGER NOT NULL DEFAULT 0,
    updated REAL NOT NULL, PRIMARY KEY(user, area));
CREATE TABLE IF NOT EXISTS groups(
    project TEXT NOT NULL, name TEXT NOT NULL, members TEXT NOT NULL DEFAULT '[]', created_by TEXT NOT NULL DEFAULT '',
    updated REAL NOT NULL, PRIMARY KEY(project, name));
CREATE TABLE IF NOT EXISTS thread_watches(
    user TEXT NOT NULL, root_id INTEGER NOT NULL, mode TEXT NOT NULL, ts REAL NOT NULL, PRIMARY KEY(user, root_id));
CREATE TABLE IF NOT EXISTS subscriptions(
    user TEXT NOT NULL, channel TEXT NOT NULL, ts REAL NOT NULL, PRIMARY KEY(user, channel));
CREATE TABLE IF NOT EXISTS feed_cursors(user TEXT PRIMARY KEY, last_id INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS pastes(
    id TEXT PRIMARY KEY, name TEXT NOT NULL, content TEXT NOT NULL, size INTEGER NOT NULL,
    lines INTEGER NOT NULL, user TEXT NOT NULL, ts REAL NOT NULL,
    channel TEXT NOT NULL DEFAULT '', project TEXT NOT NULL DEFAULT '', message_id INTEGER);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(text, content='messages', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, text) VALUES (new.id, new.text); END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, text) VALUES('delete', old.id, old.text); END;
CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE OF text ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, text) VALUES('delete', old.id, old.text);
    INSERT INTO messages_fts(rowid, text) VALUES (new.id, new.text); END;
"""


class Store:
    def __init__(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.path = path
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.fts = False
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=FULL")  # every commit fsynced: survives power loss, not just crashes
            self.migrate()
            self.conn.executescript(SCHEMA)
            try:
                had_fts = self.one("SELECT name FROM sqlite_master WHERE name='messages_fts'") is not None
                self.conn.executescript(FTS_SCHEMA)
                if not had_fts:
                    self.conn.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
                self.fts = True
            except sqlite3.OperationalError as e:
                sys.stderr.write("FTS5 unavailable (%s); search falls back to LIKE\n" % e)
            self.ensure_project(DEFAULT_PROJECT, "Main", "Cross-project chat", "")
            for u in self.q("SELECT name FROM users WHERE emoji=''"):
                self.x("UPDATE users SET emoji=? WHERE name=?", (self.pick_emoji(u["name"]), u["name"]))
            self.conn.execute("INSERT OR REPLACE INTO meta(k, v) VALUES('schema', '2')")

    # -- helpers
    def q(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def x(self, sql, args=()):
        with self.lock:
            return self.conn.execute(sql, args)

    def columns(self, table):
        return [r["name"] for r in self.q("PRAGMA table_info(%s)" % table)]

    def migrate(self):
        """v1 (flat channels) -> v2 (projects, threads, kinds)."""
        tables = {r["name"] for r in self.q("SELECT name FROM sqlite_master WHERE type='table'")}
        if self.conn.execute("PRAGMA user_version").fetchone()[0] < 3:
            # v3: priority 1..100 (1 = merge first) instead of the four levels P0..P3
            remap = "CASE priority WHEN 0 THEN 1 WHEN 1 THEN 10 WHEN 2 THEN 50 WHEN 3 THEN 90 ELSE priority END"
            for t in ("prs", "bundles"):
                if t in tables:
                    self.conn.execute("UPDATE %s SET priority = %s WHERE priority <= 3" % (t, remap))
            self.conn.execute("PRAGMA user_version = 3")
            sys.stderr.write("migrated PR/bundle priorities to the 1..100 scale\n")
        if "channels" in tables and "key" not in self.columns("channels"):
            sys.stderr.write("migrating schema v1 -> v2 (projects)\n")
            self.conn.executescript(
                """
                ALTER TABLE channels RENAME TO channels_v1;
                CREATE TABLE channels(
                    key TEXT PRIMARY KEY, project TEXT NOT NULL, name TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'channel', topic TEXT NOT NULL DEFAULT '',
                    members TEXT NOT NULL DEFAULT '[]', created REAL NOT NULL, created_by TEXT NOT NULL DEFAULT '');
                INSERT INTO channels(key, project, name, kind, topic, members, created, created_by)
                    SELECT 'main/' || name, 'main', name, 'channel', topic, '[]', created, created_by FROM channels_v1;
                DROP TABLE channels_v1;
                UPDATE messages SET channel = 'main/' || channel WHERE channel NOT LIKE '%/%';
                UPDATE cursors SET channel = 'main/' || channel WHERE channel NOT LIKE '%/%';
                """
            )
        if "users" in tables:
            for col, decl in (("emoji", "TEXT NOT NULL DEFAULT ''"), ("status", "TEXT NOT NULL DEFAULT ''"),
                              ("status_ts", "REAL"), ("project", "TEXT NOT NULL DEFAULT ''"),
                              ("cwd", "TEXT NOT NULL DEFAULT ''"), ("human", "INTEGER NOT NULL DEFAULT 0")):
                if col not in self.columns("users"):
                    self.conn.execute("ALTER TABLE users ADD COLUMN %s %s" % (col, decl))
        if "prs" in tables and "tldr" not in self.columns("prs"):
            self.conn.execute("ALTER TABLE prs ADD COLUMN tldr TEXT NOT NULL DEFAULT ''")
        if "users" in tables and "live_until" not in self.columns("users"):
            self.conn.execute("ALTER TABLE users ADD COLUMN live_until REAL")
            self.conn.execute("ALTER TABLE users ADD COLUMN live_kind TEXT NOT NULL DEFAULT ''")
        if "channels" in tables and "state" not in self.columns("channels"):
            self.conn.execute("ALTER TABLE channels ADD COLUMN state TEXT NOT NULL DEFAULT ''")
            self.conn.execute("ALTER TABLE channels ADD COLUMN state_by TEXT NOT NULL DEFAULT ''")
            self.conn.execute("ALTER TABLE channels ADD COLUMN state_ts REAL")
        if "tasks" in tables and "tags" not in self.columns("tasks"):
            self.conn.execute("ALTER TABLE tasks ADD COLUMN tags TEXT NOT NULL DEFAULT '[]'")
        if "messages" in tables:
            for col, decl in (("reply_to", "INTEGER"), ("kind", "TEXT NOT NULL DEFAULT 'message'"),
                              ("meta", "TEXT NOT NULL DEFAULT '{}'"), ("pinned_by", "TEXT"), ("pinned_ts", "REAL"),
                              ("resolved_by", "TEXT"), ("resolved_ts", "REAL")):
                if col not in self.columns("messages"):
                    self.conn.execute("ALTER TABLE messages ADD COLUMN %s %s" % (col, decl))

    @staticmethod
    def msg(row):
        if row is None:
            return None
        m = dict(row)
        try:
            m["meta"] = json.loads(m.get("meta") or "{}")
        except ValueError:
            m["meta"] = {}
        if "/" in m["channel"]:
            m["project"], m["chname"] = m["channel"].split("/", 1)
        else:
            m["project"], m["chname"] = "", m["channel"]
        return m

    def msgs(self, sql, args=()):
        with self.lock:
            return [self.msg(r) for r in self.conn.execute(sql, args).fetchall()]

    # -- users
    def ensure_user(self, name, **fields):
        valid_user(name)
        with self.lock:
            row = self.one("SELECT * FROM users WHERE name=?", (name,))
            created = row is None
            if created:
                self.x("INSERT INTO users(name, created, last_seen) VALUES(?,?,?)", (name, now(), now()))
            sets, args = ["last_seen=?"], [now()]
            if fields.get("emoji") is not None:
                sets.append("emoji=?"); args.append(valid_emoji(fields["emoji"]))
            if fields.get("status") is not None:
                sets.append("status=?"); args.append(str(fields["status"])[:200])
                sets.append("status_ts=?"); args.append(now())
            if fields.get("project") is not None:
                sets.append("project=?"); args.append(str(fields["project"])[:64])
            if fields.get("cwd") is not None:
                sets.append("cwd=?"); args.append(str(fields["cwd"])[:300])
            if fields.get("human") is not None:
                sets.append("human=?"); args.append(1 if truthy(fields["human"]) else 0)
            args.append(name)
            self.x("UPDATE users SET %s WHERE name=?" % ", ".join(sets), args)
            user = self.one("SELECT * FROM users WHERE name=?", (name,))
            if not user["emoji"]:
                self.x("UPDATE users SET emoji=? WHERE name=?", (self.pick_emoji(name), name))
                user = self.one("SELECT * FROM users WHERE name=?", (name,))
            return user, created

    def pick_emoji(self, name):
        used = {r["emoji"] for r in self.q("SELECT emoji FROM users WHERE emoji<>''")}
        start = sum(ord(c) * (i + 7) for i, c in enumerate(name)) % len(EMOJI_POOL)
        for i in range(len(EMOJI_POOL)):
            e = EMOJI_POOL[(start + i) % len(EMOJI_POOL)]
            if e not in used:
                return e
        return EMOJI_POOL[start]

    def touch_user(self, name):
        if name:
            self.x("UPDATE users SET last_seen=? WHERE name=?", (now(), name))

    def get_user(self, name):
        return self.one("SELECT * FROM users WHERE name=?", (name,))

    def users(self, project=None):
        sql = ("SELECT u.*, (SELECT COUNT(*) FROM messages m WHERE m.user=u.name) AS messages, "
               "(SELECT MAX(ts) FROM messages m WHERE m.user=u.name) AS last_message_ts, "
               "(SELECT channel FROM messages m WHERE m.user=u.name ORDER BY id DESC LIMIT 1) AS last_channel, "
               "(SELECT COUNT(*) FROM tasks t WHERE t.owner=u.name AND t.status='taken') AS tasks_taken "
               "FROM users u ")
        args = ()
        if project:
            sql += "WHERE u.project=? "
            args = (project,)
        rows = self.q(sql + "ORDER BY last_seen DESC", args)
        for r in rows:
            r["live"] = bool((r.get("live_until") or 0) > now())
            r["areas"] = [a["area"] for a in self.areas(r["name"], limit=4)]
        return rows

    def humans(self):
        return [r["name"] for r in self.q("SELECT name FROM users WHERE human=1")]

    def mention_names(self, user):
        """The user's own name plus every group they belong to (any project)."""
        names = [user]
        for g in self.q("SELECT name, members FROM groups"):
            try:
                members = json.loads(g["members"] or "[]")
            except ValueError:
                members = []
            if user in members and g["name"] not in names:
                names.append(g["name"])
        return names

    def mention_matcher(self, user):
        """Returns is_mentioned(msg): @user anywhere; @group only in the group's project; @all/@here/@everyone only in
        the project the user belongs to (DMs count as the user's own project)."""
        u = self.get_user(user) or {}
        uproj = u.get("project") or DEFAULT_PROJECT
        direct = re.compile(r"(?<![\w.-])@%s(?![\w.-])" % re.escape(user), re.IGNORECASE)
        groups = {}
        for g in self.q("SELECT project, name, members FROM groups"):
            try:
                members = json.loads(g["members"] or "[]")
            except ValueError:
                members = []
            if user in members:
                groups.setdefault(g["project"], []).append(g["name"])
        group_rx = {proj: re.compile(r"(?<![\w.-])@(?:%s)(?![\w.-])" % "|".join(re.escape(n) for n in names), re.IGNORECASE)
                    for proj, names in groups.items()}
        bcast = re.compile(r"(?<![\w.-])@(?:%s)(?![\w.-])" % "|".join(BROADCAST_MENTIONS), re.IGNORECASE)

        def is_mentioned(m):
            text = m.get("text") or ""
            if "@" not in text:
                return False
            if direct.search(text):
                return True
            proj = m.get("project") or (m.get("channel", "").split("/", 1)[0] if "/" in m.get("channel", "") else "")
            rx = group_rx.get(proj)
            if rx and rx.search(text):
                return True
            if bcast.search(text):
                return proj == uproj or proj == DM_PROJECT
            return False
        return is_mentioned

    # -- presence (a session with the channel or a background `mb wait --feed` is "live": it wakes on delivery)
    def presence(self, user, kind, ttl=90):
        self.x("UPDATE users SET live_until=?, live_kind=?, last_seen=? WHERE name=?", (now() + ttl, kind, now(), user))

    def offline(self, user):
        self.x("UPDATE users SET live_until=NULL, live_kind='' WHERE name=?", (user,))

    # -- areas: responsibilities derived from activity (PR tags, reviews, tagged tasks), adjustable by hand
    def bump_areas(self, user, tags, weight):
        for t in tags or []:
            self.x("INSERT INTO areas(user, area, score, manual, updated) VALUES(?,?,?,0,?) "
                   "ON CONFLICT(user, area) DO UPDATE SET score = CASE WHEN manual < 0 THEN score ELSE score + excluded.score END, "
                   "updated = excluded.updated", (user, t, float(weight), now()))

    def areas(self, user, limit=12):
        return self.q("SELECT area, score, manual, updated FROM areas WHERE user=? AND manual >= 0 AND score > 0 "
                      "ORDER BY score DESC, updated DESC LIMIT ?", (user, limit))

    def set_area(self, user, area, op):
        area = self.parse_tags([area])
        if not area:
            raise ApiError(400, "bad area name")
        area = area[0]
        if op == "add":
            self.x("INSERT INTO areas(user, area, score, manual, updated) VALUES(?,?,5,1,?) "
                   "ON CONFLICT(user, area) DO UPDATE SET score = MAX(score, 5), manual = 1, updated = excluded.updated",
                   (user, area, now()))
        else:
            self.x("INSERT INTO areas(user, area, score, manual, updated) VALUES(?,?,0,-1,?) "
                   "ON CONFLICT(user, area) DO UPDATE SET score = 0, manual = -1, updated = excluded.updated",
                   (user, area, now()))
        return self.areas(user)

    def who(self, topic, project=None, limit=8):
        """Rank agents by evidence about a topic: areas, PRs authored/reviewed, tasks, messages."""
        topic = (topic or "").strip().lower()
        if not topic:
            raise ApiError(400, "topic required")
        like = "%" + topic + "%"
        scores, proof = {}, {}
        recent = now() - 7 * 86400

        def add(user, pts, why):
            scores[user] = scores.get(user, 0) + pts
            proof.setdefault(user, []).append(why)

        for r in self.q("SELECT user, area, score FROM areas WHERE manual >= 0 AND score > 0 AND area LIKE ?", (like,)):
            add(r["user"], 3 * min(r["score"], 10), "area %s (%.0f)" % (r["area"], r["score"]))
        proj = " AND project=?" if project else ""
        for r in self.q("SELECT id, title, author, updated FROM prs WHERE (lower(title) LIKE ? OR lower(tldr) LIKE ? OR lower(body) LIKE ? OR lower(tags) LIKE ?)"
                        + proj + " ORDER BY updated DESC LIMIT 200", [like, like, like, like] + ([project] if project else [])):
            add(r["author"], 2 if r["updated"] > recent else 1, "PR #%d %s" % (r["id"], r["title"][:40]))
            for rv in self.q("SELECT reviewer FROM pr_reviews WHERE pr_id=? AND stale=0", (r["id"],)):
                add(rv["reviewer"], 1, "reviewed PR #%d" % r["id"])
        for r in self.q("SELECT id, title, owner, updated FROM tasks WHERE owner<>'' AND (lower(title) LIKE ? OR lower(tags) LIKE ?)"
                        + proj + " ORDER BY updated DESC LIMIT 200", [like, like] + ([project] if project else [])):
            add(r["owner"], 1.5, "task #%d %s" % (r["id"], r["title"][:40]))
        msg_rows = []
        if self.fts:
            try:
                term = '"%s"' % topic.replace('"', '""')
                msg_rows = self.q("SELECT m.user, COUNT(*) AS n FROM messages_fts JOIN messages m ON m.id = messages_fts.rowid "
                                  "WHERE messages_fts MATCH ? AND m.kind <> 'system'" + (" AND m.channel LIKE ?" if project else "") +
                                  " GROUP BY m.user", [term] + ([project + "/%"] if project else []))
            except sqlite3.OperationalError:
                msg_rows = []
        for r in msg_rows:
            add(r["user"], min(r["n"], 20) * 0.3, "%d message%s" % (r["n"], "" if r["n"] == 1 else "s"))
        humans = set(self.humans())
        out = []
        for user, sc in sorted(scores.items(), key=lambda kv: -kv[1])[:limit]:
            u = self.get_user(user)
            out.append({"user": user, "score": round(sc, 1), "proof": proof[user][:6], "human": user in humans,
                        "status": (u or {}).get("status", ""), "live": bool(u and (u.get("live_until") or 0) > now()),
                        "last_seen": (u or {}).get("last_seen")})
        return out

    def profile(self, user):
        u = self.get_user(user)
        if not u:
            raise ApiError(404, "no such user %r" % (user,))
        week = now() - 7 * 86400
        return {"user": dict(u, live=bool((u.get("live_until") or 0) > now())),
                "areas": self.areas(user),
                "prs": self.prs(author=user, status="all", limit=10),
                "reviews": self.one("SELECT COUNT(*) AS n FROM pr_reviews WHERE reviewer=?", (user,))["n"],
                "tasks": self.tasks(owner=user, limit=10),
                "answers": self.one("SELECT COUNT(*) AS n FROM messages WHERE user=? AND kind='answer'", (user,))["n"],
                "channels": self.q("SELECT channel, COUNT(*) AS n FROM messages WHERE user=? AND ts > ? AND kind <> 'system' "
                                   "GROUP BY channel ORDER BY n DESC LIMIT 6", (user, week)),
                "groups": [g["name"] for g in self.q("SELECT name, members FROM groups")
                           if user in json.loads(g["members"] or "[]")]}

    # -- groups: @name mentions every member
    def groups(self, project=None, member=None):
        rows = self.q("SELECT * FROM groups" + (" WHERE project=?" if project else "") + " ORDER BY project, name",
                      (project,) if project else ())
        out = []
        for g in rows:
            g = dict(g)
            try:
                g["members"] = json.loads(g["members"] or "[]")
            except ValueError:
                g["members"] = []
            if member and member not in g["members"]:
                continue
            out.append(g)
        return out

    def set_group(self, project, name, user, add=(), remove=(), delete=False):
        valid_name(name, "group")
        self.get_project(project)
        if self.one("SELECT 1 FROM users WHERE name=?", (name,)):
            raise ApiError(400, "%r is a user name; groups need a distinct name" % name)
        if delete:
            self.x("DELETE FROM groups WHERE project=? AND name=?", (project, name))
            return None
        row = self.one("SELECT members FROM groups WHERE project=? AND name=?", (project, name))
        members = json.loads(row["members"]) if row else []
        for m in add:
            valid_user(m)
            if m not in members:
                members.append(m)
        members = [m for m in members if m not in set(remove)]
        self.x("INSERT INTO groups(project, name, members, created_by, updated) VALUES(?,?,?,?,?) "
               "ON CONFLICT(project, name) DO UPDATE SET members=excluded.members, updated=excluded.updated",
               (project, name, json.dumps(members), user, now()))
        return self.groups(project=project)

    # -- thread watch / mute
    def set_watch(self, user, root_id, mode):
        m = self.get_message(int(root_id))
        root = m["reply_to"] or m["id"]
        if mode == "off":
            self.x("DELETE FROM thread_watches WHERE user=? AND root_id=?", (user, root))
        else:
            self.x("INSERT INTO thread_watches(user, root_id, mode, ts) VALUES(?,?,?,?) "
                   "ON CONFLICT(user, root_id) DO UPDATE SET mode=excluded.mode, ts=excluded.ts", (user, root, mode, now()))
        return root

    def watches(self, user):
        return {r["root_id"]: r["mode"] for r in self.q("SELECT root_id, mode FROM thread_watches WHERE user=?", (user,))}

    # -- channel state line
    def set_state(self, key, text, by):
        self.get_channel(key)
        self.x("UPDATE channels SET state=?, state_by=?, state_ts=? WHERE key=?", ((text or "").strip()[:300], by, now(), key))
        return self.get_channel(key)

    # -- briefing: everything an agent needs to orient itself, in one screen
    def brief(self, user, project=None):
        u = self.get_user(user) or {"emoji": "", "status": "", "project": ""}
        project = project or u.get("project") or DEFAULT_PROJECT
        p = self.one("SELECT * FROM projects WHERE name=?", (project,)) or {"name": project, "title": project, "description": ""}
        unread = {r["channel"]: r for r in self.unread(user)}
        channels = [c for c in self.channels(project=project) if c["kind"] != "dm"]
        feed_pending = self.feed(user, limit=50)
        asks_to_me = [a for a in self.asks(user=user, project=None) if a["user"] != user]
        my_prs = self.prs(author=user, status="active", limit=20)
        to_review = [x for x in self.prs(project=project, status="active", limit=100)
                     if x["author"] != user and user not in [r["reviewer"] for r in x["reviews"] if not r["stale"]]]
        my_tasks = self.tasks(owner=user, status="taken", limit=10)
        open_tasks = self.tasks(project=project, status="open", limit=8)
        pins = self.pins(project=project, limit=4)
        people = [x for x in self.users(project=project) if x["name"] != user]
        groups = self.groups(project=project)
        areas = self.areas(user, limit=8)
        lines = ["You are %s @%s in project %s%s." % (u.get("emoji") or "", user, project,
                                                       (" (%s)" % p["title"]) if p.get("title") and p["title"] != project else "")]
        if p.get("description"):
            lines.append("Project: " + p["description"][:300])
        if u.get("status"):
            lines.append("Your status: " + u["status"])
        lines.append("Your areas (from your PRs, reviews, tasks): " + (", ".join("%s(%d)" % (a["area"], round(a["score"])) for a in areas)
                                                                      if areas else "none yet; take a task or register a PR to earn some"))
        if feed_pending:
            lines.append("While you were away: %d item%s for you arrived (delivered next)." % (len(feed_pending), "" if len(feed_pending) == 1 else "s"))
        chs = []
        for c in channels:
            n = (unread.get(c["key"]) or {}).get("unread", 0)
            bit = "%s%s" % (c["name"], (" %d unread" % n) if n else "")
            if c.get("state"):
                bit += " [%s]" % c["state"][:80]
            chs.append(bit)
        lines.append("Channels: " + (" · ".join(chs) or "none"))
        if asks_to_me:
            lines.append("Open questions for you: " + "; ".join("#%d from %s: %s" % (a["id"], a["user"], a["text"].replace("\n", " ")[:70]) for a in asks_to_me[:4]))
        if my_prs:
            lines.append("Your PRs: " + "; ".join("#%d %s (%s%s)" % (x["id"], x["title"][:40], x["review_state"], (", %d approvals" % x["approvals"]) if x["approvals"] else "") for x in my_prs[:5]))
        if to_review:
            lines.append("PRs waiting for your review: " + ", ".join("#%d %s" % (x["id"], x["title"][:35]) for x in to_review[:5]) + (" (+%d)" % (len(to_review) - 5) if len(to_review) > 5 else ""))
        if my_tasks:
            lines.append("Tasks you hold: " + "; ".join("#%d %s" % (t["id"], t["title"][:40]) for t in my_tasks))
        if open_tasks:
            lines.append("Open tasks (take one with mb task take <id>): " + "; ".join("#%d %s" % (t["id"], t["title"][:40]) for t in open_tasks[:5]))
        if pins:
            lines.append("Pinned: " + " | ".join("#%d %s" % (m["id"], m["text"].replace("\n", " ")[:60]) for m in pins))
        if people:
            ppl = []
            for x in people[:10]:
                ppl.append("%s@%s%s%s%s" % ("●" if x.get("live") else "", x["name"], " (human)" if x["human"] else "",
                                            (" " + ",".join(x.get("areas") or [])) if x.get("areas") else "",
                                            (' "%s"' % x["status"][:40]) if x.get("status") else ""))
            lines.append("People: " + " · ".join(ppl) + " (● = live now)")
        if groups:
            lines.append("Groups: " + "; ".join("@%s = %s" % (g["name"], ",".join(g["members"])) for g in groups))
        lines.append("Next: mb inbox · mb read <channel> --unread · mb who <topic> · mb tasks · mb brief")
        return {"text": "\n".join(lines), "project": project, "feed_pending": len(feed_pending), "asks_to_me": len(asks_to_me),
                "my_prs": len(my_prs), "to_review": len(to_review), "my_tasks": len(my_tasks), "open_tasks": len(open_tasks)}

    # -- projects
    def ensure_project(self, name, title="", description="", by=""):
        valid_name(name, "project")
        with self.lock:
            row = self.one("SELECT * FROM projects WHERE name=?", (name,))
            if row:
                if title or description:
                    self.x("UPDATE projects SET title=COALESCE(NULLIF(?, ''), title), "
                           "description=COALESCE(NULLIF(?, ''), description) WHERE name=?",
                           (title or "", description or "", name))
                    row = self.one("SELECT * FROM projects WHERE name=?", (name,))
                return row, False
            self.x("INSERT INTO projects(name, title, description, created, created_by) VALUES(?,?,?,?,?)",
                   (name, title or name, description or "", now(), by or ""))
            self.ensure_channel("%s/%s" % (name, DEFAULT_CHANNEL), "Project home channel", by)
            return self.one("SELECT * FROM projects WHERE name=?", (name,)), True

    def get_project(self, name):
        row = self.one("SELECT * FROM projects WHERE name=?", (name,))
        if not row:
            raise ApiError(404, "no such project %r" % (name,))
        return row

    def projects(self):
        return self.q(
            """
            SELECT p.*,
              (SELECT COUNT(*) FROM channels c WHERE c.project=p.name) AS channels,
              (SELECT COUNT(*) FROM tasks t WHERE t.project=p.name AND t.status<>'done') AS open_tasks,
              (SELECT COUNT(*) FROM messages m JOIN channels c ON c.key=m.channel WHERE c.project=p.name) AS messages,
              (SELECT MAX(m.ts) FROM messages m JOIN channels c ON c.key=m.channel WHERE c.project=p.name) AS last_ts,
              (SELECT COUNT(*) FROM users u WHERE u.project=p.name) AS members
            FROM projects p ORDER BY (p.name<>'main'), p.created ASC
            """
        )

    # -- channels
    def ensure_channel(self, key, topic="", by="", kind="channel", members=None):
        project, name = split_key(key)
        with self.lock:
            row = self.one("SELECT * FROM channels WHERE key=?", (key,))
            if row:
                return row, False
            valid_name(project, "project")
            if kind != "dm":
                valid_name(name, "channel")
                if project != DM_PROJECT and not self.one("SELECT 1 FROM projects WHERE name=?", (project,)):
                    self.ensure_project(project, "", "", by)  # also creates <project>/general
                    row = self.one("SELECT * FROM channels WHERE key=?", (key,))
                    if row:
                        return row, True
            self.x("INSERT INTO channels(key, project, name, kind, topic, members, created, created_by) "
                   "VALUES(?,?,?,?,?,?,?,?)",
                   (key, project, name, kind, topic or "", json.dumps(members or []), now(), by or ""))
            return self.one("SELECT * FROM channels WHERE key=?", (key,)), True

    def get_channel(self, key):
        row = self.one("SELECT * FROM channels WHERE key=?", (key,))
        if not row:
            raise ApiError(404, "no such channel %r" % (key,))
        return row

    def set_topic(self, key, topic):
        self.get_channel(key)
        self.x("UPDATE channels SET topic=? WHERE key=?", (topic, key))
        return self.get_channel(key)

    def channels(self, project=None, user=None):
        sql = (
            "SELECT c.*, COUNT(m.id) AS messages, MAX(m.ts) AS last_ts, MAX(m.id) AS last_id, "
            "(SELECT COUNT(*) FROM messages p WHERE p.channel=c.key AND p.pinned_ts IS NOT NULL) AS pins "
            "FROM channels c LEFT JOIN messages m ON m.channel = c.key "
        )
        args = []
        if project:
            sql += "WHERE c.project=? "
            args.append(project)
        rows = self.q(sql + "GROUP BY c.key ORDER BY c.project, c.created ASC", args)
        for r in rows:
            try:
                r["members"] = json.loads(r["members"] or "[]")
            except ValueError:
                r["members"] = []
        if user:
            unread = {u["channel"]: u for u in self.unread(user)}
            for r in rows:
                u = unread.get(r["key"])
                r["unread"] = u["unread"] if u else 0
        return rows

    def resolve_channel(self, ref, project=None):
        """Short channel ref -> key. Returns (key, exists)."""
        if "/" in ref:
            p, n = split_key(ref)
            valid_name(p, "project")
            if p != DM_PROJECT:
                valid_name(n, "channel")
            return ref, bool(self.one("SELECT 1 FROM channels WHERE key=?", (ref,)))
        valid_name(ref, "channel")
        rows = self.q("SELECT key FROM channels WHERE name=? AND kind='channel'", (ref,))
        if project:
            # a session project is authoritative: name -> project/name (created there by `send` if missing);
            # other projects' channels of that name are only a hint
            key = "%s/%s" % (project, ref)
            exists = bool(self.one("SELECT 1 FROM channels WHERE key=?", (key,)))
            self.last_candidates = [] if exists else [r["key"] for r in rows]
            return key, exists
        self.last_candidates = []
        if len(rows) == 1:
            return rows[0]["key"], True
        if len(rows) > 1:
            raise ApiError(409, "channel %r exists in several projects; say which: %s"
                           % (ref, ", ".join(r["key"] for r in rows)), candidates=[r["key"] for r in rows])
        return "%s/%s" % (DEFAULT_PROJECT, ref), False

    def dm_channel(self, a, b):
        valid_user(a)
        valid_user(b)
        return self.ensure_channel(dm_key(a, b), "", a, kind="dm", members=sorted([a, b]))

    # -- messages
    def add_message(self, channel, user, text, reply_to=None, kind="message", meta=None):
        if kind not in MSG_KINDS:
            raise ApiError(400, "bad message kind %r" % (kind,))
        root = None
        if reply_to is not None:
            root = self.get_message(int(reply_to))
            if root.get("reply_to"):
                root = self.get_message(root["reply_to"])
            channel = root["channel"]
        with self.lock:
            cur = self.x("INSERT INTO messages(channel, user, text, ts, reply_to, kind, meta) VALUES(?,?,?,?,?,?,?)",
                         (channel, user, text, now(), root["id"] if root else None, kind,
                          json.dumps(meta or {})))
            return self.get_message(cur.lastrowid)

    def get_message(self, mid):
        rows = self.msgs(MSG_SELECT + "WHERE m.id=?", (mid,))
        if not rows:
            raise ApiError(404, "no such message #%s" % (mid,))
        return rows[0]

    def latest_id(self):
        return self.one("SELECT COALESCE(MAX(id), 0) AS id FROM messages")["id"]

    def history(self, channel, since=None, before=None, around=None, limit=50, threads=False):
        """One channel. since: ascending after id (replies included). around: window around id.
        Otherwise the last N top-level messages (replies only with threads=True)."""
        top = "" if threads else "AND m.reply_to IS NULL "
        if since is not None:
            return self.msgs(MSG_SELECT + "WHERE m.channel=? AND m.id>? ORDER BY m.id ASC LIMIT ?",
                             (channel, since, limit))
        if around is not None:
            half = max(limit // 2, 5)
            older = self.msgs(MSG_SELECT + "WHERE m.channel=? AND m.id<=? " + top + "ORDER BY m.id DESC LIMIT ?",
                              (channel, around, half))
            newer = self.msgs(MSG_SELECT + "WHERE m.channel=? AND m.id>? " + top + "ORDER BY m.id ASC LIMIT ?",
                              (channel, around, half))
            older.reverse()
            return older + newer
        if before is not None:
            rows = self.msgs(MSG_SELECT + "WHERE m.channel=? AND m.id<? " + top + "ORDER BY m.id DESC LIMIT ?",
                             (channel, before, limit))
        else:
            rows = self.msgs(MSG_SELECT + "WHERE m.channel=? " + top + "ORDER BY m.id DESC LIMIT ?",
                             (channel, limit))
        rows.reverse()
        return rows

    def thread(self, root_id):
        root = self.get_message(root_id)
        if root.get("reply_to"):
            root = self.get_message(root["reply_to"])
        replies = self.msgs(MSG_SELECT + "WHERE m.reply_to=? ORDER BY m.id ASC", (root["id"],))
        return root, replies

    def pending(self, channel=None, project=None, user=None, since=None, default_since=0,
                mention=None, thread=None, no_threads=False, limit=200):
        """Messages not yet seen by `user`, excluding their own. Cursor per channel unless `since`."""
        args = {"user": user or "", "channel": channel, "project": project, "default_since": default_since,
                "since": since, "limit": limit, "thread": thread}
        lower = "m.id > :since" if since is not None else "m.id > COALESCE(c.last_id, :default_since)"
        sql = (MSG_SELECT +
               "LEFT JOIN cursors c ON c.channel = m.channel AND c.user = :user "
               "WHERE " + lower + " AND m.user <> :user "
               "AND (:channel IS NULL OR m.channel = :channel) "
               "AND (:project IS NULL OR m.channel LIKE :project || '/%') "
               "AND (:thread IS NULL OR m.reply_to = :thread OR m.id = :thread) ")
        if no_threads:
            sql += "AND m.reply_to IS NULL "
        if mention:
            sql += "AND m.text LIKE '%@%' ORDER BY m.id ASC"
            hit = self.mention_matcher(mention)
            out = []
            with self.lock:
                for r in self.conn.execute(sql, args):
                    mm = self.msg(r)
                    if hit(mm):
                        out.append(mm)
                        if len(out) >= limit:
                            break
            return out
        return self.msgs(sql + "ORDER BY m.id ASC LIMIT :limit", args)

    def search(self, query, project=None, channel=None, limit=20):
        query = (query or "").strip()
        if not query:
            raise ApiError(400, "empty search query")
        where, args = "", []
        if channel:
            where += " AND m.channel=?"; args.append(channel)
        elif project:
            where += " AND m.channel LIKE ?"; args.append(project + "/%")
        if self.fts:
            terms = []
            for t in query.split():
                prefix = t.endswith("*")
                t = t.rstrip("*").replace('"', '""')
                if t:
                    terms.append('"%s"%s' % (t, "*" if prefix else ""))
            match = " ".join(terms) or '""'
            sql = ("SELECT m.*, snippet(messages_fts, 0, '[', ']', ' … ', 14) AS snippet, "
                   "(SELECT COUNT(*) FROM messages r WHERE r.reply_to = m.id) AS reply_count "
                   "FROM messages_fts JOIN messages m ON m.id = messages_fts.rowid WHERE messages_fts MATCH ?"
                   + where + " ORDER BY rank LIMIT ?")
            try:
                return self.msgs(sql, [match] + args + [limit])
            except sqlite3.OperationalError as e:
                raise ApiError(400, "bad search query: %s" % e)
        rows = self.msgs(MSG_SELECT + "WHERE m.text LIKE ?" + where + " ORDER BY m.id DESC LIMIT ?",
                         ["%" + query + "%"] + args + [limit])
        for r in rows:
            r["snippet"] = r["text"][:200]
        return rows

    # -- pins / asks
    def set_pin(self, mid, user, pinned):
        self.get_message(mid)
        if pinned:
            self.x("UPDATE messages SET pinned_by=?, pinned_ts=? WHERE id=?", (user, now(), mid))
        else:
            self.x("UPDATE messages SET pinned_by=NULL, pinned_ts=NULL WHERE id=?", (mid,))
        return self.get_message(mid)

    def pins(self, channel=None, project=None, limit=100):
        where, args = "WHERE m.pinned_ts IS NOT NULL", []
        if channel:
            where += " AND m.channel=?"; args.append(channel)
        elif project:
            where += " AND m.channel LIKE ?"; args.append(project + "/%")
        return self.msgs(MSG_SELECT + where + " ORDER BY m.pinned_ts DESC LIMIT ?", args + [limit])

    def resolve_ask(self, mid, user):
        m = self.get_message(mid)
        if m.get("reply_to"):
            m = self.get_message(m["reply_to"])
        self.x("UPDATE messages SET resolved_by=?, resolved_ts=? WHERE id=?", (user, now(), m["id"]))
        return self.get_message(m["id"])

    def asks(self, user=None, project=None, open_only=True, limit=100):
        where, args = "WHERE m.kind='ask'", []
        if open_only:
            where += " AND m.resolved_ts IS NULL"
        if project:
            where += " AND m.channel LIKE ?"; args.append(project + "/%")
        rows = self.msgs(MSG_SELECT + where + " ORDER BY m.id DESC LIMIT ?", args + [limit])
        if user:
            hit = self.mention_matcher(user)
            rows = [r for r in rows if r["user"] == user or hit(r)
                    or user in (r["meta"].get("to") or [])]
        return rows

    # -- cursors / unread / inbox
    def advance_cursor(self, user, channel, last_id):
        if not user or not channel or not last_id:
            return
        self.x("INSERT INTO cursors(user, channel, last_id) VALUES(?,?,?) "
               "ON CONFLICT(user, channel) DO UPDATE SET last_id = MAX(last_id, excluded.last_id)",
               (user, channel, last_id))

    def advance_cursors(self, user, messages):
        if not user:
            return
        latest = {}
        for m in messages:
            latest[m["channel"]] = max(latest.get(m["channel"], 0), m["id"])
        for ch, last_id in latest.items():
            self.advance_cursor(user, ch, last_id)

    def unread(self, user):
        return self.q(
            """
            SELECT c.key AS channel, c.project, c.name, c.kind, COALESCE(cu.last_id, 0) AS last_id,
                   COALESCE(MAX(m.id), 0) AS latest_id,
                   SUM(CASE WHEN m.id > COALESCE(cu.last_id, 0) AND m.user <> :user THEN 1 ELSE 0 END) AS unread
            FROM channels c
            LEFT JOIN cursors cu ON cu.channel = c.key AND cu.user = :user
            LEFT JOIN messages m ON m.channel = c.key
            WHERE c.kind <> 'dm' OR c.members LIKE '%"' || :user || '"%'
            GROUP BY c.key ORDER BY c.project, c.created ASC
            """,
            {"user": user},
        )

    def inbox(self, user):
        unread_sql = ("LEFT JOIN cursors c ON c.channel = m.channel AND c.user = :user "
                      "WHERE m.id > COALESCE(c.last_id, 0) AND m.user <> :user ")
        rows = self.msgs(MSG_SELECT + unread_sql + "AND m.text LIKE '%@%' ORDER BY m.id DESC LIMIT 400",
                         {"user": user})[::-1]  # newest first, then back in order: a big backlog must not hide recent mentions
        hit = self.mention_matcher(user)
        mentions = [r for r in rows if hit(r)]
        seen = {r["id"] for r in mentions}
        replies = self.msgs(
            MSG_SELECT + unread_sql +
            "AND m.reply_to IS NOT NULL AND (EXISTS(SELECT 1 FROM messages p WHERE p.id = m.reply_to AND p.user = :user) "
            "OR EXISTS(SELECT 1 FROM messages p WHERE p.reply_to = m.reply_to AND p.user = :user)) "
            "ORDER BY m.id DESC LIMIT 200", {"user": user})[::-1]
        replies = [r for r in replies if r["id"] not in seen]
        seen.update(r["id"] for r in replies)
        dms = self.msgs(MSG_SELECT + unread_sql + "AND m.channel LIKE 'dm/%' AND "
                        "(m.channel LIKE '%/' || :user || '+%' OR m.channel LIKE '%+' || :user) "
                        "ORDER BY m.id DESC LIMIT 200", {"user": user})[::-1]
        dms = [r for r in dms if r["id"] not in seen]
        all_asks = self.asks(user=user)
        asks_to_me = [a for a in all_asks if a["user"] != user]
        my_asks = [a for a in all_asks if a["user"] == user]
        my_tasks = self.q("SELECT * FROM tasks WHERE owner=? AND status='taken' ORDER BY updated DESC", (user,))
        my_prs = self.prs(author=user, status="active", limit=50)
        unread = [u for u in self.unread(user) if u["unread"]]
        parts = []
        if mentions:
            parts.append("%d mention%s" % (len(mentions), "s" if len(mentions) != 1 else ""))
        if replies:
            parts.append("%d thread repl%s" % (len(replies), "ies" if len(replies) != 1 else "y"))
        if dms:
            parts.append("%d DM%s" % (len(dms), "s" if len(dms) != 1 else ""))
        if asks_to_me:
            parts.append("%d open question%s for you" % (len(asks_to_me), "s" if len(asks_to_me) != 1 else ""))
        total_unread = sum(u["unread"] for u in unread)
        if total_unread:
            parts.append("%d unread in %s" % (total_unread, ", ".join(u["channel"] for u in unread[:6])))
        return {"mentions": mentions, "thread_replies": replies, "dms": dms, "asks_to_me": asks_to_me,
                "my_open_asks": my_asks, "my_tasks": my_tasks, "my_prs": my_prs, "unread": unread,
                "summary": "; ".join(parts)}

    # -- feed: what a session should be woken up for (mentions, DMs, replies in its threads, subscribed channels)
    def subscriptions(self, user):
        return [r["channel"] for r in self.q("SELECT channel FROM subscriptions WHERE user=? ORDER BY ts", (user,))]

    def subscribe(self, user, channel, on=True):
        self.get_channel(channel)
        if on:
            self.x("INSERT OR IGNORE INTO subscriptions(user, channel, ts) VALUES(?,?,?)", (user, channel, now()))
        else:
            self.x("DELETE FROM subscriptions WHERE user=? AND channel=?", (user, channel))
        return self.subscriptions(user)

    def feed_cursor(self, user):
        row = self.one("SELECT last_id FROM feed_cursors WHERE user=?", (user,))
        return row["last_id"] if row else None

    def feed_ack(self, user, last_id):
        self.x("INSERT INTO feed_cursors(user, last_id) VALUES(?,?) ON CONFLICT(user) DO UPDATE SET last_id=MAX(last_id, excluded.last_id)",
               (user, int(last_id)))
        return self.feed_cursor(user)

    def feed(self, user, since=None, limit=100):
        """Pending feed items for `user` after `since` (defaults to the feed cursor; a user with no cursor gets
        nothing older than now, so a fresh session is not flooded with history)."""
        if since is None:
            since = self.feed_cursor(user)
            if since is None:
                since = self.latest_id()
                self.feed_ack(user, since)
        subs = self.subscriptions(user)
        args = {"user": user, "since": since, "limit": limit, "dm1": "dm/%s+%%" % user, "dm2": "dm/%%+%s" % user}
        chan_in = ""
        if subs:
            chan_in = " OR m.channel IN (%s)" % ",".join(":s%d" % i for i in range(len(subs)))
            args.update({"s%d" % i: c for i, c in enumerate(subs)})
        sql = (MSG_SELECT +
               "WHERE m.id > :since AND m.user <> :user AND ("
               "m.channel LIKE :dm1 OR m.channel LIKE :dm2"
               " OR (m.reply_to IS NOT NULL AND (EXISTS(SELECT 1 FROM messages p WHERE p.id = m.reply_to AND p.user = :user)"
               "   OR EXISTS(SELECT 1 FROM messages p WHERE p.reply_to = m.reply_to AND p.user = :user)))"
               " OR m.text LIKE '%@%'" + chan_in + ") ORDER BY m.id ASC LIMIT :limit")
        hit = self.mention_matcher(user)
        watches = self.watches(user)
        watched = [rid for rid, mode in watches.items() if mode == "watch"]
        if watched:
            sql = sql.replace(" OR m.text LIKE '%@%'", " OR m.reply_to IN (%s) OR m.text LIKE '%%@%%'" % ",".join(str(int(x)) for x in watched))
        out = []
        subset = set(subs)
        for r in self.msgs(sql, args):
            ch = r["channel"]
            why = []
            if r.get("reply_to") and watches.get(r["reply_to"]) == "watch":
                why.append("watch")
            if ch.startswith("dm/") and user in ch.split("/", 1)[1].split("+"):
                why.append("dm")
            if r.get("reply_to"):
                why.append("thread")
            if hit(r):
                why.append("mention")
            if ch in subset:
                why.append("subscribed")
            if not why:
                continue
            if "thread" in why and (watches.get(r["reply_to"]) == "mute" or not (
                    self.one("SELECT 1 FROM messages p WHERE p.id=? AND p.user=?", (r["reply_to"], user))
                    or self.one("SELECT 1 FROM messages p WHERE p.reply_to=? AND p.user=?", (r["reply_to"], user)))):
                why.remove("thread")
                if not why:
                    continue
            r["why"] = why
            out.append(r)
        return out

    # -- tasks
    def task(self, tid):
        row = self.one("SELECT * FROM tasks WHERE id=?", (tid,))
        if not row:
            raise ApiError(404, "no such task #%s" % (tid,))
        return self._jrow(row, ("tags",))

    def tasks(self, project=None, status=None, owner=None, limit=500):
        where, args = ["1=1"], []
        if project:
            where.append("project=?"); args.append(project)
        if status:
            where.append("status=?"); args.append(status)
        if owner:
            where.append("owner=?"); args.append(owner)
        return [self._jrow(r, ("tags",)) for r in self.q("SELECT * FROM tasks WHERE %s ORDER BY (status='done'), updated DESC LIMIT ?"
                                                          % " AND ".join(where), args + [limit])]

    def add_task(self, project, title, body, user, channel="", take=False, tags=None):
        self.get_project(project)
        title = (title or "").strip()
        if not title:
            raise ApiError(400, "task title is required")
        with self.lock:
            cur = self.x("INSERT INTO tasks(project, channel, title, body, status, owner, created_by, created, updated, tags) "
                         "VALUES(?,?,?,?,?,?,?,?,?,?)",
                         (project, channel or "", title[:300], body or "", "taken" if take else "open",
                          user if take else "", user, now(), now(), json.dumps(self.parse_tags(tags))))
            return self.task(cur.lastrowid)

    def update_task(self, tid, action, user, title=None, body=None):
        t = self.task(tid)
        sets, args = ["updated=?"], [now()]
        if action == "take":
            if t["status"] == "taken" and t["owner"] != user:
                raise ApiError(409, "task #%d is already taken by %s" % (tid, t["owner"]))
            sets += ["status='taken'", "owner=?"]; args.append(user)
        elif action == "drop":
            sets += ["status='open'", "owner=''"]
        elif action == "done":
            sets += ["status='done'", "done_ts=?", "owner=COALESCE(NULLIF(owner, ''), ?)"]; args += [now(), user]
        elif action == "reopen":
            sets += ["status='open'", "owner=''", "done_ts=NULL"]
        elif action == "edit":
            if title is not None:
                sets.append("title=?"); args.append(title.strip()[:300])
            if body is not None:
                sets.append("body=?"); args.append(body)
        else:
            raise ApiError(400, "unknown task action %r (take, drop, done, reopen, edit)" % (action,))
        args.append(tid)
        self.x("UPDATE tasks SET %s WHERE id=?" % ", ".join(sets), args)
        return self.task(tid)

    # -- pastes
    def add_paste(self, name, content, user, channel="", project=""):
        if len(content.encode("utf-8", "replace")) > MAX_PASTE:
            raise ApiError(400, "paste too large (max %d MB)" % (MAX_PASTE // (1024 * 1024)))
        pid = secrets.token_urlsafe(8).replace("-", "a").replace("_", "b")
        self.x("INSERT INTO pastes(id, name, content, size, lines, user, ts, channel, project) VALUES(?,?,?,?,?,?,?,?,?)",
               (pid, (name or "paste.txt")[:200], content, len(content.encode("utf-8", "replace")),
                content.count("\n") + (1 if content and not content.endswith("\n") else 0), user, now(),
                channel or "", project or ""))
        return self.get_paste(pid)

    def get_paste(self, pid):
        row = self.one("SELECT * FROM pastes WHERE id=?", (pid,))
        if not row:
            raise ApiError(404, "no such paste %r" % (pid,))
        return row

    def pastes(self, project=None, channel=None, limit=50):
        where, args = "WHERE 1=1", []
        if channel:
            where += " AND channel=?"; args.append(channel)
        elif project:
            where += " AND project=?"; args.append(project)
        return self.q("SELECT id, name, size, lines, user, ts, channel, project, message_id FROM pastes "
                      + where + " ORDER BY ts DESC LIMIT ?", args + [limit])

    # -- pull requests and bundles
    PR_SELECT = ("SELECT p.*, (SELECT COUNT(*) FROM messages r WHERE r.reply_to = p.message_id AND r.kind <> 'system') AS comments, "
                 "(SELECT MAX(r.ts) FROM messages r WHERE r.reply_to = p.message_id AND r.kind <> 'system') AS last_comment_ts, "
                 "b.title AS bundle_title, b.status AS bundle_status "
                 "FROM prs p LEFT JOIN bundles b ON b.id = p.bundle_id ")
    BUNDLE_SELECT = ("SELECT b.*, (SELECT COUNT(*) FROM prs p WHERE p.bundle_id = b.id) AS pr_count, "
                     "(SELECT COUNT(*) FROM prs p WHERE p.bundle_id = b.id AND p.status = 'merged') AS merged_count, "
                     "(SELECT COUNT(*) FROM messages r WHERE r.reply_to = b.message_id AND r.kind <> 'system') AS comments, "
                     "(SELECT MAX(r.ts) FROM messages r WHERE r.reply_to = b.message_id AND r.kind <> 'system') AS last_comment_ts FROM bundles b ")

    @staticmethod
    def parse_tags(tags):
        if tags is None:
            return []
        if isinstance(tags, str):
            tags = re.split(r"[,\s]+", tags)
        out = []
        for t in tags:
            t = str(t or "").strip().lower().lstrip("#")
            if t and t not in out:
                if not TAG_RE.match(t):
                    raise ApiError(400, "bad tag %r (a-z 0-9 . _ - : /, max 32)" % t)
                out.append(t)
        return out

    @staticmethod
    def parse_priority(p, default=PRIORITY_DEFAULT):
        if p is None or p == "":
            return default
        sv = str(p).strip().lower()
        if sv in PRIORITY_NAMES:
            return PRIORITY_NAMES[sv]
        if sv.isdigit() and 0 <= int(sv) <= 100:
            return max(1, int(sv))
        raise ApiError(400, "priority must be 1..100 (1 = merge first, 50 = normal, 100 = whenever) or P0..P3, got %r" % (p,))

    @staticmethod
    def _jrow(r, fields=("tags", "merge_order")):
        if r is None:
            return None
        r = dict(r)
        for f in fields:
            if f in r:
                try:
                    r[f] = json.loads(r[f] or "[]")
                except ValueError:
                    r[f] = []
        return r

    def with_reviews(self, rows):
        """Attach reviews, review_state and counts to PR rows (one query for the batch)."""
        if not rows:
            return rows
        ids = [r["id"] for r in rows]
        by_pr = {}
        for rv in self.q("SELECT * FROM pr_reviews WHERE pr_id IN (%s) ORDER BY ts ASC" % ",".join("?" * len(ids)), ids):
            by_pr.setdefault(rv["pr_id"], []).append(rv)
        for r in rows:
            revs = by_pr.get(r["id"], [])
            live = [x for x in revs if not x["stale"]]
            r["reviews"] = revs
            r["approvals"] = sum(1 for x in live if x["verdict"] == "approved")
            r["changes_requested"] = sum(1 for x in live if x["verdict"] == "changes")
            r["rejections"] = sum(1 for x in live if x["verdict"] == "rejected")
            r["approvers"] = [x["reviewer"] for x in live if x["verdict"] == "approved"]
            if r["rejections"]:
                r["review_state"] = "rejected"
            elif r["changes_requested"]:
                r["review_state"] = "changes"
            elif r["approvals"]:
                r["review_state"] = "approved"
            else:
                r["review_state"] = "pending"
            r["stale_reviews"] = len(revs) - len(live)
        return rows

    def pr(self, pid):
        row = self.one(self.PR_SELECT + "WHERE p.id=?", (pid,))
        if not row:
            raise ApiError(404, "no such PR #%s" % (pid,))
        return self.with_reviews([self._jrow(row)])[0]

    def review_pr(self, pid, reviewer, verdict, note=""):
        pr = self.pr(pid)
        if verdict not in REVIEW_VERDICTS:
            raise ApiError(400, "verdict must be one of %s" % ", ".join(REVIEW_VERDICTS))
        if reviewer == pr["author"]:
            raise ApiError(400, "you cannot review your own PR (#%d by %s)" % (pid, pr["author"]))
        if verdict in ("changes", "rejected") and not (note or "").strip():
            raise ApiError(400, "%s needs a note: what must change / why (point at your GitHub review comments)" % verdict)
        self.x("INSERT INTO pr_reviews(pr_id, reviewer, verdict, note, ts, stale) VALUES(?,?,?,?,?,0) "
               "ON CONFLICT(pr_id, reviewer) DO UPDATE SET verdict=excluded.verdict, note=excluded.note, ts=excluded.ts, stale=0",
               (pid, reviewer, verdict, (note or "").strip()[:2000], now()))
        self.x("UPDATE prs SET updated=? WHERE id=?", (now(), pid))
        return self.pr(pid)

    def rereview_pr(self, pid):
        self.pr(pid)
        n = self.x("UPDATE pr_reviews SET stale=1 WHERE pr_id=? AND stale=0", (pid,)).rowcount
        self.x("UPDATE prs SET updated=? WHERE id=?", (now(), pid))
        return self.pr(pid), n

    def prs(self, project=None, status="all", tag=None, bundle=None, author=None, q=None, review=None, limit=1000):
        where, args = ["1=1"], []
        if project:
            where.append("p.project=?"); args.append(project)
        if status == "active":
            where.append("p.status IN ('open','draft')")
        elif status and status != "all":
            if status not in PR_STATUSES:
                raise ApiError(400, "status must be one of %s, active, all" % ", ".join(PR_STATUSES))
            where.append("p.status=?"); args.append(status)
        if bundle is not None:
            where.append("p.bundle_id=?"); args.append(int(bundle))
        if author:
            where.append("p.author=?"); args.append(author)
        if q:
            where.append("(p.title LIKE ? OR p.body LIKE ? OR p.url LIKE ? OR p.branch LIKE ?)")
            args += ["%" + q + "%"] * 4
        rows = self.with_reviews([self._jrow(r) for r in self.q(
            self.PR_SELECT + "WHERE " + " AND ".join(where) +
            " ORDER BY (p.status NOT IN ('open','draft')), p.priority ASC, p.updated DESC LIMIT ?", args + [limit])])
        if tag:
            rows = [r for r in rows if tag in r["tags"]]
        if review:
            if review not in ("pending", "approved", "changes", "rejected"):
                raise ApiError(400, "review filter must be pending, approved, changes or rejected")
            rows = [r for r in rows if r["review_state"] == review]
        return rows

    TLDR_HELP = ("tldr is required: 1-3 plain sentences for someone who has not read the code, saying WHAT the change "
                 "does and WHY it matters (no jargon, no file names). Example: \"Makes the login token refresh itself "
                 "before it expires, so people stop getting logged out mid-session. Support tickets about random "
                 "logouts doubled last week.\"")

    @classmethod
    def validate_tldr(cls, text, title=""):
        t = re.sub(r"\s+", " ", (text or "")).strip()
        if len(t) < 30 or len(t.split()) < 6:
            raise ApiError(400, "tldr too short (%d chars). %s" % (len(t), cls.TLDR_HELP))
        if len(t) > 700:
            raise ApiError(400, "tldr too long (%d chars, max 700): keep it to 1-3 simple sentences" % len(t))
        if re.match(r"^https?://\S+$", t) or t.lower() == (title or "").strip().lower():
            raise ApiError(400, "tldr must explain the change in plain words, not repeat the title or a link. " + cls.TLDR_HELP)
        return t

    def prs_channel(self, project, by=""):
        return self.ensure_channel("%s/prs" % project,
                                   "Pull requests and bundles: one thread per PR (created by mb pr / mb bundle)", by)

    def add_pr(self, project, user, title, url="", body="", tags=None, priority=None, bundle_id=None,
               branch="", base="", status="open", tldr=""):
        self.get_project(project)
        title = (title or "").strip()
        if not title:
            raise ApiError(400, "PR title is required")
        tldr = self.validate_tldr(tldr, title)
        if status not in PR_STATUSES:
            raise ApiError(400, "status must be one of %s" % ", ".join(PR_STATUSES))
        m = PR_URL_RE.match(url or "")
        repo, number = (m.group(1), int(m.group(2))) if m else ("", None)
        with self.lock:
            cur = self.x("INSERT INTO prs(project, title, url, repo, number, branch, base, body, tldr, status, priority, tags, "
                         "bundle_id, author, created, updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (project, title[:300], (url or "").strip(), repo, number, branch or "", base or "", body or "",
                          tldr, status, self.parse_priority(priority), json.dumps(self.parse_tags(tags)), bundle_id, user,
                          now(), now()))
            pid = cur.lastrowid
            if bundle_id:
                self.bundle_append(bundle_id, pid)
        return self.pr(pid)

    def update_pr(self, pid, user, fields):
        """fields: title, url, body, tldr, branch, base, priority, tags (list), add_tags, remove_tags, bundle_id (None clears), status."""
        pr = self.pr(pid)
        sets, args, changes = ["updated=?"], [now()], []
        if fields.get("tldr") is not None:
            sets.append("tldr=?"); args.append(self.validate_tldr(fields["tldr"], fields.get("title") or pr["title"])); changes.append("tldr")
        for f in ("title", "url", "body", "branch", "base"):
            if f in fields and fields[f] is not None:
                v = str(fields[f]).strip() if f != "body" else str(fields[f])
                if f == "title" and not v:
                    raise ApiError(400, "title cannot be empty")
                sets.append("%s=?" % f); args.append(v[:300] if f == "title" else v); changes.append(f)
                if f == "url":
                    m = PR_URL_RE.match(v)
                    sets += ["repo=?", "number=?"]; args += [m.group(1) if m else "", int(m.group(2)) if m else None]
        if "priority" in fields and fields["priority"] is not None:
            sets.append("priority=?"); args.append(self.parse_priority(fields["priority"])); changes.append("priority")
        if any(k in fields and fields[k] is not None for k in ("tags", "add_tags", "remove_tags")):
            tags = self.parse_tags(fields["tags"]) if fields.get("tags") is not None else list(pr["tags"])
            for t in self.parse_tags(fields.get("add_tags")):
                if t not in tags:
                    tags.append(t)
            for t in self.parse_tags(fields.get("remove_tags")):
                if t in tags:
                    tags.remove(t)
            sets.append("tags=?"); args.append(json.dumps(tags)); changes.append("tags")
        if "bundle_id" in fields:
            new_b = fields["bundle_id"]
            if new_b != pr["bundle_id"]:
                if pr["bundle_id"]:
                    self.bundle_remove(pr["bundle_id"], pid)
                if new_b:
                    self.bundle_append(new_b, pid)
                sets.append("bundle_id=?"); args.append(new_b); changes.append("bundle")
        if "status" in fields and fields["status"] is not None:
            st = fields["status"]
            if st not in PR_STATUSES:
                raise ApiError(400, "status must be one of %s" % ", ".join(PR_STATUSES))
            sets.append("status=?"); args.append(st); changes.append("status")
            sets.append("merged_ts=?"); args.append(now() if st == "merged" else None)
            sets.append("closed_ts=?"); args.append(now() if st == "closed" else None)
        args.append(pid)
        self.x("UPDATE prs SET %s WHERE id=?" % ", ".join(sets), args)
        return self.pr(pid), changes

    def tags(self, project=None):
        counts = {}
        for table in ("prs", "bundles"):
            sql = "SELECT tags, status FROM %s" % table + (" WHERE project=?" if project else "")
            for r in self.q(sql, (project,) if project else ()):
                for t in self._jrow(r, ("tags",))["tags"]:
                    c = counts.setdefault(t, {"tag": t, "total": 0, "active": 0})
                    c["total"] += 1
                    if r["status"] in ("open", "draft"):
                        c["active"] += 1
        return sorted(counts.values(), key=lambda c: (-c["active"], -c["total"], c["tag"]))

    def bundle(self, bid):
        row = self.one(self.BUNDLE_SELECT + "WHERE b.id=?", (bid,))
        if not row:
            raise ApiError(404, "no such bundle #%s" % (bid,))
        b = self._jrow(row)
        members = self.prs(bundle=bid, status="all")
        b["approved_prs"] = sum(1 for m in members if m["review_state"] == "approved")
        b["changes_prs"] = [m["id"] for m in members if m["review_state"] == "changes"]
        b["rejected_prs"] = [m["id"] for m in members if m["review_state"] == "rejected"]
        return b

    def bundles(self, project=None, status="all", limit=500):
        where, args = ["1=1"], []
        if project:
            where.append("b.project=?"); args.append(project)
        if status == "active":
            where.append("b.status IN ('open','draft')")
        elif status and status != "all":
            where.append("b.status=?"); args.append(status)
        rows = [self._jrow(r) for r in self.q(
            self.BUNDLE_SELECT + "WHERE " + " AND ".join(where) +
            " ORDER BY (b.status NOT IN ('open','draft')), b.priority ASC, b.updated DESC LIMIT ?", args + [limit])]
        for b in rows:
            members = self.prs(bundle=b["id"], status="all")
            b["approved_prs"] = sum(1 for m in members if m["review_state"] == "approved")
            b["changes_prs"] = [m["id"] for m in members if m["review_state"] == "changes"]
            b["rejected_prs"] = [m["id"] for m in members if m["review_state"] == "rejected"]
        return rows

    def bundle_prs(self, bid):
        b = self.bundle(bid)
        rows = {r["id"]: r for r in self.prs(bundle=bid, status="all")}
        ordered = [rows.pop(i) for i in b["merge_order"] if i in rows]
        return ordered + sorted(rows.values(), key=lambda r: r["id"])

    def add_bundle(self, project, user, title, body="", priority=None, tags=None):
        self.get_project(project)
        title = (title or "").strip()
        if not title:
            raise ApiError(400, "bundle title is required")
        cur = self.x("INSERT INTO bundles(project, title, body, status, priority, tags, merge_order, created_by, created, updated) "
                     "VALUES(?,?,?,?,?,?,?,?,?,?)",
                     (project, title[:300], body or "", "open", self.parse_priority(priority),
                      json.dumps(self.parse_tags(tags)), "[]", user, now(), now()))
        return self.bundle(cur.lastrowid)

    def resolve_bundle(self, project, ref, user):
        """ref: id or title. Returns (bundle, created)."""
        if isinstance(ref, int) or (isinstance(ref, str) and ref.strip().lstrip("#").isdigit()):
            b = self.bundle(int(str(ref).strip().lstrip("#")))
            if b["project"] != project:
                raise ApiError(400, "bundle #%d belongs to project %s, not %s" % (b["id"], b["project"], project))
            return b, False
        title = str(ref).strip()
        row = self.one("SELECT id FROM bundles WHERE project=? AND lower(title)=lower(?)", (project, title))
        if row:
            return self.bundle(row["id"]), False
        return self.add_bundle(project, user, title), True

    def bundle_append(self, bid, pid):
        b = self.bundle(bid)
        order = [i for i in b["merge_order"] if i != pid] + [pid]
        self.x("UPDATE bundles SET merge_order=?, updated=? WHERE id=?", (json.dumps(order), now(), bid))

    def bundle_remove(self, bid, pid):
        b = self.bundle(bid)
        order = [i for i in b["merge_order"] if i != pid]
        self.x("UPDATE bundles SET merge_order=?, updated=? WHERE id=?", (json.dumps(order), now(), bid))

    def update_bundle(self, bid, user, fields):
        b = self.bundle(bid)
        sets, args, changes = ["updated=?"], [now()], []
        for f in ("title", "body"):
            if f in fields and fields[f] is not None:
                v = str(fields[f]).strip() if f == "title" else str(fields[f])
                if f == "title" and not v:
                    raise ApiError(400, "title cannot be empty")
                sets.append("%s=?" % f); args.append(v[:300] if f == "title" else v); changes.append(f)
        if "priority" in fields and fields["priority"] is not None:
            sets.append("priority=?"); args.append(self.parse_priority(fields["priority"])); changes.append("priority")
        if any(k in fields and fields[k] is not None for k in ("tags", "add_tags", "remove_tags")):
            tags = self.parse_tags(fields["tags"]) if fields.get("tags") is not None else list(b["tags"])
            for t in self.parse_tags(fields.get("add_tags")):
                if t not in tags:
                    tags.append(t)
            for t in self.parse_tags(fields.get("remove_tags")):
                if t in tags:
                    tags.remove(t)
            sets.append("tags=?"); args.append(json.dumps(tags)); changes.append("tags")
        if "order" in fields and fields["order"] is not None:
            ids = [int(str(i).lstrip("#")) for i in fields["order"]]
            members = {r["id"] for r in self.prs(bundle=bid)}
            bad = [i for i in ids if i not in members]
            if bad:
                raise ApiError(400, "not in bundle #%d: %s" % (bid, ", ".join("#%d" % i for i in bad)))
            order = ids + [i for i in sorted(members) if i not in ids]
            sets.append("merge_order=?"); args.append(json.dumps(order)); changes.append("order")
        if "status" in fields and fields["status"] is not None:
            st = fields["status"]
            if st not in PR_STATUSES:
                raise ApiError(400, "status must be one of %s" % ", ".join(PR_STATUSES))
            sets.append("status=?"); args.append(st); changes.append("status")
        args.append(bid)
        self.x("UPDATE bundles SET %s WHERE id=?" % ", ".join(sets), args)
        return self.bundle(bid), changes

    # -- overview
    def project_overview(self, name, user=None):
        p = self.get_project(name)
        channels = self.channels(project=name, user=user)
        tasks = self.tasks(project=name)
        pins = self.pins(project=name, limit=30)
        asks = self.asks(project=name)
        people = self.q(
            """
            SELECT u.*, (SELECT MAX(m.ts) FROM messages m WHERE m.user=u.name AND m.channel LIKE :like) AS last_post_ts,
                   (SELECT COUNT(*) FROM tasks t WHERE t.owner=u.name AND t.project=:p AND t.status='taken') AS tasks_taken
            FROM users u
            WHERE u.project=:p OR EXISTS(SELECT 1 FROM messages m WHERE m.user=u.name AND m.channel LIKE :like AND m.ts > :cut)
            ORDER BY u.last_seen DESC
            """, {"p": name, "like": name + "/%", "cut": now() - 86400})
        activity = self.msgs(MSG_SELECT + "WHERE m.channel LIKE ? ORDER BY m.id DESC LIMIT 15", (name + "/%",))
        activity.reverse()
        prs = self.prs(project=name, status="active", limit=12)
        pr_counts = {st: self.one("SELECT COUNT(*) AS n FROM prs WHERE project=? AND status=?", (name, st))["n"]
                     for st in PR_STATUSES}
        for x in people:
            x["live"] = bool((x.get("live_until") or 0) > now())
            x["areas"] = [a["area"] for a in self.areas(x["name"], limit=4)]
        return {"project": p, "channels": channels, "tasks": tasks, "pins": pins, "asks": asks,
                "people": people, "activity": activity, "pastes": self.pastes(project=name, limit=20),
                "prs": prs, "pr_counts": pr_counts, "bundles": self.bundles(project=name, status="active", limit=12),
                "groups": self.groups(project=name)}

    # -- deletion (humans only; the GUI and `mb rm` use these)
    def delete_channel(self, key):
        ch = self.get_channel(key)
        if key == "%s/%s" % (DEFAULT_PROJECT, DEFAULT_CHANNEL):
            raise ApiError(400, "%s is the board's home channel and cannot be deleted" % key)
        with self.lock:
            n = self.one("SELECT COUNT(*) AS n FROM messages WHERE channel=?", (key,))["n"]
            self.x("DELETE FROM messages WHERE channel=?", (key,))
            self.x("DELETE FROM cursors WHERE channel=?", (key,))
            self.x("DELETE FROM subscriptions WHERE channel=?", (key,))
            self.x("UPDATE pastes SET channel='' WHERE channel=?", (key,))
            self.x("UPDATE tasks SET channel='' WHERE channel=?", (key,))
            self.x("DELETE FROM channels WHERE key=?", (key,))
        ch["deleted_messages"] = n
        return ch

    def delete_project(self, name):
        if name == DEFAULT_PROJECT:
            raise ApiError(400, "project %s cannot be deleted" % name)
        p = self.get_project(name)
        with self.lock:
            keys = [c["key"] for c in self.q("SELECT key FROM channels WHERE project=?", (name,))]
            total = 0
            for key in keys:
                total += self.one("SELECT COUNT(*) AS n FROM messages WHERE channel=?", (key,))["n"]
                self.x("DELETE FROM messages WHERE channel=?", (key,))
                self.x("DELETE FROM cursors WHERE channel=?", (key,))
                self.x("DELETE FROM subscriptions WHERE channel=?", (key,))
            self.x("DELETE FROM channels WHERE project=?", (name,))
            self.x("DELETE FROM tasks WHERE project=?", (name,))
            self.x("DELETE FROM groups WHERE project=?", (name,))
            self.x("DELETE FROM pr_reviews WHERE pr_id IN (SELECT id FROM prs WHERE project=?)", (name,))
            self.x("DELETE FROM prs WHERE project=?", (name,))
            self.x("DELETE FROM bundles WHERE project=?", (name,))
            self.x("DELETE FROM pastes WHERE project=?", (name,))
            self.x("UPDATE users SET project='' WHERE project=?", (name,))
            self.x("DELETE FROM projects WHERE name=?", (name,))
        p["deleted_channels"] = keys
        p["deleted_messages"] = total
        return p

    def backup(self, keep=14):
        """Consistent copy of the database into data/backups (called daily by the server and by `mb backup`)."""
        bdir = os.path.join(os.path.dirname(self.path), "backups")
        os.makedirs(bdir, exist_ok=True)
        target = os.path.join(bdir, "msgboard-%s.db" % time.strftime("%Y%m%d-%H%M%S"))
        with self.lock:
            dest = sqlite3.connect(target)
            try:
                self.conn.backup(dest)
            finally:
                dest.close()
        old = sorted(f for f in os.listdir(bdir) if f.startswith("msgboard-") and f.endswith(".db"))
        for f in old[:-keep]:
            try:
                os.remove(os.path.join(bdir, f))
            except OSError:
                pass
        return target

    def counts(self):
        return {
            "users": self.one("SELECT COUNT(*) AS n FROM users")["n"],
            "projects": self.one("SELECT COUNT(*) AS n FROM projects")["n"],
            "channels": self.one("SELECT COUNT(*) AS n FROM channels")["n"],
            "messages": self.one("SELECT COUNT(*) AS n FROM messages")["n"],
            "tasks_open": self.one("SELECT COUNT(*) AS n FROM tasks WHERE status<>'done'")["n"],
            "prs_active": self.one("SELECT COUNT(*) AS n FROM prs WHERE status IN ('open','draft')")["n"],
            "latest_id": self.latest_id(),
        }


# --------------------------------------------------------------------------- notifications

class Notifier:
    """macOS notification when a human is mentioned, DM'd or asked (MB_NOTIFY=0 disables)."""

    def __init__(self, store):
        self.store = store
        self.enabled = sys.platform == "darwin" and os.environ.get("MB_NOTIFY", "1") != "0"

    def on_message(self, m):
        if not self.enabled or m["kind"] == "system":
            return
        humans = [h for h in self.store.humans() if h != m["user"]]
        if not humans:
            return
        mentioned = mentions_in(m["text"])
        hit = any(h.lower() in mentioned for h in humans) or bool(mentioned & set(BROADCAST_MENTIONS))
        if not hit and m["channel"].startswith(DM_PROJECT + "/"):
            hit = any(h in m["channel"].split("/", 1)[1].split("+") for h in humans)
        if not hit:
            return
        title = "msgboard %s" % ("DM" if m["project"] == DM_PROJECT else "#" + m["chname"])
        text = m["text"].replace("\n", " ")[:180]
        kind = {"ask": "question from ", "answer": "answer from "}.get(m["kind"], "")
        threading.Thread(target=self._send, args=(title, kind + m["user"], text), daemon=True).start()

    @staticmethod
    def _send(title, subtitle, text):
        def esc(s):
            return s.replace("\\", "").replace('"', "'")
        script = 'display notification "%s" with title "%s" subtitle "%s"' % (esc(text), esc(title), esc(subtitle))
        try:
            subprocess.run(["osascript", "-e", script], timeout=10, capture_output=True)
        except Exception as e:  # never let notifications break the server
            sys.stderr.write("notification failed: %r\n" % (e,))


# --------------------------------------------------------------------------- http

class Handler(BaseHTTPRequestHandler):
    server_version = "msgboard/2.0"
    QUIET_PATHS = ("/api/wait", "/api/events", "/api/health", "/api/unread", "/api/inbox")

    # -- plumbing
    def log_message(self, fmt, *args):
        path = self.path.split("?", 1)[0]
        if path in self.QUIET_PATHS:
            return
        sys.stderr.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), fmt % args))

    def send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_bytes(self, body, ctype, status=200, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path, ctype):
        with open(path, "rb") as f:
            self.send_bytes(f.read(), ctype)

    def send_vendor(self, path):
        base = os.path.join(STATIC_DIR, "vendor")
        target = os.path.normpath(os.path.join(STATIC_DIR, path.lstrip("/")))
        if not target.startswith(base + os.sep) or not os.path.isfile(target):
            raise ApiError(404, "not found")
        ctype = {".js": "application/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8"}.get(os.path.splitext(target)[1], "application/octet-stream")
        return self.send_file(target, ctype)

    def read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if not raw.strip():
            return {}
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype == "application/x-www-form-urlencoded":
            return {k: v[-1] for k, v in urllib.parse.parse_qs(raw.decode("utf-8")).items()}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            raise ApiError(400, "invalid JSON body: %s" % e)
        if not isinstance(data, dict):
            raise ApiError(400, "JSON body must be an object")
        return data

    @staticmethod
    def int_param(q, key, default=None, lo=None, hi=None):
        v = q.get(key)
        if v in (None, ""):
            return default
        try:
            v = int(v)
        except ValueError:
            raise ApiError(400, "%s must be an integer" % key)
        if lo is not None:
            v = max(lo, v)
        if hi is not None:
            v = min(hi, v)
        return v

    def authorized(self, q):
        token = self.server.token
        if not token:
            return True
        auth = self.headers.get("Authorization") or ""
        if auth.startswith("Bearer ") and secrets.compare_digest(auth[7:].strip(), token):
            return True
        if secrets.compare_digest(self.headers.get("X-MB-Token") or "", token):
            return True
        if secrets.compare_digest(q.get("token") or "", token):
            return True
        cookie = self.headers.get("Cookie") or ""
        for part in cookie.split(";"):
            k, _, v = part.strip().partition("=")
            if k == "mb_token" and secrets.compare_digest(urllib.parse.unquote(v), token):
                return True
        return False

    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def do_DELETE(self):
        self.route("DELETE")

    def route(self, method):
        parsed = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(parsed.path)
        q = {k: v[-1] for k, v in urllib.parse.parse_qs(parsed.query, keep_blank_values=True).items()}
        try:
            if method == "GET" and path in ("/", "/index.html"):
                return self.send_file(os.path.join(STATIC_DIR, "index.html"), "text/html; charset=utf-8")
            if method == "GET" and path.startswith("/vendor/"):
                return self.send_vendor(path)
            if (path.startswith("/api/") or path.startswith("/p/")) and not self.authorized(q):
                raise ApiError(401, "missing or wrong token (MB_TOKEN)")
            if method == "GET" and path == "/api/events":
                return self.sse(q)
            if path.startswith("/p/"):
                return self.paste_page(path)
            handler = self.find_route(method, path)
            if handler is None:
                raise ApiError(404, "no such endpoint: %s %s" % (method, path))
            fn, groups = handler
            return fn(q, *groups)
        except ApiError as e:
            err = {"error": e.message}
            err.update(e.extra)
            self.send_json(err, e.status)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # keep the server alive, report the bug
            sys.stderr.write("ERROR %s %s: %r\n" % (method, path, e))
            try:
                self.send_json({"error": "internal error: %r" % (e,)}, 500)
            except OSError:
                pass

    def find_route(self, method, path):
        for m, rx, fn in ROUTES:
            if m != method:
                continue
            mt = rx.match(path)
            if mt:
                return getattr(self, fn), mt.groups()
        return None

    # -- endpoints: meta
    def ep_health(self, q):
        c = self.server.store.counts()
        c.update(ok=True, time=now(), uptime=now() - self.server.started, url=self.server.url,
                 pid=os.getpid(), default_user=self.server.default_user, auth=bool(self.server.token),
                 fts=self.server.store.fts, version=2)
        return self.send_json(c)

    def ep_links(self, q):
        path = os.path.join(os.path.dirname(self.server.store_path), "links-state.json")
        try:
            with open(path) as f:
                st = json.load(f)
        except (OSError, ValueError):
            st = {}
        return self.send_json([dict(v, name=k) for k, v in st.items()])

    # -- users
    def ep_users_get(self, q):
        return self.send_json(self.server.store.users(project=q.get("project") or None))

    def ep_users_post(self, q):
        store, bus = self.server.store, self.server.bus
        body = self.read_body()
        user, created = store.ensure_user(body.get("name"), emoji=body.get("emoji"), status=body.get("status"),
                                          project=body.get("project"), cwd=body.get("cwd"), human=body.get("human"))
        bus.publish("user", user)
        return self.send_json(dict(user, created=created), 201 if created else 200)

    # -- projects
    def ep_projects_get(self, q):
        return self.send_json(self.server.store.projects())

    def ep_projects_post(self, q):
        store, bus = self.server.store, self.server.bus
        body = self.read_body()
        by = body.get("user") or ""
        p, created = store.ensure_project(body.get("name"), body.get("title") or "", body.get("description") or "", by)
        bus.publish("project", p)
        if created:
            for ch in store.channels(project=p["name"]):
                bus.publish("channel", ch)
        return self.send_json(dict(p, created=created), 201 if created else 200)

    def ep_project_overview(self, q, name):
        return self.send_json(self.server.store.project_overview(name, user=q.get("as") or None))

    def ep_project_delete(self, q, name):
        store, bus = self.server.store, self.server.bus
        p = store.delete_project(name)
        bus.publish("project_deleted", {"name": name, "channels": p["deleted_channels"]})
        sys.stderr.write("deleted project %s (%d channels, %d messages)\n" % (name, len(p["deleted_channels"]), p["deleted_messages"]))
        return self.send_json(p)

    def ep_channel_delete(self, q, key):
        store, bus = self.server.store, self.server.bus
        ch = store.delete_channel(key)
        bus.publish("channel_deleted", {"key": key, "project": ch["project"]})
        if ch["project"] != DM_PROJECT:
            bus.publish("project", store.get_project(ch["project"]))
        sys.stderr.write("deleted channel %s (%d messages)\n" % (key, ch["deleted_messages"]))
        return self.send_json(ch)

    def ep_backup(self, q):
        path = self.server.store.backup()
        return self.send_json({"ok": True, "path": path, "size": os.path.getsize(path)})

    # -- channels
    def ep_channels_get(self, q):
        return self.send_json(self.server.store.channels(project=q.get("project") or None, user=q.get("as") or None))

    def ep_channels_post(self, q):
        store, bus = self.server.store, self.server.bus
        body = self.read_body()
        by = body.get("user") or ""
        if by:
            store.touch_user(by)
        key = body.get("key")
        if not key:
            key, _ = store.resolve_channel(body.get("name") or "", body.get("project") or DEFAULT_PROJECT)
        ch, created = store.ensure_channel(key, body.get("topic") or "", by)
        if not created and body.get("topic"):
            ch = store.set_topic(key, body["topic"])
        bus.publish("channel", ch)
        if created and ch["project"] != DM_PROJECT:
            bus.publish("project", store.get_project(ch["project"]))
        return self.send_json(dict(ch, created=created), 201 if created else 200)

    def ep_resolve(self, q):
        store = self.server.store
        key, exists = store.resolve_channel(q.get("channel") or "", q.get("project") or None)
        return self.send_json({"key": key, "exists": exists, "candidates": getattr(store, "last_candidates", [])})

    def ep_dm(self, q):
        store, bus = self.server.store, self.server.bus
        me, other = valid_user(q.get("as")), valid_user(q.get("with"))
        ch, created = store.dm_channel(me, other)
        if created:
            bus.publish("channel", ch)
        return self.send_json(ch)

    # -- messages
    def ep_messages_get(self, q, channel):
        store = self.server.store
        store.get_channel(channel)
        limit = self.int_param(q, "limit", 50, 1, 5000)
        around = self.int_param(q, "around")
        rows = store.history(channel, since=self.int_param(q, "since"), before=self.int_param(q, "before"),
                             around=around, limit=limit, threads=truthy(q.get("threads", "0")))
        user = q.get("as")
        if user:
            valid_user(user)
            store.touch_user(user)
            if rows and not truthy(q.get("peek", "0")) and around is None:
                store.advance_cursors(user, rows)
        return self.send_json(rows)

    def post_message(self, channel, body, reply_to=None):
        store, bus = self.server.store, self.server.bus
        user = valid_user(body.get("user"))
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ApiError(400, "text is required")
        if len(text) > MAX_TEXT:
            raise ApiError(400, "text too long (max %d chars); use a paste" % MAX_TEXT)
        text = text.rstrip("\n")
        kind = body.get("kind") or "message"
        meta = body.get("meta") if isinstance(body.get("meta"), dict) else {}
        _, u_created = store.ensure_user(user, cwd=body.get("cwd"))
        if u_created:
            bus.publish("user", store.get_user(user))
        if reply_to is None and channel:
            ch, c_created = store.ensure_channel(channel, "", user)
            if c_created:
                bus.publish("channel", ch)
                if ch["project"] != DM_PROJECT:
                    bus.publish("project", store.get_project(ch["project"]))
        msg = store.add_message(channel, user, text, reply_to=reply_to, kind=kind, meta=meta)
        if kind == "answer" and msg.get("reply_to"):
            store.resolve_ask(msg["reply_to"], user)
        bus.publish("message", msg)
        if msg.get("reply_to"):
            bus.publish("message_update", store.get_message(msg["reply_to"]))
        self.server.notifier.on_message(msg)
        return msg

    def ep_messages_post(self, q, channel):
        body = self.read_body()
        reply_to = body.get("reply_to")
        msg = self.post_message(channel, body, reply_to=int(reply_to) if reply_to else None)
        return self.send_json(msg, 201)

    def ep_reply(self, q, mid):
        body = self.read_body()
        msg = self.post_message(None, body, reply_to=int(mid))
        return self.send_json(msg, 201)

    def ep_read(self, q, channel):
        store = self.server.store
        body = self.read_body()
        user = valid_user(body.get("user"))
        store.get_channel(channel)
        last_id = body.get("last_id")
        if last_id is None:
            last_id = store.latest_id()
        store.advance_cursor(user, channel, int(last_id))
        store.touch_user(user)
        return self.send_json({"ok": True, "channel": channel, "last_id": int(last_id)})

    def ep_message_get(self, q, mid):
        return self.send_json(self.server.store.get_message(int(mid)))

    def ep_thread(self, q, mid):
        store = self.server.store
        root, replies = store.thread(int(mid))
        user = q.get("as")
        if user:
            valid_user(user)
            store.touch_user(user)
        return self.send_json({"root": root, "replies": replies})

    def ep_pin(self, q, mid, action):
        store, bus = self.server.store, self.server.bus
        body = self.read_body()
        user = valid_user(body.get("user"))
        m = store.set_pin(int(mid), user, action == "pin")
        bus.publish("message_update", m)
        return self.send_json(m)

    def ep_resolve_ask(self, q, mid):
        store, bus = self.server.store, self.server.bus
        body = self.read_body()
        user = valid_user(body.get("user"))
        m = store.resolve_ask(int(mid), user)
        bus.publish("message_update", m)
        return self.send_json(m)

    def ep_pins(self, q):
        return self.send_json(self.server.store.pins(channel=q.get("channel") or None,
                                                     project=q.get("project") or None))

    def ep_asks(self, q):
        return self.send_json(self.server.store.asks(user=q.get("as") or None, project=q.get("project") or None,
                                                     open_only=not truthy(q.get("all", "0"))))

    def ep_search(self, q):
        return self.send_json(self.server.store.search(q.get("q"), project=q.get("project") or None,
                                                       channel=q.get("channel") or None,
                                                       limit=self.int_param(q, "limit", 20, 1, 200)))

    def ep_unread(self, q):
        store = self.server.store
        user = valid_user(q.get("as"))
        store.touch_user(user)
        return self.send_json(store.unread(user))

    def ep_inbox(self, q):
        store = self.server.store
        user = valid_user(q.get("as"))
        store.touch_user(user)
        return self.send_json(store.inbox(user))

    def ep_wait(self, q):
        """Long-poll: block until messages arrive (max LONGPOLL_MAX seconds)."""
        store, bus = self.server.store, self.server.bus
        user = q.get("as") or None
        if user:
            valid_user(user)
            store.touch_user(user)
        channel = q.get("channel") or None
        if channel:
            store.get_channel(channel)
        project = q.get("project") or None
        mention = q.get("mention") or None
        if mention:
            valid_user(mention)
        thread = self.int_param(q, "thread")
        since = self.int_param(q, "since")
        default_since = self.int_param(q, "default_since")
        if default_since is None:
            default_since = store.latest_id()
        timeout = min(float(q.get("timeout") or LONGPOLL_MAX), LONGPOLL_MAX)
        limit = self.int_param(q, "limit", 200, 1, 5000)
        peek = truthy(q.get("peek", "0")) or bool(mention) or thread is not None
        client_acks = truthy(q.get("ack", "server")) if q.get("ack") in ("1", "true", "client") else False
        deadline = time.monotonic() + timeout
        while True:
            seen = bus.seq
            rows = store.pending(channel=channel, project=project, user=user, since=since,
                                 default_since=default_since, mention=mention, thread=thread,
                                 no_threads=truthy(q.get("no_threads", "0")), limit=limit)
            if rows:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0 or self.server.stopping:
                break
            # wait in short slices so a client that hung up (dead tunnel, killed process) is noticed
            bus.wait_for_change(seen, min(remaining, 2.0))
            if self.client_gone():
                return
        try:
            self.send_json({"messages": rows, "latest_id": store.latest_id(), "timeout": not rows})
        except OSError:
            return  # response not delivered: cursors untouched, the messages stay pending
        if rows and not peek and not client_acks:
            store.advance_cursors(user, rows)

    def client_gone(self):
        """True if the client closed its side (EOF) while we were waiting."""
        try:
            readable, _, _ = select.select([self.connection], [], [], 0)
            if readable:
                return self.connection.recv(1, socket.MSG_PEEK) == b""
        except (OSError, ValueError):
            return True
        return False

    # -- orientation: brief / who / profile / areas / groups / watches / state / presence
    def ep_brief(self, q):
        store = self.server.store
        user = valid_user(q.get("as"))
        store.touch_user(user)
        return self.send_json(store.brief(user, q.get("project") or None))

    def ep_who(self, q):
        return self.send_json(self.server.store.who(q.get("q") or q.get("topic") or "", project=q.get("project") or None))

    def ep_profile(self, q, name):
        return self.send_json(self.server.store.profile(name))

    def ep_areas(self, q, name):
        store, bus = self.server.store, self.server.bus
        if self.command == "GET":
            return self.send_json({"areas": store.areas(name)})
        body = self.read_body()
        user = valid_user(body.get("user") or name)
        for a in body.get("add") or []:
            store.set_area(user, a, "add")
        for a in body.get("remove") or []:
            store.set_area(user, a, "remove")
        bus.publish("user", store.get_user(user))
        return self.send_json({"areas": store.areas(user)})

    def ep_groups(self, q):
        store, bus = self.server.store, self.server.bus
        if self.command == "GET":
            return self.send_json(store.groups(project=q.get("project") or None, member=q.get("member") or None))
        body = self.read_body()
        user = valid_user(body.get("user"))
        project = body.get("project") or DEFAULT_PROJECT
        res = store.set_group(project, body.get("name") or "", user, add=body.get("add") or [], remove=body.get("remove") or [],
                              delete=truthy(body.get("delete", "0")))
        bus.publish("group", {"project": project, "name": body.get("name")})
        return self.send_json({"groups": res or store.groups(project=project)})

    def ep_watch(self, q, mid):
        store = self.server.store
        body = self.read_body()
        user = valid_user(body.get("user"))
        mode = body.get("mode") or "watch"
        if mode not in ("watch", "mute", "off"):
            raise ApiError(400, "mode must be watch, mute or off")
        root = store.set_watch(user, int(mid), mode)
        return self.send_json({"root": root, "mode": mode, "watches": store.watches(user)})

    def ep_state(self, q, channel):
        store, bus = self.server.store, self.server.bus
        body = self.read_body()
        user = valid_user(body.get("user"))
        ch = store.set_state(channel, body.get("state") or "", user)
        bus.publish("channel", ch)
        return self.send_json(ch)

    def ep_presence(self, q):
        store, bus = self.server.store, self.server.bus
        body = self.read_body()
        user = valid_user(body.get("user"))
        store.ensure_user(user, project=body.get("project"))
        if body.get("state") == "offline":
            store.offline(user)
        else:
            store.presence(user, body.get("kind") or "channel", ttl=int(body.get("ttl") or 90))
        bus.publish("user", store.get_user(user))
        return self.send_json({"ok": True})

    # -- feed
    def ep_feed(self, q):
        store = self.server.store
        user = valid_user(q.get("as"))
        store.touch_user(user)
        rows = store.feed(user, since=self.int_param(q, "since"), limit=self.int_param(q, "limit", 100, 1, 500))
        return self.send_json({"messages": rows, "latest_id": store.latest_id(), "cursor": store.feed_cursor(user),
                               "subscriptions": store.subscriptions(user)})

    def ep_feed_wait(self, q):
        """Long-poll the feed (max LONGPOLL_MAX seconds). The client acknowledges with POST /api/feed/ack."""
        store, bus = self.server.store, self.server.bus
        user = valid_user(q.get("as"))
        store.touch_user(user)
        since = self.int_param(q, "since")
        timeout = min(float(q.get("timeout") or LONGPOLL_MAX), LONGPOLL_MAX)
        limit = self.int_param(q, "limit", 100, 1, 500)
        deadline = time.monotonic() + timeout
        while True:
            seen = bus.seq
            rows = store.feed(user, since=since, limit=limit)
            if rows:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0 or self.server.stopping:
                break
            bus.wait_for_change(seen, min(remaining, 2.0))
            if self.client_gone():
                return
        return self.send_json({"messages": rows, "latest_id": store.latest_id(), "cursor": store.feed_cursor(user),
                               "timeout": not rows})

    def ep_feed_ack(self, q):
        store = self.server.store
        body = self.read_body()
        user = valid_user(body.get("user"))
        return self.send_json({"ok": True, "cursor": store.feed_ack(user, int(body.get("last_id") or 0))})

    def ep_subscriptions(self, q):
        store, bus = self.server.store, self.server.bus
        if self.command == "GET":
            user = valid_user(q.get("as"))
            return self.send_json({"subscriptions": store.subscriptions(user)})
        body = self.read_body()
        user = valid_user(body.get("user"))
        store.ensure_user(user)
        channel = body.get("channel") or ""
        subs = store.subscribe(user, channel, on=not truthy(body.get("remove", "0")))
        bus.publish("user", store.get_user(user))
        return self.send_json({"subscriptions": subs})

    # -- tasks
    def task_event(self, t, user, action):
        """System message in the project's home channel + SSE event."""
        store, bus = self.server.store, self.server.bus
        bus.publish("task", t)
        verbs = {"add": "added", "take": "took", "drop": "released", "done": "finished", "reopen": "reopened",
                 "edit": "edited"}
        channel = t["channel"] or "%s/%s" % (t["project"], DEFAULT_CHANNEL)
        ch, created = store.ensure_channel(channel, "", user)
        if created:
            bus.publish("channel", ch)
        text = "%s task #%d: %s" % (verbs.get(action, action), t["id"], t["title"])
        msg = store.add_message(channel, user, text, kind="system", meta={"task": t["id"], "action": action})
        bus.publish("message", msg)

    def ep_tasks_get(self, q):
        return self.send_json(self.server.store.tasks(project=q.get("project") or None, status=q.get("status") or None,
                                                      owner=q.get("owner") or None))

    def ep_tasks_post(self, q):
        store = self.server.store
        body = self.read_body()
        user = valid_user(body.get("user"))
        store.ensure_user(user)
        project = body.get("project") or DEFAULT_PROJECT
        channel = body.get("channel") or ""
        if channel:
            store.get_channel(channel)
            if channel.split("/", 1)[0] != DM_PROJECT:
                project = channel.split("/", 1)[0]
        t = store.add_task(project, body.get("title"), body.get("body") or "", user, channel=channel,
                           take=truthy(body.get("take", "0")), tags=body.get("tags"))
        if t["status"] == "taken":
            store.bump_areas(user, t["tags"], 2)
        self.task_event(t, user, "take" if t["status"] == "taken" else "add")
        return self.send_json(t, 201)

    def ep_task_get(self, q, tid):
        return self.send_json(self.server.store.task(int(tid)))

    def ep_task_post(self, q, tid):
        store = self.server.store
        body = self.read_body()
        user = valid_user(body.get("user"))
        store.ensure_user(user)
        action = body.get("action") or "edit"
        t = store.update_task(int(tid), action, user, title=body.get("title"), body=body.get("body"))
        if action in ("take", "done"):
            store.bump_areas(user, t["tags"], 2)
        self.task_event(t, user, action)
        return self.send_json(t)

    # -- pull requests / bundles
    def pr_root_text(self, pr):
        head = "**PR: %s**" % pr["title"]
        bits = ["prio %d" % pr["priority"]]
        if pr["tags"]:
            bits.append("tags: " + ", ".join(pr["tags"]))
        if pr.get("bundle_title"):
            bits.append("bundle: %s (#%d)" % (pr["bundle_title"], pr["bundle_id"]))
        lines = [head + " (" + ", ".join(bits) + ")"]
        if pr.get("tldr"):
            lines.append("**TL;DR:** " + pr["tldr"])
        if pr["url"]:
            lines.append(pr["url"])
        if pr["branch"]:
            lines.append("branch `%s`%s" % (pr["branch"], (" -> `%s`" % pr["base"]) if pr["base"] else ""))
        if pr["body"]:
            lines.append("")
            lines.append(pr["body"][:4000])
        return "\n".join(lines)

    def bundle_root_text(self, b):
        bits = ["prio %d" % b["priority"]]
        if b["tags"]:
            bits.append("tags: " + ", ".join(b["tags"]))
        lines = ["**Bundle: %s** (%s): PRs that must be merged together" % (b["title"], ", ".join(bits))]
        if b["body"]:
            lines += ["", b["body"][:4000]]
        return "\n".join(lines)

    def ensure_pr_root(self, pr, user):
        """The PR's discussion thread root in <project>/prs (recreated if the channel was deleted)."""
        store = self.server.store
        if pr.get("message_id"):
            try:
                return store.get_message(pr["message_id"])
            except ApiError:
                pass
        ch, created = store.prs_channel(pr["project"], user)
        if created:
            self.server.bus.publish("channel", ch)
        msg = self.post_message(ch["key"], {"user": pr["author"] or user, "text": self.pr_root_text(pr), "kind": "pr",
                                            "meta": {"pr": pr["id"], "status": pr["status"], "priority": pr["priority"],
                                                     "tags": pr["tags"], "bundle": pr["bundle_id"]}})
        store.x("UPDATE prs SET message_id=? WHERE id=?", (msg["id"], pr["id"]))
        return msg

    def ensure_bundle_root(self, b, user):
        store = self.server.store
        if b.get("message_id"):
            try:
                return store.get_message(b["message_id"])
            except ApiError:
                pass
        ch, created = store.prs_channel(b["project"], user)
        if created:
            self.server.bus.publish("channel", ch)
        msg = self.post_message(ch["key"], {"user": b["created_by"] or user, "text": self.bundle_root_text(b),
                                            "kind": "bundle", "meta": {"bundle": b["id"], "status": b["status"],
                                                                       "priority": b["priority"], "tags": b["tags"]}})
        store.x("UPDATE bundles SET message_id=? WHERE id=?", (msg["id"], b["id"]))
        return msg

    def pr_note(self, root, user, text, meta=None):
        """System note inside a PR/bundle thread (status changes etc.)."""
        return self.post_message(None, {"user": user, "text": text, "kind": "system", "meta": meta or {}},
                                 reply_to=root["id"])

    def ep_prs_get(self, q):
        store = self.server.store
        return self.send_json(store.prs(project=q.get("project") or None, status=q.get("status") or "all",
                                        tag=q.get("tag") or None, bundle=q.get("bundle") or None,
                                        author=q.get("author") or None, q=q.get("q") or None,
                                        review=q.get("review") or None))

    def ep_prs_post(self, q, _unused=None):
        store, bus = self.server.store, self.server.bus
        body = self.read_body()
        user = valid_user(body.get("user"))
        store.ensure_user(user)
        project = body.get("project") or DEFAULT_PROJECT
        bundle_id = None
        if body.get("bundle") not in (None, ""):
            b, created = store.resolve_bundle(project, body["bundle"], user)
            bundle_id = b["id"]
            if created:
                self.ensure_bundle_root(b, user)
                bus.publish("bundle", store.bundle(b["id"]))
        pr = store.add_pr(project, user, body.get("title"), url=body.get("url") or "", body=body.get("body") or "",
                          tags=body.get("tags"), priority=body.get("priority"), bundle_id=bundle_id,
                          branch=body.get("branch") or "", base=body.get("base") or "",
                          status=body.get("status") or "open", tldr=body.get("tldr") or "")
        store.bump_areas(user, pr["tags"], 3)
        self.ensure_pr_root(pr, user)
        pr = store.pr(pr["id"])
        bus.publish("pr", pr)
        if bundle_id:
            bus.publish("bundle", store.bundle(bundle_id))
        return self.send_json(pr, 201)

    def ep_pr_get(self, q, pid):
        store = self.server.store
        pr = store.pr(int(pid))
        root, replies = (store.thread(pr["message_id"]) if pr.get("message_id") else (None, []))
        bundle = store.bundle(pr["bundle_id"]) if pr.get("bundle_id") else None
        siblings = store.bundle_prs(pr["bundle_id"]) if pr.get("bundle_id") else []
        agents = [u["name"] for u in store.users(project=pr["project"]) if not u["human"] and u["name"] != pr["author"]]
        reviewed = {r["reviewer"] for r in pr["reviews"] if not r["stale"]}
        return self.send_json({"pr": pr, "bundle": bundle, "bundle_prs": siblings, "root": root, "replies": replies,
                               "not_yet_reviewed": [a for a in agents if a not in reviewed]})

    def ep_pr_post(self, q, pid):
        store, bus = self.server.store, self.server.bus
        body = self.read_body()
        user = valid_user(body.get("user"))
        store.ensure_user(user)
        pid = int(pid)
        before = store.pr(pid)
        fields = {k: body.get(k) for k in ("title", "url", "body", "tldr", "branch", "base", "priority", "tags", "add_tags",
                                           "remove_tags", "status") if k in body}
        if "bundle" in body:
            if body["bundle"] in (None, "", "none", "null"):
                fields["bundle_id"] = None
            else:
                b, created = store.resolve_bundle(before["project"], body["bundle"], user)
                if created:
                    self.ensure_bundle_root(b, user)
                    bus.publish("bundle", store.bundle(b["id"]))
                fields["bundle_id"] = b["id"]
        pr, changes = store.update_pr(pid, user, fields)
        root = self.ensure_pr_root(pr, user)
        notes = []
        if "status" in changes and pr["status"] != before["status"]:
            notes.append("marked **%s**" % pr["status"])
        if "priority" in changes and pr["priority"] != before["priority"]:
            notes.append("priority %d" % pr["priority"])
        if "tags" in changes and pr["tags"] != before["tags"]:
            notes.append("tags: %s" % (", ".join(pr["tags"]) or "none"))
        if "bundle" in changes:
            notes.append(("bundle: %s (#%d)" % (pr["bundle_title"], pr["bundle_id"])) if pr["bundle_id"] else "removed from its bundle")
        if "title" in changes:
            notes.append("title: %s" % pr["title"])
        if "tldr" in changes:
            notes.append("TL;DR: %s" % pr["tldr"])
        if notes:
            self.pr_note(root, user, "; ".join(notes), meta={"pr": pid, "status": pr["status"]})
        bus.publish("pr", pr)
        for bid in {before.get("bundle_id"), pr.get("bundle_id")}:
            if bid:
                b = store.bundle(bid)
                if b["status"] in ("open", "draft") and b["pr_count"] and b["merged_count"] == b["pr_count"]:
                    b, _ = store.update_bundle(bid, user, {"status": "merged"})
                    broot = self.ensure_bundle_root(b, user)
                    self.pr_note(broot, user, "all %d PRs merged: bundle marked **merged**" % b["pr_count"], meta={"bundle": bid})
                bus.publish("bundle", b)
        return self.send_json(pr)

    def ep_pr_comment(self, q, pid):
        store, bus = self.server.store, self.server.bus
        body = self.read_body()
        user = valid_user(body.get("user"))
        pr = store.pr(int(pid))
        root = self.ensure_pr_root(pr, user)
        msg = self.post_message(None, {"user": user, "text": body.get("text"), "kind": body.get("kind") or "message"},
                                reply_to=root["id"])
        bus.publish("pr", store.pr(pr["id"]))
        return self.send_json(msg, 201)

    def ep_pr_review(self, q, pid):
        store, bus = self.server.store, self.server.bus
        body = self.read_body()
        user = valid_user(body.get("user"))
        store.ensure_user(user)
        verdict = (body.get("verdict") or "").strip().lower()
        verdict = {"approve": "approved", "request-changes": "changes", "changes-requested": "changes", "reject": "rejected"}.get(verdict, verdict)
        note = (body.get("note") or "").strip()
        pr = store.review_pr(int(pid), user, verdict, note)
        store.bump_areas(user, pr["tags"], 1)
        root = self.ensure_pr_root(pr, user)
        label = {"approved": "approved", "changes": "requested changes", "rejected": "rejected"}[verdict]
        text = "%s **%s** by %s" % (REVIEW_ICON[verdict], label, user) + (("\n\n" + note) if note else "")
        self.post_message(None, {"user": user, "text": text, "kind": "review",
                                 "meta": {"pr": pr["id"], "verdict": verdict, "reviewer": user}}, reply_to=root["id"])
        pr = store.pr(pr["id"])
        bus.publish("pr", pr)
        if pr.get("bundle_id"):
            bus.publish("bundle", store.bundle(pr["bundle_id"]))
        return self.send_json(pr)

    def ep_pr_rereview(self, q, pid):
        store, bus = self.server.store, self.server.bus
        body = self.read_body()
        user = valid_user(body.get("user"))
        pr, n = store.rereview_pr(int(pid))
        root = self.ensure_pr_root(pr, user)
        note = (body.get("note") or "").strip()
        self.post_message(None, {"user": user, "text": "🔄 **re-review requested** by %s (%d earlier verdict%s now stale)%s"
                                 % (user, n, "" if n == 1 else "s", ("\n\n" + note) if note else ""),
                                 "kind": "review", "meta": {"pr": pr["id"], "verdict": "rereview"}}, reply_to=root["id"])
        pr = store.pr(pr["id"])
        bus.publish("pr", pr)
        if pr.get("bundle_id"):
            bus.publish("bundle", store.bundle(pr["bundle_id"]))
        return self.send_json(pr)

    def ep_pr_reviews(self, q, pid):
        pr = self.server.store.pr(int(pid))
        agents = [u["name"] for u in self.server.store.users(project=pr["project"])
                  if not u["human"] and u["name"] != pr["author"]]
        reviewed = {r["reviewer"] for r in pr["reviews"] if not r["stale"]}
        return self.send_json({"reviews": pr["reviews"], "review_state": pr["review_state"],
                               "not_yet": [a for a in agents if a not in reviewed]})

    def ep_prs_sync(self, q, _unused=None):
        """Refresh statuses from GitHub with the gh CLI (if installed and authenticated on this machine)."""
        store, bus = self.server.store, self.server.bus
        body = self.read_body()
        gh = shutil.which("gh")
        if not gh:
            raise ApiError(400, "gh CLI not installed on the board machine; mark statuses with mb pr merged|close")
        project = body.get("project") or q.get("project") or None
        user = body.get("user") or "msgboard"
        rows = [r for r in store.prs(project=project, status="active") if r["url"]][:40]
        updated, errors = [], []
        for pr in rows:
            try:
                r = subprocess.run([gh, "pr", "view", pr["url"], "--json", "state,isDraft,mergedAt,title,headRefName,baseRefName"],
                                   capture_output=True, text=True, timeout=25)
            except Exception as e:
                errors.append("#%d: %s" % (pr["id"], e)); continue
            if r.returncode != 0:
                errors.append("#%d: %s" % (pr["id"], (r.stderr or r.stdout).strip()[:160])); continue
            try:
                info = json.loads(r.stdout)
            except ValueError:
                errors.append("#%d: bad gh output" % pr["id"]); continue
            state = (info.get("state") or "").upper()
            status = "merged" if state == "MERGED" else "closed" if state == "CLOSED" else ("draft" if info.get("isDraft") else "open")
            fields = {}
            if status != pr["status"]:
                fields["status"] = status
            if not pr["branch"] and info.get("headRefName"):
                fields["branch"] = info["headRefName"]
            if not pr["base"] and info.get("baseRefName"):
                fields["base"] = info["baseRefName"]
            if fields:
                npr, changes = store.update_pr(pr["id"], user, fields)
                if "status" in changes:
                    self.pr_note(self.ensure_pr_root(npr, user), user, "GitHub says **%s** (synced)" % npr["status"],
                                 meta={"pr": pr["id"], "status": npr["status"]})
                bus.publish("pr", npr)
                updated.append({"id": pr["id"], "status": npr["status"]})
        return self.send_json({"checked": len(rows), "updated": updated, "errors": errors})

    def ep_bundles_get(self, q):
        return self.send_json(self.server.store.bundles(project=q.get("project") or None, status=q.get("status") or "all"))

    def ep_bundles_post(self, q, _unused=None):
        store, bus = self.server.store, self.server.bus
        body = self.read_body()
        user = valid_user(body.get("user"))
        store.ensure_user(user)
        project = body.get("project") or DEFAULT_PROJECT
        b = store.add_bundle(project, user, body.get("title"), body=body.get("body") or "", priority=body.get("priority"),
                             tags=body.get("tags"))
        for ref in body.get("prs") or []:
            pr = store.pr(int(str(ref).lstrip("#")))
            if pr["project"] != project:
                raise ApiError(400, "PR #%d is in project %s" % (pr["id"], pr["project"]))
            store.update_pr(pr["id"], user, {"bundle_id": b["id"]})
            bus.publish("pr", store.pr(pr["id"]))
        self.ensure_bundle_root(b, user)
        b = store.bundle(b["id"])
        bus.publish("bundle", b)
        return self.send_json(b, 201)

    def ep_bundle_get(self, q, bid):
        store = self.server.store
        b = store.bundle(int(bid))
        root, replies = (store.thread(b["message_id"]) if b.get("message_id") else (None, []))
        return self.send_json({"bundle": b, "prs": store.bundle_prs(b["id"]), "root": root, "replies": replies})

    def ep_bundle_post(self, q, bid):
        store, bus = self.server.store, self.server.bus
        body = self.read_body()
        user = valid_user(body.get("user"))
        store.ensure_user(user)
        bid = int(bid)
        before = store.bundle(bid)
        fields = {k: body.get(k) for k in ("title", "body", "priority", "tags", "add_tags", "remove_tags", "status", "order") if k in body}
        for ref in body.get("add_prs") or []:
            pr = store.pr(int(str(ref).lstrip("#")))
            store.update_pr(pr["id"], user, {"bundle_id": bid})
            bus.publish("pr", store.pr(pr["id"]))
        for ref in body.get("remove_prs") or []:
            pr = store.pr(int(str(ref).lstrip("#")))
            if pr["bundle_id"] == bid:
                store.update_pr(pr["id"], user, {"bundle_id": None})
                bus.publish("pr", store.pr(pr["id"]))
        b, changes = store.update_bundle(bid, user, fields)
        root = self.ensure_bundle_root(b, user)
        notes = []
        if "status" in changes and b["status"] != before["status"]:
            notes.append("marked **%s**" % b["status"])
        if "priority" in changes and b["priority"] != before["priority"]:
            notes.append("priority %d" % b["priority"])
        if "tags" in changes and b["tags"] != before["tags"]:
            notes.append("tags: %s" % (", ".join(b["tags"]) or "none"))
        if "order" in changes:
            notes.append("merge order: " + " -> ".join("#%d" % i for i in b["merge_order"]))
        if body.get("add_prs"):
            notes.append("added PR%s %s" % ("s" if len(body["add_prs"]) != 1 else "", ", ".join("#%s" % str(r).lstrip("#") for r in body["add_prs"])))
        if body.get("remove_prs"):
            notes.append("removed PR%s %s" % ("s" if len(body["remove_prs"]) != 1 else "", ", ".join("#%s" % str(r).lstrip("#") for r in body["remove_prs"])))
        if notes:
            self.pr_note(root, user, "; ".join(notes), meta={"bundle": bid, "status": b["status"]})
        b = store.bundle(bid)
        bus.publish("bundle", b)
        return self.send_json(b)

    def ep_bundle_comment(self, q, bid):
        store, bus = self.server.store, self.server.bus
        body = self.read_body()
        user = valid_user(body.get("user"))
        b = store.bundle(int(bid))
        root = self.ensure_bundle_root(b, user)
        msg = self.post_message(None, {"user": user, "text": body.get("text"), "kind": "message"}, reply_to=root["id"])
        bus.publish("bundle", store.bundle(b["id"]))
        return self.send_json(msg, 201)

    def ep_tags(self, q):
        return self.send_json(self.server.store.tags(project=q.get("project") or None))

    # -- pastes
    def ep_pastes_get(self, q):
        return self.send_json(self.server.store.pastes(project=q.get("project") or None,
                                                       channel=q.get("channel") or None))

    def ep_pastes_post(self, q):
        store = self.server.store
        body = self.read_body()
        user = valid_user(body.get("user"))
        content = body.get("content")
        if not isinstance(content, str) or not content:
            raise ApiError(400, "content is required")
        channel = body.get("channel") or ""
        project = channel.split("/", 1)[0] if channel else (body.get("project") or "")
        if channel:
            store.get_channel(channel)
        p = store.add_paste(body.get("name") or "paste.txt", content, user, channel=channel, project=project)
        url = "%s/p/%s" % (self.server.public_url, p["id"])
        result = {k: p[k] for k in ("id", "name", "size", "lines", "user", "ts", "channel", "project")}
        result["url"] = url
        if channel:
            text = (body.get("text") or "").strip()
            text = (text + "\n\n" if text else "") + "📎 **%s** (%s, %d lines): %s" % (
                p["name"], human_size(p["size"]), p["lines"], url)
            msg = self.post_message(channel, {"user": user, "text": text, "kind": "paste",
                                              "meta": {"paste": {"id": p["id"], "name": p["name"], "size": p["size"],
                                                                 "lines": p["lines"], "url": url}}})
            store.x("UPDATE pastes SET message_id=? WHERE id=?", (msg["id"], p["id"]))
            result["message_id"] = msg["id"]
        return self.send_json(result, 201)

    def ep_paste_get(self, q, pid):
        return self.send_json(self.server.store.get_paste(pid))

    def paste_page(self, path):
        parts = path.strip("/").split("/")
        pid = parts[1] if len(parts) > 1 else ""
        p = self.server.store.get_paste(pid)
        if len(parts) > 2 and parts[2] == "view":
            page = PASTE_VIEW_HTML.replace("{{id}}", p["id"]).replace("{{name}}", html_escape(p["name"]))
            return self.send_bytes(page.encode("utf-8"), "text/html; charset=utf-8")
        return self.send_bytes(p["content"].encode("utf-8", "replace"), "text/plain; charset=utf-8",
                               extra={"Content-Disposition": "inline; filename=\"%s\"" % p["name"].replace('"', "")})

    # -- SSE
    def sse(self, q):
        """Server-Sent Events for the GUI: message / message_update / channel / project / user / task / reset."""
        bus = self.server.bus
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        seen = bus.seq
        last = self.headers.get("Last-Event-ID") or q.get("last")
        if last:
            try:
                seen = int(last)
            except ValueError:
                pass
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while not self.server.stopping:
                events, newest, lost = bus.since(seen)
                if lost:
                    self.wfile.write(b"event: reset\ndata: {}\n\n")
                for seq, type_, payload in events:
                    data = json.dumps(payload, ensure_ascii=False)
                    self.wfile.write(("id: %d\nevent: %s\ndata: %s\n\n" % (seq, type_, data)).encode("utf-8"))
                seen = newest
                if not events:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
                bus.wait_for_change(seen, SSE_PING)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


def html_escape(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def human_size(n):
    if n < 1024:
        return "%d B" % n
    if n < 1024 * 1024:
        return "%.1f KB" % (n / 1024.0)
    return "%.1f MB" % (n / (1024.0 * 1024.0))


PASTE_VIEW_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>{{name}}</title>
<script src="/vendor/marked.min.js"></script><script src="/vendor/purify.min.js"></script>
<style>body{margin:0;background:#313338;color:#dbdee1;font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
header{padding:10px 20px;background:#2b2d31;border-bottom:1px solid #1f2023;display:flex;gap:16px;align-items:center}
header a{color:#00a8fc;text-decoration:none}main{max-width:980px;margin:0 auto;padding:20px}
pre{background:#2b2d31;border:1px solid #1f2023;border-radius:6px;padding:12px;overflow:auto;font:13px/1.45 ui-monospace,Menlo,monospace;white-space:pre-wrap}
code{font:13px ui-monospace,Menlo,monospace;background:#2b2d31;padding:1px 4px;border-radius:3px}pre code{background:none;padding:0}
table{border-collapse:collapse}th,td{border:1px solid #383a40;padding:4px 8px}blockquote{border-left:4px solid #383a40;margin:4px 0;padding:2px 12px;color:#949ba4}
a{color:#00a8fc}</style></head><body><header><b>{{name}}</b><a href="/p/{{id}}">raw</a><a href="/">board</a></header>
<main id="m">loading…</main><script>
fetch('/api/pastes/{{id}}').then(r=>r.json()).then(p=>{const m=document.getElementById('m');
if(/\\.(md|markdown)$/i.test(p.name)&&window.marked){m.innerHTML=DOMPurify.sanitize(marked.parse(p.content,{gfm:true,breaks:false}));}
else{const pre=document.createElement('pre');pre.textContent=p.content;m.innerHTML='';m.appendChild(pre);}});
</script></body></html>"""

ROUTES = [
    ("GET", re.compile(r"^/api/health$"), "ep_health"),
    ("GET", re.compile(r"^/api/links$"), "ep_links"),
    ("GET", re.compile(r"^/api/users$"), "ep_users_get"),
    ("POST", re.compile(r"^/api/users$"), "ep_users_post"),
    ("GET", re.compile(r"^/api/projects$"), "ep_projects_get"),
    ("POST", re.compile(r"^/api/projects$"), "ep_projects_post"),
    ("GET", re.compile(r"^/api/projects/([^/]+)$"), "ep_project_overview"),
    ("DELETE", re.compile(r"^/api/projects/([^/]+)$"), "ep_project_delete"),
    ("POST", re.compile(r"^/api/projects/([^/]+)/delete$"), "ep_project_delete"),
    ("DELETE", re.compile(r"^/api/channels/(.+)$"), "ep_channel_delete"),
    ("POST", re.compile(r"^/api/channels/(.+)/delete$"), "ep_channel_delete"),
    ("POST", re.compile(r"^/api/backup$"), "ep_backup"),
    ("GET", re.compile(r"^/api/channels$"), "ep_channels_get"),
    ("POST", re.compile(r"^/api/channels$"), "ep_channels_post"),
    ("GET", re.compile(r"^/api/resolve$"), "ep_resolve"),
    ("GET", re.compile(r"^/api/dm$"), "ep_dm"),
    ("GET", re.compile(r"^/api/channels/(.+)/messages$"), "ep_messages_get"),
    ("POST", re.compile(r"^/api/channels/(.+)/messages$"), "ep_messages_post"),
    ("POST", re.compile(r"^/api/channels/(.+)/read$"), "ep_read"),
    ("GET", re.compile(r"^/api/messages/(\d+)$"), "ep_message_get"),
    ("GET", re.compile(r"^/api/messages/(\d+)/thread$"), "ep_thread"),
    ("POST", re.compile(r"^/api/messages/(\d+)/reply$"), "ep_reply"),
    ("POST", re.compile(r"^/api/messages/(\d+)/(pin|unpin)$"), "ep_pin"),
    ("POST", re.compile(r"^/api/messages/(\d+)/resolve$"), "ep_resolve_ask"),
    ("GET", re.compile(r"^/api/pins$"), "ep_pins"),
    ("GET", re.compile(r"^/api/asks$"), "ep_asks"),
    ("GET", re.compile(r"^/api/search$"), "ep_search"),
    ("GET", re.compile(r"^/api/unread$"), "ep_unread"),
    ("GET", re.compile(r"^/api/inbox$"), "ep_inbox"),
    ("GET", re.compile(r"^/api/wait$"), "ep_wait"),
    ("GET", re.compile(r"^/api/brief$"), "ep_brief"),
    ("GET", re.compile(r"^/api/who$"), "ep_who"),
    ("GET", re.compile(r"^/api/users/([^/]+)/profile$"), "ep_profile"),
    ("GET", re.compile(r"^/api/users/([^/]+)/areas$"), "ep_areas"),
    ("POST", re.compile(r"^/api/users/([^/]+)/areas$"), "ep_areas"),
    ("GET", re.compile(r"^/api/groups$"), "ep_groups"),
    ("POST", re.compile(r"^/api/groups$"), "ep_groups"),
    ("POST", re.compile(r"^/api/messages/(\d+)/watch$"), "ep_watch"),
    ("POST", re.compile(r"^/api/channels/(.+)/state$"), "ep_state"),
    ("POST", re.compile(r"^/api/presence$"), "ep_presence"),
    ("GET", re.compile(r"^/api/feed$"), "ep_feed"),
    ("GET", re.compile(r"^/api/feed/wait$"), "ep_feed_wait"),
    ("POST", re.compile(r"^/api/feed/ack$"), "ep_feed_ack"),
    ("GET", re.compile(r"^/api/subscriptions$"), "ep_subscriptions"),
    ("POST", re.compile(r"^/api/subscriptions$"), "ep_subscriptions"),
    ("GET", re.compile(r"^/api/tasks$"), "ep_tasks_get"),
    ("POST", re.compile(r"^/api/tasks$"), "ep_tasks_post"),
    ("GET", re.compile(r"^/api/tasks/(\d+)$"), "ep_task_get"),
    ("POST", re.compile(r"^/api/tasks/(\d+)$"), "ep_task_post"),
    ("GET", re.compile(r"^/api/prs$"), "ep_prs_get"),
    ("POST", re.compile(r"^/api/prs$"), "ep_prs_post"),
    ("POST", re.compile(r"^/api/prs/sync$"), "ep_prs_sync"),
    ("GET", re.compile(r"^/api/prs/(\d+)$"), "ep_pr_get"),
    ("POST", re.compile(r"^/api/prs/(\d+)$"), "ep_pr_post"),
    ("POST", re.compile(r"^/api/prs/(\d+)/comment$"), "ep_pr_comment"),
    ("POST", re.compile(r"^/api/prs/(\d+)/review$"), "ep_pr_review"),
    ("POST", re.compile(r"^/api/prs/(\d+)/rereview$"), "ep_pr_rereview"),
    ("GET", re.compile(r"^/api/prs/(\d+)/reviews$"), "ep_pr_reviews"),
    ("GET", re.compile(r"^/api/bundles$"), "ep_bundles_get"),
    ("POST", re.compile(r"^/api/bundles$"), "ep_bundles_post"),
    ("GET", re.compile(r"^/api/bundles/(\d+)$"), "ep_bundle_get"),
    ("POST", re.compile(r"^/api/bundles/(\d+)$"), "ep_bundle_post"),
    ("POST", re.compile(r"^/api/bundles/(\d+)/comment$"), "ep_bundle_comment"),
    ("GET", re.compile(r"^/api/tags$"), "ep_tags"),
    ("GET", re.compile(r"^/api/pastes$"), "ep_pastes_get"),
    ("POST", re.compile(r"^/api/pastes$"), "ep_pastes_post"),
    ("GET", re.compile(r"^/api/pastes/([A-Za-z0-9]+)$"), "ep_paste_get"),
]


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, store, bus, token=""):
        super().__init__(addr, Handler)
        self.store = store
        self.store_path = store.path
        self.bus = bus
        self.notifier = Notifier(store)
        self.token = token or ""
        self.started = now()
        self.stopping = False
        host = addr[0] if addr[0] not in ("0.0.0.0", "") else "127.0.0.1"
        self.url = "http://%s:%d" % (host, addr[1])
        self.public_url = (os.environ.get("MB_PUBLIC_URL") or self.url).rstrip("/")
        try:
            self.default_user = re.sub(r"[^A-Za-z0-9_.-]", "", getpass.getuser())[:32]
        except Exception:
            self.default_user = ""


def main():
    ap = argparse.ArgumentParser(description="msgboard server")
    ap.add_argument("--host", default=os.environ.get("MB_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("MB_PORT", "8765")))
    ap.add_argument("--data-dir", default=os.environ.get("MB_DATA_DIR") or os.path.join(HERE, "data"))
    ap.add_argument("--token", default=os.environ.get("MB_TOKEN", ""), help="require this token on API calls")
    args = ap.parse_args()

    os.makedirs(args.data_dir, exist_ok=True)
    store = Store(os.path.join(args.data_dir, "msgboard.db"))
    bus = Bus()
    try:
        server = Server((args.host, args.port), store, bus, token=args.token)
    except OSError as e:
        sys.stderr.write("cannot bind %s:%d: %s\n" % (args.host, args.port, e))
        return 1
    pidfile = os.path.join(args.data_dir, "server.pid")
    with open(pidfile, "w") as f:
        f.write(str(os.getpid()))

    def stop(signum, _frame):
        sys.stderr.write("signal %d: shutting down\n" % signum)
        server.stopping = True
        bus.publish("shutdown", {})
        threading.Thread(target=server.shutdown, daemon=True).start()

    def backups():
        while not server.stopping:
            try:
                path = store.backup()
                sys.stderr.write("%s backup written: %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), path))
            except Exception as e:
                sys.stderr.write("backup failed: %r\n" % (e,))
            for _ in range(24 * 60):  # daily
                if server.stopping:
                    return
                time.sleep(60)

    threading.Thread(target=backups, daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    sys.stderr.write("%s msgboard v2 listening on %s:%d (data: %s%s)\n"
                     % (time.strftime("%Y-%m-%d %H:%M:%S"), args.host, args.port, args.data_dir,
                        ", token auth on" if args.token else ""))
    sys.stderr.flush()
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        try:
            os.remove(pidfile)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
