# The accurate transcript

The live transcript is the fast pass. When a call ends the server starts [`postprocess.py`](../postprocess.py) as a detached process. Every ending counts, including one no server was running for: at startup the server looks at the call `current` points at, and writes the accurate transcript for it if hark is not recording it and it has none. A stop from the page, `hark-viewer stop` or `hark-viewer quit` starts the job at once. The other endings are hark reporting the session `failed`, hark disowning its capture, and the agent dying with the call still open. Those wait a minute first (`HARK_VIEWER_ENDED_GRACE`). That minute belongs to Restart, which records on into the same call, and a call that already has its accurate transcript will not be restarted. It outlives the page and the server, runs at low priority, and never touches the audio or `transcript.json`. Because `stopped` comes before the audio is complete, the job first waits until no part's audio has changed for 5 seconds, for at most 120, and records that wait as `settled: {"waited", "capped"}`. `hark-viewer quit` waits the same way before it kills the agent. The job writes:

- `transcript.final.json`, from `hark -i <audio> --speakers --speaker-mode source --speaker-labels "Microphone,Others"` over each part, joined on the call's clock. JSON Lines with the keys of `transcript.json`. It needs a hark whose `--speaker-mode source` reads a file's two channels, see [Getting your own voice back out](agent-skill.md#getting-your-own-voice-back-out).
- `transcript.mw.txt`, from MacWhisper's `mw transcribe <audio> --speakers`, tried twice, because it fails now and then with `GRDB.RecordError error 0` and works the next time. This one is there to compare the two transcribers and will go. `HARK_VIEWER_MW=off` turns it off, and without MacWhisper installed the step is skipped.
- the languages the call was in, into `postprocess.json` under `steps.languages`. See [below](#which-languages-the-call-was-in).
- `postprocess.json`, the state of the job, which `/api/status` also carries as `postprocess` for the last call. The page shows it as `final transcript: running`, `ready` or `failed`.

```json
{"state": "done", "pid": 48020, "started": 1790010463.1, "finished": 1790010632.7, "settled": {"waited": 5.2, "capped": false},
 "steps": {"final": {"state": "done", "started": 1790010463.1, "finished": 1790010495.4, "error": null,
                     "skipped_spans": [], "warning": null},
           "languages": {"state": "done", "started": 1790010495.4, "finished": 1790010496.2, "error": null,
                         "skipped_spans": [], "warning": null,
                         "languages": {"dominant": "en", "present": ["en"], "mixed": false, "judged": 230,
                                       "shares": {"en": 1.0}, "other_lines": 0, "other": [], "source": "transcript.final.json"}},
           "mw":    {"state": "done", "started": 1790010496.2, "finished": 1790010632.7, "error": null,
                     "skipped_spans": [], "warning": null}}}
```

`state` is `running`, `done` or `failed`, and follows the `final` step. A step is `pending`, `running`, `done`, `failed` or `skipped`. A job that died reads as `failed`, also when another process has taken its pid since. A job sent SIGTERM removes its scratch folder and records `failed`, and each job sweeps the scratch folders of jobs that were killed outright.

hark can refuse a whole recording. It did on a 51-minute file, with `Invalid audio data provided. Must be at least 300ms of 16kHz audio`. The job then cuts that part into 10-minute pieces with `ffmpeg`, halves any piece hark still refuses down to about 20 seconds, and skips only the piece that fails at that size. `skipped_spans` lists what it skipped as `{"start", "end", "part", "error"}` on the call's clock. When hark refuses every piece the step fails and writes no transcript.

A step that finished but has something to tell you puts it in `warning`. There is one so far: when no line of the accurate transcript came from the call side, every line reads `Microphone`, which is what a hark that reads only channel 0 of the recording produces. The step still succeeds, because those lines are real, they are just half the call, and the page shows the warning next to the transcript's state.

Every tool the job runs has a deadline, so a hark, an `mw` or an `ffmpeg` that never returns fails its step instead of leaving the job reading `running` for ever. `HARK_VIEWER_TOOL_TIMEOUT` is the one for anything that reads a whole call, which includes the pass `hark-viewer relabel` makes over the recording to pull out the call channel, and `HARK_VIEWER_PROBE_TIMEOUT` the one for the quick tools: `ffprobe`, the language recognizer, and an `ffmpeg` cutting one piece.

`postprocess.json` is also the lock. The job links it into place already filled in, so it runs once per call and nobody ever reads the file empty. `./hark-viewer finalize [call] --force` runs it again, and without `--force` it does a call that never got one, such as a call recorded before this existed.

## Which languages the call was in

The job also says what the call was spoken in, because that decides which live model is the right one. Apple's on-device recognizer reads every line of the accurate transcript, or of the live one when the accurate pass has not run, and the answer lands in `postprocess.json` under `steps.languages`:

```json
{"engine": "NLLanguageRecognizer", "lines": 79, "judged": 67, "dominant": "en", "shares": {"en": 0.985, "pt": 0.015},
 "present": ["en"], "mixed": false, "other_lines": 0, "other": [], "source": "transcript.final.json"}
```

`dominant` is the language most lines are in. `present` is what the call is judged to have been spoken in and `mixed` says whether that is more than one language, which is the pair to read. `shares` is the raw per-line tally, `other` lists the lines not in the dominant language as `{"start", "end", "language", "confidence"}`, and `judged` counts the lines long enough to be worth reading at all, which is also what the share above is measured against, so `shares` totals under 1 when the recogniser could not call some of them.

Two rules keep a wrong guess from reading as a second language. A line counts as another language only at 0.95 confidence or better: on an all-English call the recognizer called one line Portuguese at 0.929, while real English lines went as low as 0.829, so confidence alone does not separate them. Spanish speech scores 0.996, even from the garbled live transcription of it. And one such line is not enough. A language joins `present` when it holds over at least two lines, or over a tenth of a call too short for two lines to mean anything. So the stray Portuguese line above leaves `mixed` false, and `shares` still reports it.

To ask about any call without writing anything:

```sh
./hark-viewer languages                          # the call in `current`
./hark-viewer languages work/2026-09-21_101500
```

It needs `/usr/bin/python3`, the one interpreter here that carries the PyObjC bridge to the recognizer. A Homebrew or mise python has no bridge. Point `HARK_VIEWER_PY3` elsewhere if that path ever moves; without it the step is skipped and the rest of the job goes on.
