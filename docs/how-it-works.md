# How it works

```
browser page  ──►  server.py :8474  ──►  hark --remote-control :8473
     ▲                  │                          │
     └── transcript ────┴──── reads ◄── writes ────┘
                     ~/Recordings/calls/…
```

[`server.py`](https://github.com/GeiserX/hark-viewer/blob/main/server.py) is a single standard-library Python file, and the page is [`viewer.html`](https://github.com/GeiserX/hark-viewer/blob/main/viewer.html). The server serves the page and the call folders, and forwards the control requests to hark's [remote-control agent](https://github.com/PhantomYdn/hark/blob/main/docs/remote-control.md). The page cannot call the agent directly because the agent sends no CORS headers. The server starts the agent when it is not running.

## API

| Request | Does |
|---|---|
| `GET /api/status` | Agent state, the active session, the call it belongs to, its number of `parts`, the `postprocess` state of that call, the workspace folders, and `patience`, the longest a start can take. The session goes through as hark sent it, so it carries `partial` while a streaming hark has a line open, `callAudio` on a hark that checks its capture and `capturing` on one that disowns it, and none of those keys otherwise |
| `POST /api/new` with `{"workspace", "title"}` | Creates the call folder and starts recording |
| `POST /api/restart` | Stops the recording and starts the next part in the same call folder. Answers `{"call", "part"}`, or 409 when nothing is recording |
| `POST /api/stop`, `/pause`, `/resume`, `/mute`, `/unmute` | Forwarded to hark |
| `GET /<call>/transcript.json` | The live transcript. For a restarted call, every part joined on the call's clock |

Every `POST` needs the header `X-Hark-Viewer: 1`.

## What it asks hark to record

The `START` dictionary at the top of [`server.py`](https://github.com/GeiserX/hark-viewer/blob/main/server.py) holds what hark records with: system audio with the microphone mixed in, speaker labels, the Core Audio backend, `tracks: stereo` so the microphone and the call stay on their own channels, `liveStreaming` for the line still being spoken, and `ifExists: error` so hark never writes over a recording. A hark that does not know a key ignores it.

Every setting and its default is on [Configuration](configuration.md).
