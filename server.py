#!/usr/bin/env python3
"""hark-viewer server.

Serves the live transcript page and the call folders, and forwards /api/* to
hark's remote-control agent (which sends no CORS headers, so the page cannot
call it directly). Starts that agent if it is not running. Loopback only.
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("HARK_VIEWER_ROOT", Path.home() / "Recordings" / "calls")).expanduser()
PORT = int(os.environ.get("HARK_VIEWER_PORT", "8474"))
HARK_PORT = int(os.environ.get("HARK_REMOTE_CONTROL_PORT", "8473"))
HARK_BIN = os.environ.get("HARK_BIN", "hark")  # point this at your own build to run an unreleased hark
HARK_URL = f"http://127.0.0.1:{HARK_PORT}"
HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
CONTROLS = {"pause", "resume", "mute", "unmute", "stop"}
# What every call is recorded with. Opus because it stays playable while hark is
# still writing it, so a crash costs nothing; m4a and flac hold back the header
# until hark stops, and WAV costs 635 MB an hour.
START = {"system": True, "mix": True, "speakers": True, "captureBackend": "coreaudio", "ifExists": "error",
         # Mic on the left channel, the call on the right, so a later pass can still tell them
         # apart: `hark -i audio.opus --speakers --speaker-mode source` gives You and Others.
         # Needs a hark with --tracks; drop this key on a build that lacks it.
         "tracks": "stereo"}
AUDIO = "audio.opus"

opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never route loopback through a proxy


def hark(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else (b"" if method == "POST" else None)
    req = urllib.request.Request(HARK_URL + path, data=data, method=method)
    try:
        with opener.open(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw or b"{}")
        except ValueError:
            return e.code, {"error": raw.decode(errors="replace")}
    except OSError as e:
        return 502, {"error": f"hark agent unreachable: {e}"}


def ensure_agent():
    if hark("GET", "/status")[0] == 200:
        return True
    log = open(ROOT / ".hark-agent.log", "ab")
    subprocess.Popen([HARK_BIN, "--remote-control", str(HARK_PORT), "-C", str(ROOT), "--keep-awake"],
                     stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    for _ in range(50):
        time.sleep(0.2)
        if hark("GET", "/status")[0] == 200:
            return True
    return False


def slug(text, fallback=""):
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")[:60] or fallback


def call_of(session):
    """'workspace/name' for a session recording under ROOT, else None."""
    try:
        return str(Path(session["transcript"]).resolve().parent.relative_to(ROOT.resolve()))
    except (KeyError, TypeError, ValueError):
        return None


def status():
    code, body = hark("GET", "/status")
    session = body.get("session") if code == 200 else None
    active = bool(session) and session.get("state") in ("recording", "paused")
    call = call_of(session) if session else None
    if not call and (ROOT / "current").is_symlink():       # agent restarted: fall back to the last call made
        call = call_of({"transcript": str(ROOT / "current" / "transcript.json")})
    return code, {"agent": code == 200, "active": active, "session": session, "call": call, "error": body.get("error")}


def new_call(workspace, title):
    if not ensure_agent():
        return 502, {"error": f"could not start the hark agent ({HARK_BIN}); is hark installed?"}
    code, st = status()
    if st["active"]:
        return 409, {"error": "a call is already being recorded", "call": st["call"]}
    workspace = slug(workspace, "calls")
    name = time.strftime("%Y-%m-%d_%H%M%S") + (f"_{slug(title)}" if slug(title) else "")
    folder = ROOT / workspace / name
    folder.mkdir(parents=True)
    code, body = hark("POST", "/start", {**START, "audio": str(folder / AUDIO),
                                         "transcript": str(folder / "transcript.json")})
    if code not in (200, 201):                              # hark answers a started recording with 201
        folder.rmdir()
        return code, body
    (folder / "meta.json").write_text(json.dumps(
        {"started": time.time(), "workspace": workspace, "title": str(title).strip(), "id": body.get("id")}))
    link = ROOT / "current"
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(folder)
    call = f"{workspace}/{name}"
    return 200, {"call": call, "folder": str(folder), "url": f"http://127.0.0.1:{PORT}/?call={call}"}


def workspaces():
    return sorted(p.name for p in ROOT.iterdir() if p.is_dir() and not p.is_symlink() and not p.name.startswith("."))


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(ROOT), **kw)

    def log_message(self, *a):
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def list_directory(self, path):
        self.send_error(404)

    def reply(self, code, body):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def local(self):
        """Refuse other Host names, so a web page cannot reach this through DNS rebinding."""
        if self.headers.get("Host") in HOSTS:
            return True
        self.send_error(403)
        return False

    def do_GET(self):
        if not self.local():
            return
        path = self.path.split("?")[0]
        if path == "/api/status":
            code, body = status()
            return self.reply(200, {**body, "workspaces": workspaces()})
        if path in ("/", "/index.html"):
            raw = (HERE / "viewer.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            return self.wfile.write(raw)
        super().do_GET()

    def do_POST(self):
        if not self.local():
            return
        # A custom header forces a CORS preflight, which this server never answers:
        # only this page and local scripts can start or stop a recording.
        if self.headers.get("X-Hark-Viewer") != "1":
            return self.send_error(403)
        path = self.path.split("?")[0]
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if path == "/api/new":
            try:
                body = json.loads(raw or b"{}")
            except ValueError:
                return self.reply(400, {"error": "body must be JSON"})
            return self.reply(*new_call(body.get("workspace", ""), body.get("title", "")))
        if path.startswith("/api/") and path[5:] in CONTROLS:
            return self.reply(*hark("POST", "/" + path[5:]))
        self.send_error(404)


if __name__ == "__main__":
    ROOT.mkdir(parents=True, exist_ok=True)
    ensure_agent()
    try:
        ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
    except OSError as e:
        sys.exit(f"hark-viewer: cannot listen on 127.0.0.1:{PORT}: {e}")
