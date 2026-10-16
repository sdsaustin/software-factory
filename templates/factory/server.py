#!/usr/bin/env python3
"""Local dashboard server for the software factory. Standard library only.

Serves the files in this directory (dashboard.html, board.json, ...) and accepts
one write: POST /api/action, which appends a human decision to actions.jsonl.
It never touches board.json; the /factory session is the only writer of state
and picks queued actions up with `/factory apply`.

Run:  python3 factory/server.py [--port 8000]
Binds to 127.0.0.1 only. Open the printed URL (its #t=... part is a per-run token).
"""
import argparse
import hmac
import json
import os
import re
import secrets
import sys
import threading
import uuid
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
ACTIONS = os.path.join(ROOT, "actions.jsonl")
BOARD = os.path.join(ROOT, "board.json")
MAX_BODY = 16 * 1024
JOB_RE = re.compile(r"^\d{3,}-[a-z0-9][a-z0-9-]{0,60}$")
TYPES = {"approve", "rework", "resume"}
CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
LOCK = threading.Lock()


def load_board():
    try:
        with open(BOARD, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def validate(d, board):
    """Return an error string, or None if the action is acceptable."""
    if not isinstance(d, dict):
        return "body must be an object"
    t = d.get("type")
    if t not in TYPES:
        return "unknown action type"
    job = d.get("job")
    if t == "resume":
        if job is not None:
            return "resume takes no job"
        if (board.get("run") or {}).get("status") != "stopped":
            return "there is no stopped run to resume"
    else:
        if not isinstance(job, str) or not JOB_RE.match(job):
            return "bad job id"
        j = next((x for x in board.get("jobs", []) if x.get("id") == job), None)
        if j is None or j.get("stage") != "needs-human":
            return "job is not waiting for a human"
        if t == "approve" and not os.path.isfile(os.path.join(ROOT, "jobs", job, "build.md")):
            return "nothing to merge yet: this job was never built; answer it with rework"
    answers = d.get("answers", {})
    if not isinstance(answers, dict) or len(answers) > 10:
        return "bad answers"
    for k, v in answers.items():
        if not (isinstance(k, str) and re.fullmatch(r"\d{1,2}", k)
                and isinstance(v, str) and re.fullmatch(r"[a-z0-9]{1,8}", v)):
            return "bad answers"
    note = d.get("note", "")
    if not isinstance(note, str) or len(note) > 4000:
        return "bad note"
    if t == "rework" and not (note.strip() or answers):
        return "rework needs a note or an answer"
    return None


def make_handler(port, token):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=ROOT, **kw)

        def end_headers(self):
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            super().end_headers()

        def log_message(self, fmt, *args):
            if self.command == "POST":
                sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

        def _host_ok(self):
            return self.headers.get("Host", "") in allowed_hosts

        def _json(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self._host_ok():
                return self._json(403, {"error": "bad host"})
            if self.path.split("?")[0] == "/api/ping":
                return self._json(200, {"ok": True})
            return super().do_GET()

        def do_HEAD(self):
            if not self._host_ok():
                return self._json(403, {"error": "bad host"})
            return super().do_HEAD()

        def do_POST(self):
            if self.path != "/api/action":
                return self._json(404, {"error": "not found"})
            if not self._host_ok():
                return self._json(403, {"error": "bad host"})
            origin = self.headers.get("Origin")
            if origin is not None and origin != f"http://{self.headers.get('Host')}":
                return self._json(403, {"error": "bad origin"})
            if not hmac.compare_digest(self.headers.get("X-Factory-Token", ""), token):
                return self._json(403, {"error": "bad token"})
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                return self._json(415, {"error": "send application/json"})
            try:
                n = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return self._json(400, {"error": "bad length"})
            if n <= 0 or n > MAX_BODY:
                return self._json(413, {"error": "body too large"})
            try:
                d = json.loads(self.rfile.read(n))
            except ValueError:
                return self._json(400, {"error": "bad json"})
            err = validate(d, load_board())
            if err:
                return self._json(409, {"error": err})
            rec = {
                "id": uuid.uuid4().hex,
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "type": d["type"],
                "job": d.get("job"),
                "answers": d.get("answers", {}),
                "note": CTRL_RE.sub("", d.get("note", "")).strip(),
            }
            with LOCK:
                with open(ACTIONS, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec) + "\n")
                    f.flush()
                    os.fsync(f.fileno())
            return self._json(201, {"id": rec["id"]})

    return Handler


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    token = secrets.token_urlsafe(24)
    for port in range(args.port, args.port + 21):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(port, token))
            break
        except OSError:
            continue
    else:
        sys.exit(f"No free port in {args.port}-{args.port + 20}.")
    out = lambda s: print(s, flush=True)  # flush: the URL must show even when piped
    out("Software Factory dashboard")
    out(f"  http://127.0.0.1:{port}/dashboard.html#t={token}")
    out("Open that exact URL: the #t=... part lets the page queue actions.")
    out("Queued actions are applied by running /factory apply in your Claude session. Ctrl+C stops.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
