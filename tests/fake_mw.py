#!/usr/bin/env python3
"""A stand-in for `mw transcribe FILE --speakers` that fails the first time it is ever run."""
import os
import sys
from pathlib import Path

log = Path(os.environ["FAKE_LOG"])
first = "mw " not in (log.read_text() if log.exists() else "")
with open(log, "a") as f:
    f.write("mw " + " ".join(sys.argv[1:]) + "\n")
if first:
    sys.exit("The operation couldn't be completed. (GRDB.RecordError error 0.)")
print(f"Speaker 1: words from {Path(sys.argv[2]).name}")
