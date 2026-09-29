# Troubleshooting

## When the capture dies mid-call

hark's tap on the call side can go silent while hark still reports `recording`. On a 71-minute call it happened twice, and nothing on the page said so.

A hark that checks its own capture reports it in `/status` as `session.callAudio`, `{"state", "silentFor", "restarts"}`, and the page shows it under the header:

| `state` | Means | Page |
|---|---|---|
| `ok` | audio is arriving | nothing |
| `silent` | the call side is quiet, and hark checked that nothing is playing | grey note, `call side quiet for 00:42` |
| `dead` | audio is playing and the capture hears none of it; hark is restarting the capture | red banner |
| `recovered` | the capture is back | green note with the restart count |
| `unknown` | nothing has been measured: no non-zero sample has arrived at all, which is what a missing or stale System Audio Recording grant looks like | nothing for the first 20 seconds, then a grey note pointing at System Settings > Privacy & Security |

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

## Reporting a bug

Open an [issue](https://github.com/GeiserX/hark-viewer/issues) with:

- the hark version (`brew list --versions hark`, or the commit you built) and the macOS version
- the JSON `/api/status` returns (`curl -s http://127.0.0.1:8474/api/status`)
- what the launcher printed to the terminal
- whether the call was restarted, and `meta.json` of the call if it was
