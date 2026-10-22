#!/usr/bin/env python3
"""
msgboard link supervisor: keeps reverse SSH tunnels alive so agents on remote machines reach the board.

Each link forwards a Unix socket on the remote machine (0600, owner only) to the board on this host:
    ssh -N -R <remote socket>:127.0.0.1:8765 <host>
The supervisor restarts the tunnel on exit, verifies it end to end from the remote machine every N seconds,
recognises ssh authentication failures, and reports state changes on the board (#general) and with
macOS notifications. Config: data/links.json (written by `mb link add`). State: data/links-state.json.

Run by launchd (`mb service install`) or by hand: python3 link.py
"""
import collections
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("MB_DATA_DIR") or os.path.join(HERE, "data")
LINKS = os.path.join(DATA_DIR, "links.json")
STATE = os.path.join(DATA_DIR, "links-state.json")
LOG = os.path.join(DATA_DIR, "links.log")
BOARD = (os.environ.get("MB_URL") or "http://127.0.0.1:8765").rstrip("/")
LOCAL = os.environ.get("MB_LINK_LOCAL") or "127.0.0.1:8765"
AUTH_RE = re.compile(r"sso|token has expired|expiredtoken|security token|unauthorized|credential|not authorized|"
                     r"accessdenied|access denied|login", re.I)
DOWN_ANNOUNCE_SECS = 120
AUTH_RENOTIFY_SECS = 1800
CHECK_SCRIPT = r'''
import socket, sys
p = sys.argv[1]
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); s.settimeout(15); s.connect(p)
s.sendall(b"GET /api/health HTTP/1.0\r\nHost: localhost\r\n\r\n"); d = b""
while True:
    c = s.recv(65536)
    if not c: break
    d += c
print("OK" if d.startswith(b"HTTP/1.0 200") or d.startswith(b"HTTP/1.1 200") else "BAD " + d[:80].decode("utf-8", "replace"))
'''

stop_event = threading.Event()
state_lock = threading.Lock()
state = {}
try:
    with open(STATE) as _f:
        previous_state = json.load(_f)
except (OSError, ValueError):
    previous_state = {}


def log(msg):
    line = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass
    sys.stderr.write(line + "\n")
    sys.stderr.flush()


def save_state():
    with state_lock:
        tmp = STATE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, indent=1)
        os.replace(tmp, STATE)


def load_links():
    try:
        with open(LINKS) as f:
            links = json.load(f)
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in links.items() if v.get("enabled", True)}


def notify(title, text):
    if sys.platform != "darwin":
        return
    esc = lambda s: s.replace("\\", "").replace('"', "'")
    try:
        subprocess.run(["osascript", "-e", 'display notification "%s" with title "%s"' % (esc(text[:200]), esc(title))],
                       timeout=10, capture_output=True)
    except Exception:
        pass


def post_board(text, host, link_state):
    body = json.dumps({"user": "msgboard", "text": text, "kind": "system",
                       "meta": {"link": host, "state": link_state}}).encode()
    req = urllib.request.Request(BOARD + "/api/channels/main%2Fgeneral/messages", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=5).read()
    except Exception as e:
        log("could not post to the board: %r" % (e,))


def fmt_dur(secs):
    secs = int(secs)
    if secs < 60:
        return "%ds" % secs
    if secs < 3600:
        return "%dm" % (secs // 60)
    return "%dh%02dm" % (secs // 3600, (secs % 3600) // 60)


class Link(threading.Thread):
    def __init__(self, name, cfg):
        super().__init__(daemon=True, name="link-" + name)
        self.name_ = name
        self.host = cfg.get("host") or name
        self.socket = cfg["socket"]
        self.local = cfg.get("local") or LOCAL
        self.interval = int(cfg.get("check_interval") or 30)
        self.ctl = "/tmp/mb-link-%s.ctl" % re.sub(r"[^A-Za-z0-9_.-]", "_", name)[:40]
        self.proc = None
        self.stderr = collections.deque(maxlen=40)
        self.s = {"host": self.host, "socket": self.socket, "state": "starting", "since": time.time(),
                  "last_ok": None, "restarts": -1, "last_error": "", "pid": None, "connected_at": None}
        self.down_since = None
        self.down_announced = False
        self.auth_notified_at = 0
        # a supervisor restart (mb link add, reboot) is not news: inherit "was up" from the previous state file
        prev = (previous_state.get(name) or {})
        self.ever_up = (time.time() - (prev.get("last_ok") or 0)) < 900
        with state_lock:
            state[name] = self.s

    # -- state
    def set(self, st, error=None):
        if st != self.s["state"]:
            log("[%s] %s -> %s%s" % (self.name_, self.s["state"], st, (" (%s)" % error) if error else ""))
            self.s["state"] = st
            self.s["since"] = time.time()
        if error is not None:
            self.s["last_error"] = error[:300]
        save_state()

    def ssh_base(self):
        # ControlPersist=no: a host config with ControlPersist would daemonize the master and return 0 at once
        return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=30", "-o", "ServerAliveInterval=15",
                "-o", "ServerAliveCountMax=3", "-o", "TCPKeepAlive=yes", "-o", "LogLevel=ERROR",
                "-o", "ControlPersist=no"]

    def start_tunnel(self):
        """Start the ssh master (no forward yet; the forward is added once the stale socket is cleaned)."""
        try:
            os.unlink(self.ctl)
        except OSError:
            pass
        cmd = self.ssh_base() + ["-N", "-M", "-S", self.ctl, self.host]
        self.stderr.clear()
        self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                     start_new_session=True, text=True)
        self.s["pid"] = self.proc.pid
        threading.Thread(target=self.read_stderr, args=(self.proc,), daemon=True).start()

    def mux(self, extra, timeout=30, stdin=None):
        """Run an ssh command through the master's control socket (no new ssh session)."""
        return subprocess.run(self.ssh_base() + ["-S", self.ctl] + extra, input=stdin, capture_output=True, text=True,
                              timeout=timeout)

    def establish_forward(self, deadline=90):
        """Wait for the master, remove a stale socket on the remote machine, request the reverse forward, verify end to end."""
        t0 = time.time()
        while time.time() - t0 < deadline and not stop_event.is_set():
            if self.proc.poll() is not None:
                return False, "ssh exited (%s): %s" % (self.proc.returncode, " | ".join(list(self.stderr)[-3:]))
            try:
                if self.mux(["-O", "check", self.host], timeout=20).returncode == 0:
                    break
            except subprocess.TimeoutExpired:
                pass
            time.sleep(2)
        else:
            return False, "ssh master not ready in %ds: %s" % (deadline, " | ".join(list(self.stderr)[-3:]))
        try:
            self.mux([self.host, "rm -f '%s'" % self.socket.replace("'", "")], timeout=30)
            r = self.mux(["-O", "forward", "-R", "%s:%s" % (self.socket, self.local), self.host], timeout=30)
        except subprocess.TimeoutExpired:
            return False, "timed out setting up the forward"
        if r.returncode != 0:
            return False, "reverse forward refused: %s" % (r.stderr or r.stdout).strip()[-200:]
        last = ""
        for _ in range(8):
            ok, last = self.check(timeout=40)
            if ok:
                return True, ""
            time.sleep(3)
        return False, "forward set up but the remote machine cannot reach the board: %s" % last

    def read_stderr(self, proc):
        try:
            for line in proc.stderr:
                line = line.strip()
                if line:
                    self.stderr.append(line)
        except Exception:
            pass

    def kill_tunnel(self):
        p = self.proc
        if not p:
            return
        try:
            self.mux(["-O", "exit", self.host], timeout=10)
        except Exception:
            pass
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGTERM)
        except OSError:
            pass
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGKILL)
            except OSError:
                pass
        self.proc = None
        self.s["pid"] = None

    def check(self, timeout=45):
        """End-to-end: run a tiny client on the remote machine, through the control socket, that hits the board via the Unix socket."""
        if not os.path.exists(self.ctl):
            return False, "control socket missing"
        try:
            r = subprocess.run(self.ssh_base() + ["-S", self.ctl, self.host, "python3", "-", self.socket],
                               input=CHECK_SCRIPT, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return False, "health check timed out"
        except OSError as e:
            return False, "health check failed: %s" % e
        out = (r.stdout or "").strip()
        if r.returncode == 0 and out == "OK":
            return True, ""
        return False, ("health check: %s %s" % (out, (r.stderr or "").strip()[-200:])).strip()

    def classify(self, err):
        text = err + " " + " ".join(self.stderr)
        return "auth" if AUTH_RE.search(text) else "down"

    def on_failure(self, err):
        kind = self.classify(err)
        now = time.time()
        if self.down_since is None:
            self.down_since = now
        self.set(kind, err)
        if kind == "auth":
            if now - self.auth_notified_at > AUTH_RENOTIFY_SECS:
                self.auth_notified_at = now
                msg = ("🔗 link to %s cannot connect: ssh authentication failed. Log in to the host again from the board "
                       "host; the link reconnects by itself afterwards. (%s)" % (self.host, err[:160]))
                post_board(msg, self.host, "auth")
                notify("msgboard link: %s" % self.host, "ssh authentication failed. Log in again; the link reconnects by itself.")
                self.down_announced = True
        elif self.ever_up and not self.down_announced and now - self.down_since >= DOWN_ANNOUNCE_SECS:
            self.down_announced = True
            post_board("🔗 link to %s has been down for %s: %s" % (self.host, fmt_dur(now - self.down_since), err[:200]),
                       self.host, "down")
            notify("msgboard link: %s down" % self.host, err[:200])

    def on_up(self):
        now = time.time()
        self.s["connected_at"] = now
        self.s["last_ok"] = now
        self.s["restarts"] += 1
        self.set("up")
        if not self.ever_up:
            post_board("🔗 link to %s is up: agents on that machine can reach the board." % self.host, self.host, "up")
        elif self.down_announced:
            post_board("🔗 link to %s is back up (was down %s)." % (self.host, fmt_dur(now - (self.down_since or now))),
                       self.host, "up")
        self.ever_up = True
        self.down_since = None
        self.down_announced = False

    def run(self):
        backoff = 5
        while not stop_event.is_set():
            self.set("connecting")
            try:
                self.start_tunnel()
            except OSError as e:
                self.on_failure("cannot start ssh: %s" % e)
                stop_event.wait(backoff)
                backoff = min(backoff * 2, 60)
                continue
            ok, err = self.establish_forward()
            if not ok:
                self.kill_tunnel()
                self.on_failure(err)
                stop_event.wait(backoff if self.s["state"] != "auth" else 60)
                backoff = min(backoff * 2, 60)
                continue
            self.on_up()
            backoff = 5
            fails = 0
            while not stop_event.is_set():
                # sleep in 2s slices so an ssh exit is noticed quickly; full health check every interval
                for _ in range(max(1, self.interval // 2)):
                    if stop_event.wait(2) or self.proc.poll() is not None:
                        break
                if stop_event.is_set():
                    break
                if self.proc.poll() is not None:
                    err = "ssh exited (%s): %s" % (self.proc.returncode, " | ".join(list(self.stderr)[-3:]))
                    break
                ok, cerr = self.check()
                if ok:
                    fails = 0
                    self.s["last_ok"] = time.time()
                    save_state()
                else:
                    fails += 1
                    log("[%s] check failed (%d/2): %s" % (self.name_, fails, cerr))
                    if fails >= 2:
                        err = "tunnel unhealthy: " + cerr
                        break
            else:
                err = "stopping"
            self.kill_tunnel()
            if stop_event.is_set():
                break
            self.on_failure(err)
            stop_event.wait(backoff)
            backoff = min(backoff * 2, 60)
        self.kill_tunnel()
        self.set("stopped")


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    links = load_links()
    log("link supervisor starting (%d link%s)" % (len(links), "" if len(links) == 1 else "s"))
    threads = {}

    def stop(signum, _frame):
        log("signal %d: stopping" % signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    for name, cfg in links.items():
        t = Link(name, cfg)
        threads[name] = t
        t.start()
    save_state()
    if not links:
        log("no links configured (mb link add <host>); idling")
    mtime = os.path.getmtime(LINKS) if os.path.exists(LINKS) else 0
    while not stop_event.is_set():
        stop_event.wait(10)
        new_mtime = os.path.getmtime(LINKS) if os.path.exists(LINKS) else 0
        if new_mtime != mtime:
            log("links.json changed; restarting supervisor to apply")
            stop_event.set()
    for t in threads.values():
        t.join(timeout=15)
    save_state()
    log("link supervisor stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
