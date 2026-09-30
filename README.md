<p align="center">
  <img src="docs/images/banner.svg" alt="hark-viewer" width="100%">
</p>

# hark-viewer

<p align="center">
  <a href="https://github.com/GeiserX/hark-viewer/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/GeiserX/hark-viewer/ci.yml?branch=main&style=flat-square&logo=github&label=Tests" alt="Tests"></a>
  <a href="LICENSE"><img src="https://img.shields.io/github/license/GeiserX/hark-viewer?style=flat-square" alt="License"></a>
</p>

A live transcript page and recording controls for [hark](https://github.com/PhantomYdn/hark), the macOS CLI that captures system audio and the microphone and transcribes them on-device.

hark-viewer shows the transcript in the browser while hark writes it, with a colour per speaker and a clock time per line, and puts Record, Stop, Pause and Mute on the same page. It also ships an [agent skill](SKILL.md), so a coding agent such as Claude Code can start a recording and answer questions about the call while it is happening.

![hark-viewer half an hour into a call: five lines from You, Speaker 1 and Speaker 2 with their clock times, the line still being spoken in grey, and Mute mic, Pause, Restart and Stop in the header](docs/images/screenshots/recording.png)

## Features

- Records the whole computer plus your microphone through hark's Core Audio tap, with no virtual audio driver.
- Keeps your microphone on the left channel and the call on the right.
- Labels lines `You` for the microphone and `Speaker 1..N` for voices on the computer side.
- Shows the line still being spoken in grey, when hark can stream it.
- Starts, stops, pauses and mutes from the page. No terminal window stays open.
- Warns when the capture dies, and restarts it into the same call folder.
- Files every call in its own folder, keeps the audio, and writes an accurate transcript once the call stops (its call side needs a hark newer than 0.4.3).
- Says which languages a call was in, and fixes live speaker labels after the call with `relabel`.
- Runs on `127.0.0.1` only. Nothing leaves the machine.

## Quick start

```sh
git clone https://github.com/GeiserX/hark-viewer.git
cd hark-viewer
./hark-viewer work "Weekly sync"   # record a call filed under "work" and open the page
```

Needs macOS 14.4 or later on Apple Silicon, [hark](https://github.com/PhantomYdn/hark) 0.4.3 or later with the Parakeet model, and the Python 3 macOS ships. Setup is in [Getting started](https://geiserx.github.io/hark-viewer/getting-started/).

## Documentation

The full documentation is at [geiserx.github.io/hark-viewer](https://geiserx.github.io/hark-viewer/).

- [Getting started](https://geiserx.github.io/hark-viewer/getting-started/): requirements, the hark models, first recording
- [Configuration](https://geiserx.github.io/hark-viewer/configuration/): every setting and its default
- [Usage](https://geiserx.github.io/hark-viewer/usage/): every command, URL options, where calls are filed
- [How it works](https://geiserx.github.io/hark-viewer/how-it-works/): the server, its API, what it asks hark to record
- [Troubleshooting](https://geiserx.github.io/hark-viewer/troubleshooting/): when the capture dies mid-call, the warnings, Restart, and how to report a bug
- [Development](https://geiserx.github.io/hark-viewer/development/): running the tests
- [The accurate transcript](https://geiserx.github.io/hark-viewer/accurate-transcript/): the offline pass after a call, and language detection
- [As an agent skill](https://geiserx.github.io/hark-viewer/agent-skill/): Claude Code, fixing speakers, getting your own voice back out
- [Limits](https://geiserx.github.io/hark-viewer/limits/): what it cannot do yet. Recording a call needs the consent of the people on it.

## License

[GPL-3.0-or-later](LICENSE)
