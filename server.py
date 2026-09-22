#!/usr/bin/env python3
"""hark-viewer server.

Serves the live transcript page and the call folders, and forwards /api/* to
hark's remote-control agent (which sends no CORS headers, so the page cannot
call it directly). Starts that agent if it is not running. Loopback only.
"""
import http.client
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
PROBE_WAIT = 10                                             # lsof and ps answer at once or not at all
STALE = 3600                                                # a call not written to for this long is not restarted unasked
STOP_WAIT = float(os.environ.get("HARK_VIEWER_STOP_WAIT", "15"))   # how long a start waits out a capture that is still finishing
# hark answers a start only once the capture is really running, and opening a cold recognizer
# model took 12.7 s on the first streaming call after a reboot. hark gives up waiting at 60 s
# and answers `capturing: false`, so this has to outlast that or a recording that did start
# would be reported as a failure.
START_TIMEOUT = float(os.environ.get("HARK_VIEWER_START_TIMEOUT", "90"))
AGENT_WAIT = float(os.environ.get("HARK_VIEWER_AGENT_WAIT", "30"))  # how long a freshly started hark agent gets to answer /status
# The longest one /api/new or /api/restart can take: waiting out a capture that is still
# finishing, a start hark sits on for its whole timeout, the agent relaunch that is the only way
# out of a wedged capture, and one more start. /api/status reports this number and the launcher
# makes it its own --max-time, so no client ever gives up on a recording that did begin.
PATIENCE = round(STOP_WAIT + 2 * START_TIMEOUT + AGENT_WAIT + 15)
WATCH_EVERY = float(os.environ.get("HARK_VIEWER_WATCH", "2"))   # seconds between looks at hark for a call that ended
# An ending other than a stop is where Restart records on into the same call, and the accurate
# transcript refuses a call that already has one. So those endings get the transcript only after
# this long without the call coming back.
ENDED_GRACE = float(os.environ.get("HARK_VIEWER_ENDED_GRACE", "60"))
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
AUDIO = postprocess.AUDIO

# Held by a restart from its stop to its start, and by every tick of the watcher, so
# the stop in the middle of a restart is never taken for the end of the call.
turn = threading.Lock()

opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never route loopback through a proxy


def hark(method, path, body=None, timeout=10):
    data = json.dumps(body).encode() if body is not None else (b"" if method == "POST" else None)
    req = urllib.request.Request(HARK_URL + path, data=data, method=method)
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw or b"{}")
        except ValueError:
            return e.code, {"error": raw.decode(errors="replace")}
    except OSError as e:
        return 502, {"error": f"hark agent unreachable: {e}"}
    except (ValueError, http.client.HTTPException) as e:
        # Something that is not hark's agent holds its port: an ssh -L, a VM forward, another
        # dev server. It answers, just not JSON over HTTP. JSONDecodeError is a ValueError and
        # BadStatusLine an HTTPException, so neither was caught: this killed ensure_agent at
        # boot before the page server ever bound, and dropped every poll if it turned up mid-call.
        return 502, {"error": f"whatever answers on hark's port {HARK_PORT} is not hark: {type(e).__name__}: {e}"}


agent = None                                                # the agent this server started, if it started one


def ensure_agent():
    """hark's agent, answering. Started only when nothing answers and nothing we started is alive."""
    global agent
    if hark("GET", "/status")[0] == 200:
        return True
    if agent is None or agent.poll() is not None:
        # A hark slower than AGENT_WAIT used to get a second Popen from the next call, so two
        # agents raced for the port and one became an orphan `quit` might or might not match.
        with open(ROOT / ".hark-agent.log", "ab") as log:   # closed: the old open() leaked one per call
            agent = subprocess.Popen([HARK_BIN, "--remote-control", str(HARK_PORT), "-C", str(ROOT), "--keep-awake"],
                                     stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    end = time.time() + AGENT_WAIT
    while time.time() < end:
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


def live(session):
    """Is hark recording right now?

    `capturing: false` is hark saying the capture behind a session it still calls `recording` is
    not running: it gives up waiting for its own capture at 60 s and answers that. Taking the
    state alone gave such a call a folder, a `current` symlink and a pulsing REC dot.
    """
    return bool(session) and session.get("state") in LIVE and session.get("capturing") is not False


def status():
    code, body = hark("GET", "/status")
    session = body.get("session") if code == 200 else None
    active = live(session)
    call = call_of(session) if session else None
    if not call and (ROOT / "current").is_symlink():       # agent restarted: fall back to the last call made
        call = call_of({"transcript": str(ROOT / "current" / "transcript.json")})
    # `session` goes through whole, so what a newer hark adds (partial, callAudio) reaches the page untouched.
    return code, {"agent": code == 200, "active": active, "session": session, "call": call, "error": body.get("error"),
                  "parts": len(postprocess.parts_of(ROOT / call)) if call else 0,
                  "postprocess": postprocess.read_status(ROOT / call) if call else None}


def relaunch_agent():
    """Kill whatever listens on hark's port and start a fresh agent. The only way out of a wedged capture."""
    global agent
    try:
        run = subprocess.run(["lsof", "-nP", "-t", f"-iTCP:{HARK_PORT}", "-sTCP:LISTEN"],
                             capture_output=True, text=True, timeout=PROBE_WAIT)
    except subprocess.TimeoutExpired:
        print(f"relaunch: lsof did not answer in {PROBE_WAIT} s, leaving hark's port alone", file=sys.stderr, flush=True)
        return ensure_agent()
    for pid in run.stdout.split():
        # Only hark's agent. Something else may listen on the same port of another address, an ssh -L or a VM forward.
        try:
            command = subprocess.run(["ps", "-o", "command=", "-p", pid],
                                     capture_output=True, text=True, timeout=PROBE_WAIT).stdout.strip()
        except subprocess.TimeoutExpired:
            command = ""                                    # unnamed, so not provably hark's agent: left alone below
        if "--remote-control" not in command:
            print(f"relaunch: left pid {pid} alone, it is not a hark agent: {command}", file=sys.stderr, flush=True)
            continue
        try:
            os.kill(int(pid), 15)                           # SIGTERM lets hark finalise the audio file
        except (OSError, ValueError):
            pass
    for _ in range(50):
        if hark("GET", "/status")[0] != 200:
            break
        time.sleep(0.2)
    agent = None                                            # a fresh one is the whole point, even if ours is slow to die
    return ensure_agent()


def ask_start(body):
    """POST /start once. A 2xx for a capture hark says is not running is a failed start, not a call."""
    code, answer = hark("POST", "/start", body, timeout=START_TIMEOUT)
    if code in (200, 201) and answer.get("capturing") is False:
        return 502, {"error": f"hark answered the start but says it is not capturing: {answer}"}
    return code, answer


def start_recording(audio, transcript):
    """POST /start, patient with a capture that is still finishing.

    hark says `stopped` the moment a stop is asked for. The capture that writes the audio
    finishes afterwards, /status does not show it, and until it has, /start answers 409
    "still finishing". hark gives up on it after its own stop timeout (10 s) and then refuses
    every start until the agent is restarted, so past that wait the agent is relaunched.
    """
    body = {**START, "audio": str(audio), "transcript": str(transcript)}
    end = time.time() + STOP_WAIT
    while True:
        code, answer = ask_start(body)
        if code in (200, 201) or not (code == 409 and "finishing" in str(answer.get("error"))):
            return code, answer                             # hark answers a started recording with 201
        if time.time() >= end:
            break
        time.sleep(0.5)
    if not relaunch_agent():
        return 502, {"error": f"the capture is wedged and the hark agent ({HARK_BIN}) did not come back"}
    return ask_start(body)


def recording_anyway(folder, code, answer):
    """hark's session recording into `folder`, when its start answered badly, else None.

    hark's HTTP server cuts a handler off at its ceiling and answers 500 in its place, and a
    client timeout looks the same from here; the capture is never told, so it runs on. A cold
    start that answered at 22.8 s was called a failure this way while hark recorded. What hark
    is doing counts, not what its start said.
    """
    st_code, st = hark("GET", "/status")
    session = (st.get("session") or {}) if st_code == 200 else {}
    if live(session) and Path(session.get("audio") or "").resolve() == (folder / AUDIO).resolve():
        print(f"new: hark answered the start with {code} {answer}, and is recording {folder.name}; carrying on",
              file=sys.stderr, flush=True)
        return session
    print(f"new: start failed with {code} {answer}", file=sys.stderr, flush=True)
    return None


def new_call(workspace, title):
    if not ensure_agent():
        return 502, {"error": f"could not start the hark agent ({HARK_BIN}); is hark installed?"}
    with turn:
        code, st = status()
        if st["active"]:
            return 409, {"error": "a call is already being recorded", "call": st["call"]}
        if watch.call:                                      # ended inside the watcher's last tick: it still gets its transcript
            finalize(watch.call)
            watch.call = watch.ended = None
        workspace = slug(workspace, "calls")
        name = time.strftime("%Y-%m-%d_%H%M%S") + (f"_{slug(title)}" if slug(title) else "")
        folder = ROOT / workspace / name
        folder.mkdir(parents=True)
        code, body = start_recording(folder / AUDIO, folder / "transcript.json")
        if code not in (200, 201):
            recording = recording_anyway(folder, code, body)
            if recording is None:
                try:
                    folder.rmdir()
                except OSError:                                 # hark wrote into it: keep what it wrote
                    pass
                return code, body
            body = recording
        (folder / "meta.json").write_text(json.dumps(
            {"started": time.time(), "workspace": workspace, "title": str(title).strip(), "id": body.get("id")}))
        link = ROOT / "current"
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(folder)
        call = f"{workspace}/{name}"
        watch.call, watch.ended = call, None
        return 200, {"call": call, "folder": str(folder), "url": f"http://127.0.0.1:{PORT}/?call={call}"}


def restart_call(force=False):
    """Stop the recording and start a new part in the same call folder, for when the capture broke mid-call.

    Also for a session hark reports `failed`, and for an agent that died: then the call is the one in `current`.
    """
    if not ensure_agent():
        return 502, {"error": f"could not start the hark agent ({HARK_BIN}); is hark installed?"}
    with turn:
        code, st = status()
        state = (st["session"] or {}).get("state")
        if not st["call"] or state == "stopped" or (ROOT / st["call"] / postprocess.STATUS).exists():
            return 409, {"error": "no call is being recorded, so there is nothing to restart"}
        call, folder = st["call"], ROOT / st["call"]
        meta = postprocess.read_meta(folder)
        if not st["active"] and not force:
            # With no live session the call is whatever `current` points at, which can be days old.
            written = [(folder / p["audio"]).stat().st_mtime for p in postprocess.parts_of(folder, meta) if (folder / p["audio"]).is_file()]
            if not written or time.time() - max(written) > STALE:
                return 409, {"error": f"{call} was last recorded more than an hour ago, so this looks like a finished call; "
                                      "`hark-viewer restart --force` records on into it anyway", "call": call}
        if not isinstance(meta.get("started"), (int, float)):
            # Without the call's start every part would sit at 0:00, on top of part 1.
            try:
                meta["started"] = (folder / AUDIO).stat().st_birthtime
            except (OSError, AttributeError):
                return 409, {"error": f"{call} has no start time in meta.json and no {AUDIO} to take one from", "call": call}
        if st["active"]:
            # The answer used to be discarded. A stop that failed left the call recording, the
            # /start below came back 409 "already active", and the user was told the call was
            # stopped and the part had not started: the first half false, and an invitation to
            # press Restart again. 404 is hark saying there was nothing to stop, which is fine.
            stop_code, stop_body = hark("POST", "/stop")
            if stop_code not in (200, 201, 204, 404):
                return 502, {"error": f"the recording would not stop, so it is still running: "
                                      f"{stop_body.get('error') or stop_body}", "call": call}
        parts = postprocess.parts_of(folder, meta)
        n = len(parts) + 1
        while (folder / f"audio.part{n}.opus").exists() or (folder / f"transcript.part{n}.json").exists():
            n += 1                                          # hark never overwrites, so never offer it a taken name
        part = {"n": n, "audio": f"audio.part{n}.opus", "transcript": f"transcript.part{n}.json", "started": time.time()}
        # The part is in meta.json before hark is asked for it. Written afterwards, a crash or a
        # SIGKILL in between left part n recording into a file no parts_of would ever list, so both
        # the joined live transcript and the accurate one skipped it, with no error anywhere.
        was = json.dumps(meta)
        postprocess.write_atomic(folder / "meta.json", json.dumps({**meta, "parts": parts + [part]}))
        code, body = start_recording(folder / part["audio"], folder / part["transcript"])
        if code not in (200, 201):
            postprocess.write_atomic(folder / "meta.json", was)   # it never started: take it back out
            return 502, {"error": f"the call is stopped and part {n} did not start: {body.get('error') or body}", "call": call}
        part["started"] = time.time()                             # when the capture opened, not when it was asked for
        postprocess.write_atomic(folder / "meta.json", json.dumps({**meta, "parts": parts + [part]}))
        watch.call, watch.ended = call, None
        return 200, {"call": call, "folder": str(folder), "part": n, "url": f"http://127.0.0.1:{PORT}/?call={call}"}


def finalize(call):
    """The accurate transcript, as a detached job that outlives this server. It refuses to run twice for one call."""
    folder = ROOT / call
    if (folder / postprocess.STATUS).exists() or not folder.is_dir():
        return
    log = open(ROOT / ".postprocess.log", "ab")
    job = subprocess.Popen([sys.executable, str(HERE / "postprocess.py"), str(folder)], env={**os.environ, "HARK_BIN": HARK_BIN},
                           stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    log.close()
    threading.Thread(target=job.wait, daemon=True).start()  # reaped, or a killed job stays a zombie that reads as running


def watch():
    """Notice the end of a call however it ended.

    A stop from the page or the command line is the end at once. Every other ending - hark
    reporting the session `failed`, hark disowning its own capture, the agent dying with the call
    still open - is the end too, once ENDED_GRACE has passed without the call coming back, because
    that is the window in which Restart records on into the same call.
    """
    while True:
        time.sleep(WATCH_EVERY)
        try:
            with turn:
                code, st = status()
                if st["active"]:
                    watch.call, watch.ended = st["call"], None
                elif watch.call:
                    stopped = (st["session"] or {}).get("state") == "stopped" and st["call"] == watch.call
                    if not stopped:
                        watch.ended = watch.ended or time.time()
                    if stopped or time.time() - watch.ended >= ENDED_GRACE:
                        finalize(watch.call)
                        watch.call = watch.ended = None
        except Exception as e:                              # noqa: BLE001 - the watcher must outlive any one bad tick
            print(f"watch: {e}", file=sys.stderr, flush=True)


watch.call = None                                           # the call last seen recording
watch.ended = None                                          # when it stopped looking live, for an ending that is not a stop


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
            return self.reply(200, {**body, "workspaces": workspaces(), "patience": PATIENCE})
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
            try:
                force = json.loads(raw or b"{}").get("force") is True
            except (ValueError, AttributeError):
                force = False
            return self.reply(*restart_call(force))
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
