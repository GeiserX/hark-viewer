---
hide:
  - navigation
---

# hark-viewer { .hv-visually-hidden }

<p align="center">
  <img src="images/banner.svg" alt="hark-viewer: Watch your call's transcript as it's spoken" width="100%">
</p>

<p align="center">
  <a href="https://github.com/GeiserX/hark-viewer/actions/workflows/ci.yml"><img alt="Tests" src="https://img.shields.io/github/actions/workflow/status/GeiserX/hark-viewer/ci.yml?branch=main&style=flat-square&logo=github&label=Tests"></a>
  <a href="https://github.com/GeiserX/hark-viewer/stargazers"><img alt="GitHub Stars" src="https://img.shields.io/github/stars/GeiserX/hark-viewer?style=flat-square&logo=github"></a>
  <a href="https://github.com/GeiserX/hark-viewer/blob/main/LICENSE"><img alt="License: GPL-3.0-or-later" src="https://img.shields.io/github/license/GeiserX/hark-viewer?style=flat-square"></a>
</p>

---

**hark-viewer** shows a call's transcript in the browser while [hark](https://github.com/PhantomYdn/hark), the macOS command line that records system audio and the microphone and transcribes them on-device, is still writing it. Without it you read a growing JSON file, and a capture that lost the call side looks like a quiet call, because hark can go on reporting `recording`. hark-viewer gives every speaker a colour and every line a clock time, puts Record, Stop, Pause and Mute on the same page, says so when the capture dies, and records on into the same call folder. Start with [Getting started](getting-started.md), then [Usage](usage.md).

<div class="grid cards" markdown>

-   :material-download: **[Getting started](getting-started.md)**

    ---

    macOS 14.4 or later on Apple Silicon, hark 0.4.3 with the Parakeet model, and a clone. Nothing to build or install.

-   :material-record-rec: **[Your first recording](getting-started.md#first-recording)**

    ---

    One command, the two macOS permissions it asks for, and what the page shows once it records.

-   :material-console: **[Usage](usage.md)**

    ---

    Every command, the three URL options of the page, and the folder each call is filed in.

-   :material-format-list-bulleted: **[Configuration](configuration.md)**

    ---

    Every setting with its default, from the port to the timeouts.

</div>

## The page

![The page half an hour into a call: the REC header with Mute mic, Pause, Restart and Stop, five lines from You, Speaker 1 and Speaker 2 with their clock times, and the line still being spoken in grey](images/screenshots/recording.png){ .hv-shot }

A line appears a moment after its speaker pauses, labelled `You` for your microphone and `Speaker 1..N` for voices on the computer side. When hark can stream, the line still being spoken shows in grey under the finished ones.

![The same call when the capture lost the call side: a red banner under the header reads CALL AUDIO LOST for 00:42, says hark is restarting the capture, and says to press Restart if it stays](images/screenshots/capture-dead.png){ .hv-shot }

When the capture dies the page says so, in red when hark proves it and in amber when it can only guess, and **Restart** records on into the same call folder, so one call stays one folder on one clock ([Troubleshooting](troubleshooting.md#when-the-capture-dies-mid-call)).

![The page after Stop: SAVED, 5 lines, final transcript: ready, and the folder picker, title field and Record button for the next call](images/screenshots/saved.png){ .hv-shot }

## Ask an agent during the call

The repository is also an agent skill. Clone it into `~/.claude/skills/record-call` and `/record-call` in Claude Code starts a recording. While the call runs, ask the agent what was just said or what was decided, and it reads the live transcript before it answers. See [As an agent skill](agent-skill.md).

## After the call

- The [accurate transcript](accurate-transcript.md) is written on its own once the call stops, from the whole recording, into `transcript.final.json`. Its call side needs a hark newer than 0.4.3; on 0.4.3 it hears the microphone alone and says so ([why](agent-skill.md#getting-your-own-voice-back-out)).
- `./hark-viewer relabel` [fixes the live speaker numbers](agent-skill.md#fixing-the-speakers-after-the-call) with a diarizer that hears the whole call at once.
- `./hark-viewer languages` [says which languages the call was in](accurate-transcript.md#which-languages-the-call-was-in), using Apple's on-device language recognizer.

## How it runs

```mermaid
flowchart LR
    B[Browser page]
    S[server.py<br/>one standard-library Python file]
    H[hark agent<br/>remote control]
    F[(Call folders<br/>~/Recordings/calls)]
    B <-->|127.0.0.1:8474| S
    S <-->|127.0.0.1:8473| H
    H -->|writes audio and transcript| F
    S -->|reads| F
```

- The server serves the page and the call folders, and passes Record, Stop, Pause and Mute on to hark's agent, which it starts when it is not running. [How it works](how-it-works.md) has the [API](how-it-works.md#api).
- Every call gets its own folder with its audio, its live transcript and its metadata. [Where calls go](usage.md#where-calls-go) lists the files.
- Python 3 as macOS ships it, with no packages to install, and a zsh launcher. Nothing to build.

## What it does not do

- It runs on macOS 14.4 or later on Apple Silicon only, because hark's Homebrew binary and the Parakeet model are Apple Silicon only.
- It records one microphone, the macOS default input.
- Live speaker numbers are a guess, and they start over in every part of a restarted call. `relabel` fixes part 1 after the call.
- The full list, with the reasons, is on [Limits](limits.md).

## Privacy

- The page server listens on `127.0.0.1` only, refuses any other `Host` name, and needs the header `X-Hark-Viewer: 1` on every request that changes something.
- hark transcribes on the Mac, and the audio and transcripts stay in `~/Recordings/calls`. Nothing leaves the machine.
- Recording a call needs the consent of the people on it. The rules depend on where you and they are.

## Getting help

- The capture died or the page shows a warning: [Troubleshooting](troubleshooting.md). To report a bug, [what to include](troubleshooting.md#reporting-a-bug).
- A security problem: the [security policy](https://github.com/GeiserX/hark-viewer/blob/main/SECURITY.md), never a public issue.
- Running the tests and sending a fix: [Development](development.md).

## License

hark-viewer is released under the [GPL-3.0-or-later](https://github.com/GeiserX/hark-viewer/blob/main/LICENSE) license. It drives [hark](https://github.com/PhantomYdn/hark), which is a separate project.
