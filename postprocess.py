#!/usr/bin/env python3
"""The accurate transcript of a finished call.

`server.py` starts this once a call stops, detached, so it outlives the page and
the server. It reads the recording and writes three new files into the call folder:

    transcript.final.json  hark's offline pass over every part, on the call's clock,
                           JSON Lines like transcript.json: {"start","end","speaker","text"}
                           with the speakers `Microphone` and `Others`
    transcript.mw.txt      MacWhisper's pass over the same audio, to compare against
    postprocess.json       the state of each step, so another tool can wait on it

It never touches the audio or the live transcript. `postprocess.json` is created
exclusively, so a second run for the same call exits 0 and does nothing; `--force`
runs again.

This file also holds what `server.py` needs to read a call made of parts.
"""
import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(os.environ.get("HARK_VIEWER_ROOT", Path.home() / "Recordings" / "calls")).expanduser()
HARK_BIN = os.environ.get("HARK_BIN", "hark")
# MacWhisper's pass is a comparison lane. HARK_VIEWER_MW=off turns it off, a path names another mw.
MW_BIN = os.environ.get("HARK_VIEWER_MW", "/Applications/MacWhisper.app/Contents/MacOS/mw")
STATUS = "postprocess.json"
FINAL = "transcript.final.json"
MW_OUT = "transcript.mw.txt"
CHUNK = float(os.environ.get("HARK_VIEWER_CHUNK", "600"))          # a part hark refuses is retried in pieces this long
MIN_PIECE = float(os.environ.get("HARK_VIEWER_MIN_PIECE", "20"))   # a piece this short that still fails is skipped
# hark reports `stopped` before its capture has finished writing the audio, and says nothing when it has.
SETTLE = float(os.environ.get("HARK_VIEWER_SETTLE", "5"))            # a recording this long unchanged is finished
SETTLE_CAP = float(os.environ.get("HARK_VIEWER_SETTLE_CAP", "120"))  # and past this the job goes on regardless
TMP_PREFIX = "hark-viewer-job-"
MAX_FAILURES = 25   # per part. A hark that refuses everything (a missing flag, a missing model) must not be bisected for an hour.


# ---- a call and its parts (server.py imports these) ----

def read_meta(folder):
    try:
        meta = json.loads((folder / "meta.json").read_text())
        return meta if isinstance(meta, dict) else {}
    except (OSError, ValueError):
        return {}


def parts_of(folder, meta=None):
    """The parts of a call, oldest first. A call never restarted has one, and no `parts` key."""
    meta = read_meta(folder) if meta is None else meta
    parts = [p for p in meta.get("parts") or [] if isinstance(p, dict) and p.get("audio") and p.get("transcript")]
    return parts or [{"n": 1, "started": meta.get("started"), "audio": "audio.opus", "transcript": "transcript.json"}]


def offset_of(part, meta):
    """Seconds from the start of the call to the start of this part."""
    try:
        return max(0.0, float(part["started"]) - float(meta["started"]))
    except (KeyError, TypeError, ValueError):
        return 0.0


def read_lines(path):
    out = []
    try:
        text = path.read_text()
    except OSError:
        return out
    for raw in text.splitlines():
        if not raw.strip():
            continue
        try:
            entry = json.loads(raw)
        except ValueError:
            continue                       # a half-written last line
        if isinstance(entry, dict) and isinstance(entry.get("text"), str):
            out.append(entry)
    return out


def shift(entries, offset):
    if not offset:
        return entries
    out = []
    for e in entries:
        e = dict(e)
        for key in ("start", "end"):
            if isinstance(e.get(key), (int, float)):
                e[key] = round(e[key] + offset, 3)
        out.append(e)
    return out


def merged_lines(folder, pick=lambda path: path):
    """Every part's lines on the call's clock. `pick` lets the caller swap a part's file for a better one."""
    meta = read_meta(folder)
    out = []
    for part in parts_of(folder, meta):
        out += shift(read_lines(pick(folder / part["transcript"])), offset_of(part, meta))
    return out


def jsonl(entries):
    return "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries)


def write_atomic(path, payload):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(payload)
    os.replace(tmp, path)


def read_status(folder):
    """postprocess.json, with a job that died mid-run reported as failed instead of running forever."""
    try:
        st = json.loads((folder / STATUS).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(st, dict):
        return None
    if st.get("state") == "running" and not alive(st.get("pid"), st.get("started")):
        st["state"] = "failed"
        st["error"] = "the job died before it finished; run `hark-viewer finalize --force`"
    return st


def alive(pid, started=None):
    """Is this the job that wrote `started`? A zombie is dead, and so is a stranger that got the same pid later."""
    try:
        run = subprocess.run(["ps", "-o", "stat=,lstart=", "-p", str(int(pid))], capture_output=True, text=True,
                             env={**os.environ, "LC_ALL": "C"})
        stat, born = run.stdout.strip().split(None, 1)
        born = time.mktime(time.strptime(born.strip(), "%a %b %d %H:%M:%S %Y"))
    except (OSError, TypeError, ValueError):
        return False
    if stat.startswith("Z"):
        return False
    return not isinstance(started, (int, float)) or born <= started + 2   # the job writes `started` after it is born


def settle(paths):
    """Wait until no recording has changed for SETTLE seconds. Returns (seconds waited, gave up at the cap)."""
    began = time.time()
    while True:
        newest = 0.0
        for path in paths:
            try:
                newest = max(newest, path.stat().st_mtime)
            except OSError:
                pass
        if time.time() - newest > SETTLE:
            return round(time.time() - began, 1), False
        if time.time() - began > SETTLE_CAP:
            return round(time.time() - began, 1), True
        time.sleep(min(0.5, SETTLE / 5))


def sweep_tmp():
    """Scratch folders of jobs that were killed before they could clean up. Each holds up to 38 MB of WAV."""
    for old in Path(tempfile.gettempdir()).glob(TMP_PREFIX + "*"):
        pid = old.name[len(TMP_PREFIX):].split("-")[0]
        if not alive(pid):
            shutil.rmtree(old, ignore_errors=True)


# ---- the job ----

class Refused(Exception):
    """hark refused everything it was given, so going on would only burn time."""


def duration_of(audio):
    run = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(audio)],
                         capture_output=True, text=True)
    try:
        return float(run.stdout.strip().splitlines()[0])
    except (ValueError, IndexError):
        raise RuntimeError(f"ffprobe could not read {audio.name}: {run.stderr.strip()[-300:]}")


def cut(audio, start, length, dest):
    """One piece as 16 kHz WAV, both channels kept: the microphone and the call stay apart."""
    run = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", f"{start:.3f}", "-t", f"{length:.3f}",
                          "-i", str(audio), "-ar", "16000", "-c:a", "pcm_s16le", str(dest)],
                         capture_output=True, text=True)
    if run.returncode != 0 or not dest.is_file():
        raise RuntimeError(f"ffmpeg could not cut {audio.name} at {start:.0f}s: {run.stderr.strip()[-300:]}")


def run_hark(audio, tmp):
    """(lines, None) or (None, why). The output file is always a fresh name: hark never overwrites."""
    out = Path(tmp) / f"{uuid.uuid4().hex}.json"
    try:
        run = subprocess.run([HARK_BIN, "-i", str(audio), "--speakers", "--speaker-mode", "source",
                              "--speaker-labels", "Microphone,Others", "-t", str(out)],
                             stdin=subprocess.DEVNULL, capture_output=True, text=True)
    except OSError as e:
        return None, f"{HARK_BIN}: {e}"
    if run.returncode != 0:
        return None, f"hark exited {run.returncode}: {(run.stderr or run.stdout).strip()[-400:]}"
    try:
        raw = json.loads(out.read_text())
    except (OSError, ValueError) as e:
        return None, f"hark wrote no readable transcript: {e}"
    if isinstance(raw, dict):
        raw = raw.get("spans") or raw.get("segments") or []
    return [e for e in raw if isinstance(e, dict) and isinstance(e.get("text"), str)], None


def transcribe_part(audio, tmp, log):
    """(lines, skipped) for one part, on the part's own clock.

    The whole file first. If hark refuses it, 10-minute pieces, and a piece hark
    refuses is halved until the half that still fails is about 20 s. Only that is skipped.
    """
    lines, why = run_hark(audio, tmp)
    if lines is not None:
        return lines, []
    log(f"{audio.name}: {why}; retrying in pieces")
    total = duration_of(audio)
    out, skipped, tally = [], [], {"ok": 0, "failed": 1}

    def piece(start, length):
        wav = Path(tmp) / f"{audio.stem}_{start:.3f}_{length:.3f}.wav"
        cut(audio, start, length, wav)
        got, why = run_hark(wav, tmp)
        wav.unlink(missing_ok=True)
        if got is not None:
            tally["ok"] += 1
            out.extend(shift(got, start))
            return
        tally["failed"] += 1
        if tally["failed"] > MAX_FAILURES and not tally["ok"]:
            raise Refused(f"hark refused the first {tally['failed']} attempts on {audio.name}; last: {why}")
        if length <= MIN_PIECE:
            log(f"{audio.name}: skipped {start:.1f}s to {start + length:.1f}s: {why}")
            skipped.append({"start": round(start, 3), "end": round(start + length, 3), "error": why})
            return
        half = length / 2
        piece(start, half)
        piece(start + half, length - half)

    start = 0.0
    while start < total:
        left = total - start
        length = left if left < CHUNK + 1 else CHUNK   # a tail under a second is no piece of its own: hark refuses audio that short
        piece(start, length)
        start += length
    if not tally["ok"]:
        raise Refused(f"hark refused every piece of {audio.name}; last: {skipped[-1]['error']}")
    return out, skipped


def step_final(folder, tmp, log):
    meta = read_meta(folder)
    lines, skipped = [], []
    for part in parts_of(folder, meta):
        audio = folder / part["audio"]
        if not audio.is_file():
            raise RuntimeError(f"{part['audio']} is missing")
        offset = offset_of(part, meta)
        got, gaps = transcribe_part(audio, tmp, log)
        lines += [{"start": round(float(e.get("start") or 0) + offset, 3), "end": round(float(e.get("end") or 0) + offset, 3),
                   "speaker": e.get("speaker"), "text": e["text"]} for e in got]
        skipped += [{**g, "start": round(g["start"] + offset, 3), "end": round(g["end"] + offset, 3), "part": part.get("n")}
                    for g in gaps]
    lines.sort(key=lambda e: e["start"])
    write_atomic(folder / FINAL, jsonl(lines))
    return skipped


def step_mw(folder, tmp, log):
    meta = read_meta(folder)
    parts = parts_of(folder, meta)
    chunks = []
    for part in parts:
        audio = folder / part["audio"]
        if not audio.is_file():
            raise RuntimeError(f"{part['audio']} is missing")
        for attempt in (1, 2):                      # it fails now and then with "GRDB.RecordError error 0" and works the second time
            run = subprocess.run([MW_BIN, "transcribe", str(audio), "--speakers"],
                                 stdin=subprocess.DEVNULL, capture_output=True, text=True)
            if run.returncode == 0 and run.stdout.strip():
                break
            why = f"mw exited {run.returncode}: {(run.stderr or run.stdout).strip()[-400:]}"
            log(f"{audio.name}: {why}" + ("; trying once more" if attempt == 1 else ""))
            if attempt == 2:
                raise RuntimeError(why)
            time.sleep(2)
        head = f"== part {part.get('n')}, starts {offset_of(part, meta):.0f} s into the call ==\n" if len(parts) > 1 else ""
        chunks.append(head + run.stdout.strip() + "\n")
    write_atomic(folder / MW_OUT, "\n".join(chunks))
    return []


def mw_skip():
    """Why MacWhisper's pass does not run, or None when it does."""
    if MW_BIN.lower() in ("", "off", "0", "no", "false"):
        return "turned off (HARK_VIEWER_MW)"
    if not os.access(MW_BIN, os.X_OK):
        return f"MacWhisper's mw is not at {MW_BIN}"
    return None


class Terminated(BaseException):
    """SIGTERM. A BaseException, so it passes the step's `except Exception` and unwinds the scratch folder."""


def main():
    ap = argparse.ArgumentParser(description="Write the accurate transcript of a finished call.")
    ap.add_argument("call", nargs="?", default="current",
                    help="call folder: absolute, workspace/name under HARK_VIEWER_ROOT, or current")
    ap.add_argument("--force", action="store_true", help="run again even when postprocess.json exists")
    ap.add_argument("--settle-only", action="store_true", help="wait until hark has finished writing the audio, do nothing else")
    args = ap.parse_args()

    folder = Path(args.call).expanduser()
    folder = (folder if folder.is_absolute() else ROOT / args.call).resolve()
    if not folder.is_dir():
        sys.exit(f"finalize: no such call folder: {folder}")
    audios = [folder / part["audio"] for part in parts_of(folder)]
    if args.settle_only:
        settle(audios)
        return

    status_path = folder / STATUS
    if args.force:
        old = read_status(folder)
        if old and old.get("state") == "running":
            sys.exit(f"finalize: already running for this call (pid {old.get('pid')})")
        status_path.unlink(missing_ok=True)
    steps = [("final", step_final, None), ("mw", step_mw, mw_skip())]
    status = {"state": "running", "pid": os.getpid(), "started": time.time(), "finished": None, "settled": None,
              "steps": {name: {"state": "pending", "started": None, "finished": None, "error": None, "skipped_spans": []}
                        for name, _, _ in steps}}
    # The one-run-per-call lock. Linked into place whole, so nobody ever reads it empty, not even after a kill.
    first = status_path.with_name(f"{STATUS}.{os.getpid()}.tmp")
    first.write_text(json.dumps(status, indent=1))
    try:
        os.link(first, status_path)
    except FileExistsError:
        print(f"finalize: {status_path} exists, nothing to do (--force runs again)")
        return
    finally:
        first.unlink(missing_ok=True)

    def save():
        write_atomic(status_path, json.dumps(status, indent=1))

    def log(message):
        print(f"{time.strftime('%H:%M:%S')} {folder.name}: {message}", file=sys.stderr, flush=True)

    def terminated(*_):
        raise Terminated()

    signal.signal(signal.SIGTERM, terminated)
    try:
        os.nice(10)                                   # a new call may already be recording
    except OSError:
        pass
    sweep_tmp()
    try:
        waited, capped = settle(audios)
        status["settled"] = {"waited": waited, "capped": capped}
        if capped:
            log(f"the audio was still changing after {waited:.0f} s, going on anyway")
        save()
        with tempfile.TemporaryDirectory(prefix=f"{TMP_PREFIX}{os.getpid()}-") as tmp:
            for name, run, skip in steps:
                step = status["steps"][name]
                if skip:
                    step.update(state="skipped", error=skip)
                    save()
                    continue
                step.update(state="running", started=time.time())
                save()
                try:
                    step["skipped_spans"] = run(folder, tmp, log)
                    step["state"] = "done"
                except Exception as e:                # noqa: BLE001 - the status file is the only place anyone looks
                    step.update(state="failed", error=str(e))
                    log(f"{name} failed: {e}")
                step["finished"] = time.time()
                save()
        status["state"] = "done" if status["steps"]["final"]["state"] == "done" else "failed"
    except Terminated:
        status.update(state="failed", error="terminated; run `hark-viewer finalize --force`")
        log("terminated")
    status["finished"] = time.time()
    save()
    sys.exit(0 if status["state"] == "done" else 1)


if __name__ == "__main__":
    main()
