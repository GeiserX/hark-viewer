# hark-viewer

A live transcript page and recording controls for [hark](https://github.com/PhantomYdn/hark), the macOS CLI that captures system audio and the microphone and transcribes them on-device.

hark writes a call's transcript to a file while people are still speaking. hark-viewer shows that file in the browser as it grows, with a colour per speaker and a clock time per line, and puts Record, Stop, Pause and Mute on the same page. It also ships an [agent skill](SKILL.md), so a coding agent such as Claude Code can start a recording and answer questions about the call while it is happening.

![A call being recorded](docs/images/recording.png)

## What it does

- Records the whole computer plus your microphone through hark's Core Audio tap. No per-app tracking, no virtual audio driver.
- Keeps your microphone on the left channel and the call on the right, so a later pass can still tell them apart.
- Shows each utterance a moment after the speaker pauses, labelled `You` for the microphone and `Speaker 1..N` for voices on the computer side.
- Shows the line still being spoken in grey under the finished ones, when hark reports one. The start request asks for it (`liveStreaming` in `START`), so a hark that can stream does, and one that cannot ignores the key. The first words appear about 2 seconds after they are spoken and the open line grows every 0.6 seconds until it closes. It is never corrected as it grows. With the setting off the page behaves as before.
- Starts, stops, pauses and mutes from the page. No terminal window stays open.
- Says so when the capture dies. hark can lose the call side and go on reporting `recording`; the page shows a red banner when hark proves it, and an amber note when it can only guess.
- Restarts a broken capture into the same call folder, so one call stays one folder on one clock.
- Files every call in its own folder, keeps the audio, and writes the accurate transcript on its own once the call stops.
- Runs on `127.0.0.1` only. Nothing leaves the machine.

## Requirements

- macOS 14.4 or later on Apple Silicon
- [hark](https://github.com/PhantomYdn/hark) 0.4.3 or later, with the Parakeet model:

  ```sh
  brew tap PhantomYdn/hark https://github.com/PhantomYdn/hark
  brew install phantomydn/hark/hark
  hark models download parakeet:v3 --default
  hark models download fluidaudio:diarizer
  ```

  Recording, the live transcript and everything on the page work on 0.4.3. The accurate transcript
  written after a call needs a hark whose `--speaker-mode source` reads both channels of a file,
  which is not released yet; point `HARK_BIN` at such a build, see [below](#getting-your-own-voice-back-out).
  Without one that step still runs, hears the microphone alone, and says so as a warning.

- Python 3. The one macOS ships is enough, and there are no packages to install.

## Use

```sh
git clone https://github.com/GeiserX/hark-viewer.git
cd hark-viewer

./hark-viewer work "Weekly sync"   # record a call filed under "work" and open the page
./hark-viewer open                 # open the page without recording
./hark-viewer stop                 # stop and save the call
./hark-viewer restart              # capture broke mid-call: stop, and record on into the same folder
./hark-viewer quit                 # stop the call, the page server and the hark agent
./hark-viewer relabel              # fix the speaker labels of the call just finished
./hark-viewer finalize             # write the accurate transcript by hand (a stopped call gets it on its own)
./hark-viewer languages            # say which languages a call was in, and write nothing
```

The first recording asks for the Microphone and System Audio Recording permissions. macOS attributes them to the terminal app you ran the command from.

You can also start from the page. Pick a folder, type a title and press **Record**.

Three things can be asked of the page through its URL. `?call=workspace/name` opens that call
instead of the current one, `?workspace=name` puts that folder at the top of the picker even
before any call has been filed under it, and `?quiet=SECONDS` changes how long the page waits
before it guesses that a capture has died.

![A saved call, ready to record the next one](docs/images/saved.png)

## Where calls go

```
~/Recordings/calls/<workspace>/<YYYY-MM-DD_HHMMSS>[_title]/
    audio.opus         the recording, microphone left, call right
    transcript.json    one JSON object per line: {"start", "end", "speaker", "text"}
    meta.json          {"started", "workspace", "title", "id"}, plus "parts" once the call was restarted
    transcript.final.json   the accurate transcript, written after the call stops
    transcript.mw.txt       MacWhisper's transcript of the same audio, when MacWhisper is installed
    postprocess.json        how far those two have got, and which languages the call was in
~/Recordings/calls/current  ->  the call being recorded, or the last one
```

`transcript.json` is JSON Lines. hark appends a complete line per utterance, so any program can read the file during the call. `start` and `end` are seconds into the recording; add `start` to `started` in `meta.json` to get the clock time.

The audio is Opus because an Opus file stays playable while hark is still writing it, so a crash mid-call costs nothing. `.m4a` and `.flac` hold back their header until hark stops, and `.wav` would cost 635 MB an hour against Opus's 23. macOS types the file as `org.xiph.ogg-audio`, so transcription apps open it like any other recording.

## When the capture dies mid-call

hark's tap on the call side can go silent while hark still reports `recording`. On a 71-minute call it happened twice, and nothing on the page said so.

A hark that checks its own capture reports it in `/status` as `session.callAudio`, `{"state", "silentFor", "restarts"}`, and the page shows it under the header:

| `state` | Means | Page |
|---|---|---|
| `ok` | audio is arriving | nothing |
| `silent` | the call side is quiet, and hark checked that nothing is playing | grey note, `call side quiet for 00:42` |
| `dead` | audio is playing and the capture hears none of it; hark is restarting the capture | red banner |
| `recovered` | the capture is back | green note with the restart count |

A hark without `callAudio` leaves the page to guess. After 90 seconds of recording with no new line and no open line it shows an amber note, `no new lines for 01:35`. Amber because nobody talking before a meeting starts looks the same from the page as a dead capture. `?quiet=SECONDS` on the page URL changes the 90.

hark can also answer a start and then disown the capture behind it, keeping the session at `recording` and setting `session.capturing` to false. That is not a recording, and it is treated as one nowhere: a start answered that way is a failure rather than a call folder, `/api/status` reports `active: false`, and a call already on the page turns red and says nothing is being recorded.

**Restart** on the page, or `./hark-viewer restart`, stops the recording and starts a new one into the same call folder. hark never overwrites a file, so the new part gets new names and `meta.json` lists them:

```
audio.opus        transcript.json          part 1, the names every call has
audio.part2.opus  transcript.part2.json    part 2, and so on

meta.json  "parts": [{"n": 1, "started": 1790000000.0, "audio": "audio.opus",       "transcript": "transcript.json"},
                     {"n": 2, "started": 1790001173.2, "audio": "audio.part2.opus", "transcript": "transcript.part2.json"}]
```

A call never restarted has no `parts` key and reads as it always did. The server answers `GET /<call>/transcript.json` for a restarted call with every part's lines joined, each part moved onto the call's clock by `part.started - meta.started`. The page reads that URL, so it shows one continuous transcript. A program reading the folder does the same sum, or asks the server. Restart also works after hark reports the session `failed`, after hark disowns its capture, and after the agent died, when it goes on in the call `current` points at. It refuses a call that already has its `postprocess.json`. With no live session it also refuses a call whose audio was last written over an hour ago, because `current` can point at an old call. `./hark-viewer restart --force` records on into it anyway, and the page has no such override.

The page shows the Restart button whenever the call on screen is the one the server is on and has no accurate transcript yet, which includes a dead agent, when there is no session at all and Restart is the only thing that recovers the call. Mute and Pause are disabled while the call recording is not the one on screen, because they would otherwise reach the other call silently; only Stop says which call it means.

The new part is written into `meta.json` before hark is asked to start it, so a crash between the two leaves a part that is listed rather than a recording no reader will ever find. If the start fails it is taken back out.

A start is answered only once hark's capture is open, so nothing said after the page says Recording is lost. Opening it takes a moment, and a recognizer model that is not in memory yet took 12.7 seconds on the first streamed call after a reboot, so the wait for that answer is 90 seconds (`HARK_VIEWER_START_TIMEOUT`) and not the 10 the other calls use. With a shorter wait than the start's own time, a recording that did begin is reported as a failure. And when the answer itself goes wrong for a capture that did begin, as hark's HTTP server does when its own ceiling on a handler cuts the start off with a 500, the page asks hark what it is recording and carries on with that. It keeps asking for a couple of seconds (`HARK_VIEWER_DISOWNED_WAIT`), because a capture that is still opening is not in `/status` yet and one look would call it a failure.

The waiting only works if the client waits too. The server adds up its own worst case for one start and reports it as `patience` in `/api/status`. That sum walks the whole chain, the agent check, the status read, the wait for a capture that is still finishing, the relaunch and the start after it, rather than estimating, because a number smaller than the chain reaches you as a timeout on a call that was recording. `./hark-viewer` reads that number and hands it to curl as the request's own limit. One number, both sides, so a start the server is still waiting out never reaches you as a timeout.

hark answers `stopped` the moment a stop is asked for. Its capture finishes writing the audio afterwards, `/status` does not show that, and until it is done `/start` answers 409 "still finishing". So a restart, and a new call, keep asking for up to 15 seconds (`HARK_VIEWER_STOP_WAIT`). hark itself gives up on a capture after 10 seconds and then refuses every start until its agent is restarted, so past the 15 the server kills the agent on hark's port, and only a process whose command line says `--remote-control`, starts a new one and asks once more. A fresh agent gets 30 seconds to answer (`HARK_VIEWER_AGENT_WAIT`), and a hark slower than that is waited for rather than started a second time. The old agent has 10 seconds to let go of the port (`HARK_VIEWER_DIE_WAIT`). An agent that outlives the kill still holds it, so nothing fresh can bind it, and the restart then says the agent did not come back rather than handing the wedged one back. A hark that is not installed at all leaves the page server running, because saying so is the page's job.

## The accurate transcript

The live transcript is the fast pass. When a call ends the server starts [`postprocess.py`](postprocess.py) as a detached process. Every ending counts, including one no server was running for: at startup the server looks at the call `current` points at, and writes the accurate transcript for it if hark is not recording it and it has none. A stop from the page, `hark-viewer stop` or `hark-viewer quit` starts the job at once. The other endings are hark reporting the session `failed`, hark disowning its capture, and the agent dying with the call still open. Those wait a minute first (`HARK_VIEWER_ENDED_GRACE`). That minute belongs to Restart, which records on into the same call, and a call that already has its accurate transcript will not be restarted. It outlives the page and the server, runs at low priority, and never touches the audio or `transcript.json`. Because `stopped` comes before the audio is complete, the job first waits until no part's audio has changed for 5 seconds, for at most 120, and records that wait as `settled: {"waited", "capped"}`. `hark-viewer quit` waits the same way before it kills the agent. The job writes:

- `transcript.final.json`, from `hark -i <audio> --speakers --speaker-mode source --speaker-labels "Microphone,Others"` over each part, joined on the call's clock. JSON Lines with the keys of `transcript.json`. It needs a hark whose `--speaker-mode source` reads a file's two channels, see [below](#getting-your-own-voice-back-out).
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

## How it fits together

```
browser page  ──►  server.py :8474  ──►  hark --remote-control :8473
     ▲                  │                          │
     └── transcript ────┴──── reads ◄── writes ────┘
                     ~/Recordings/calls/…
```

[`server.py`](server.py) is a single standard-library Python file, and the page is [`viewer.html`](viewer.html). The server serves the page and the call folders, and forwards the control requests to hark's [remote-control agent](https://github.com/PhantomYdn/hark/blob/main/docs/remote-control.md). The page cannot call the agent directly because the agent sends no CORS headers. The server starts the agent when it is not running.

### API

| Request | Does |
|---|---|
| `GET /api/status` | Agent state, the active session, the call it belongs to, its number of `parts`, the `postprocess` state of that call, the workspace folders, and `patience`, the longest a start can take. The session goes through as hark sent it, so it carries `partial` while a streaming hark has a line open, `callAudio` on a hark that checks its capture and `capturing` on one that disowns it, and none of those keys otherwise |
| `POST /api/new` with `{"workspace", "title"}` | Creates the call folder and starts recording |
| `POST /api/restart` | Stops the recording and starts the next part in the same call folder. Answers `{"call", "part"}`, or 409 when nothing is recording |
| `POST /api/stop`, `/pause`, `/resume`, `/mute`, `/unmute` | Forwarded to hark |
| `GET /<call>/transcript.json` | The live transcript. For a restarted call, every part joined on the call's clock |

Every `POST` needs the header `X-Hark-Viewer: 1`.

### Settings

| Variable | Default | |
|---|---|---|
| `HARK_VIEWER_ROOT` | `~/Recordings/calls` | Where calls are filed |
| `HARK_VIEWER_PORT` | `8474` | Port of the page |
| `HARK_REMOTE_CONTROL_PORT` | `8473` | Port of hark's agent |
| `HARK_VIEWER_BROWSER` | `Firefox` | App that opens the page; the system default is used when it is missing, and `off` records without opening anything |
| `HARK_BIN` | `hark` | The hark binary to run. Point it at your own build to run an unreleased hark |
| `HARK_VIEWER_CONFIG` | `~/.config/hark-viewer.env` | The settings file the launcher reads |
| `HARK_VIEWER_MW` | `/Applications/MacWhisper.app/Contents/MacOS/mw` | MacWhisper's command line, for `transcript.mw.txt`. `off` skips that step |
| `HARK_VIEWER_PY3` | `/usr/bin/python3` | The interpreter that carries the PyObjC bridge to Apple's language recognizer |

Timing, all in seconds. The defaults are what a real call needs, and nothing here has to be set.

| Variable | Default | |
|---|---|---|
| `HARK_VIEWER_START_TIMEOUT` | `90` | How long to wait for hark to answer a start. hark answers only once the capture is open, and a recognizer model that is not in memory yet took 12.7 s on the first streamed call after a reboot |
| `HARK_VIEWER_STOP_WAIT` | `15` | How long a start waits out a capture that is still finishing before the agent is relaunched |
| `HARK_VIEWER_AGENT_WAIT` | `30` | How long a freshly started hark agent gets to answer |
| `HARK_VIEWER_DIE_WAIT` | `10` | How long a killed agent gets to let go of hark's port. Past it the relaunch fails rather than handing the same agent back |
| `HARK_VIEWER_DISOWNED_WAIT` | `2` | How long a start this side gave up on is watched for, in case hark is still opening its capture |
| `HARK_VIEWER_ENDED_GRACE` | `60` | How long an ending other than a stop is left for a Restart before the accurate transcript is written |
| `HARK_VIEWER_WATCH` | `2` | Between looks at hark for a call that ended |
| `HARK_VIEWER_SETTLE` | `5` | A recording unchanged for this long is finished, so the offline passes may read it |
| `HARK_VIEWER_SETTLE_CAP` | `120` | And past this they go on regardless, which is recorded as `settled.capped` |
| `HARK_VIEWER_TOOL_TIMEOUT` | `7200` | Deadline for one hark or `mw` pass over a whole call |
| `HARK_VIEWER_PROBE_TIMEOUT` | `120` | Deadline for `ffprobe`, `ffmpeg` and the language recognizer |

And three the offline pass uses to decide what it reads: `HARK_VIEWER_CHUNK` (600) is how long a piece is when hark has refused a whole part, `HARK_VIEWER_MIN_PIECE` (20) how short a piece has to get before a failure is skipped rather than halved again, and `HARK_VIEWER_LANG_MIN_CHARS` (25) how long a line must be to be worth judging a language from.

Set any of these in the environment, or in `~/.config/hark-viewer.env`, which the launcher reads if it exists and exports whole, so a setting only the page server or the offline job reads still reaches it. A server already running keeps the settings it started with, so `./hark-viewer quit` before changing one.

The `START` dictionary at the top of [`server.py`](server.py) holds what hark records with: system audio with the microphone mixed in, speaker labels, the Core Audio backend, `tracks: stereo` so the microphone and the call stay on their own channels, `liveStreaming` for the line still being spoken, and `ifExists: error` so hark never writes over a recording. A hark that does not know a key ignores it.

## As an agent skill

The repository root is a skill. [`SKILL.md`](SKILL.md) sits next to the [`hark-viewer`](hark-viewer) command it runs. For Claude Code:

```sh
git clone https://github.com/GeiserX/hark-viewer.git ~/.claude/skills/record-call
```

Then `/record-call` starts a recording. While the call runs, ask the agent what was just said or what was decided, and it reads `~/Recordings/calls/current/transcript.json` before answering.

### Fixing the speakers after the call

Live speaker numbers are guessed as the audio arrives, so a long call with several voices reuses one number for two people. A diarizer that gets the whole recording at once does better:

```sh
./hark-viewer relabel                                   # the call in `current`
./hark-viewer relabel work/2026-09-21_101500 --dry-run   # counts only, writes nothing
```

[`relabel_speakers.py`](relabel_speakers.py) splits the call side of `audio.opus`, runs it through hark, and writes two files next to the recording:

- `speakers.json`, the spans it found, so a second run needs no model
- `transcript.speakers.json`, the live lines with the speaker of the span each one overlaps most

It never touches `transcript.json`, and it leaves lines labelled `You` alone. [`server.py`](server.py) serves `transcript.speakers.json` in place of `transcript.json` when it exists, so the page and any agent reading the call get the better labels for free. A page already on screen keeps the rows it has drawn. Reload for the new colours. Run it once the call is over: a line hark appends after the relabel makes the live file the newer one, and the newer file is the one served. Run straight after Stop it waits for the recording to stop changing first, the same way the accurate pass does, and says how long it waited.

On the eight-person call this was built against, the live pass used three speaker numbers and the offline pass found all seven. 59 of the 69 non-`You` lines matched a span and the other 10 kept their live label. The run took eleven seconds. `relabel` exits 3 and writes nothing when fewer than 60% of the lines match, which catches spans belonging to a different recording.

### Getting your own voice back out

The recording keeps the microphone on channel 0 and the call on channel 1, so a manual pass can still tell them apart:

```sh
ffmpeg -i audio.opus -af "pan=mono|c0=c1" call.wav   # just the call (c0=c0 for your microphone)
hark -i audio.opus --speakers --speaker-mode source -t final.json   # You / Others
```

**On hark 0.4.3, `hark -i audio.opus` transcribes your microphone and loses the call**, because it reads channel 0 of a stereo file and stops there. The result looks like a call nobody else spoke on. On one recording the stereo file gave 1544 characters of text, and the call channel alone gave 7891. Split the channel with `ffmpeg` first, which is what `relabel` does, or use a hark built from [the pull requests](https://github.com/PhantomYdn/hark/issues/6) that read both channels, named through `HARK_BIN`. `--speaker-mode source` on a file is unreleased for the same reason.

A hark built from those branches used to stop the offline pass with `Must be at least 300ms of 16kHz audio`, because a diarized span can come out shorter than the recognizer accepts. That is fixed: a short span is padded with silence up to the recognizer's floor, so it is transcribed instead of killing the run. On a build from before that fix, `relabel --spans FILE` takes spans from any build that works.

## Tests

```sh
python3 -m unittest discover tests
```

They need `ffmpeg`, `lsof` and `zsh`, and no hark. A fake hark, a fake `mw`, a fake diarizer and a fake remote-control agent on spare ports stand in, so they never touch a live recording or ports 8473 and 8474. The language tests also need `/usr/bin/python3` with the PyObjC bridge and skip themselves without it, which is quiet enough to miss.

Run them under `/usr/bin/python3` as well as whichever `python3` is on your PATH. That one is 3.9, it is what the language step always uses, and syntax newer than 3.9 passes the first run and fails the second. [GitHub Actions](.github/workflows/ci.yml) runs both on every pull request and on every push to `main`, one job per interpreter, on a macOS runner, because `st_birthtime`, `lsof` and the PyObjC bridge have no counterpart elsewhere. The step also fails if fewer than fifty tests ran, since `unittest discover` reports success against a tests directory that has been moved.

## Limits

- Live speaker numbers are a guess made as the audio arrives, and two voices can share a number. `hark-viewer relabel` fixes them once the call is over. A line that spans two offline speakers gets the one it overlaps most, because splitting it would need word timings `transcript.json` does not carry.
- Speaker numbers start over in every part of a restarted call, so `Speaker 1` before a restart and after it can be two people. `relabel` covers part 1 only. `transcript.final.json` avoids both, because it only tells `Microphone` from `Others`.
- `hark-viewer quit` used to kill every `hark` process. It now kills only the remote-control agent, so an offline pass still running for the last call survives it.
- Relabelling is manual. Most of its ten seconds goes on the recognizer producing text the relabel then throws away, because hark has no way to diarize a file without transcribing it. A `hark speakers -i FILE` would make this near instant.
- A line appears when the speaker pauses for about 0.7 seconds, or after 12 seconds of unbroken speech. Those two values are fixed inside hark.
- hark records one microphone, the macOS default input.
- Clock times drift by the length of any pause, because hark leaves paused time out of the recording.
- Recording a call needs the consent of the people on it. The rules depend on where you and they are.

## License

[GPL-3.0](LICENSE)
