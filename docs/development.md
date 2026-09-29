# Development

## Tests

```sh
python3 -m unittest discover tests
```

They need `ffmpeg`, `lsof` and `zsh`, and no hark. A fake hark, a fake `mw`, a fake diarizer and a fake remote-control agent on spare ports stand in, so they never touch a live recording or ports 8473 and 8474. The language tests also need `/usr/bin/python3` with the PyObjC bridge and skip themselves without it, which is quiet enough to miss.

Run them under `/usr/bin/python3` as well as whichever `python3` is on your PATH. That one is 3.9, it is what the language step always uses, and syntax newer than 3.9 passes the first run and fails the second. [GitHub Actions](https://github.com/GeiserX/hark-viewer/blob/main/.github/workflows/ci.yml) runs both on every pull request and on every push to `main`, one job per interpreter, on a macOS runner, because `st_birthtime`, `lsof` and the PyObjC bridge have no counterpart elsewhere. The step also fails if fewer than fifty tests ran, since `unittest discover` reports success against a tests directory that has been moved.
