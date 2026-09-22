#!/usr/bin/env python3
"""A stand-in for the `hark -i FILE -t - --speaker-mode acoustic` that relabel runs.

It writes the spans of FAKE_SPANS, a JSON list, to stdout, the way hark's `-t -` does, and
logs its arguments to FAKE_LOG. FAKE_DIARIZER_EXIT makes it refuse instead.
"""
import json
import os
import sys

with open(os.environ["FAKE_LOG"], "a") as log:
    log.write("diarizer " + " ".join(sys.argv[1:]) + "\n")
code = int(os.environ.get("FAKE_DIARIZER_EXIT", "0"))
if code:
    sys.exit(code)
print(os.environ.get("FAKE_SPANS", json.dumps([{"start": 0.0, "end": 9.0, "speaker": "Ada"}])))
