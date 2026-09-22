#!/usr/bin/env python3
"""hark's remote-control agent, as far as server.py uses it, with hark's real stop.

/stop answers `stopped` at once. The capture goes on writing the audio for FAKE_FINISH
seconds after that, /status does not show it, and until it is over /start answers 409
"still finishing". With `wedged` set it never is over, and after FAKE_STOP_TIMEOUT the
session reads `failed`, as hark's own watchdog does it. Only a new agent gets out of that.

`capturing` is true unless FAKE_CAPTURING says otherwise, and it rides on both the start's
answer and every session: hark gives up waiting for its own capture at 60 s and then says
`recording` with `capturing: false`, which is a start that recorded nothing.

`stop_answer` makes /stop answer that code and change nothing, which is hark refusing to stop.

POST /_fake {"session": {...}, "finish": s, "wedged": bool, "capturing": bool, "stop_answer": code}
changes it from a test. Every request is a line in FAKE_LOG: `agent <pid> <path> <json body>`.
"""
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

S = {"session": None, "finish": float(os.environ.get("FAKE_FINISH", "0")), "wedged": False, "writing_until": 0.0,
     "stop_timeout": float(os.environ.get("FAKE_STOP_TIMEOUT", "10")),
     "capturing": os.environ.get("FAKE_CAPTURING", "1") not in ("0", "false", "no"),
     "stop_answer": int(os.environ.get("FAKE_STOP_ANSWER", "0"))}


def finishing():
    return S["wedged"] and S["writing_until"] or time.time() < S["writing_until"]


def keep_writing(audio, stopped_at):
    while finishing():
        with open(audio, "ab") as f:
            f.write(b"tail")
        if S["wedged"] and time.time() - stopped_at > S["stop_timeout"] and S["session"]["state"] == "stopped":
            S["session"] = {**S["session"], "state": "failed", "error": "capture did not finish"}
        time.sleep(0.1)


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
        session = S["session"] and {**S["session"], "capturing": S["capturing"]}
        self.answer(200, {"session": session})

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
            if live:
                return self.answer(409, {"error": "a recording is already active"})
            if finishing():
                return self.answer(409, {"error": "the previous capture is still finishing and no new recording can start; "
                                                  "check GET /status and restart the agent if it stays wedged"})
            Path(body["audio"]).write_bytes(b"audio")
            S["session"] = {"state": "recording", "elapsed": 0, "muted": False, "id": "X",
                            "audio": body["audio"], "transcript": body["transcript"]}
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
    port = int(sys.argv[sys.argv.index("--remote-control") + 1])
    with open(os.environ["FAKE_LOG"], "a") as log:
        log.write(f"agent {os.getpid()} launched\n")
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
