#!/usr/bin/env python3
"""Fix the speaker labels of a finished call.

The live labels are guessed as the audio arrives, so a long call with many
voices reuses a number for two people. Once the call is over the whole
recording is available at once, and a diarizer run over the call side of
`audio.opus` gets the speakers right.

This writes two new files next to the call and never touches `transcript.json`:

    speakers.json             the diarized spans, so a second run is free
    transcript.speakers.json  the live lines, each given the speaker of the
                              span it overlaps most

`server.py` serves `transcript.speakers.json` in place of `transcript.json`
when it exists, so the page and any agent reading the call get the better
labels without knowing about this script.

Lines labelled `You` are the microphone and are left alone.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(os.environ.get("HARK_VIEWER_ROOT", Path.home() / "Recordings" / "calls")).expanduser()
AUDIO = "audio.opus"
MIC_LABEL = "You"


def die(message):
    sys.exit(f"relabel: {message}")


def resolve_call(name):
    """An absolute folder, `workspace/name` under HARK_VIEWER_ROOT, or `current`."""
    folder = Path(name).expanduser()
    if not folder.is_absolute():
        folder = ROOT / name
    folder = folder.resolve()
    if not folder.is_dir():
        die(f"no such call folder: {folder}")
    if not (folder / "transcript.json").is_file():
        die(f"no transcript.json in {folder}")
    if not (folder / AUDIO).is_file():
        die(f"no {AUDIO} in {folder}")
    return folder


def read_lines(path):
    """The live transcript. Each line keeps its own key order."""
    out = []
    for raw in path.read_text().splitlines():
        if not raw.strip():
            continue
        try:
            entry = json.loads(raw)
        except ValueError:
            continue                       # a half-written last line
        if isinstance(entry, dict) and isinstance(entry.get("text"), str):
            out.append(entry)
    return out


def channel_count(audio):
    run = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=channels", "-of", "csv=p=0", str(audio)],
        capture_output=True, text=True)
    try:
        return int(run.stdout.strip().splitlines()[0])
    except (ValueError, IndexError):
        die(f"ffprobe could not read {audio}: {run.stderr.strip()}")


def split_call_channel(audio, dest):
    """The call side as 16 kHz mono WAV. Channel 1 of a stereo recording, the lot of a mono one.

    Folding the two channels together is wrong: the microphone then counts as
    another voice wherever it overlaps, and the diarizer invents speakers.
    """
    channel = 1 if channel_count(audio) > 1 else 0
    run = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(audio),
         "-af", f"pan=mono|c0=c{channel}", "-ar", "16000", "-c:a", "pcm_s16le", str(dest)],
        capture_output=True, text=True)
    if run.returncode != 0 or not dest.is_file():
        die(f"ffmpeg could not split the call channel: {run.stderr.strip()}")
    return channel


def diarize(hark_bin, wav, engine):
    """Diarized spans from hark. `-t -` writes the transcript to stdout, notices to stderr."""
    cmd = [hark_bin, "-i", str(wav), "-t", "-", "--speakers", "--speaker-mode", "acoustic",
           "--transcript-format", "json", "--engine", engine]
    run = subprocess.run(cmd, capture_output=True, text=True)
    if run.stderr.strip():
        print(run.stderr.rstrip(), file=sys.stderr)
    if run.returncode != 0:
        if "300ms" in run.stderr:
            print("relabel: this hark drops a diarized span shorter than the recognizer's floor "
                  "instead of padding it, so the whole run stops. Use a hark carrying that fix, "
                  "or pass --spans with spans from one.", file=sys.stderr)
        die(f"{hark_bin} exited {run.returncode}")
    return parse_spans(run.stdout, f"{hark_bin} stdout")


def parse_spans(text, origin):
    """A list of {start, end, speaker}, from hark's JSON transcript or a saved copy of it."""
    try:
        raw = json.loads(text)
    except ValueError as e:
        die(f"{origin} is not JSON: {e}")
    if isinstance(raw, dict):
        raw = raw.get("spans", [])
    if not isinstance(raw, list):
        die(f"{origin} is not a list of spans")
    spans = [{"start": float(s["start"]), "end": float(s["end"]), "speaker": str(s["speaker"])}
             for s in raw
             if isinstance(s, dict) and s.get("speaker") is not None
             and s.get("start") is not None and s.get("end") is not None]
    if not spans:
        die(f"{origin} carries no labelled spans")
    return spans


def best_label(spans, start, end):
    """The label of the span this line overlaps most, or None when it overlaps none."""
    best, most = None, 0.0
    for s in spans:
        shared = min(end, s["end"]) - max(start, s["start"])
        if shared > most:
            best, most = s["speaker"], shared
    return best


def write_atomic(path, payload):
    """Through a temp file in the same folder, so a reader never sees half a file."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(payload)
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser(
        description="Relabel a finished call's speakers from an offline diarizer pass.")
    ap.add_argument("call", nargs="?", default="current",
                    help="call folder: absolute, workspace/name under HARK_VIEWER_ROOT, or current")
    ap.add_argument("--engine", default="parakeet",
                    help="hark engine for the offline pass (default parakeet)")
    ap.add_argument("--hark", default=os.environ.get("HARK_BIN", "hark"),
                    help="the hark binary to run (default $HARK_BIN, else hark)")
    ap.add_argument("--spans", metavar="FILE",
                    help="read the spans from this file instead of running hark")
    ap.add_argument("--min-coverage", type=float, default=0.6,
                    help="exit 3 when fewer than this share of non-You lines match a span (default 0.6)")
    ap.add_argument("--force", action="store_true",
                    help="diarize again even when speakers.json is newer than the audio")
    ap.add_argument("--dry-run", action="store_true", help="print the counts and write nothing")
    args = ap.parse_args()

    folder = resolve_call(args.call)
    audio = folder / AUDIO
    lines = read_lines(folder / "transcript.json")
    if not lines:
        die(f"{folder / 'transcript.json'} has no lines")

    cache = folder / "speakers.json"
    spans = source = None
    if args.spans:
        source = args.spans
        spans = parse_spans(Path(args.spans).expanduser().read_text(), source)
    elif not args.force and cache.is_file() and cache.stat().st_mtime >= audio.stat().st_mtime:
        source = f"{cache} (cached)"
        spans = parse_spans(cache.read_text(), source)
    if spans is None:
        if shutil.which(args.hark) is None and not Path(args.hark).is_file():
            die(f"hark not found: {args.hark} (set --hark or HARK_BIN)")
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "call.wav"
            channel = split_call_channel(audio, wav)
            spans = diarize(args.hark, wav, args.engine)
        source = f"{args.hark} --engine {args.engine} on channel {channel}"
        if not args.dry_run:
            write_atomic(cache, json.dumps(
                {"generated": time.time(), "hark": args.hark, "engine": args.engine,
                 "channel": channel, "spans": spans}, indent=1))

    others = [e for e in lines if e.get("speaker") != MIC_LABEL]
    out, covered = [], 0
    for entry in lines:
        entry = dict(entry)                        # keeps this line's key order
        if entry.get("speaker") != MIC_LABEL:
            label = best_label(spans, float(entry.get("start") or 0), float(entry.get("end") or 0))
            if label:
                covered += 1
                entry["speaker"] = label
        out.append(entry)

    changed = sum(1 for a, b in zip(lines, out) if a.get("speaker") != b.get("speaker"))
    live_names = sorted({str(e.get("speaker")) for e in lines})
    offline_names = sorted({s["speaker"] for s in spans})
    print(f"spans: {len(spans)} from {source}")
    print(f"speakers: live {len(live_names)} {live_names} -> offline {len(offline_names)} {offline_names}")
    print(f"lines: {len(lines)} total, {len(lines) - len(others)} {MIC_LABEL}, {len(others)} other")
    print(f"matched: {covered}/{len(others)} non-{MIC_LABEL} lines, {changed} labels changed")

    if args.dry_run:
        print("dry run: nothing written")
    else:
        write_atomic(folder / "transcript.speakers.json",
                     "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in out))
        print(f"wrote {folder / 'transcript.speakers.json'}")

    share = covered / len(others) if others else 1.0
    if share < args.min_coverage:
        print(f"relabel: only {share:.0%} of the non-{MIC_LABEL} lines matched a span, below "
              f"--min-coverage {args.min_coverage:.0%}; these labels are probably wrong",
              file=sys.stderr)
        sys.exit(3)


if __name__ == "__main__":
    main()
