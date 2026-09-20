# hark-viewer

A live transcript page and recording controls for [hark](https://github.com/PhantomYdn/hark), the macOS CLI that captures system audio and the microphone and transcribes them on-device.

hark writes a call's transcript to a file while people are still speaking. hark-viewer shows that file in the browser as it grows, with a colour per speaker and a clock time per line, and puts Record, Stop, Pause and Mute on the same page. It also ships an [agent skill](SKILL.md), so a coding agent such as Claude Code can start a recording and answer questions about the call while it is happening.

![A call being recorded](docs/images/recording.png)

## What it does

- Records the whole computer plus your microphone through hark's Core Audio tap. No per-app tracking, no virtual audio driver.
- Keeps your microphone on the left channel and the call on the right, so a later pass can still tell them apart.
- Shows each utterance a moment after the speaker pauses, labelled `You` for the microphone and `Speaker 1..N` for voices on the computer side.
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
| `GET /api/status` | Agent state, the active session, the call it belongs to, and the workspace folders |
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

### Getting your own voice back out

The recording keeps the microphone on channel 0 and the call on channel 1, so the accurate pass afterwards can still say who spoke:

```sh
hark -i audio.opus --speakers --speaker-mode source -t final.json   # You / Others
ffmpeg -i audio.opus -af "pan=mono|c0=c0" mic.wav                   # just your side (c0=c1 for the call)
```

`--tracks` and `--speaker-mode source` on a file are unreleased, so this needs a hark built from [the pull requests](https://github.com/PhantomYdn/hark/issues/6) that add them, named through `HARK_BIN`. On a hark without `--tracks`, drop that key from `START` in [`server.py`](server.py) and the recording is a normal mixed file.

## Limits

- Live speaker numbers are a guess made as the audio arrives. Two similar voices can share a number. For an accurate transcript, run `audio.opus` through a full transcription pass after the call.
- A line appears when the speaker pauses for about 0.7 seconds, or after 12 seconds of unbroken speech. Those two values are fixed inside hark.
- hark records one microphone, the macOS default input.
- Clock times drift by the length of any pause, because hark leaves paused time out of the recording.
- Recording a call needs the consent of the people on it. The rules depend on where you and they are.

## License

[GPL-3.0](LICENSE)
