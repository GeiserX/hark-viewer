---
name: record-call
description: Record a call (whole computer + mic) with hark, show the live transcript in the browser, and answer questions about the call in progress from that transcript. Use when the user says record this call/meeting, starts a call, or asks what was said or what is being discussed during a call.
---

# record-call

One **call** = one folder. `hark` records every app's audio plus the mic and appends the transcript one utterance at a time while people speak, so the transcript is readable mid-call.

```
~/Recordings/calls/<workspace>/<YYYY-MM-DD_HHMMSS>[_title]/
    audio.opus         grows during the call; microphone left, call right
    transcript.json    live, one JSON object per line: {"start","end","speaker","text"}
    meta.json          {"started": epoch seconds, "workspace", "title"}
    transcript.final.json, transcript.mw.txt, postprocess.json   appear on their own after Stop
~/Recordings/calls/current  ->  the call being recorded (or the last one)
```

A call that was restarted also holds `audio.partN.opus` and `transcript.partN.json`, listed under `parts` in `meta.json`. It is still one call on one clock.

`<workspace>` is the folder the call is filed under. Use the name of the workspace or client the call belongs to; it defaults to `calls`.

## Start

```sh
<this skill's directory>/hark-viewer <workspace> [title words]
```

It starts the page server and hark's remote-control agent if they are down, begins recording, opens the live page in the browser and prints `{"call", "folder", "url"}`. Exit 75 means a call is already recording; the message names it.

Done when `curl -s --noproxy '*' http://127.0.0.1:8474/api/status` reports `"active": true` with the new call. Then tell the user the folder, and that Stop, Pause and Mute are buttons on the page.

## Answer a question during the call

Read the transcript first, every time: `curl -s --noproxy '*' http://127.0.0.1:8474/current/transcript.json`. The server joins the parts of a restarted call onto the call's clock; the file on disk holds part 1 only. Take the last lines for "what are they saying now" and the whole file for "what did we decide". Answer from the transcript and quote the line you relied on.

The server already serves `transcript.speakers.json` in place of the live file once `hark-viewer relabel` has written it.

On a hark set to stream, `/api/status` also carries the line still being spoken, as `session.partial`. That text is unfinished, so use it for "what are they saying right now" and never for a decision.

`You` is the user's microphone. `Speaker N` are voices on the computer side, numbered live, so two similar voices can share a number, and a name exists only once the user gives one ("Speaker 2 is Alex"). `start` is seconds into the call; add it to `started` in `meta.json` for the clock time.

The live text comes from Parakeet v3, multilingual, language detected per utterance. Treat an odd word as a mishearing and say so rather than building on it.

## When the capture breaks

hark can lose the call side while it still says `recording`. `/api/status` reports it as `session.callAudio`: `{"state", "silentFor", "restarts"}`.

- `ok` and `recovered` need nothing.
- `silent` is a quiet call, checked by hark. Normal before people join.
- `dead` means audio is playing and hark hears none of it. The page turns red and hark restarts its capture. Tell the user at once. If it is still `dead` a minute later, run `hark-viewer restart`.

An older hark sends no `callAudio`. The page then shows an amber "no new lines for 1:35" note after 90 s without a line. That is a guess, so ask the user whether people are talking before you restart.

`hark-viewer restart` stops the recording and starts a new part in the same call folder. Done when it prints `{"call", "part"}` and `/api/status` reports `"active": true` with the same call. Use it instead of stop and start, which makes a second call folder on a second clock.

## Stop

The user presses Stop on the page, or run `hark-viewer stop`. Done when `/api/status` reports `"active": false` and the session `"state": "stopped"`, which is when hark finalises `audio.opus`. `hark-viewer quit` also shuts down the page server and the agent.

## After the call

The accurate transcript writes itself. When a call stops, the server starts a background job that runs hark's offline pass over every part and writes `transcript.final.json` into the call folder: JSON Lines like `transcript.json`, on the call's clock, speakers `Microphone` (the user) and `Others`. `transcript.mw.txt` is MacWhisper's pass over the same audio, kept to compare against.

Wait on `postprocess.json` in the call folder, or on `postprocess` in `/api/status`. Done when its `state` is `done` or `failed`. Allow as long as the call lasted. Then read `steps.final`: `skipped_spans` lists the seconds hark refused even in 20 s pieces, and `error` says why a step failed. `steps.mw.state` is `skipped` when MacWhisper is missing or `HARK_VIEWER_MW=off`. Use `transcript.final.json` for anything written after the call. To run the job by hand, or again: `hark-viewer finalize [call] [--force]`.

`audio.opus` costs about 23 MB per hour. The live transcript's speaker numbers are guessed as the audio arrives, so a long call reuses a number for two people. To fix them in the live transcript:

```sh
<this skill's directory>/hark-viewer relabel            # the call in `current`
<this skill's directory>/hark-viewer relabel --dry-run  # counts only, writes nothing
```

It writes `transcript.speakers.json` next to the live file and leaves `transcript.json` alone. It covers part 1 only. It prints how many lines matched a span and exits 3 when too few did. Lines it cannot place keep their live label.

Delete a recording only when the user says so.

## Traps

- **hark answers a started recording with HTTP 201**, not 200.
- **Opus on purpose.** It stays playable while hark writes it, so a crash costs nothing. `.m4a` and `.flac` hold back the header until hark stops, and `.wav` costs 635 MB an hour.
- **The agent never overwrites.** Every call gets a fresh folder and every restart a fresh part number; keep it that way.
- **Speaker numbers start over in each part**, so `Speaker 1` before a restart and after it can be two people.
- **`session.state: "failed"`** in `/api/status` carries the reason in `session.error`. The page shows it too.
- **The shell may export an HTTP proxy.** Talk to `127.0.0.1` with `curl --noproxy '*'`.
- **`hark -i audio.opus` on hark 0.4.3 reads only channel 0, the user's microphone**, so the call side disappears and the transcript looks like a call nobody else spoke on. Split the channel first (`ffmpeg -i audio.opus -af "pan=mono|c0=c1" call.wav`), which is what `hark-viewer relabel` does.
- **Consent:** recording a call needs the other side's agreement. Remind the user once when a call has outside participants.
