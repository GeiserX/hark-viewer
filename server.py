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
HARK_CALL = 10                                              # every call to hark's agent that is not a start
STALE = 3600                                                # a call not written to for this long is not restarted unasked
STOP_WAIT = float(os.environ.get("HARK_VIEWER_STOP_WAIT", "15"))   # how long a start waits out a capture that is still finishing
# hark answers a start only once the capture is really running, and opening a cold recognizer
# model took 12.7 s on the first streaming call after a reboot. hark gives up waiting at 60 s
# and answers `capturing: false`, so this has to outlast that or a recording that did start
# would be reported as a failure.
START_TIMEOUT = float(os.environ.get("HARK_VIEWER_START_TIMEOUT", "90"))
AGENT_WAIT = float(os.environ.get("HARK_VIEWER_AGENT_WAIT", "30"))  # how long a freshly started hark agent gets to answer /status
DIE_WAIT = float(os.environ.get("HARK_VIEWER_DIE_WAIT", "10"))      # how long a killed agent gets to let go of hark's port
# How long a start this side gave up on is watched for, in case hark is still opening its capture.
DISOWNED_WAIT = float(os.environ.get("HARK_VIEWER_DISOWNED_WAIT", "2"))
# The longest one /api/new or /api/restart can take, added up over the chain rather than
# estimated. /api/status reports this number and the launcher makes it its own --max-time, so a
# client must never give up before the server does: the earlier number left out both probes, the
# drain, the relaunch's own wait for a fresh agent and every plain call to hark, and came to 240
# against a chain that really runs to about 350.
PATIENCE = round(
    HARK_CALL + AGENT_WAIT                                  # ensure_agent, before the turn lock
    + HARK_CALL                                             # the status() the call starts from
    + STOP_WAIT + START_TIMEOUT + HARK_CALL                 # waiting out a capture, and the start that wait ends on
    + 2 * PROBE_WAIT + DIE_WAIT + HARK_CALL + AGENT_WAIT    # relaunch_agent: lsof, one ps, the drain, a fresh agent
    + START_TIMEOUT + HARK_CALL                             # the start after the relaunch
    + DISOWNED_WAIT + HARK_CALL                             # recording_anyway watching what hark is really doing
    + 5)
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
         # Stream the line being spoken (about 2 s behind) instead of waiting for a
         # pause; the page shows it as the grey row. A hark without it ignores the key.
         "liveStreaming": True}
AUDIO = postprocess.AUDIO

# Held by a restart from its stop to its start, and by every tick of the watcher, so
# the stop in the middle of a restart is never taken for the end of the call.
turn = threading.Lock()

opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never route loopback through a proxy


def object_response(code, value):
    """hark answers with a JSON object. Anything else on its port is not hark.

    A listener that answers `200 []` decodes fine and then meets .get() in every caller, which is
    an AttributeError and a 500 from the page: the same failure a non-JSON answer used to cause.
    """
    if isinstance(value, dict):
        return code, value
    return 502, {"error": f"whatever answers on hark's port {HARK_PORT} is not hark: "
                          f"it returned a JSON {type(value).__name__}, not an object"}


def hark(method, path, body=None, timeout=HARK_CALL):
    data = json.dumps(body).encode() if body is not None else (b"" if method == "POST" else None)
    req = urllib.request.Request(HARK_URL + path, data=data, method=method)
    try:
        with opener.open(req, timeout=timeout) as r:
            return object_response(r.status, json.loads(r.read() or b"{}"))
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return object_response(e.code, json.loads(raw or b"{}"))
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
# Held while the agent is being started. Both callers run ensure_agent before they take `turn`,
# so two overlapping POSTs with the agent down each read `agent is None` and each ran Popen: the
# two-agents-on-one-port race the single-agent guard exists to prevent, arrived at concurrently
# instead of sequentially.
starting = threading.Lock()


def ensure_agent():
    """hark's agent, answering. Started only when nothing answers and nothing we started is alive."""
    global agent
    if hark("GET", "/status")[0] == 200:
        return True
    with starting:
        if hark("GET", "/status")[0] == 200:
            return True                                     # another request started it while we waited
        return launch_agent()


def launch_agent():
    """Start the agent and wait for it, under `starting`."""
    global agent
    if agent is None or agent.poll() is not None:
        # A hark slower than AGENT_WAIT used to get a second Popen from the next call, so two
        # agents raced for the port and one became an orphan `quit` might or might not match.
        try:
            with open(ROOT / ".hark-agent.log", "ab") as log:   # closed: the old open() leaked one per call
                agent = subprocess.Popen([HARK_BIN, "--remote-control", str(HARK_PORT), "-C", str(ROOT), "--keep-awake"],
                                         stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        except OSError as e:
            # A HARK_BIN that is not there, or is not executable. This raised out of a request
            # thread, and at boot it killed the page server before it bound, so the page could not
            # even come up to say hark was missing.
            print(f"agent: cannot start {HARK_BIN}: {e}", file=sys.stderr, flush=True)
            return False
    end = time.time() + AGENT_WAIT
    began = time.time()
    while time.time() < end:
        time.sleep(0.2)
        if hark("GET", "/status")[0] == 200:
            return True
    # Which of the two it is matters: a process that is gone died and its own log says why, one
    # that is still there was simply slower than this wait. Without this line a CI failure looks
    # the same either way, and the first one took two rounds to tell apart.
    alive = agent is not None and agent.poll() is None
    print(f"agent: {HARK_BIN} did not answer in {time.time() - began:.1f} s; "
          f"the process it started is {'still running' if alive else 'gone'}", file=sys.stderr, flush=True)
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


WEDGED = f"the capture is wedged and the hark agent ({HARK_BIN}) did not come back"


def relaunch_agent():
    """Kill whatever listens on hark's port and start a fresh agent, the only way out of a wedged
    capture. Returns None once an agent answers, else why one does not."""
    global agent
    try:
        run = subprocess.run(["lsof", "-nP", "-t", f"-iTCP:{HARK_PORT}", "-sTCP:LISTEN"],
                             capture_output=True, text=True, timeout=PROBE_WAIT)
    except subprocess.TimeoutExpired:
        print(f"relaunch: lsof did not answer in {PROBE_WAIT} s, leaving hark's port alone", file=sys.stderr, flush=True)
        return None if ensure_agent() else WEDGED
    foreign, killed = [], []
    for pid in run.stdout.split():
        # Only hark's agent. Something else may listen on the same port of another address, an ssh -L or a VM forward.
        try:
            command = subprocess.run(["ps", "-o", "command=", "-p", pid],
                                     capture_output=True, text=True, timeout=PROBE_WAIT).stdout.strip()
        except subprocess.TimeoutExpired:
            command = ""                                    # unnamed, so not provably hark's agent: left alone below
        if "--remote-control" not in command:
            print(f"relaunch: left pid {pid} alone, it is not a hark agent: {command}", file=sys.stderr, flush=True)
            foreign.append(f"pid {pid} ({command or 'unnamed'})")
            continue
        try:
            os.kill(int(pid), 15)                           # SIGTERM lets hark finalise the audio file
            killed.append(pid)
        except (OSError, ValueError):
            pass
    if foreign and not killed:
        # Nothing here can free the port, and a hark started against a port it cannot bind waits
        # out AGENT_WAIT and dies: the caller then reported a wedged capture and an agent that did
        # not come back, while the real reason was sitting on the port, and every attempt leaked
        # one doomed process and more lines into the agent log.
        why = hark("GET", "/status")[1].get("error") or f"something that is not hark holds port {HARK_PORT}"
        return f"{why}. It is {', '.join(foreign)}, and no hark agent can have that port until it goes"
    # An agent that outlives the kill still holds the port, so nothing fresh can bind it. Falling
    # out of this wait used to leave ensure_agent facing that same agent, answering: it returned
    # True without starting anything and the next /start went back to the wedged capture.
    end = time.time() + DIE_WAIT
    while hark("GET", "/status")[0] == 200:
        if time.time() > end:
            return (f"whatever listens on hark's port {HARK_PORT} still answers {DIE_WAIT} s after it was "
                    "asked to stop, so no fresh agent can have it")
        time.sleep(0.2)
    with starting:
        agent = None                                        # a fresh one is the whole point, even if ours is slow to die
    return None if ensure_agent() else WEDGED


def ask_start(body):
    """POST /start once. A 2xx for a capture hark says is not running is a failed start, not a call.

    Such a start is stopped before it is reported, because hark goes on holding the session at
    `recording` while this server reports the call inactive. Left there, hark's own "already
    active" check refused every later start, and the page had no Stop button to clear it: from
    a browser the recorder was dead until someone ran `hark-viewer stop` in a terminal.
    """
    code, answer = hark("POST", "/start", body, timeout=START_TIMEOUT)
    if code in (200, 201) and answer.get("capturing") is False:
        error = f"hark answered the start but says it is not capturing: {answer}"
        stop_code, stop_body = hark("POST", "/stop")
        if stop_code not in (200, 201, 204, 404):
            error += f". Its session would not stop either: {stop_body.get('error') or stop_body}"
        return 502, {"error": error}
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
    problem = relaunch_agent()
    if problem:
        return 502, {"error": problem}
    return ask_start(body)


def recording_anyway(folder, audio, code, answer):
    """hark's session recording into `audio`, when its start answered badly, else None.

    hark's HTTP server cuts a handler off at its ceiling and answers 500 in its place, and a
    client timeout looks the same from here. The capture is never told, so it runs on. A cold
    start that answered at 22.8 s was called a failure this way while hark recorded. What hark
    is doing counts, not what its start said.

    It is asked for a couple of seconds, not once. A start this side gave up on can still be
    opening its capture, which is not in /status yet, and one look then called it a failure: the
    folder was removed if hark had written nothing, and left with no meta.json and `current` still
    on the previous call if hark had. The transcript survived that, the workspace and the title did
    not, and `relabel current` or `finalize current` aimed at the wrong call.
    """
    end = time.time() + DISOWNED_WAIT
    while True:
        st_code, st = hark("GET", "/status")
        session = (st.get("session") or {}) if st_code == 200 else {}
        if live(session) and Path(session.get("audio") or "").resolve() == Path(audio).resolve():
            print(f"start: hark answered the start with {code} {answer}, and is recording "
                  f"{folder.name}/{Path(audio).name}; carrying on", file=sys.stderr, flush=True)
            return session
        if time.time() >= end:
            print(f"start: start failed with {code} {answer}", file=sys.stderr, flush=True)
            return None
        time.sleep(0.2)


def point_at(link, folder):
    """Point `link` at `folder` atomically, through a temporary link and a rename.

    Returns None, or why it could not. A rename cannot replace a directory, so an empty one of that
    name is removed, and one with anything in it is left exactly where it is: it could be a call
    folder somebody named `current`, and no link is worth deleting a recording over.
    """
    if link.is_dir() and not link.is_symlink():
        try:
            link.rmdir()
        except OSError as e:
            return f"{link} is a directory that is not empty, so it cannot be the current link: {e}"
    temp = link.with_name(f".{link.name}.new")
    if temp.is_symlink() or temp.exists():
        temp.unlink()
    temp.symlink_to(folder)
    os.replace(temp, link)
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
        # The timestamp is only unique to the second, and a start that failed after hark had
        # already written audio leaves its folder behind on purpose. The second call in that
        # second used to raise FileExistsError out of the request thread, so the caller got no
        # answer at all: an empty reply to the launcher and nothing to the page.
        folder = ROOT / workspace / name
        for n in range(2, 60):
            try:
                folder.mkdir(parents=True)
                break
            except FileExistsError:
                folder = ROOT / workspace / f"{name}-{n}"
        else:
            return 500, {"error": f"{ROOT / workspace / name} and 58 names after it are all taken"}
        code, body = start_recording(folder / AUDIO, folder / "transcript.json")
        if code not in (200, 201):
            recording = recording_anyway(folder, folder / AUDIO, code, body)
            if recording is None:
                try:
                    folder.rmdir()
                except OSError:                                 # hark wrote into it: keep what it wrote
                    pass
                return code, body
            body = recording
        (folder / "meta.json").write_text(json.dumps(
            {"started": time.time(), "workspace": workspace, "title": str(title).strip(), "id": body.get("id")}))
        # A temporary link and a rename, so `current` is never briefly absent: the page and the
        # launcher both read it, and `finalize current` would have hit nothing in that window. It
        # also survives a real directory called `current`, which used to make unlink raise.
        problem = point_at(ROOT / "current", folder)
        if problem:
            # The recording is real and running. `current` is a convenience, so losing the call
            # over it would be the wrong trade; the page reads `?call=` and stop goes through the API.
            print(f"new: recording {folder.name}, but {problem}", file=sys.stderr, flush=True)
        call = f"{workspace}/{folder.name}"
        watch.call, watch.ended = call, None
        return 200, {"call": call, "folder": str(folder), "url": f"http://127.0.0.1:{PORT}/?call={call}"}


def restart_call(force=False):
    """Stop the recording and start a new part in the same call folder, for when the capture broke mid-call.

    Also for a session hark reports `failed`, for a capture hark has disowned, and for an agent
    that died: then the call is the one in `current`.
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
        # hark's own state, not this server's judgement of the capture. Once hark sets
        # `capturing: false` the server reports the call inactive while hark still holds the
        # session at `recording`, so gating on `active` skipped the stop, the start met hark's
        # "already active" and Restart answered "the call is stopped and part 2 did not start",
        # which was false and left the only recovery outside the browser.
        if state in LIVE:
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
            # The same reconciliation /api/new does, because a bad start answer means the same thing
            # here: hark's HTTP server answers 500 for a capture that runs on. Taking the part back
            # out of meta.json then drops a part that is recording from both transcripts.
            recording = recording_anyway(folder, folder / part["audio"], code, body)
            if recording is None:
                postprocess.write_atomic(folder / "meta.json", was)   # it never started: take it back out
                return 502, {"error": f"the call is stopped and part {n} did not start: {body.get('error') or body}", "call": call}
            body = recording
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


def reconcile():
    """The accurate transcript for a call that ended while this server was not running.

    The watcher only knows the calls it saw recording, so an ending it was not alive for left the
    call with the live transcript alone and nothing saying so, and only `hark-viewer finalize`
    recovered it. A call hark still holds is left to the watcher, which is what the grace before
    an ending other than a stop is for.
    """
    link = ROOT / "current"
    if not link.is_symlink():
        return
    call = call_of({"transcript": str(link / "transcript.json")})
    if not call or not (ROOT / call).is_dir() or (ROOT / call / postprocess.STATUS).exists():
        return
    code, st = status()
    if st["call"] == call and ((st["session"] or {}).get("state") in LIVE or st["active"]):
        return                                              # hark is still on it: the watcher's business
    if not any((ROOT / call / part["audio"]).is_file() for part in postprocess.parts_of(ROOT / call)):
        return                                              # a folder nothing was ever recorded into
    print(f"boot: {call} ended while no server was running; writing its accurate transcript",
          file=sys.stderr, flush=True)
    finalize(call)


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
    reconcile()
    threading.Thread(target=watch, daemon=True).start()
    try:
        ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
    except OSError as e:
        sys.exit(f"hark-viewer: cannot listen on 127.0.0.1:{PORT}: {e}")
