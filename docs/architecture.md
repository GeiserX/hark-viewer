# How it fits together

```
browser page  ──►  server.py :8474  ──►  hark --remote-control :8473
     ▲                  │                          │
     └── transcript ────┴──── reads ◄── writes ────┘
                     ~/Recordings/calls/…
```

[`server.py`](../server.py) is a single standard-library Python file, and the page is [`viewer.html`](../viewer.html). The server serves the page and the call folders, and forwards the control requests to hark's [remote-control agent](https://github.com/PhantomYdn/hark/blob/main/docs/remote-control.md). The page cannot call the agent directly because the agent sends no CORS headers. The server starts the agent when it is not running.

## API

| Request | Does |
|---|---|
| `GET /api/status` | Agent state, the active session, the call it belongs to, its number of `parts`, the `postprocess` state of that call, the workspace folders, and `patience`, the longest a start can take. The session goes through as hark sent it, so it carries `partial` while a streaming hark has a line open, `callAudio` on a hark that checks its capture and `capturing` on one that disowns it, and none of those keys otherwise |
| `POST /api/new` with `{"workspace", "title"}` | Creates the call folder and starts recording |
| `POST /api/restart` | Stops the recording and starts the next part in the same call folder. Answers `{"call", "part"}`, or 409 when nothing is recording |
| `POST /api/stop`, `/pause`, `/resume`, `/mute`, `/unmute` | Forwarded to hark |
| `GET /<call>/transcript.json` | The live transcript. For a restarted call, every part joined on the call's clock |

Every `POST` needs the header `X-Hark-Viewer: 1`.

## Settings

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
| `HARK_VIEWER_PROBE_WAIT` | `10` | How long `lsof` and `ps` get while a relaunch works out what holds hark's port |
| `HARK_VIEWER_ENDED_GRACE` | `60` | How long an ending other than a stop is left for a Restart before the accurate transcript is written |
| `HARK_VIEWER_WATCH` | `2` | Between looks at hark for a call that ended |
| `HARK_VIEWER_SETTLE` | `5` | A recording unchanged for this long is finished, so the offline passes may read it |
| `HARK_VIEWER_SETTLE_CAP` | `120` | And past this they go on regardless, which is recorded as `settled.capped` |
| `HARK_VIEWER_TOOL_TIMEOUT` | `7200` | Deadline for one hark or `mw` pass over a whole call |
| `HARK_VIEWER_PROBE_TIMEOUT` | `120` | Deadline for `ffprobe`, `ffmpeg` and the language recognizer |

And three the offline pass uses to decide what it reads: `HARK_VIEWER_CHUNK` (600) is how long a piece is when hark has refused a whole part, `HARK_VIEWER_MIN_PIECE` (20) how short a piece has to get before a failure is skipped rather than halved again, and `HARK_VIEWER_LANG_MIN_CHARS` (25) how long a line must be to be worth judging a language from.

Set any of these in the environment, or in `~/.config/hark-viewer.env`, which the launcher reads if it exists and exports whole, so a setting only the page server or the offline job reads still reaches it. A server already running keeps the settings it started with, so `./hark-viewer quit` before changing one.

The `START` dictionary at the top of [`server.py`](../server.py) holds what hark records with: system audio with the microphone mixed in, speaker labels, the Core Audio backend, `tracks: stereo` so the microphone and the call stay on their own channels, `liveStreaming` for the line still being spoken, and `ifExists: error` so hark never writes over a recording. A hark that does not know a key ignores it.
