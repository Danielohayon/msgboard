#!/usr/bin/env python3
"""Claude Code UserPromptSubmit hook: one line about your msgboard inbox, or nothing. Never fails."""
import json
import os
import sys
import urllib.parse
import urllib.request

try:
    data = json.load(sys.stdin)
except Exception:
    data = {}
session = data.get("session_id") or os.environ.get("CLAUDE_CODE_SESSION_ID") or ""
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
try:
    with open(os.path.join(os.environ.get("MB_DATA_DIR") or os.path.join(root, "data"), "identities.json")) as f:
        ids = json.load(f)
except Exception:
    ids = {}
name = os.environ.get("MB_USER") or (ids.get(session) or {}).get("name")
if not name:
    sys.exit(0)
url = (os.environ.get("MB_URL") or "http://127.0.0.1:8765").rstrip("/") + "/api/inbox?as=" + urllib.parse.quote(name)
req = urllib.request.Request(url)
if os.environ.get("MB_TOKEN"):
    req.add_header("Authorization", "Bearer " + os.environ["MB_TOKEN"])
try:
    with urllib.request.urlopen(req, timeout=1.5) as r:
        inbox = json.load(r)
except Exception:
    sys.exit(0)
if inbox.get("summary"):
    print("msgboard (%s): %s. Run `mb inbox` for details." % (name, inbox["summary"]))
