"""Discord Rich Presence for the whole Claude Code app.

Every session runs a copy of this script; a file lock lets one copy own the
Discord connection while the others wait to take over. The owner reads the
small heartbeat files the sessions write, shows the most recently active chat,
and totals today's tokens from the local transcripts. Standard library only.
"""

import glob
import json
import os
import socket
import struct
import sys
import time
import uuid
from datetime import datetime

OP_HANDSHAKE, OP_FRAME, OP_CLOSE = 0, 1, 2
POLL_SECONDS = 2
MIN_SEND_GAP = 4  # Discord allows 5 activity updates per 20 seconds.
RETRY_SECONDS = 15
STALE_SECONDS = 150
TOKENS_EVERY = 30

STATES = {
    "thinking": "Thinking",
    "tools": "Running tools",
    "waiting": "Waiting for you",
    "idle": "Idle",
}


def log(msg):
    print(msg, flush=True)


# --- single owner ----------------------------------------------------------

def try_lock(path):
    f = open(path, "a+b")
    try:
        if sys.platform == "win32":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f
    except OSError:
        f.close()
        return None


# --- Discord IPC -----------------------------------------------------------

class Pipe:
    def __init__(self):
        self.f = None
        self.sock = None

    def open(self):
        if sys.platform == "win32":
            for i in range(10):
                try:
                    self.f = open(rf"\\.\pipe\discord-ipc-{i}", "r+b", buffering=0)
                    return
                except OSError:
                    continue
        else:
            bases = [os.environ.get(k) for k in ("XDG_RUNTIME_DIR", "TMPDIR", "TMP", "TEMP")] + ["/tmp"]
            for base in filter(None, bases):
                for sub in ("", "app/com.discordapp.Discord", "snap.discord"):
                    for i in range(10):
                        path = os.path.join(base, sub, f"discord-ipc-{i}")
                        if not os.path.exists(path):
                            continue
                        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                        try:
                            s.connect(path)
                            self.sock = s
                            return
                        except OSError:
                            s.close()
        raise OSError("Discord is not running")

    def write(self, data):
        if self.f:
            self.f.write(data)
            self.f.flush()
        else:
            self.sock.sendall(data)

    def read(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.f.read(n - len(buf)) if self.f else self.sock.recv(n - len(buf))
            if not chunk:
                raise OSError("pipe closed")
            buf += chunk
        return buf

    def close(self):
        for h in (self.f, self.sock):
            try:
                if h:
                    h.close()
            except OSError:
                pass
        self.f = self.sock = None


def send(pipe, op, payload):
    body = json.dumps(payload).encode()
    pipe.write(struct.pack("<II", op, len(body)) + body)
    reply_op, length = struct.unpack("<II", pipe.read(8))
    reply = json.loads(pipe.read(length))
    if reply_op == OP_CLOSE:
        raise RuntimeError(reply.get("message", "Discord closed the connection"))
    if reply.get("evt") == "ERROR":
        raise RuntimeError(reply.get("data", {}).get("message", "Discord returned an error"))
    return reply


def set_activity(pipe, activity):
    send(pipe, OP_FRAME, {
        "cmd": "SET_ACTIVITY",
        "args": {"pid": os.getpid(), "activity": activity},
        "nonce": str(uuid.uuid4()),
    })


# --- sessions --------------------------------------------------------------

def live_sessions(run_dir):
    now = time.time() * 1000
    sessions = []
    for path in glob.glob(os.path.join(run_dir, "*.json")):
        try:
            with open(path, encoding="utf-8") as f:
                s = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(s, dict):
            continue
        if s.get("closed") or now - float(s.get("heartbeat", 0)) > STALE_SECONDS * 1000:
            try:
                os.remove(path)
            except OSError:
                pass
            continue
        sessions.append(s)
    return sessions


class Titles:
    def __init__(self, projects_dir):
        self.projects_dir = projects_dir
        self.cache = {}

    def get(self, session_id):
        if not isinstance(session_id, str) or not session_id.replace("-", "").isalnum():
            return None
        paths = glob.glob(os.path.join(self.projects_dir, "*", f"{session_id}.jsonl"))
        if not paths:
            return None
        path = paths[0]
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return None
        hit = self.cache.get(path)
        if hit and hit[0] == mtime:
            return hit[1]
        custom = ai = None
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if '"custom-title"' in line or '"ai-title"' in line:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(row, dict):
                        continue
                    if row.get("type") == "custom-title" and isinstance(row.get("customTitle"), str):
                        custom = row["customTitle"]
                    elif row.get("type") == "ai-title" and isinstance(row.get("aiTitle"), str):
                        ai = row["aiTitle"]
        title = custom or ai
        self.cache[path] = (mtime, title)
        return title


# --- tokens today ----------------------------------------------------------

class TodayTokens:
    """Totals every response since local midnight, reading each transcript once."""

    FIELDS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")

    def __init__(self, projects_dir):
        self.projects_dir = projects_dir
        self.day = None
        self.offsets = {}
        self.seen = set()
        self.total = 0

    def update(self):
        today = datetime.now().date()
        if today != self.day:
            self.day, self.offsets, self.seen, self.total = today, {}, set(), 0
        midnight = datetime.combine(today, datetime.min.time()).timestamp()
        for path in glob.glob(os.path.join(self.projects_dir, "**", "*.jsonl"), recursive=True):
            try:
                if os.path.getmtime(path) < midnight:
                    continue
                size = os.path.getsize(path)
            except OSError:
                continue
            start = self.offsets.get(path, 0)
            if size < start:
                start = 0
            if size == start:
                continue
            with open(path, "rb") as f:
                f.seek(start)
                data = f.read()
            end = data.rfind(b"\n") + 1
            self.offsets[path] = start + end
            for raw in data[:end].splitlines():
                if b'"usage"' not in raw or b'"assistant"' not in raw:
                    continue
                self.add(raw, midnight)
        return self.total

    def add(self, raw, midnight):
        try:
            row = json.loads(raw)
        except ValueError:
            return
        if not isinstance(row, dict):
            return
        m = row.get("message")
        if not isinstance(m, dict) or not isinstance(m.get("usage"), dict):
            return
        stamp = row.get("timestamp")
        try:
            at = datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
        except ValueError:
            return
        if at < midnight:
            return
        key = m.get("id") or row.get("requestId")
        if key:
            if key in self.seen:
                return
            self.seen.add(key)
        u = m["usage"]
        self.total += sum(u.get(k) for k in self.FIELDS if isinstance(u.get(k), (int, float)))


def compact(n):
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.2f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1000:
        return f"{n / 1000:.1f}K"
    return str(int(n))


# --- main ------------------------------------------------------------------

def build_activity(session, title, tokens, image_base):
    name = session.get("state") if session.get("state") in STATES else "idle"
    details = (title or "Untitled chat")[:128]
    if len(details) < 2:
        details = f"{details}  "
    activity = {
        "details": details,
        "state": f"{compact(tokens)} tokens today · {STATES[name]}"[:128],
        "assets": {
            "large_image": f"{image_base}/large.png",
            "large_text": "Claude Code",
            "small_image": f"{image_base}/{name}.png",
            "small_text": STATES[name],
        },
    }
    started = session.get("startedAt")
    if isinstance(started, (int, float)) and started > 0:
        activity["timestamps"] = {"start": int(started // 1000)}
    return activity


def own(client_id, run_dir, projects_dir, image_base):
    pipe = Pipe()
    titles = Titles(projects_dir)
    tokens = TodayTokens(projects_dir)
    connected = False
    sent = "unset"
    last_send = 0.0
    next_try = 0.0
    next_tokens = 0.0
    total = 0

    while True:
        if time.monotonic() >= next_tokens:
            try:
                total = tokens.update()
            except OSError as err:
                log(f"token scan: {err}")
            next_tokens = time.monotonic() + TOKENS_EVERY

        sessions = live_sessions(run_dir)
        if not sessions:
            if connected:
                try:
                    set_activity(pipe, None)
                except (OSError, RuntimeError, ValueError, struct.error):
                    pass
            pipe.close()
            log("no live sessions")
            return

        if not connected and time.monotonic() >= next_try:
            try:
                pipe.open()
                send(pipe, OP_HANDSHAKE, {"v": 1, "client_id": client_id})
                connected, sent = True, "unset"
                log("connected")
            except (OSError, RuntimeError, ValueError, struct.error) as err:
                pipe.close()
                next_try = time.monotonic() + RETRY_SECONDS
                log(f"waiting: {err}")

        if connected and sessions:
            viewed = [s for s in sessions if s.get("isViewed")]
            active = max(viewed or sessions, key=lambda s: float(s.get("lastActive", 0)))
            activity = build_activity(active, titles.get(active.get("sessionId")), total, image_base)
            if activity != sent and time.monotonic() - last_send >= MIN_SEND_GAP:
                try:
                    set_activity(pipe, activity)
                    sent, last_send = activity, time.monotonic()
                except (OSError, RuntimeError, ValueError, struct.error) as err:
                    pipe.close()
                    connected = False
                    next_try = time.monotonic() + RETRY_SECONDS
                    log(f"lost: {err}")

        time.sleep(POLL_SECONDS)


def main():
    client_id, run_dir, projects_dir, image_base = sys.argv[1:5]
    image_base = image_base.rstrip("/")
    os.makedirs(run_dir, exist_ok=True)
    while True:
        lock = try_lock(os.path.join(run_dir, "helper.lock"))
        if lock:
            log("owner")
            try:
                own(client_id, run_dir, projects_dir, image_base)
            finally:
                lock.close()
            return
        if not live_sessions(run_dir):
            return
        time.sleep(5)


if __name__ == "__main__":
    main()
