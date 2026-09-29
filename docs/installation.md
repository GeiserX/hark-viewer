# Installation

## Requirements

- macOS 14.4 or later on Apple Silicon
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
