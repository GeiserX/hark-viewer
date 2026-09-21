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
import threading
import time
import urllib.error
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import postprocess

HERE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("HARK_VIEWER_ROOT", Path.home() / "Recordings" / "calls")).expanduser()
PORT = int(os.environ.get("HARK_VIEWER_PORT", "8474"))
HARK_PORT = int(os.environ.get("HARK_REMOTE_CONTROL_PORT", "8473"))
HARK_BIN = os.environ.get("HARK_BIN", "hark")  # point this at your own build to run an unreleased hark
HARK_URL = f"http://127.0.0.1:{HARK_PORT}"
HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
CONTROLS = {"pause", "resume", "mute", "unmute", "stop"}
LIVE = ("recording", "paused")
WATCH_EVERY = float(os.environ.get("HARK_VIEWER_WATCH", "2"))   # seconds between looks at hark for a call that ended
# What every call is recorded with. Opus because it stays playable while hark is
# still writing it, so a crash costs nothing; m4a and flac hold back the header
# until hark stops, and WAV costs 635 MB an hour.
START = {"system": True, "mix": True, "speakers": True, "captureBackend": "coreaudio", "ifExists": "error",
         # Mic on the left channel, the call on the right, so a later pass can still tell them
         # apart: `hark -i audio.opus --speakers --speaker-mode source` gives You and Others.
         # A hark without --tracks ignores the key and records a mixed file.
         "tracks": "stereo",
         # Stream the line being spoken (about 2.5 s behind) instead of waiting for a
         # pause; the page shows it as the grey row. A hark without it ignores the key.
         "liveStreaming": True}
AUDIO = "audio.opus"

# Held by a restart from its stop to its start, and by every tick of the watcher, so
# the stop in the middle of a restart is never taken for the end of the call.
turn = threading.Lock()

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
    active = bool(session) and session.get("state") in LIVE
    call = call_of(session) if session else None
    if not call and (ROOT / "current").is_symlink():       # agent restarted: fall back to the last call made
        call = call_of({"transcript": str(ROOT / "current" / "transcript.json")})
    # `session` goes through whole, so what a newer hark adds (partial, callAudio) reaches the page untouched.
    return code, {"agent": code == 200, "active": active, "session": session, "call": call, "error": body.get("error"),
                  "parts": len(postprocess.parts_of(ROOT / call)) if call else 0,
                  "postprocess": postprocess.read_status(ROOT / call) if call else None}


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
    watch.call = call
    return 200, {"call": call, "folder": str(folder), "url": f"http://127.0.0.1:{PORT}/?call={call}"}


def restart_call():
    """Stop the recording and start a new part in the same call folder, for when the capture broke mid-call."""
    with turn:
        code, st = status()
        state = (st["session"] or {}).get("state")
        if not st["call"] or not (st["active"] or state == "failed"):
            return 409, {"error": "no call is being recorded, so there is nothing to restart"}
        call, folder = st["call"], ROOT / st["call"]
        if st["active"]:
            hark("POST", "/stop")
            for _ in range(150):                            # hark finalises the audio before it says stopped
                if not status()[1]["active"]:
                    break
                time.sleep(0.2)
            else:
                return 504, {"error": "hark did not stop within 30 s; the call is still on its old part", "call": call}
        meta = postprocess.read_meta(folder)
        parts = postprocess.parts_of(folder, meta)
        n = len(parts) + 1
        while (folder / f"audio.part{n}.opus").exists() or (folder / f"transcript.part{n}.json").exists():
            n += 1                                          # hark never overwrites, so never offer it a taken name
        part = {"n": n, "audio": f"audio.part{n}.opus", "transcript": f"transcript.part{n}.json"}
        for attempt in range(3):
            code, body = hark("POST", "/start", {**START, "audio": str(folder / part["audio"]),
                                                 "transcript": str(folder / part["transcript"])})
            if code in (200, 201):
                break
            time.sleep(1)
        else:
            return 502, {"error": f"the call is stopped and part {n} did not start: {body.get('error') or body}", "call": call}
        part["started"] = time.time()
        postprocess.write_atomic(folder / "meta.json", json.dumps({**meta, "parts": parts + [part]}))
        watch.call = call
        return 200, {"call": call, "folder": str(folder), "part": n, "url": f"http://127.0.0.1:{PORT}/?call={call}"}


def finalize(call):
    """The accurate transcript, as a detached job that outlives this server. It refuses to run twice for one call."""
    folder = ROOT / call
    if (folder / postprocess.STATUS).exists() or not folder.is_dir():
        return
    log = open(ROOT / ".postprocess.log", "ab")
    subprocess.Popen([sys.executable, str(HERE / "postprocess.py"), str(folder)], env={**os.environ, "HARK_BIN": HARK_BIN},
                     stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)


def watch():
    """Notice the end of a call however it ended: the page, `hark-viewer stop`, or hark itself."""
    while True:
        time.sleep(WATCH_EVERY)
        try:
            with turn:
                code, st = status()
                if st["active"]:
                    watch.call = st["call"]
                elif watch.call and (st["session"] or {}).get("state") == "stopped" and st["call"] == watch.call:
                    finalize(watch.call)
                    watch.call = None
        except Exception as e:                              # noqa: BLE001 - the watcher must outlive any one bad tick
            print(f"watch: {e}", file=sys.stderr, flush=True)


watch.call = None                                           # the call last seen recording


def workspaces():
    return sorted(p.name for p in ROOT.iterdir() if p.is_dir() and not p.is_symlink() and not p.name.startswith("."))


def better_of(live):
    """transcript.speakers.json in place of transcript.json, while it is the newer of the two."""
    better = live.with_name("transcript.speakers.json")
    if live.name == "transcript.json" and better.is_file() and (
            not live.is_file() or better.stat().st_mtime >= live.stat().st_mtime):
        return better
    return live


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
        # `hark-viewer relabel` writes transcript.speakers.json next to the live
        # file, same lines with the speakers corrected by an offline pass. Prefer
        # it, so the page and any agent reading the call get the better labels.
        # Only while it is current: a line hark appended after the relabel makes the
        # live file newer, and the page must keep seeing new lines.
        if path.endswith("/transcript.json"):
            live = Path(self.translate_path(path))            # translate_path resolves under ROOT
            if len(postprocess.parts_of(live.parent)) > 1:
                # A restarted call: every part's lines as one transcript on the call's clock.
                raw = postprocess.jsonl(postprocess.merged_lines(live.parent, better_of)).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                return self.wfile.write(raw)
            if better_of(live) != live:
                self.path = path[: -len("transcript.json")] + "transcript.speakers.json"
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
        if path == "/api/restart":
            return self.reply(*restart_call())
        if path.startswith("/api/") and path[5:] in CONTROLS:
            return self.reply(*hark("POST", "/" + path[5:]))
        self.send_error(404)


if __name__ == "__main__":
    ROOT.mkdir(parents=True, exist_ok=True)
    ensure_agent()
    threading.Thread(target=watch, daemon=True).start()
    try:
        ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
    except OSError as e:
        sys.exit(f"hark-viewer: cannot listen on 127.0.0.1:{PORT}: {e}")
