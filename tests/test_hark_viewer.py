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


EN = ["Good evening everyone, the repository migration finished today and the pull request is ready for review.",
      "I also updated the setup guide, and the automatic merge is now enabled for the template.",
      "We will look at the remaining details of the project tomorrow with the whole team.",
      "The token service is the last one left, and its infrastructure lives in the application repository."]
ES = ["Hola a todos, hoy terminamos la migración del repositorio y la solicitud está lista para revisión.",
      "También actualicé la guía de configuración y la fusión automática ya está activada."]


class Job(unittest.TestCase):
    """postprocess.py as the server runs it: a process of its own, against fake hark and mw."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hv-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.log = self.tmp / "calls.log"
        self.scratch = self.tmp / "scratch"
        self.scratch.mkdir()

    def run_job(self, folder, *args, **env):
        return subprocess.run(
            [sys.executable, str(REPO / "postprocess.py"), str(folder), *args], capture_output=True, text=True,
            env={**os.environ, "HARK_VIEWER_ROOT": str(self.tmp), "HARK_BIN": str(HERE / "fake_hark.py"),
                 "HARK_VIEWER_MW": str(HERE / "fake_mw.py"), "FAKE_LOG": str(self.log), "HARK_VIEWER_SETTLE": "0.3",
                 "TMPDIR": str(self.scratch), **env})

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
        self.assertEqual({k: v["state"] for k, v in status["steps"].items()},
                         {"final": "done", "languages": "done", "mw": "done"})
        self.assertEqual(status["steps"]["final"]["skipped_spans"], [])
        self.assertIsNotNone(status["steps"]["mw"]["finished"])
        self.assertEqual(len(self.calls("mw")), 3)                       # part 1 failed once, then both worked
        text = (folder / "transcript.mw.txt").read_text()
        self.assertIn("words from audio.opus", text)
        self.assertIn("== part 2, starts 1200 s into the call ==\nSpeaker 1: words from audio.part2.opus", text)
        self.assertEqual(digest(folder), before)                         # audio and the live transcript untouched

    def test_a_python_without_the_recognizer_fails_only_the_language_step(self):
        folder = make_call(self.tmp, [(0, [])])
        run = self.run_job(folder, HARK_VIEWER_PY3=sys.executable,    # this interpreter has no PyObjC bridge
                           FAKE_HARK_TEXT=EN[0])                      # a line long enough to be worth judging
        self.assertEqual(run.returncode, 0, run.stderr)              # the accurate transcript is the point, not this
        status = json.loads((folder / "postprocess.json").read_text())
        self.assertEqual(status["state"], "done")
        self.assertEqual(status["steps"]["languages"]["state"], "failed")
        self.assertIn("objc", status["steps"]["languages"]["error"])
        self.assertEqual(status["steps"]["final"]["state"], "done")
        self.assertEqual(status["steps"]["mw"]["state"], "done")

    def test_no_interpreter_for_the_recognizer_skips_the_language_step(self):
        folder = make_call(self.tmp, [(0, [])])
        run = self.run_job(folder, HARK_VIEWER_PY3=str(self.tmp / "no-such-python"), FAKE_HARK_TEXT=EN[0])
        self.assertEqual(run.returncode, 0, run.stderr)
        step = json.loads((folder / "postprocess.json").read_text())["steps"]["languages"]
        self.assertEqual(step["state"], "skipped")
        self.assertIn("no interpreter at", step["error"])

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
        zombie = subprocess.Popen([sys.executable, "-c", "pass"])          # never waited for, as a server that does not reap
        self.addCleanup(zombie.wait)
        time.sleep(0.5)
        (folder / "postprocess.json").write_text(json.dumps({"state": "running", "pid": zombie.pid, "steps": {}}))
        self.assertEqual(postprocess.read_status(folder)["state"], "failed")
        mine = {"state": "running", "pid": os.getpid(), "started": time.time(), "steps": {}}
        (folder / "postprocess.json").write_text(json.dumps(mine))
        self.assertEqual(postprocess.read_status(folder)["state"], "running")
        # the same pid, but the job that wrote this started long before this process was born: a reused pid
        (folder / "postprocess.json").write_text(json.dumps({**mine, "started": time.time() - 86400}))
        self.assertEqual(postprocess.read_status(folder)["state"], "failed")

    def keep_writing(self, audio, seconds):
        """hark, still writing the audio after it said stopped."""
        stop = time.time() + seconds

        def capture():
            while time.time() < stop:
                with open(audio, "ab") as f:
                    f.write(b"tail")
                time.sleep(0.05)
        writer = threading.Thread(target=capture)
        writer.start()
        self.addCleanup(writer.join)

    def test_the_job_waits_until_hark_has_finished_writing_the_audio(self):
        folder = make_call(self.tmp, [(0, [])])
        self.keep_writing(folder / "audio.opus", 1.5)
        self.assertEqual(self.run_job(folder, HARK_VIEWER_SETTLE="0.5", HARK_VIEWER_MW="off").returncode, 0)
        status = json.loads((folder / "postprocess.json").read_text())
        self.assertGreaterEqual(status["steps"]["final"]["started"], (folder / "audio.opus").stat().st_mtime + 0.5)
        self.assertFalse(status["settled"]["capped"])
        self.assertGreaterEqual(status["settled"]["waited"], 1.4)

    def test_the_wait_for_the_audio_has_a_cap_and_says_when_it_hit_it(self):
        folder = make_call(self.tmp, [(0, [])])
        self.keep_writing(folder / "audio.opus", 3)
        run = self.run_job(folder, HARK_VIEWER_SETTLE="0.5", HARK_VIEWER_SETTLE_CAP="1", HARK_VIEWER_MW="off")
        self.assertEqual(run.returncode, 0)
        self.assertTrue(json.loads((folder / "postprocess.json").read_text())["settled"]["capped"])

    def popen_job(self, folder, sleep, **kw):
        return subprocess.Popen(
            [sys.executable, str(REPO / "postprocess.py"), str(folder)], stderr=subprocess.DEVNULL, **kw,
            env={**os.environ, "HARK_BIN": str(HERE / "fake_hark.py"), "HARK_VIEWER_MW": "off", "FAKE_LOG": str(self.log),
                 "HARK_VIEWER_SETTLE": "0.3", "TMPDIR": str(self.scratch), "FAKE_HARK_SLEEP": sleep})

    def start_slow_job(self, folder):
        job = self.popen_job(folder, "30")
        self.addCleanup(job.wait)
        self.addCleanup(job.kill)
        end = time.time() + 15
        while time.time() < end and not self.calls("hark"):
            time.sleep(0.05)
        self.assertTrue(self.calls("hark"), "the job never reached hark")
        return job

    def test_a_terminated_job_cleans_up_and_says_it_failed(self):
        folder = make_call(self.tmp, [(0, [])])
        job = self.start_slow_job(folder)
        self.assertEqual(len(list(self.scratch.glob("hark-viewer-job-*"))), 1)
        job.terminate()
        job.wait(15)
        self.assertEqual(list(self.scratch.glob("hark-viewer-job-*")), [])
        status = json.loads((folder / "postprocess.json").read_text())
        self.assertEqual(status["state"], "failed")
        self.assertIn("terminated", status["error"])

    def test_a_killed_job_reads_as_failed_can_be_forced_and_its_scratch_is_swept(self):
        folder = make_call(self.tmp, [(0, [])])
        job = self.start_slow_job(folder)
        job.kill()
        job.wait(15)
        left = list(self.scratch.glob("hark-viewer-job-*"))
        self.assertEqual(len(left), 1)
        self.assertEqual(postprocess.read_status(folder)["state"], "failed")
        self.assertEqual(self.run_job(folder, "--force", HARK_VIEWER_MW="off").returncode, 0)
        self.assertEqual(postprocess.read_status(folder)["state"], "done")
        self.assertFalse(left[0].exists())

    def test_the_status_file_is_never_empty_and_only_one_of_four_racing_jobs_runs(self):
        folder = make_call(self.tmp, [(0, [])])
        jobs = [self.popen_job(folder, "1", stdout=subprocess.DEVNULL) for _ in range(4)]
        seen = set()
        while any(j.poll() is None for j in jobs):
            try:
                seen.add(bool((folder / "postprocess.json").read_text().strip()))
            except OSError:
                pass
        self.assertEqual(seen, {True})
        self.assertEqual(len(self.calls("hark")), 1)
        self.assertEqual([p.name for p in folder.glob("postprocess.json.*")], [])


def can_recognize():
    if postprocess.languages_skip():
        return False
    try:
        return bool(postprocess.recognize([EN[0]])[0][0])
    except RuntimeError:
        return False


@unittest.skipUnless(can_recognize(), "needs the system python and Apple's language recognizer")
class Languages(unittest.TestCase):
    """Which languages a call was in, judged by Apple's on-device recognizer over the real text."""

    def verdict(self, texts):
        return postprocess.language_verdict([line(float(n), n + 1.0, "Others", t) for n, t in enumerate(texts)])

    def test_a_call_in_one_language_is_not_mixed(self):
        got = self.verdict(EN)
        self.assertEqual(got["dominant"], "en")
        self.assertEqual(got["present"], ["en"])
        self.assertFalse(got["mixed"])
        self.assertEqual(got["other"], [])
        self.assertEqual(got["judged"], len(EN))

    def test_a_call_with_both_languages_is_mixed_and_says_where(self):
        got = self.verdict(EN + ES)
        self.assertEqual(got["dominant"], "en")
        self.assertEqual(sorted(got["present"]), ["en", "es"])
        self.assertTrue(got["mixed"])
        self.assertEqual([e["language"] for e in got["other"]], ["es", "es"])
        self.assertEqual(got["other"][0]["start"], float(len(EN)))      # the first Spanish line, at its own time
        self.assertIn("es", postprocess.say_verdict(got))

    def test_one_stray_line_does_not_make_a_long_call_mixed(self):
        got = self.verdict(EN * 8 + ES[:1])
        self.assertEqual(got["present"], ["en"])
        self.assertFalse(got["mixed"])
        self.assertEqual(got["other_lines"], 1)                        # still reported, just not enough to call it
        self.assertIn("stray line", postprocess.say_verdict(got))

    def test_a_line_too_short_to_judge_is_left_out(self):
        got = self.verdict(["Mm-hmm.", "Cool.", "All right."] + EN)
        self.assertEqual(got["lines"], len(EN) + 3)
        self.assertEqual(got["judged"], len(EN))

    def test_a_call_with_nothing_long_enough_to_judge_says_so(self):
        got = self.verdict(["Mm-hmm.", "Cool."])
        self.assertIsNone(got["dominant"])
        self.assertEqual(got["present"], [])
        self.assertFalse(got["mixed"])
        self.assertIn("long enough", postprocess.say_verdict(got))


class LanguageSource(unittest.TestCase):
    """The accurate transcript is read when it is there, the live one when it is not."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hv-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_the_live_transcript_is_read_until_the_accurate_one_exists(self):
        folder = make_call(self.tmp, [(0, [line(1.0, 2.0, "Speaker 1", "the live text of this call")])])
        lines, source = postprocess.language_lines(folder)
        self.assertEqual(source, "transcript.json")
        self.assertEqual([e["text"] for e in lines], ["the live text of this call"])
        (folder / "transcript.final.json").write_text(postprocess.jsonl([line(1.0, 2.0, "Others", "the accurate text")]))
        lines, source = postprocess.language_lines(folder)
        self.assertEqual(source, "transcript.final.json")
        self.assertEqual([e["text"] for e in lines], ["the accurate text"])


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ServerCase(unittest.TestCase):
    """server.py on spare ports. It starts the fake agent itself, the way it starts hark."""
    WATCH = "0.1"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hv-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.log = self.tmp / "calls.log"
        self.port, self.agent_port = free_port(), free_port()
        self.assertFalse({self.port, self.agent_port} & {8473, 8474})
        self.proc = subprocess.Popen(
            [sys.executable, str(REPO / "server.py")], stderr=subprocess.DEVNULL,
            env={**os.environ, "HARK_VIEWER_ROOT": str(self.tmp), "HARK_VIEWER_PORT": str(self.port),
                 "HARK_REMOTE_CONTROL_PORT": str(self.agent_port), "HARK_VIEWER_WATCH": self.WATCH,
                 "HARK_VIEWER_STOP_WAIT": "8", "HARK_VIEWER_SETTLE": "1", "FAKE_STOP_TIMEOUT": "1",
                 "HARK_BIN": str(HERE / "fake_hark.py"), "HARK_VIEWER_MW": "off", "FAKE_LOG": str(self.log)})
        self.addCleanup(self.kill_agent)
        self.addCleanup(self.proc.wait)
        self.addCleanup(self.proc.terminate)
        self.wait_for(lambda: self.get("/api/status")[0] == 200, "the server to answer")

    def kill_agent(self):
        run = subprocess.run(["lsof", "-nP", "-t", f"-iTCP:{self.agent_port}", "-sTCP:LISTEN"], capture_output=True, text=True)
        for pid in run.stdout.split():
            os.kill(int(pid), 9)

    def get(self, path, method="GET", body=None, port=None):
        req = urllib.request.Request(f"http://127.0.0.1:{port or self.port}{path}", method=method,
                                     data=json.dumps(body).encode() if body is not None else (b"" if method == "POST" else None),
                                     headers={"X-Hark-Viewer": "1"})
        try:
            with opener.open(req, timeout=60) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except OSError:
            return 0, b""

    def api(self, path, method="GET", body=None):
        code, raw = self.get(path, method, body)
        return code, json.loads(raw or b"{}")

    def fake(self, **settings):
        self.assertEqual(self.get("/_fake", "POST", settings, self.agent_port)[0], 200)

    def seen(self, who="agent"):
        """What the fake agent was asked, as [(path, body)]. For the fake hark, its raw log lines."""
        lines = [l for l in self.log.read_text().splitlines() if l.startswith(who + " ")] if self.log.exists() else []
        if who != "agent":
            return lines
        return [(l.split(" ", 3)[2], json.loads(l.split(" ", 3)[3])) for l in lines if not l.endswith(" launched")]

    def launches(self):
        return [l.split()[1] for l in self.log.read_text().splitlines() if l.endswith(" launched")]

    def wait_for(self, test, what, seconds=20):
        end = time.time() + seconds
        while time.time() < end:
            if test():
                return
            time.sleep(0.05)
        self.fail(f"waited {seconds} s for {what}")

    def new(self, title=""):
        code, made = self.api("/api/new", "POST", {"workspace": "work", "title": title})
        self.assertEqual(code, 200, made)
        return made, Path(made["folder"])


class Server(ServerCase):
    def test_call_audio_reaches_the_page_untouched_and_is_absent_on_an_older_hark(self):
        self.new()
        self.assertNotIn("callAudio", self.api("/api/status")[1]["session"])
        for report in ({"state": "dead", "silentFor": 42, "restarts": 2}, {"state": "silent", "silentFor": 7.5, "restarts": 0}):
            self.fake(session={"callAudio": report})
            code, st = self.api("/api/status")
            self.assertTrue(st["active"])
            self.assertEqual(st["session"]["callAudio"], report)

    def test_restart_records_on_into_the_same_folder_and_does_not_end_the_call(self):
        # hark as it is: `stopped` at once, the capture finishing for 5 s behind it, /start refused meanwhile.
        self.fake(finish=5)
        made, folder = self.new("sync")
        first = json.loads((folder / "meta.json").read_text())["started"]

        code, again = self.api("/api/restart", "POST")
        self.assertEqual((code, again.get("call"), again.get("part")), (200, made["call"], 2), again)
        asked = self.seen()
        self.assertEqual([p for p, _ in asked[:2]], ["/start", "/stop"])
        self.assertGreater(len(asked), 3)                                # it was refused while finishing, and kept asking
        self.assertEqual({p for p, _ in asked[2:]}, {"/start"})
        self.assertEqual(asked[-1][1]["audio"], str(folder / "audio.part2.opus"))
        self.assertEqual(asked[-1][1]["transcript"], str(folder / "transcript.part2.json"))
        self.assertEqual(len(self.launches()), 1)                        # patience was enough, the agent was left alone
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

        # The real stop is. The job waits for the capture to finish writing, then covers both parts, once.
        self.fake(finish=2)
        self.assertEqual(self.api("/api/stop", "POST")[0], 200)
        self.wait_for(lambda: (self.api("/api/status")[1]["postprocess"] or {}).get("state") == "done", "the final transcript")
        status = self.api("/api/status")[1]["postprocess"]
        self.assertGreaterEqual(status["steps"]["final"]["started"], (folder / "audio.part2.opus").stat().st_mtime + 1)
        self.assertEqual([(e["start"], e["text"]) for e in postprocess.read_lines(folder / "transcript.final.json")],
                         [(1.0, "audio.opus"), (101.0, "audio.part2.opus")])
        self.assertEqual(status["steps"]["mw"]["state"], "skipped")
        time.sleep(0.5)
        self.assertEqual(len(self.seen("hark")), 2, self.seen("hark"))

    def test_a_wedged_capture_gets_a_new_agent_and_the_call_goes_on(self):
        self.fake(wedged=True, finish=1)
        made, folder = self.new()
        code, again = self.api("/api/restart", "POST")
        self.assertEqual((code, again.get("part")), (200, 2), again)
        self.assertEqual(len(set(self.launches())), 2)                   # hark refuses every start once wedged: only a new agent helps
        st = self.api("/api/status")[1]
        self.assertEqual((st["active"], st["call"], st["parts"]), (True, made["call"], 2))

    def test_a_bystander_listening_on_the_same_port_survives_the_relaunch(self):
        # The shape of an ssh -L or a VM forward: another process, same port, the IPv6 loopback.
        bystander = subprocess.Popen([sys.executable, "-c", "import socket, sys, time; s = socket.socket(socket.AF_INET6); "
                                      "s.bind(('::1', int(sys.argv[1]))); s.listen(); print('up', flush=True); time.sleep(120)",
                                      str(self.agent_port)], stdout=subprocess.PIPE, text=True)
        self.addCleanup(bystander.wait)
        self.addCleanup(bystander.kill)
        self.addCleanup(bystander.stdout.close)
        self.assertEqual(bystander.stdout.readline().strip(), "up")
        self.fake(wedged=True, finish=1)
        self.new()
        code, again = self.api("/api/restart", "POST")
        self.assertEqual((code, again.get("part")), (200, 2), again)
        self.assertEqual(len(set(self.launches())), 2)                   # the wedged agent was replaced
        self.assertIsNone(bystander.poll(), "the relaunch killed a process that is not a hark agent")

    def test_a_call_last_recorded_over_an_hour_ago_is_restarted_only_with_force(self):
        made, folder = self.new()
        self.kill_agent()
        self.wait_for(lambda: not self.api("/api/status")[1]["agent"], "the agent to be gone")
        old = time.time() - 2 * 3600
        os.utime(folder / "audio.opus", (old, old))
        code, body = self.api("/api/restart", "POST")
        self.assertEqual(code, 409)
        self.assertIn("more than an hour ago", body["error"])
        self.assertIn("--force", body["error"])
        self.assertFalse((folder / "audio.part2.opus").exists())
        code, again = self.api("/api/restart", "POST", {"force": True})
        self.assertEqual((code, again.get("call"), again.get("part")), (200, made["call"], 2), again)

    def test_restart_brings_back_an_agent_that_died_and_goes_on_in_the_current_call(self):
        made, folder = self.new()
        self.kill_agent()
        self.wait_for(lambda: not self.api("/api/status")[1]["agent"], "the agent to be gone")
        code, again = self.api("/api/restart", "POST")
        self.assertEqual((code, again.get("call"), again.get("part")), (200, made["call"], 2), again)
        self.assertTrue((folder / "audio.part2.opus").exists())

    def test_restart_after_hark_reports_the_session_failed(self):
        made, folder = self.new()
        self.fake(session={"state": "failed", "error": "captured no audio"})
        code, again = self.api("/api/restart", "POST")
        self.assertEqual((code, again.get("call"), again.get("part")), (200, made["call"], 2), again)

    def test_restart_of_a_call_with_no_start_time_does_not_stack_part_2_on_part_1(self):
        made, folder = self.new()
        time.sleep(1.2)
        (folder / "meta.json").write_text(json.dumps({"workspace": "work", "title": ""}))
        self.assertEqual(self.api("/api/restart", "POST")[0], 200)
        meta = json.loads((folder / "meta.json").read_text())
        self.assertAlmostEqual(meta["started"], (folder / "audio.opus").stat().st_birthtime, delta=0.01)
        self.assertGreater(postprocess.offset_of(meta["parts"][1], meta), 1.0)

    def test_restart_with_nothing_recording_is_refused(self):
        code, body = self.api("/api/restart", "POST")
        self.assertEqual(code, 409)
        self.assertEqual(self.seen(), [])

    def test_a_finished_call_is_not_restarted(self):
        made, folder = self.new()
        self.assertEqual(self.api("/api/stop", "POST")[0], 200)
        self.wait_for(lambda: (folder / "postprocess.json").exists(), "the job")
        self.assertEqual(self.api("/api/restart", "POST")[0], 409)
        self.kill_agent()                                                # and a new agent, which knows no session, changes nothing
        self.wait_for(lambda: not self.api("/api/status")[1]["agent"], "the agent to be gone")
        self.assertEqual(self.api("/api/restart", "POST")[0], 409)
        self.assertFalse((folder / "audio.part2.opus").exists())

    def test_a_second_call_while_one_records_is_still_refused(self):
        self.new()
        self.assertEqual(self.api("/api/new", "POST", {"workspace": "work", "title": ""})[0], 409)

    def test_a_job_the_server_started_and_someone_killed_reads_as_failed(self):
        made, folder = self.new()
        self.fake(finish=3)                                              # keeps the job waiting on the audio, so it is there to kill
        self.assertEqual(self.api("/api/stop", "POST")[0], 200)
        self.wait_for(lambda: (folder / "postprocess.json").exists(), "the job")
        pid = json.loads((folder / "postprocess.json").read_text())["pid"]
        os.kill(pid, 9)                                                  # a child of the server, which has to reap it
        self.wait_for(lambda: subprocess.run(["ps", "-p", str(pid)], capture_output=True).returncode != 0, "the zombie to go", 10)
        self.wait_for(lambda: self.api("/api/status")[1]["postprocess"]["state"] == "failed", "the dead job to read as failed", 10)


class SlowWatcher(ServerCase):
    WATCH = "60"

    def test_a_call_that_ended_inside_the_watchers_tick_still_gets_its_transcript(self):
        first, folder = self.new("one")
        self.assertEqual(self.api("/api/stop", "POST")[0], 200)
        second, _ = self.new("two")
        self.assertNotEqual(first["call"], second["call"])
        self.wait_for(lambda: (postprocess.read_status(folder) or {}).get("state") == "done", "the first call's transcript")


if __name__ == "__main__":
    unittest.main()
