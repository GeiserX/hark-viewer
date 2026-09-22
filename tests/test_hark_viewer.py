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
import server  # noqa: E402

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


def real_audio(case, folder, seconds):
    """A real stereo Opus file, for anything that runs ffprobe or ffmpeg over a recording."""
    case.assertTrue(shutil.which("ffmpeg") and shutil.which("ffprobe"), "this test needs ffmpeg and ffprobe")
    made = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                           "-t", str(seconds), "-c:a", "libopus", str(folder / "audio.opus")], capture_output=True, text=True)
    case.assertEqual(made.returncode, 0, made.stderr)


def keep_writing(case, audio, seconds):
    """hark, still writing the audio after it said stopped."""
    stop = time.time() + seconds

    def capture():
        while time.time() < stop:
            with open(audio, "ab") as f:
                f.write(b"tail")
            time.sleep(0.05)
    writer = threading.Thread(target=capture)
    writer.start()
    case.addCleanup(writer.join)


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

    def test_an_accurate_pass_that_heard_only_the_microphone_warns_and_still_succeeds(self):
        """On stock hark 0.4.3 the offline pass reads channel 0 and loses the call side, and it
        failed silently: every line came back as the microphone and the step read `done`."""
        folder = make_call(self.tmp, [(0, [])])
        run = self.run_job(folder, FAKE_HARK_SPEAKER="Microphone", HARK_VIEWER_MW="off")
        self.assertEqual(run.returncode, 0, run.stderr)      # the lines are real, they are just half the call
        step = json.loads((folder / "postprocess.json").read_text())["steps"]["final"]
        self.assertEqual(step["state"], "done")
        self.assertIn("every line is Microphone", step["warning"])
        self.assertIn("--speaker-mode source", step["warning"])

    def test_a_pass_that_heard_the_call_side_carries_no_warning(self):
        folder = make_call(self.tmp, [(0, [])])
        self.assertEqual(self.run_job(folder, HARK_VIEWER_MW="off").returncode, 0)
        step = json.loads((folder / "postprocess.json").read_text())["steps"]["final"]
        self.assertEqual(step["state"], "done")
        self.assertIsNone(step["warning"])

    def test_a_python_without_the_recognizer_fails_only_the_language_step(self):
        folder = make_call(self.tmp, [(0, [])])
        # Not this interpreter: whether it carries the bridge decides the test, and
        # /usr/bin/python3 does carry it, so the step would succeed and the premise would be gone.
        no_bridge = self.tmp / "python3-without-pyobjc"
        no_bridge.write_text("#!/bin/sh\necho \"ModuleNotFoundError: No module named 'objc'\" >&2\nexit 1\n")
        no_bridge.chmod(0o755)
        run = self.run_job(folder, HARK_VIEWER_PY3=str(no_bridge),
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
        real_audio(self, folder, seconds)

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

    def test_a_hark_that_never_finishes_is_killed_and_fails_the_step(self):
        """Not one subprocess used to carry a timeout, so a hung hark, mw or ffmpeg pinned the job
        and read_status went on reporting `running` for ever, because the pid was still alive."""
        folder = make_call(self.tmp, [(0, [])])
        self.real_audio(folder, 5)
        run = self.run_job(folder, FAKE_HARK_SLEEP="20", HARK_VIEWER_TOOL_TIMEOUT="0.5", HARK_VIEWER_MW="off")
        self.assertEqual(run.returncode, 1)
        step = json.loads((folder / "postprocess.json").read_text())["steps"]["final"]
        self.assertEqual(step["state"], "failed")
        self.assertIn("without finishing and was killed", step["error"])
        self.assertFalse((folder / "transcript.final.json").exists())

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
        keep_writing(self, audio, seconds)

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
            time.sleep(0.005)       # not a bare busy loop: on one core it starved its own four jobs
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

    def test_a_long_line_it_cannot_call_still_counts(self):
        """A line long enough to read counts even when the recogniser will not name it.

        Counting only the named lines shrinks the denominator, and a tenth of a smaller
        number is easier to reach, so one Spanish line in ten named lines used to be
        enough to call the call mixed while the same line in eleven read lines is not.
        """
        unreadable = "asdf qwer zxcv hjkl poiu lkjh mnbv tyui ghjk"      # scores 0.29, under the floor
        self.assertLess(postprocess.recognize([unreadable])[0][1], postprocess.LANG_MIN_CONFIDENCE)
        got = self.verdict((EN * 3)[:9] + [unreadable] + ES[:1])
        self.assertEqual(got["judged"], 11)                              # 9 English, the unreadable one, 1 Spanish
        self.assertEqual(got["present"], ["en"])
        self.assertFalse(got["mixed"])                                   # 1 of 11 is under a tenth
        self.assertEqual(got["other_lines"], 1)                          # the Spanish line is still reported
        # The unreadable line is in no share, so they cannot total 1, and every other line was
        # named. An exact figure here would be a real recognizer score, and would move with macOS.
        self.assertLess(sum(got["shares"].values()), 1.0)
        self.assertGreater(sum(got["shares"].values()), 0.8)

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


class Alive(unittest.TestCase):
    """Whether a pid is still the job that wrote a call's status file."""

    def test_the_elapsed_time_ps_reports_reads_as_seconds(self):
        """The age used to come from `ps -o lstart` through the local clock, which is an hour out
        for an hour after a DST fall-back: a running job read as failed, and --force then started a
        second job over the same call. Elapsed time needs no timezone."""
        for etime, seconds in (("        0:01", 1), ("05:06", 306), ("1:02:03", 3723),
                               ("2-03:04:05", 183845), ("  12-00:00:00  ", 1036800)):
            self.assertEqual(postprocess.elapsed_seconds(etime), seconds, etime)

    def test_this_process_is_alive_and_the_same_pid_with_an_older_start_is_not(self):
        self.assertTrue(postprocess.alive(os.getpid(), time.time()))
        self.assertFalse(postprocess.alive(os.getpid(), time.time() - 86400))
        self.assertFalse(postprocess.alive(2 ** 30))          # no such pid
        self.assertFalse(postprocess.alive("not a pid"))


class Relabel(unittest.TestCase):
    """relabel_speakers.py, driven with saved spans, so it needs neither hark nor ffmpeg."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hv-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def spans(self, *triples):
        path = self.tmp / "spans.json"
        path.write_text(json.dumps([{"start": s, "end": e, "speaker": who} for s, e, who in triples]))
        return str(path)

    def run_relabel(self, folder, *args, **env):
        return subprocess.run([sys.executable, str(REPO / "relabel_speakers.py"), str(folder), *args],
                              capture_output=True, text=True,
                              env={**os.environ, "HARK_VIEWER_ROOT": str(self.tmp),
                                   "FAKE_LOG": str(self.tmp / "calls.log"), **env})

    def run_diarizer(self, folder, *args, **env):
        """relabel's own path: ffprobe, the channel split, a diarizer pass and the cache."""
        return self.run_relabel(folder, "--hark", str(HERE / "fake_diarizer.py"), *args, **env)

    def test_spans_from_another_recording_are_refused_and_nothing_is_written(self):
        """The coverage gate is the only thing between spans that belong to a different recording
        and a transcript the page serves in place of the live one."""
        folder = make_call(self.tmp, [(0, [line(1, 2, "You", "mine"), line(3, 4, "Speaker 1", "Ada"),
                                          line(5, 6, "Speaker 1", "Bo")])])
        live = (folder / "transcript.json").read_bytes()
        run = self.run_relabel(folder, "--spans", self.spans((9000, 9001, "Ada")))
        self.assertEqual(run.returncode, 3, run.stdout + run.stderr)
        self.assertIn("below --min-coverage", run.stderr)
        self.assertFalse((folder / "transcript.speakers.json").exists())
        self.assertEqual((folder / "transcript.json").read_bytes(), live)

    def test_matching_spans_relabel_the_other_voices_and_leave_the_microphone_alone(self):
        folder = make_call(self.tmp, [(0, [line(1, 2, "You", "mine"), line(3, 4, "Speaker 1", "Ada"),
                                          line(5, 6, "Speaker 1", "Bo")])])
        live = (folder / "transcript.json").read_bytes()
        run = self.run_relabel(folder, "--spans", self.spans((2.5, 4.5, "Ada"), (4.6, 7, "Bo")))
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual([e["speaker"] for e in postprocess.read_lines(folder / "transcript.speakers.json")],
                         ["You", "Ada", "Bo"])
        self.assertEqual([e["text"] for e in postprocess.read_lines(folder / "transcript.speakers.json")],
                         ["mine", "Ada", "Bo"])
        self.assertEqual((folder / "transcript.json").read_bytes(), live)

    def test_a_relabel_run_straight_after_stop_waits_for_the_audio(self):
        """hark answers `stopped` before its capture has finished writing, so a relabel with no
        settle diarized a file that was still growing."""
        folder = make_call(self.tmp, [(0, [line(1, 2, "Speaker 1", "Ada")])])
        real_audio(self, folder, 2)
        keep_writing(self, folder / "audio.opus", 1.5)
        run = self.run_diarizer(folder, HARK_VIEWER_SETTLE="0.5")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("for audio.opus to stop changing", run.stdout)
        # The diarizer ran only after the writing stopped, so it read the whole recording.
        log = (self.tmp / "calls.log").read_text()
        self.assertIn("diarizer", log)
        self.assertEqual([e["speaker"] for e in postprocess.read_lines(folder / "transcript.speakers.json")], ["Ada"])
        self.assertEqual(json.loads((folder / "speakers.json").read_text())["channel"], 1)   # the call side, not the mic

    def test_a_dry_run_writes_nothing(self):
        folder = make_call(self.tmp, [(0, [line(3, 4, "Speaker 1", "Ada")])])
        run = self.run_relabel(folder, "--spans", self.spans((2.5, 4.5, "Ada")), "--dry-run")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("nothing written", run.stdout)
        self.assertFalse((folder / "transcript.speakers.json").exists())
        self.assertFalse((folder / "speakers.json").exists())


class BetterOf(unittest.TestCase):
    """The server serves transcript.speakers.json in place of the live file, but only while it is
    the newer of the two: a line hark appends after a relabel still has to reach the page."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hv-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.folder = make_call(self.tmp, [(0, [line(1, 2, "Speaker 1", "live")])])
        self.live = self.folder / "transcript.json"
        self.better = self.folder / "transcript.speakers.json"

    def relabelled(self, when):
        self.better.write_text(postprocess.jsonl([line(1, 2, "Ada", "live")]))
        os.utime(self.better, (when, when))

    def test_the_relabelled_file_wins_only_while_it_is_the_newer(self):
        self.assertEqual(server.better_of(self.live), self.live)          # nothing has relabelled this call
        made = self.live.stat().st_mtime
        self.relabelled(made + 10)
        self.assertEqual(server.better_of(self.live), self.better)
        os.utime(self.live, (made + 20, made + 20))                       # hark appended a line after the relabel
        self.assertEqual(server.better_of(self.live), self.live)

    def test_only_the_live_transcript_is_ever_swapped(self):
        self.relabelled(self.live.stat().st_mtime + 10)
        part2 = self.folder / "transcript.part2.json"
        self.assertEqual(server.better_of(part2), part2)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class HarkClient(unittest.TestCase):
    """What hark() makes of something that is not hark's agent holding hark's port."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hv-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def listener(self, reply):
        """A socket that answers one request with these exact bytes, whatever was asked."""
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        self.addCleanup(sock.close)

        def serve():
            try:
                conn, _ = sock.accept()
            except OSError:
                return
            with conn:
                conn.recv(4096)
                conn.sendall(reply)
        threading.Thread(target=serve, daemon=True).start()
        return sock.getsockname()[1]

    def ask(self, port):
        run = subprocess.run(
            [sys.executable, "-c", "import json, server; print(json.dumps(server.hark('GET', '/status')))"],
            cwd=str(REPO), capture_output=True, text=True,
            env={**os.environ, "HARK_REMOTE_CONTROL_PORT": str(port), "HARK_VIEWER_ROOT": str(self.tmp)})
        self.assertEqual(run.returncode, 0, run.stderr)      # it used to raise, and at boot that killed the server
        return json.loads(run.stdout)

    def test_a_listener_answering_http_but_not_json_is_reported(self):
        code, body = self.ask(self.listener(b"HTTP/1.1 200 OK\r\nContent-Length: 8\r\n\r\nnot json"))
        self.assertEqual(code, 502)
        self.assertIn("is not hark", body["error"])

    def test_a_listener_that_does_not_speak_http_is_reported(self):
        code, body = self.ask(self.listener(b"SSH-2.0-OpenSSH_9.8\r\n"))
        self.assertEqual(code, 502)
        self.assertIn("is not hark", body["error"])


class ServerCase(unittest.TestCase):
    """server.py on spare ports. It starts the fake agent itself, the way it starts hark."""
    WATCH = "0.1"
    EXTRA_ENV = {}

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hv-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.log = self.tmp / "calls.log"
        self.server_log = open(self.tmp / ".server.log", "ab")
        self.addCleanup(self.server_log.close)
        # free_port() lets the port go before server.py binds it, so on a busy box something else
        # can take it in between. A server that could not listen is retried on another pair.
        for _ in range(5):
            self.port, self.agent_port = free_port(), free_port()
            self.assertFalse({self.port, self.agent_port} & {8473, 8474})
            self.proc = self.spawn()
            if self.came_up():
                break
            self.proc.terminate()
            self.proc.wait()
        else:
            self.fail(f"server.py would not listen, see {self.tmp / '.server.log'}")
        self.addCleanup(self.kill_agent)
        self.addCleanup(self.proc.wait)
        self.addCleanup(self.proc.terminate)

    def spawn(self):
        return subprocess.Popen(
            [sys.executable, str(REPO / "server.py")], stderr=self.server_log,
            env={**os.environ, "HARK_VIEWER_ROOT": str(self.tmp), "HARK_VIEWER_PORT": str(self.port),
                 "HARK_REMOTE_CONTROL_PORT": str(self.agent_port), "HARK_VIEWER_WATCH": self.WATCH,
                 "HARK_VIEWER_STOP_WAIT": "8", "HARK_VIEWER_SETTLE": "1", "FAKE_STOP_TIMEOUT": "1",
                 "HARK_BIN": str(HERE / "fake_hark.py"), "HARK_VIEWER_MW": "off", "FAKE_LOG": str(self.log),
                 **self.EXTRA_ENV})

    def came_up(self, seconds=20):
        end = time.time() + seconds
        while time.time() < end:
            if self.proc.poll() is not None:
                return False                     # it could not listen, and .server.log says why
            # A short timeout: on a port collision whatever holds it may accept and never answer,
            # and the default 60 would then be spent before the retry.
            if self.get("/api/status", timeout=5)[0] == 200:
                return True
            time.sleep(0.05)
        return False

    def kill_agent(self):
        run = subprocess.run(["lsof", "-nP", "-t", f"-iTCP:{self.agent_port}", "-sTCP:LISTEN"], capture_output=True, text=True)
        for pid in run.stdout.split():
            os.kill(int(pid), 9)

    def get(self, path, method="GET", body=None, port=None, timeout=60):
        req = urllib.request.Request(f"http://127.0.0.1:{port or self.port}{path}", method=method,
                                     data=json.dumps(body).encode() if body is not None else (b"" if method == "POST" else None),
                                     headers={"X-Hark-Viewer": "1"})
        try:
            with opener.open(req, timeout=timeout) as r:
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
        lines = self.log.read_text().splitlines() if self.log.exists() else []
        return [l.split()[1] for l in lines if l.endswith(" launched")]

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
        # hark as it is: `stopped` at once, the capture finishing behind it, /start refused meanwhile.
        # Well inside HARK_VIEWER_STOP_WAIT, or CPU contention alone relaunches the agent.
        self.fake(finish=3)
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

    def test_a_restart_whose_stop_failed_says_the_recording_is_still_running(self):
        """The stop's answer was thrown away. A stop that failed left the call recording, the
        /start that follows came back 409 "already active", and the user was told the call was
        stopped and the part had not started, of which the first half was false."""
        made, folder = self.new()
        self.fake(stop_answer=500)
        code, body = self.api("/api/restart", "POST")
        self.assertEqual(code, 502, body)
        self.assertIn("would not stop", body["error"])
        self.assertEqual(body["call"], made["call"])
        self.assertEqual([p for p, _ in self.seen()], ["/start", "/stop"])   # no part was offered to hark
        self.assertFalse((folder / "audio.part2.opus").exists())
        self.assertTrue(self.api("/api/status")[1]["active"])               # and the call is still recording

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

    def test_a_capture_hark_says_is_not_running_is_not_an_active_call(self):
        """hark keeps calling the session `recording` and sets `capturing: false`. Reading the state
        alone left a pulsing REC dot and an elapsed clock over a capture hark knew was not there."""
        made, folder = self.new()
        self.assertTrue(self.api("/api/status")[1]["active"])
        self.fake(capturing=False)
        st = self.api("/api/status")[1]
        self.assertFalse(st["active"])
        self.assertIs(st["session"]["capturing"], False)     # and the page gets the field to say so with
        self.assertEqual(st["call"], made["call"])

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


class SlowStart(ServerCase):
    """hark answers a start only once the capture is really running, and a cold recognizer
    model kept that waiting 12.7 s. The page must wait it out instead of calling it a failure."""
    EXTRA_ENV = {"FAKE_START_DELAY": "1.5", "HARK_VIEWER_START_TIMEOUT": "20"}

    def test_a_start_that_takes_its_time_is_still_a_recording(self):
        code, made = self.api("/api/new", "POST", {"workspace": "work", "title": ""})
        self.assertEqual(code, 200, made)
        call = made["call"]
        self.assertTrue((self.tmp / call / "audio.opus").is_file())
        self.assertTrue(self.api("/api/status")[1]["active"])


class ImpatientStart(ServerCase):
    """The control for the above: with a timeout under the start's own time, the page reports a
    failure for a recording that did begin. That is what the old fixed 10 s timeout would do."""
    EXTRA_ENV = {"FAKE_START_DELAY": "1.5", "HARK_VIEWER_START_TIMEOUT": "0.4"}

    def test_a_timeout_shorter_than_the_start_reports_a_failure(self):
        code, answer = self.api("/api/new", "POST", {"workspace": "work", "title": ""})
        self.assertEqual(code, 502, answer)
        self.assertIn("unreachable", str(answer))


class CutOffStart(ServerCase):
    """hark's HTTP server cuts a handler off at its timeout and answers 500 in its place, and the
    capture it started runs on. A cold start took 22.8 s against a 15 s ceiling and the page called
    it a failure while hark recorded. What hark is doing counts, not what its start said."""
    EXTRA_ENV = {"FAKE_START_ANSWER": "500"}

    def test_a_start_answered_500_is_still_the_recording(self):
        code, made = self.api("/api/new", "POST", {"workspace": "work", "title": "cut off"})
        self.assertEqual(code, 200, made)
        st = self.api("/api/status")[1]
        self.assertTrue(st["active"])
        self.assertEqual(st["call"], made["call"])
        self.assertEqual((self.tmp / "current").resolve(), (self.tmp / made["call"]).resolve())
        self.assertEqual(json.loads((self.tmp / made["call"] / "meta.json").read_text())["id"], "X")
        self.assertIn("answered the start with 500", (self.tmp / ".server.log").read_text())


class SlowAgent(ServerCase):
    """A hark slower to open its port than the server's patience for it used to get a second Popen
    from the next call, so two agents raced for the port and one became an orphan."""
    EXTRA_ENV = {"FAKE_AGENT_DELAY": "3", "HARK_VIEWER_AGENT_WAIT": "0.4"}

    def test_an_agent_slow_to_come_up_is_waited_for_and_never_started_twice(self):
        code, body = self.api("/api/new", "POST", {"workspace": "work", "title": ""})
        self.assertEqual(code, 502, body)                    # nothing answers on hark's port yet
        self.wait_for(lambda: self.api("/api/status")[1]["agent"], "the slow agent to answer", 30)
        made, folder = self.new()
        self.assertTrue(self.api("/api/status")[1]["active"])
        self.assertEqual(len(self.launches()), 1)


class SlowRestart(ServerCase):
    """The part has to reach meta.json before hark is asked to start it. Written afterwards, a
    crash in between left the part recording into a file no parts_of would ever list."""
    EXTRA_ENV = {"FAKE_START_DELAY": "3", "HARK_VIEWER_START_TIMEOUT": "30"}

    def test_the_part_is_listed_before_hark_is_asked_to_start_it(self):
        made, folder = self.new()
        restart = threading.Thread(target=lambda: self.api("/api/restart", "POST"))
        restart.start()
        self.addCleanup(restart.join)
        self.wait_for(lambda: len(postprocess.parts_of(folder)) == 2, "part 2 in meta.json")
        self.assertFalse((folder / "audio.part2.opus").exists())       # hark has not answered the start yet
        restart.join(60)
        parts = postprocess.parts_of(folder)
        self.assertEqual([p["audio"] for p in parts], ["audio.opus", "audio.part2.opus"])
        self.assertTrue((folder / "audio.part2.opus").exists())
        # The recorded start is when the capture opened, not when it was asked for, or every line
        # of the part would sit three seconds early on the call's clock.
        self.assertGreaterEqual(parts[1]["started"], (folder / "audio.part2.opus").stat().st_mtime - 1)

    def test_a_part_that_never_started_is_taken_back_out_of_meta_json(self):
        made, folder = self.new()
        self.fake(refuse_start=True)
        code, body = self.api("/api/restart", "POST")
        self.assertEqual(code, 502, body)
        self.assertEqual(len(postprocess.parts_of(folder)), 1)
        self.assertNotIn("parts", postprocess.read_meta(folder))
        self.assertFalse((folder / "audio.part2.opus").exists())


class EndedAnyWay(ServerCase):
    """Only a session hark called `stopped` used to get the accurate transcript. hark's own
    `failed`, and an agent that vanished with the call still open, left it unwritten, and a server
    restart lost it for good with nothing saying so. The grace is the room Restart needs."""
    EXTRA_ENV = {"HARK_VIEWER_ENDED_GRACE": "0.5"}

    def test_a_session_hark_reports_failed_still_gets_its_transcript(self):
        made, folder = self.new()
        self.fake(session={"state": "failed", "error": "captured no audio"})
        self.wait_for(lambda: (postprocess.read_status(folder) or {}).get("state") == "done", "the transcript")

    def test_an_agent_that_vanished_with_the_call_open_still_gets_its_transcript(self):
        made, folder = self.new()
        self.kill_agent()
        self.wait_for(lambda: not self.api("/api/status")[1]["agent"], "the agent to be gone")
        self.wait_for(lambda: (postprocess.read_status(folder) or {}).get("state") == "done", "the transcript")


class NotCapturing(ServerCase):
    """hark gives up waiting for its own capture at 60 s and answers the start 2xx with
    `capturing: false`. Nothing read the field, so that start became a call folder, a `current`
    symlink and a live recording on the page while hark recorded nothing."""
    EXTRA_ENV = {"FAKE_CAPTURING": "0"}

    def test_a_start_hark_answered_without_capturing_is_not_a_recording(self):
        code, answer = self.api("/api/new", "POST", {"workspace": "work", "title": ""})
        self.assertEqual(code, 502, answer)
        self.assertIn("not capturing", answer["error"])
        self.assertFalse(self.api("/api/status")[1]["active"])
        self.assertFalse((self.tmp / "current").exists())         # the last call, whichever it was, is not this one


class SlowWatcher(ServerCase):
    WATCH = "60"

    def test_a_call_that_ended_inside_the_watchers_tick_still_gets_its_transcript(self):
        first, folder = self.new("one")
        self.assertEqual(self.api("/api/stop", "POST")[0], 200)
        second, _ = self.new("two")
        self.assertNotEqual(first["call"], second["call"])
        self.wait_for(lambda: (postprocess.read_status(folder) or {}).get("state") == "done", "the first call's transcript")


class Launcher(unittest.TestCase):
    """The `hark-viewer` command against a stub page server: no browser, no hark, no server.py."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hv-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.port = free_port()
        self.delay = 0.0
        self.patience = 240
        case = self

        class Stub(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def answer(self, body):
                raw = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                self.answer({"agent": True, "active": False, "session": None, "call": None,
                             "parts": 0, "postprocess": None, "workspaces": [], "patience": case.patience})

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                time.sleep(case.delay)
                self.answer({"call": "work/2026-09-21_170000", "folder": str(case.tmp),
                             "url": f"http://127.0.0.1:{case.port}/?call=work/2026-09-21_170000"})

        self.stub = ThreadingHTTPServer(("127.0.0.1", self.port), Stub)
        threading.Thread(target=self.stub.serve_forever, daemon=True).start()
        self.addCleanup(self.stub.server_close)
        self.addCleanup(self.stub.shutdown)

    def run_launcher(self, *args, **env):
        return subprocess.run(
            [str(REPO / "hark-viewer"), *args], capture_output=True, text=True, timeout=120,
            env={**os.environ, "HARK_VIEWER_PORT": str(self.port), "HARK_VIEWER_ROOT": str(self.tmp),
                 "HARK_VIEWER_BROWSER": "off", "HARK_VIEWER_CONFIG": str(self.tmp / "absent.env"), **env})

    def test_a_start_the_server_takes_longer_than_15_s_to_answer_is_still_a_recording(self):
        """It used to pass --max-time 15 to every call while the server's own budget for one start
        ran to 105 s, so a start the server waited out reached the user as curl exit 28."""
        self.delay = 16
        run = self.run_launcher("work", "slow start")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("work/2026-09-21_170000", run.stdout)

    def test_a_setting_in_the_config_file_reaches_the_child_process(self):
        """Only three variables used to be exported, so anything else in ~/.config/hark-viewer.env
        was read into the launcher's own shell and never reached the server or the offline job."""
        folder = make_call(self.tmp, [(0, [line(1.0, 2.0, "Others", "a line long enough to be worth judging")])])
        cfg = self.tmp / "settings.env"
        cfg.write_text("HARK_VIEWER_PY3=/nonexistent/python3\n")
        run = self.run_launcher("languages", str(folder), HARK_VIEWER_CONFIG=str(cfg))
        self.assertEqual(run.returncode, 1, run.stdout)
        self.assertIn("no interpreter at /nonexistent/python3", run.stderr)


if __name__ == "__main__":
    unittest.main()
