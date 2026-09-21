#!/usr/bin/env python3
"""A stand-in for `hark -i FILE ... -t OUT`. FAKE_HARK_MODE picks what it does:

ok      one line per file, at 1.0 s, whose text is the input's file name
poison  refuses the whole recording and any piece covering second FAKE_HARK_POISON,
        with the message the real one gave. A piece is named <stem>_<start>_<length>.wav.
fail    refuses everything
"""
import json
import os
import sys
import time
from pathlib import Path

args = sys.argv[1:]
if "--remote-control" in args:                      # server.py starting the agent
    os.execv(sys.executable, [sys.executable, str(Path(__file__).with_name("fake_agent.py")), *args])
audio, out = Path(args[args.index("-i") + 1]), Path(args[args.index("-t") + 1])
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write("hark " + " ".join(args) + "\n")
mode = os.environ.get("FAKE_HARK_MODE", "ok")
time.sleep(float(os.environ.get("FAKE_HARK_SLEEP", "0")))
refuse = mode == "fail"
if mode == "poison":
    if audio.suffix != ".wav":
        refuse = True
    else:
        _, start, length = audio.stem.rsplit("_", 2)
        refuse = float(start) <= float(os.environ["FAKE_HARK_POISON"]) < float(start) + float(length)
if refuse:
    sys.exit("Error: Invalid audio data provided. Must be at least 300ms of 16kHz audio")
if out.exists():
    sys.exit(f"{out} exists")            # the real one never overwrites
out.write_text(json.dumps([{"text": audio.name, "speaker": "Others", "start": 1.0, "end": 2.0}]))
