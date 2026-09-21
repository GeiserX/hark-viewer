# hark-viewer

A live transcript page and recording controls for [hark](https://github.com/PhantomYdn/hark), the macOS CLI that captures system audio and the microphone and transcribes them on-device.

hark writes a call's transcript to a file while people are still speaking. hark-viewer shows that file in the browser as it grows, with a colour per speaker and a clock time per line, and puts Record, Stop, Pause and Mute on the same page. It also ships an [agent skill](SKILL.md), so a coding agent such as Claude Code can start a recording and answer questions about the call while it is happening.

![A call being recorded](docs/images/recording.png)

## What it does

- Records the whole computer plus your microphone through hark's Core Audio tap. No per-app tracking, no virtual audio driver.
- Keeps your microphone on the left channel and the call on the right, so a later pass can still tell them apart.
- Shows each utterance a moment after the speaker pauses, labelled `You` for the microphone and `Speaker 1..N` for voices on the computer side.
- Shows the line still being spoken in grey under the finished ones, when hark reports one. Run `hark config set live-streaming true` once and the next recording streams. Text then lands about 2.5 seconds behind the audio and the open line grows in place until it closes. It is never corrected as it grows. With the setting off the page behaves as before.
- Starts, stops, pauses and mutes from the page. No terminal window stays open.
- Files every call in its own folder and keeps the audio, so you can run it through a larger model afterwards.
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

- Python 3. The one macOS ships is enough, and there are no packages to install.

## Use

```sh
git clone https://github.com/GeiserX/hark-viewer.git
cd hark-viewer

./hark-viewer work "Weekly sync"   # record a call filed under "work" and open the page
./hark-viewer open                 # open the page without recording
./hark-viewer stop                 # stop and save the call
./hark-viewer quit                 # stop the call, the page server and the hark agent
./hark-viewer relabel              # fix the speaker labels of the call just finished
```

The first recording asks for the Microphone and System Audio Recording permissions. macOS attributes them to the terminal app you ran the command from.

You can also start from the page. Pick a folder, type a title and press **Record**.

![A saved call, ready to record the next one](docs/images/saved.png)

## Where calls go

```
~/Recordings/calls/<workspace>/<YYYY-MM-DD_HHMMSS>[_title]/
    audio.opus         the recording, microphone left, call right
    transcript.json    one JSON object per line: {"start", "end", "speaker", "text"}
    meta.json          {"started", "workspace", "title"}
~/Recordings/calls/current  ->  the call being recorded, or the last one
```

`transcript.json` is JSON Lines. hark appends a complete line per utterance, so any program can read the file during the call. `start` and `end` are seconds into the recording; add `start` to `started` in `meta.json` to get the clock time.

The audio is Opus because an Opus file stays playable while hark is still writing it, so a crash mid-call costs nothing. `.m4a` and `.flac` hold back their header until hark stops, and `.wav` would cost 635 MB an hour against Opus's 23. macOS types the file as `org.xiph.ogg-audio`, so transcription apps open it like any other recording.

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
| `GET /api/status` | Agent state, the active session, the call it belongs to, and the workspace folders. The session carries `partial` while a streaming hark has a line open, and no such key otherwise |
| `POST /api/new` with `{"workspace", "title"}` | Creates the call folder and starts recording |
| `POST /api/stop`, `/pause`, `/resume`, `/mute`, `/unmute` | Forwarded to hark |

Every `POST` needs the header `X-Hark-Viewer: 1`.

### Settings

| Variable | Default | |
|---|---|---|
| `HARK_VIEWER_ROOT` | `~/Recordings/calls` | Where calls are filed |
| `HARK_VIEWER_PORT` | `8474` | Port of the page |
| `HARK_REMOTE_CONTROL_PORT` | `8473` | Port of hark's agent |
| `HARK_VIEWER_BROWSER` | `Firefox` | App that opens the page; the system default is used when it is missing |
| `HARK_BIN` | `hark` | The hark binary to run. Point it at your own build to run an unreleased hark |

Set any of these in the environment, or in `~/.config/hark-viewer.env`, which the launcher reads if it exists.

The `START` dictionary at the top of [`server.py`](server.py) holds what hark records with. By default that is system audio with the microphone mixed in, speaker labels, and the Core Audio backend.

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

It never touches `transcript.json`, and it leaves lines labelled `You` alone. [`server.py`](server.py) serves `transcript.speakers.json` in place of `transcript.json` when it exists, so the page and any agent reading the call get the better labels for free. A page already on screen keeps the rows it has drawn. Reload for the new colours. Run it once the call is over: a line hark appends after the relabel makes the live file the newer one, and the newer file is the one served.

On the eight-person call this was built against, the live pass used three speaker numbers and the offline pass found all seven. 59 of the 69 non-`You` lines matched a span and the other 10 kept their live label. The run took eleven seconds. `relabel` exits 3 and writes nothing when fewer than 60% of the lines match, which catches spans belonging to a different recording.

### Getting your own voice back out

The recording keeps the microphone on channel 0 and the call on channel 1, so a manual pass can still tell them apart:

```sh
ffmpeg -i audio.opus -af "pan=mono|c0=c1" call.wav   # just the call (c0=c0 for your microphone)
hark -i audio.opus --speakers --speaker-mode source -t final.json   # You / Others
```

**On hark 0.4.3, `hark -i audio.opus` transcribes your microphone and loses the call**, because it reads channel 0 of a stereo file and stops there. The result looks like a call nobody else spoke on. On one recording the stereo file gave 1544 characters of text, and the call channel alone gave 7891. Split the channel with `ffmpeg` first, which is what `relabel` does, or use a hark built from [the pull requests](https://github.com/PhantomYdn/hark/issues/6) that read both channels, named through `HARK_BIN`. `--speaker-mode source` on a file is unreleased for the same reason.

A hark built from those branches can stop the offline pass with `Must be at least 300ms of 16kHz audio`. The recording is fine; the branches drop a diarized span shorter than the recognizer accepts. 0.4.3 never hits it, and `relabel --spans FILE` takes spans from any build that works.

## Limits

- Live speaker numbers are a guess made as the audio arrives, and two voices can share a number. `hark-viewer relabel` fixes them once the call is over. A line that spans two offline speakers gets the one it overlaps most, because splitting it would need word timings `transcript.json` does not carry.
- Relabelling is manual. Most of its ten seconds goes on the recognizer producing text the relabel then throws away, because hark has no way to diarize a file without transcribing it. A `hark speakers -i FILE` would make this near instant.
- A line appears when the speaker pauses for about 0.7 seconds, or after 12 seconds of unbroken speech. Those two values are fixed inside hark.
- hark records one microphone, the macOS default input.
- Clock times drift by the length of any pause, because hark leaves paused time out of the recording.
- Recording a call needs the consent of the people on it. The rules depend on where you and they are.

## License

[GPL-3.0](LICENSE)
