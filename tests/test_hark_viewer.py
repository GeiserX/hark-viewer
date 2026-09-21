"""Run with `python3 -m unittest discover tests`. Needs ffmpeg for the chunking test, and no hark.

Nothing here touches ports 8473/8474 or ~/Recordings: every test has its own folder,
and the server tests talk to a fake hark agent on spare ports.
"""
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))
import postprocess  # noqa: E402

opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def make_call(root, parts, started=1000.0):
    """A call folder with one fake recording and one live transcript per part: [(offset, lines)]."""
    folder = root / "work" / "2026-09-21_170000"
    folder.mkdir(parents=True)
    listed = []
    for n, (offset, lines) in enumerate(parts, 1):
        audio, transcript = ("audio.opus", "transcript.json") if n == 1 else (f"audio.part{n}.opus", f"transcript.part{n}.json")
        (folder / audio).write_bytes(b"not really opus %d" % n)
        (folder / transcript).write_text(postprocess.jsonl(lines))
        listed.append({"n": n, "started": started + offset, "audio": audio, "transcript": transcript})
    meta = {"started": started, "workspace": "work", "title": ""}
    if len(listed) > 1:
        meta["parts"] = listed
    (folder / "meta.json").write_text(json.dumps(meta))
    return folder


def digest(folder):
    """Everything the job must leave alone."""
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(folder.iterdir())
            if p.name.startswith("audio") or (p.name.startswith("transcript") and "final" not in p.name and "mw" not in p.name)
            or p.name == "meta.json"}


def line(start, end, speaker, text):
    return {"start": start, "end": end, "speaker": speaker, "text": text}


class Merge(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hv-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_a_call_never_restarted_reads_as_before(self):
        folder = make_call(self.tmp, [(0, [line(1.5, 2.5, "You", "hello")])])
        self.assertNotIn("parts", json.loads((folder / "meta.json").read_text()))
        self.assertEqual(postprocess.merged_lines(folder), [line(1.5, 2.5, "You", "hello")])

    def test_later_parts_land_on_the_calls_clock(self):
        folder = make_call(self.tmp, [(0, [line(1.5, 2.5, "You", "one")]),
                                      (1173.25, [line(0.5, 3.0, "Speaker 1", "two")]),
                                      (4260, [line(10, 11, "You", "three")])])
        got = postprocess.merged_lines(folder)
        self.assertEqual([(e["start"], e["end"], e["text"]) for e in got],
                         [(1.5, 2.5, "one"), (1173.75, 1176.25, "two"), (4270, 4271, "three")])

    def test_a_half_written_last_line_is_left_for_the_next_read(self):
        folder = make_call(self.tmp, [(0, [line(1, 2, "You", "whole")])])
        with open(folder / "transcript.json", "a") as f:
            f.write('{"start": 3, "text": "ha')
        self.assertEqual([e["text"] for e in postprocess.merged_lines(folder)], ["whole"])


class Job(unittest.TestCase):
    """postprocess.py as the server runs it: a process of its own, against fake hark and mw."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hv-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.log = self.tmp / "calls.log"

    def run_job(self, folder, *args, **env):
        return subprocess.run(
            [sys.executable, str(REPO / "postprocess.py"), str(folder), *args], capture_output=True, text=True,
            env={**os.environ, "HARK_VIEWER_ROOT": str(self.tmp), "HARK_BIN": str(HERE / "fake_hark.py"),
                 "HARK_VIEWER_MW": str(HERE / "fake_mw.py"), "FAKE_LOG": str(self.log), **env})

    def calls(self, who):
        return [l for l in self.log.read_text().splitlines() if l.startswith(who + " ")] if self.log.exists() else []

    def test_two_parts_become_one_transcript_and_mw_gets_its_second_try(self):
        folder = make_call(self.tmp, [(0, []), (1200, [])])
        before = digest(folder)
        run = self.run_job(folder)
        self.assertEqual(run.returncode, 0, run.stderr)
        final = postprocess.read_lines(folder / "transcript.final.json")
        self.assertEqual(final, [line(1.0, 2.0, "Others", "audio.opus"), line(1201.0, 1202.0, "Others", "audio.part2.opus")])
        self.assertIn("--speaker-mode source --speaker-labels Microphone,Others", self.calls("hark")[0])
        status = json.loads((folder / "postprocess.json").read_text())
        self.assertEqual(status["state"], "done")
        self.assertEqual({k: v["state"] for k, v in status["steps"].items()}, {"final": "done", "mw": "done"})
        self.assertEqual(status["steps"]["final"]["skipped_spans"], [])
        self.assertIsNotNone(status["steps"]["mw"]["finished"])
        self.assertEqual(len(self.calls("mw")), 3)                       # part 1 failed once, then both worked
        text = (folder / "transcript.mw.txt").read_text()
        self.assertIn("words from audio.opus", text)
        self.assertIn("== part 2, starts 1200 s into the call ==\nSpeaker 1: words from audio.part2.opus", text)
        self.assertEqual(digest(folder), before)                         # audio and the live transcript untouched

    def test_a_second_run_does_nothing_and_force_runs_again(self):
        folder = make_call(self.tmp, [(0, [])])
        self.assertEqual(self.run_job(folder).returncode, 0)
        ran = len(self.calls("hark"))
        self.assertEqual(ran, 1)
        again = self.run_job(folder)
        self.assertEqual(again.returncode, 0)
        self.assertIn("nothing to do", again.stdout)
        self.assertEqual(len(self.calls("hark")), ran)
        self.assertEqual(self.run_job(folder, "--force").returncode, 0)
        self.assertEqual(len(self.calls("hark")), ran + 1)

    def test_macwhisper_turned_off_or_missing_is_skipped(self):
        for value, reason in (("off", "turned off"), (str(self.tmp / "no-such-mw"), "is not at")):
            folder = make_call(self.tmp / value.replace("/", "_"), [(0, [])])
            self.assertEqual(self.run_job(folder, HARK_VIEWER_MW=value).returncode, 0)
            step = json.loads((folder / "postprocess.json").read_text())["steps"]["mw"]
            self.assertEqual(step["state"], "skipped")
            self.assertIn(reason, step["error"])
            self.assertFalse((folder / "transcript.mw.txt").exists())
        self.assertEqual(self.calls("mw"), [])

    def real_audio(self, folder, seconds):
        self.assertTrue(shutil.which("ffmpeg") and shutil.which("ffprobe"), "this test needs ffmpeg and ffprobe")
        made = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                               "-t", str(seconds), "-c:a", "libopus", str(folder / "audio.opus")], capture_output=True, text=True)
        self.assertEqual(made.returncode, 0, made.stderr)

    def test_a_part_hark_refuses_is_cut_up_and_only_the_bad_piece_is_skipped(self):
        folder = make_call(self.tmp, [(0, [])])
        self.real_audio(folder, 50)
        run = self.run_job(folder, FAKE_HARK_MODE="poison", FAKE_HARK_POISON="33", HARK_VIEWER_MW="off",
                           HARK_VIEWER_CHUNK="20", HARK_VIEWER_MIN_PIECE="5")
        self.assertEqual(run.returncode, 0, run.stderr)
        status = json.loads((folder / "postprocess.json").read_text())
        self.assertEqual(status["steps"]["final"]["state"], "done")
        gaps = status["steps"]["final"]["skipped_spans"]
        self.assertEqual([(g["start"], g["end"], g["part"]) for g in gaps], [(30.0, 35.0, 1)])
        self.assertIn("300ms", gaps[0]["error"])
        # whole, [0,20) ok, [20,40) no, [20,30) ok, [30,40) no, [30,35) no -> skipped, [35,40) ok, [40,50) ok
        self.assertEqual([e["start"] for e in postprocess.read_lines(folder / "transcript.final.json")], [1.0, 21.0, 36.0, 41.0])
        self.assertEqual(len(self.calls("hark")), 8)

    def test_a_hark_that_refuses_everything_fails_the_step_and_writes_no_transcript(self):
        folder = make_call(self.tmp, [(0, [])])
        self.real_audio(folder, 50)
        run = self.run_job(folder, FAKE_HARK_MODE="fail", HARK_VIEWER_MW="off", HARK_VIEWER_CHUNK="20", HARK_VIEWER_MIN_PIECE="5")
        self.assertEqual(run.returncode, 1)
        status = json.loads((folder / "postprocess.json").read_text())
        self.assertEqual((status["state"], status["steps"]["final"]["state"]), ("failed", "failed"))
        self.assertIn("refused every piece", status["steps"]["final"]["error"])
        self.assertFalse((folder / "transcript.final.json").exists())

    def test_a_job_that_died_reads_as_failed_not_running(self):
        folder = make_call(self.tmp, [(0, [])])
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        (folder / "postprocess.json").write_text(json.dumps({"state": "running", "pid": dead.pid, "steps": {}}))
        self.assertEqual(postprocess.read_status(folder)["state"], "failed")
        (folder / "postprocess.json").write_text(json.dumps({"state": "running", "pid": os.getpid(), "steps": {}}))
        self.assertEqual(postprocess.read_status(folder)["state"], "running")


class FakeAgent:
    """hark's remote-control agent, as far as server.py uses it."""

    def __init__(self):
        agent = self
        self.session, self.seen, self.refuse_start = None, [], False

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
                self.answer(200, {"session": agent.session})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                agent.seen.append((self.path, body))
                if self.path == "/start":
                    if agent.refuse_start:
                        return self.answer(500, {"error": "no capture"})
                    if agent.session:
                        time.sleep(0.6)      # a real capture takes a moment to come up: the watcher gets to see "stopped" in between
                    Path(body["audio"]).write_bytes(b"audio")
                    agent.session = {"state": "recording", "elapsed": 0, "muted": False, "id": "X",
                                     "audio": body["audio"], "transcript": body["transcript"]}
                    return self.answer(201, {"id": "X"})
                if self.path == "/stop" and agent.session:
                    agent.session = {**agent.session, "state": "stopped"}
                self.answer(200, {})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hv-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.agent = FakeAgent()
        self.addCleanup(self.agent.httpd.shutdown)
        self.port = free_port()
        self.assertNotIn(self.port, (8473, 8474))
        self.proc = subprocess.Popen(
            [sys.executable, str(REPO / "server.py")], stderr=subprocess.PIPE,
            env={**os.environ, "HARK_VIEWER_ROOT": str(self.tmp), "HARK_VIEWER_PORT": str(self.port),
                 "HARK_REMOTE_CONTROL_PORT": str(self.agent.port), "HARK_VIEWER_WATCH": "0.1",
                 "HARK_BIN": str(HERE / "fake_hark.py"), "HARK_VIEWER_MW": "off", "FAKE_LOG": str(self.tmp / "calls.log")})
        self.addCleanup(self.proc.stderr.close)
        self.addCleanup(self.proc.wait)
        self.addCleanup(self.proc.terminate)
        self.wait_for(lambda: self.get("/api/status")[0] == 200, "the server to answer")

    def get(self, path, method="GET", body=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method,
                                     data=json.dumps(body).encode() if body is not None else (b"" if method == "POST" else None),
                                     headers={"X-Hark-Viewer": "1"})
        try:
            with opener.open(req, timeout=40) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except OSError:
            return 0, b""

    def api(self, path, method="GET", body=None):
        code, raw = self.get(path, method, body)
        return code, json.loads(raw or b"{}")

    def wait_for(self, test, what, seconds=15):
        end = time.time() + seconds
        while time.time() < end:
            if test():
                return
            time.sleep(0.05)
        self.fail(f"waited {seconds} s for {what}")

    def test_call_audio_reaches_the_page_untouched_and_is_absent_on_an_older_hark(self):
        self.assertEqual(self.api("/api/new", "POST", {"workspace": "work", "title": "t"})[0], 200)
        self.assertNotIn("callAudio", self.api("/api/status")[1]["session"])
        for report in ({"state": "dead", "silentFor": 42, "restarts": 2}, {"state": "silent", "silentFor": 7.5, "restarts": 0}):
            self.agent.session = {**self.agent.session, "callAudio": report}
            code, st = self.api("/api/status")
            self.assertTrue(st["active"])
            self.assertEqual(st["session"]["callAudio"], report)

    def test_restart_records_on_into_the_same_folder_and_does_not_end_the_call(self):
        code, made = self.api("/api/new", "POST", {"workspace": "work", "title": "sync"})
        self.assertEqual(code, 200)
        folder = Path(made["folder"])
        first = json.loads((folder / "meta.json").read_text())["started"]

        code, again = self.api("/api/restart", "POST")
        self.assertEqual((code, again["call"], again["part"]), (200, made["call"], 2))
        self.assertEqual([p for p, _ in self.agent.seen], ["/start", "/stop", "/start"])
        self.assertEqual(self.agent.seen[2][1]["audio"], str(folder / "audio.part2.opus"))
        self.assertEqual(self.agent.seen[2][1]["transcript"], str(folder / "transcript.part2.json"))
        meta = json.loads((folder / "meta.json").read_text())
        self.assertEqual(meta["started"], first)
        self.assertEqual([(p["n"], p["audio"], p["transcript"]) for p in meta["parts"]],
                         [(1, "audio.opus", "transcript.json"), (2, "audio.part2.opus", "transcript.part2.json")])
        self.assertEqual(meta["parts"][0]["started"], first)
        st = self.api("/api/status")[1]
        self.assertEqual((st["active"], st["call"], st["parts"]), (True, made["call"], 2))

        # One transcript on the call's clock, from the same URL the page already reads.
        meta["parts"][1]["started"] = first + 100
        (folder / "meta.json").write_text(json.dumps(meta))
        (folder / "transcript.json").write_text(postprocess.jsonl([line(1, 2, "You", "before")]))
        (folder / "transcript.part2.json").write_text(postprocess.jsonl([line(3, 4, "Speaker 1", "after")]))
        code, raw = self.get(f"/{made['call']}/transcript.json")
        self.assertEqual([json.loads(l) for l in raw.decode().splitlines()],
                         [line(1, 2, "You", "before"), line(103, 104, "Speaker 1", "after")])

        # The stop inside the restart was not the end of the call: many watcher ticks later, no job has run.
        time.sleep(1)
        self.assertFalse((folder / "postprocess.json").exists())

        # The real stop is. The job runs on its own and covers both parts.
        self.assertEqual(self.api("/api/stop", "POST")[0], 200)
        self.wait_for(lambda: (self.api("/api/status")[1]["postprocess"] or {}).get("state") == "done", "the final transcript")
        self.assertEqual([(e["start"], e["text"]) for e in postprocess.read_lines(folder / "transcript.final.json")],
                         [(1.0, "audio.opus"), (101.0, "audio.part2.opus")])
        self.assertEqual(self.api("/api/status")[1]["postprocess"]["steps"]["mw"]["state"], "skipped")
        time.sleep(0.5)                                                  # and it ran once
        log = (self.tmp / "calls.log").read_text().splitlines()
        self.assertEqual(len(log), 2, log)

    def test_restart_with_nothing_recording_is_refused(self):
        code, body = self.api("/api/restart", "POST")
        self.assertEqual(code, 409)
        self.assertEqual(self.agent.seen, [])

    def test_a_second_call_while_one_records_is_still_refused(self):
        self.assertEqual(self.api("/api/new", "POST", {"workspace": "work", "title": ""})[0], 200)
        self.assertEqual(self.api("/api/new", "POST", {"workspace": "work", "title": ""})[0], 409)


if __name__ == "__main__":
    unittest.main()
