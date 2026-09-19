---
name: record-call
description: Record a call (whole computer + mic) with hark, show the live transcript in the browser, and answer questions about the call in progress from that transcript. Use when the user says record this call/meeting, starts a call, or asks what was said or what is being discussed during a call.
---

# record-call

One **call** = one folder. `hark` records every app's audio plus the mic and appends the transcript one utterance at a time while people speak, so the transcript is readable mid-call.

```
~/Recordings/calls/<workspace>/<YYYY-MM-DD_HHMMSS>[_title]/
    audio.wav          grows during the call; re-transcribe it afterwards
    transcript.json    live, one JSON object per line: {"start","end","speaker","text"}
    meta.json          {"started": epoch seconds, "workspace", "title"}
~/Recordings/calls/current  ->  the call being recorded (or the last one)
```

`<workspace>` is the folder the call is filed under. Use the name of the workspace or client the call belongs to; it defaults to `calls`.

## Start

```sh
<this skill's directory>/hark-viewer <workspace> [title words]
```

It starts the page server and hark's remote-control agent if they are down, begins recording, opens the live page in the browser and prints `{"call", "folder", "url"}`. Exit 75 means a call is already recording; the message names it.

Done when `curl -s --noproxy '*' http://127.0.0.1:8474/api/status` reports `"active": true` with the new call. Then tell the user the folder, and that Stop, Pause and Mute are buttons on the page.

## Answer a question during the call

Read `~/Recordings/calls/current/transcript.json` first, every time. Take the last lines for "what are they saying now" and the whole file for "what did we decide". Answer from the transcript and quote the line you relied on.

`You` is the user's microphone. `Speaker N` are voices on the computer side, numbered live, so two similar voices can share a number, and a name exists only once the user gives one ("Speaker 2 is Alex"). `start` is seconds into the call; add it to `started` in `meta.json` for the clock time.

The live text comes from Parakeet v3, multilingual, language detected per utterance. Treat an odd word as a mishearing and say so rather than building on it.

## Stop

The user presses Stop on the page, or run `hark-viewer stop`. Done when `/api/status` reports `"active": false` and the session `"state": "stopped"`, which is when hark finalises `audio.wav`. `hark-viewer quit` also shuts down the page server and the agent.

## After the call

`audio.wav` costs about 635 MB per hour. The live transcript is the fast pass. The accurate one, with proper speaker separation, comes from running `audio.wav` through the user's transcription app afterwards. Delete or compress a WAV only when the user says so.

## Traps

- **hark answers a started recording with HTTP 201**, not 200.
- **WAV only while recording.** `.m4a` and `.flac` stay unreadable until hark stops, so a crash would lose the call.
- **The agent never overwrites.** Every call gets a fresh folder; keep it that way.
- **`session.state: "failed"`** in `/api/status` carries the reason in `session.error`. The page shows it too.
- **The shell may export an HTTP proxy.** Talk to `127.0.0.1` with `curl --noproxy '*'`.
- **Consent:** recording a call needs the other side's agreement. Remind the user once when a call has outside participants.
