# Limits

- Live speaker numbers are a guess made as the audio arrives, and two voices can share a number. `hark-viewer relabel` fixes them once the call is over. A line that spans two offline speakers gets the one it overlaps most, because splitting it would need word timings `transcript.json` does not carry.
- Speaker numbers start over in every part of a restarted call, so `Speaker 1` before a restart and after it can be two people. `relabel` covers part 1 only. `transcript.final.json` avoids both, because it only tells `Microphone` from `Others`.
- `hark-viewer quit` used to kill every `hark` process. It now kills only the remote-control agent, so an offline pass still running for the last call survives it.
- Relabelling is manual. Most of its ten seconds goes on the recognizer producing text the relabel then throws away, because hark has no way to diarize a file without transcribing it. A `hark speakers -i FILE` would make this near instant.
- A line appears when the speaker pauses for about 0.7 seconds, or after 12 seconds of unbroken speech. Those two values are fixed inside hark.
- hark records one microphone, the macOS default input.
- Clock times drift by the length of any pause, because hark leaves paused time out of the recording.
- Recording a call needs the consent of the people on it. The rules depend on where you and they are.
