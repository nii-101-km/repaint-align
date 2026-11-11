# Repaint Align

Align an original recording and an ACE-Step repaint in Audacity by waveform similarity. Select corresponding passages, review the estimated shift, then move the **entire target clip** with one-sample search resolution.

Audio samples, sample rate, tempo, and clip length stay unchanged. Everything runs locally. English is the default interface language; Russian is available in the language selector.

[Русская инструкция](README.ru.md) · [Testing](TESTING.md) · [Security and privacy](SECURITY.md)

## Requirements

- Linux; tested on Ubuntu with **Audacity 3.7.7, Flatpak** (`org.audacityteam.Audacity`). Other Audacity versions and packaging are unverified.
- Python 3.12 or newer, NumPy, SciPy, and a browser.
- Audacity's `mod-script-pipe` enabled. Use **Edit → Preferences → Modules → mod-script-pipe → Enabled**, then restart Audacity. The module grants local programs powerful control of Audacity; enable it only on a trusted account.
- Exactly **one Audacity project** open. Captured tracks must have the same sample rate and channel count (mono or stereo); the project rate may differ.

This is a standalone Python application with a local browser interface, not a compiled Audacity plugin.

## Install

```bash
git clone https://github.com/nii-101-km/repaint-align.git
cd repaint-align
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-lock.txt
./run.sh --install-macro
```

The optional pinned requirements file records the runtime versions used for validation. `requirements.txt` permits compatible newer versions. Package installation uses your configured Python package index; the running application does not download code or contact cloud services.

The last command installs a small **plain-text Audacity macro**, `RepaintAlignApply`, needed for native Undo/Redo. Save your work and restart Audacity once after installing it. The default macro directory is the Flatpak location under `~/.var/app/org.audacityteam.Audacity/data/audacity/Macros`. For a different installation, pass `--macro-dir /path/to/Macros` both when installing and when running. An existing unrelated macro of the same name is never overwritten.

## Use

```bash
./run.sh
```

The application opens a browser at `127.0.0.1:8765`. If it does not, open the **complete URL printed in the terminal**, including its fragment token. Options: `--no-browser`, `--port 8766`, `--macro-dir /path/to/Macros`. Stop with Ctrl+C.

1. In Audacity, select an informative passage inside the original clip, on exactly one audio track. Click **Capture reference** in the browser.
2. Select the corresponding passage inside the repaint clip. Click **Capture target**. Selections can start at different timeline positions and have different lengths.
3. Choose the correction range (default ±100 ms), refinement method (NCC or Huber), and candidate count (default 5). Click **Analyze**.
4. Inspect the shift, competing candidates, graph, and warnings. **Analyze does not move clips.** Positive movement goes right, negative goes left.
5. Click **Apply movement…**, review the exact movement and target, and confirm. The whole target clip moves. The adapter checks its position, whole-clip audio hash, and the other clips' positions.
6. Listen in Audacity. Use Audacity **Undo / Ctrl+Z** to undo the move; Redo is supported. Capture again after editing or undoing.

Do not edit, play, record, switch projects, or run another scripting client while an operation is in progress. Capture/verification temporarily changes Audacity's selection to read precise times and samples, then restores it; it does not alter audio or save the project. Whole-clip checks can take time on long recordings.

## How it works

Selection starts are aligned **virtually** first. An FFT-based normalized cross-correlation evaluates the entire correction range at the original sample rate, retaining separated candidate peaks. NCC or locally evaluated Huber loss refines those candidates at integer-sample resolution. All candidates use the same fixed comparison interval: if `M` is the shorter selection length and `D` the maximum lag in samples, the reference interval is `[D, M-D)`. Select longer passages or reduce the range if this leaves too little audio.

Stereo channels are compared separately in the combined objective, so antiphase stereo is not lost by downmixing. There is no resampling, time stretching, or sub-sample interpolation. Huber refinement considers neighborhoods of the NCC candidates; it is not an exhaustive global Huber search. A mathematical optimum does not guarantee musically correct alignment or repair codec artifacts/clicks.

Audacity's ordinary `GetInfo` times are rounded. The adapter reads small residual selection times near zero and uses native clip-boundary commands for precise bounds. Before Apply, it checks captured project/clip signatures and full-clip audio fingerprints. Audacity does not expose stable clip IDs here; ambiguous, stale, or potentially overlapping moves are refused. This is not a transaction against simultaneous manual edits.

## Limits and troubleshooting

- Keep a backup of valuable projects. This is an early release tested on one Audacity/Linux setup.
- Keep only one project open. Select a nonempty range entirely inside one clip, not a cursor or multiple tracks. Mono/stereo and track sample rates must match.
- Capture uses raw samples, not playback gain/pan/mute. Effects, envelopes, and changed project settings can make the listening result differ from the measured waveform.
- The full captured clips are exported for hashing. Memory use grows with clip length and search size. Selected samples remain in RAM until replaced, cleared, or the app exits; normal temporary files are deleted after each read.
- If the pipe is busy or unavailable, check the module, Audacity dialogs, and other scripting clients. The expected FIFOs are `/tmp/audacity_script_pipe.from.<uid>` and `/tmp/audacity_script_pipe.to.<uid>`. Stale files alone do not prove a working connection.
- On the tested installation, startup effect scanning interfered with the pipe. If reproduced, close Audacity, back up its configuration, and consider `[Effects] SkipEffectsScanAtStartup=1` in Audacity's configuration file. This is a workaround, not a required installation step.
- If a command times out, **do not repeat Apply**. Inspect the actual clip position, close dialogs, then restart Audacity and Repaint Align. A timed-out command may already have executed.
- A crash was reproduced during scripted project closing in the tested 3.7.7 setup. Production code blocks pipe `Close`/`Exit`; close projects manually. This does not establish that every Audacity build has the same defect.
- Do not invoke the helper macro manually. It is temporarily armed only during Apply and then reset to a no-op. After an abrupt process kill, run `./run.sh --install-macro` before resuming to reset a possibly armed helper.
- The app never edits `.aup3` files or changes Beat Grid. It does not automatically save the Audacity project.

## Development

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

Tests use generated signals and fake scripting transports; they do not connect to Audacity. See [TESTING.md](TESTING.md) for the real-editor validation procedure. Runtime dependencies are open source; no bundled recordings, model weights, or private project files are included.

## License

MIT. This project is independent of Audacity and ACE-Step.
