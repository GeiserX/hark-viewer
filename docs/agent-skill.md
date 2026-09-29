# As an agent skill

The repository root is a skill. [`SKILL.md`](../SKILL.md) sits next to the [`hark-viewer`](../hark-viewer) command it runs. For Claude Code:

```sh
git clone https://github.com/GeiserX/hark-viewer.git ~/.claude/skills/record-call
```

Then `/record-call` starts a recording. While the call runs, ask the agent what was just said or what was decided, and it reads `~/Recordings/calls/current/transcript.json` before answering.

## Fixing the speakers after the call

Live speaker numbers are guessed as the audio arrives, so a long call with several voices reuses one number for two people. A diarizer that gets the whole recording at once does better:

```sh
./hark-viewer relabel                                   # the call in `current`
./hark-viewer relabel work/2026-09-21_101500 --dry-run   # counts only, writes nothing
```

[`relabel_speakers.py`](../relabel_speakers.py) splits the call side of `audio.opus`, runs it through hark, and writes two files next to the recording:

- `speakers.json`, the spans it found, so a second run needs no model
- `transcript.speakers.json`, the live lines with the speaker of the span each one overlaps most

It never touches `transcript.json`, and it leaves lines labelled `You` alone. [`server.py`](../server.py) serves `transcript.speakers.json` in place of `transcript.json` when it exists, so the page and any agent reading the call get the better labels for free. A page already on screen keeps the rows it has drawn. Reload for the new colours. Run it once the call is over: a line hark appends after the relabel makes the live file the newer one, and the newer file is the one served. Run straight after Stop it waits for the recording to stop changing first, the same way the accurate pass does, and says how long it waited.

On the eight-person call this was built against, the live pass used three speaker numbers and the offline pass found all seven. 59 of the 69 non-`You` lines matched a span and the other 10 kept their live label. The run took eleven seconds. `relabel` exits 3 and writes nothing when fewer than 60% of the lines match, which catches spans belonging to a different recording.

## Getting your own voice back out

The recording keeps the microphone on channel 0 and the call on channel 1, so a manual pass can still tell them apart:

```sh
ffmpeg -i audio.opus -af "pan=mono|c0=c1" call.wav   # just the call (c0=c0 for your microphone)
hark -i audio.opus --speakers --speaker-mode source -t final.json   # You / Others
```

**On hark 0.4.3, `hark -i audio.opus` transcribes your microphone and loses the call**, because it reads channel 0 of a stereo file and stops there. The result looks like a call nobody else spoke on. On one recording the stereo file gave 1544 characters of text, and the call channel alone gave 7891. Split the channel with `ffmpeg` first, which is what `relabel` does, or use a hark built from [the pull requests](https://github.com/PhantomYdn/hark/issues/6) that read both channels, named through `HARK_BIN`. `--speaker-mode source` on a file is unreleased for the same reason.

A hark built from those branches used to stop the offline pass with `Must be at least 300ms of 16kHz audio`, because a diarized span can come out shorter than the recognizer accepts. That is fixed: a short span is padded with silence up to the recognizer's floor, so it is transcribed instead of killing the run. On a build from before that fix, `relabel --spans FILE` takes spans from any build that works.
