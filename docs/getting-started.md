# Getting started

## Requirements

- macOS 14.4 or later on Apple Silicon. hark itself also runs on Intel, but its Homebrew binary and the Parakeet model are Apple Silicon only.
- [hark](https://github.com/PhantomYdn/hark) 0.4.3 or later, with the Parakeet model:

  ```sh
  brew tap PhantomYdn/hark https://github.com/PhantomYdn/hark
  brew install phantomydn/hark/hark
  hark models download parakeet:v3 --default
  hark models download fluidaudio:diarizer
  ```

  Recording, the live transcript and everything on the page work on 0.4.3. The accurate transcript
  written after a call needs a hark whose `--speaker-mode source` reads both channels of a file,
  which is not released yet; point `HARK_BIN` at such a build, see [Getting your own voice back out](agent-skill.md#getting-your-own-voice-back-out).
  Without one that step still runs, hears the microphone alone, and says so as a warning.

- Python 3. The one macOS ships is enough, and there are no packages to install.

## Install

```sh
git clone https://github.com/GeiserX/hark-viewer.git
cd hark-viewer
```

There is nothing to build. To use it as an agent skill instead, see [As an agent skill](agent-skill.md).

## First recording

```sh
./hark-viewer work "Weekly sync"
```

This files a call under `work`, starts hark and opens the page. The first recording asks for the Microphone and System Audio Recording permissions, which macOS attributes to the terminal app you ran the command from. Once they are granted the page says Recording, and a line appears a moment after each speaker pauses: `You` for your microphone, `Speaker 1..N` for voices on the call. `./hark-viewer stop` ends the call. Every other command is in [Usage](usage.md).
