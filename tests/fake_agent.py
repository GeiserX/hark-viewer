#!/usr/bin/env python3
"""hark's remote-control agent, as far as server.py uses it, with hark's real stop.

/stop answers `stopped` at once. The capture goes on writing the audio for FAKE_FINISH
seconds after that, /status does not show it, and until it is over /start answers 409
"still finishing". With `wedged` set it never is over, and after FAKE_STOP_TIMEOUT the
session reads `failed`, as hark's own watchdog does it. Only a new agent gets out of that.

`capturing` belongs to the session, the way hark's does: a start stamps the session with
whatever `capturing` is then, and /status reports the session's own value. So
`{"capturing": false}` decides what the next start records, and
`{"session": {"capturing": false}}` disowns the capture behind the session that is already
running without touching the next one. hark gives up waiting for its own capture at 60 s and
then says `recording` with `capturing: false`, which is a start that recorded nothing.

`stop_answer` makes /stop answer that code and change nothing, which is hark refusing to stop,
and `refuse_start` makes /start answer 500 and start nothing.

FAKE_IGNORE_TERM makes it survive the SIGTERM of a relaunch, which is an agent still holding
hark's port after the kill, so no fresh one can bind it.

POST /_fake {"session": {...}, "finish": s, "wedged": bool, "capturing": bool, "stop_answer": code,
"refuse_start": bool}
changes it from a test. Every request is a line in FAKE_LOG: `agent <pid> <path> <json body>`.
"""
import json
import os
import signal
import socket
import socketserver
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# FAKE_SESSION is a session it already has when it starts, which is a hark that was recording
# before the page server came up: a server restart mid-call leaves exactly that.
S = {"session": json.loads(os.environ["FAKE_SESSION"]) if os.environ.get("FAKE_SESSION") else None,
     "finish": float(os.environ.get("FAKE_FINISH", "0")), "wedged": False, "writing_until": 0.0,
     "stop_timeout": float(os.environ.get("FAKE_STOP_TIMEOUT", "10")),
     "capturing": os.environ.get("FAKE_CAPTURING", "1") not in ("0", "false", "no"),
     "stop_answer": int(os.environ.get("FAKE_STOP_ANSWER", "0")), "refuse_start": False}


def finishing():
    return S["wedged"] and S["writing_until"] or time.time() < S["writing_until"]


def keep_writing(audio, stopped_at):
    while finishing():
        with open(audio, "ab") as f:
            f.write(b"tail")
        if S["wedged"] and time.time() - stopped_at > S["stop_timeout"] and S["session"]["state"] == "stopped":
            S["session"] = {**S["session"], "state": "failed", "error": "capture did not finish"}
        time.sleep(0.1)


class Serving(ThreadingHTTPServer):
    """No reverse DNS lookup between bind() and listen(), the same as the page server.

    http.server's server_bind calls socket.getfqdn in that gap, and a connect to a port that is
    bound and not listening hangs until the client gives up rather than being refused. On a runner
    with a slow reverse lookup this agent was alive and mute for half a minute, and every test that
    needed a second one failed there and passed everywhere else.
    """

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = socket.gethostname(), self.server_address[1]


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def answer(self, code, body):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self.answer(200, {"session": S["session"]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if self.path == "/_fake":
            if "session" in body:
                S["session"] = {**(S["session"] or {}), **body.pop("session")}
            S.update(body)
            return self.answer(200, {})
        with open(os.environ["FAKE_LOG"], "a") as log:
            log.write(f"agent {os.getpid()} {self.path} {json.dumps(body)}\n")
        live = S["session"] and S["session"]["state"] in ("recording", "paused")
        if self.path == "/start":
            time.sleep(float(os.environ.get("FAKE_START_DELAY", "0")))   # a cold model keeps a real start waiting
            if S["refuse_start"]:
                return self.answer(500, {"error": "hark would not start a recording"})
            if live:
                return self.answer(409, {"error": "a recording is already active"})
            if finishing():
                return self.answer(409, {"error": "the previous capture is still finishing and no new recording can start; "
                                                  "check GET /status and restart the agent if it stays wedged"})
            Path(body["audio"]).write_bytes(b"audio")
            S["session"] = {"state": "recording", "elapsed": 0, "muted": False, "id": "X",
                            "capturing": S["capturing"], "audio": body["audio"], "transcript": body["transcript"]}
            if os.environ.get("FAKE_START_ANSWER"):   # hark's HTTP server cut the handler off: a 500 for a capture that runs on
                return self.answer(int(os.environ["FAKE_START_ANSWER"]), {})
            return self.answer(201, {"id": "X", "capturing": S["capturing"]})
        if self.path == "/stop":
            if S["stop_answer"]:
                return self.answer(S["stop_answer"], {"error": "the capture would not stop"})
            if not live:
                return self.answer(404, {"error": "no active recording"})
            S["session"] = {**S["session"], "state": "stopped"}
            S["writing_until"] = time.time() + S["finish"]
            threading.Thread(target=keep_writing, args=(S["session"]["audio"], time.time()), daemon=True).start()
        self.answer(200, {})


if __name__ == "__main__":
    if os.environ.get("FAKE_IGNORE_TERM") not in (None, "", "0"):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)             # an agent that outlives the relaunch's kill
    port = int(sys.argv[sys.argv.index("--remote-control") + 1])
    with open(os.environ["FAKE_LOG"], "a") as log:               # logged when it is started, not when it answers
        log.write(f"agent {os.getpid()} launched\n")
    time.sleep(float(os.environ.get("FAKE_AGENT_DELAY", "0")))   # a hark slow to open its port
    Serving(("127.0.0.1", port), H).serve_forever()
