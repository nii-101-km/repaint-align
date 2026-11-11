# Testing

## Automated tests

Run from the repository root:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

The suite does not open Audacity or modify projects. It covers known positive/negative/zero shifts; range boundaries; mono/stereo including antiphase; noise, amplitude changes and outliers; multiple correlation peaks; NCC versus Huber; unequal selection lengths; silence, invalid settings and large DC offsets. Adapter/transport tests cover precision, project metadata (including Unicode), owned helper files, stale/consumed confirmations, timeout poisoning and blocked lifecycle commands. HTTP tests cover authentication, cross-origin and duplicate headers, malformed requests, asset traversal, and response data minimization. Resource tests check bounded diagnostics and responses; they do not establish a constant-memory bound for arbitrary audio sizes.

## Real Audacity validation

Before publication, development testing on Ubuntu with Audacity **3.7.7 Flatpak** verified:

- Capturing reference/target audio, including raw stereo samples and track rate independent of project rate.
- Precise selection and clip-boundary reads despite rounded `GetInfo` output.
- Moving a complete synthetic target clip, checking exact position, unchanged audio, and other clip positions.
- Native Undo and Redo through the helper macro.
- Refusing stale captures, deleted clips, and changes to audio outside the captured comparison passage.
- A real ACE-Step repaint alignment with a correction of **+2 samples at 48 kHz**; the move, full target audio fingerprint, and other clip positions were checked. The user also compared another passage visually and by listening.

These editor tests preceded publication hardening and translation. The final automated suite checks that hardening; it is not a substitute for live-editor coverage on every Audacity version. No private recordings or transcripts are distributed.

### Repeatable manual smoke test

Use a disposable project and local synthetic audio. Close other projects yourself. Do not automate `New`, `Close`, or `Exit` through the scripting pipe.

1. Generate a few seconds of deterministic broadband stereo audio. Make a copy with a known integer-sample delay and import both as separate tracks. Use at least 1 second of corresponding content with margins larger than the search range.
2. Install the helper, restart Audacity once, and start Repaint Align. Keep one project open and no other scripting clients connected.
3. Capture the two selections and Analyze. Check the estimated movement against the known delay, including its sign.
4. Cancel the confirmation once and verify that no clip moved. Analyze again if settings or captures changed.
5. Confirm Apply. Verify the reported position, listen, and compare independent raw exports before/after if desired. Audio values must be identical; only timeline placement should change.
6. Undo and Redo in Audacity. Verify the original/new positions and unchanged neighboring clips. Capture again after Undo.
7. Capture, then deliberately edit/delete/move the target. The next Analyze/Apply must refuse the stale capture.
8. Repeat with mono, stereo with opposite-polarity channels, unequal selection lengths, a different project rate, and several separated NCC peaks.

Do not deliberately reproduce lifecycle crashes on valuable projects. A timeout is an uncertain operation: inspect Audacity first, do not repeat Apply, and restart the editor and app.

## Static and dependency checks

CI runs pytest, Ruff, and Bandit. For a local dependency audit, install `pip-audit` in a separate environment and audit `requirements-lock.txt`. Fixed `/tmp` FIFO/lock paths are required by Audacity and checked explicitly; their Bandit B108 findings are narrowly annotated with that reasoning. No general scanner exclusions are used for runtime code.
