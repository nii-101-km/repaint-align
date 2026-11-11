"""Audacity 3.7 scripting adapter. Never opens or edits an .aup3 file."""

import hashlib
import json
import os
import tempfile
import warnings
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from scipy.io import wavfile

from pipe_transport import Pipe, quoted

MACRO_NAME = "RepaintAlignApply"
MACRO_ID = "Macro_" + MACRO_NAME
NOOP = "SelectTime: Start=0 End=0 RelativeTo=Selection\n"
OWNER = "Repaint Align macro v1\n"
DEFAULT_MACROS = Path.home() / ".var/app/org.audacityteam.Audacity/data/audacity/Macros"


def atomic_text(path, text):
    """Replace a helper file as one complete, owner-only write."""
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=".repaint-align-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def audio_fingerprint(data):
    # Avoid a second full-size bytes allocation for large clips.
    return hashlib.sha256(memoryview(np.ascontiguousarray(data)).cast("B")).hexdigest()


def project_metadata(text):
    # Nyquist ~s quotes non-ASCII bytes with Lisp escapes, not JSON escapes.
    # ~a writes UTF-8 verbatim; take the two numeric fields from the tail so
    # even a project name containing newlines remains unambiguous.
    rows = text.splitlines()
    if len(rows) < 3:
        raise ValueError("Audacity returned incomplete project metadata.")
    return "\n".join(rows[:-2]), int(rows[-2]), int(float(rows[-1]))


def install_macro(folder=DEFAULT_MACROS):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (MACRO_NAME + ".txt")
    marker = folder / ("." + MACRO_NAME + ".owner")
    if path.is_symlink() or marker.is_symlink():
        raise ValueError("The helper macro must not be a symbolic link.")
    if path.exists() and (not marker.exists() or marker.read_text() != OWNER):
        raise ValueError(
            f"The name {MACRO_NAME} is already used by another macro. No file was changed."
        )
    if not path.exists():
        with os.fdopen(
            os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600),
            "w",
            encoding="utf-8",
        ) as stream:
            stream.write(NOOP)
    else:
        atomic_text(path, NOOP)
    atomic_text(marker, OWNER)
    return path


def precise_selection(pipe):
    """Read small residuals, then reverse the temporary selection translation.

    This avoids GetInfo's six-significant-digit rounding and does not touch
    Audacity's user-owned stored-selection slot. Typical error is < 10 ns.
    """
    coarse = pipe.info("Selection")
    offset = 0.0

    def move_to(anchor):
        nonlocal offset
        delta = offset - anchor
        while delta < -100:
            pipe.command("SelectTime: Start=-100 End=-100 RelativeTo=Selection")
            offset += 100
            delta = offset - anchor
        if delta:
            pipe.command(f"SelectTime: Start={delta:.17g} End={delta:.17g} RelativeTo=Selection")
            offset = anchor

    try:
        anchor = coarse["Start"] - 0.001
        move_to(anchor)
        start = anchor + pipe.info("Selection")["Start"]
        anchor = coarse["End"] - 0.001
        move_to(anchor)
        end = anchor + pipe.info("Selection")["End"]
        return start, end
    finally:
        if offset and pipe.w is not None:
            pipe.command(f"SelectTime: Start={offset:.17g} End={offset:.17g} RelativeTo=Selection")


def set_time(pipe, start, end):
    pipe.command(f"SelectTime: Start={start:.17g} End={end:.17g}")


def clip_bounds(pipe, midpoint):
    selection = precise_selection(pipe)
    try:
        set_time(pipe, midpoint, midpoint)
        pipe.command("CursPrevClipBoundary:")
        start = precise_selection(pipe)[0]
        set_time(pipe, midpoint, midpoint)
        pipe.command("CursNextClipBoundary:")
        end = precise_selection(pipe)[0]
        return start, end
    finally:
        if pipe.w is not None:
            set_time(pipe, *selection)


@dataclass
class Capture:
    track: int
    name: str
    clip_start: float
    clip_end: float
    selection_start: float
    selection_end: float
    rate: int
    channels: int
    audio: np.ndarray
    fingerprint: str
    snapshot: str

    def public(self):
        return {
            k: getattr(self, k)
            for k in (
                "track",
                "name",
                "clip_start",
                "clip_end",
                "selection_start",
                "selection_end",
                "rate",
                "channels",
            )
        }


class Audacity:
    def __init__(self, *, macro_folder=DEFAULT_MACROS, allow_multiple_projects=False):
        self.macro_folder = Path(macro_folder)
        self.allow_multiple_projects = allow_multiple_projects
        self._connection = None

    @contextmanager
    def connection(self):
        # A desktop session is one long-lived script-pipe client. Reconnecting
        # between Capture/Analyze/Apply races Audacity's FIFO server lifecycle.
        if self._connection is None:
            self._connection = Pipe()
        yield self._connection

    def close(self):
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    @contextmanager
    def _metadata_selection(self, pipe):
        # The scripting batch dispatcher checks for a positive audio selection
        # BEFORE reading Nyquist's ';type tool' header. A cursor restored by Undo
        # otherwise opens a modal "requires one or more tracks" dialog.
        tracks = pipe.info("Tracks")
        selected = {
            i for i, t in enumerate(tracks) if t.get("selected") and t.get("kind") == "wave"
        }
        if not selected:
            raise ValueError("Select an audio track in Audacity before reading project metadata.")
        clips = [
            c
            for c in pipe.info("Clips")
            if c["track"] in selected and c["end"] - c["start"] > 0.002
        ]
        if not clips:
            raise ValueError("The selected track has no suitable clip for reading metadata.")
        original = precise_selection(pipe)
        clip = clips[0]
        start = clip["start"] + (clip["end"] - clip["start"]) / 3
        end = clip["start"] + 2 * (clip["end"] - clip["start"]) / 3
        try:
            set_time(pipe, start, min(end, start + 0.01))
            yield
        finally:
            if pipe.w is not None:
                set_time(pipe, *original)

    def _project(self, pipe):
        if not any(t.get("selected") and t.get("kind") == "wave" for t in pipe.info("Tracks")):
            raise ValueError("Select an audio track in Audacity before reading project metadata.")
        # Tool mode reads project metadata without copying tracks or changing selection.
        with (
            self._metadata_selection(pipe),
            tempfile.TemporaryDirectory(prefix="repaint-align-") as directory,
        ):
            root = Path(directory)
            output, script = root / "project.txt", root / "project.ny"
            script.write_text(
                "(let ((fp (open " + quoted(output) + " :direction :output))) "
                "(format fp \"~a~%~a~%~a\" (get '*project* 'name) "
                "(get '*project* 'projects) (get '*project* 'rate)) (close fp))\n\"\"\n"
            )
            pipe.nyquist(";type tool\n(progn (load " + quoted(script) + ') "")')
            name, count, rate = project_metadata(output.read_text())
            if count != 1 and not self.allow_multiple_projects:
                raise ValueError(
                    "Keep exactly one Audacity project open to prevent moving a clip in the wrong window."
                )
            return name, count, rate

    def snapshot(self, pipe):
        name, count, rate = self._project(pipe)
        tracks = [
            {
                k: v
                for k, v in row.items()
                if k not in ("selected", "focused", "VZoomMin", "VZoomMax")
            }
            for row in pipe.info("Tracks")
        ]
        return json.dumps(
            [name, count, rate, tracks, pipe.info("Clips"), pipe.info("Envelopes")],
            sort_keys=True,
            ensure_ascii=False,
        )

    def _select_track(self, pipe, index):
        pipe.command(f"SelectTracks: Track={index} TrackCount=1 Mode=Set")

    def _export(self, pipe, start, end, project_rate, *, rate=None):
        """Read unprocessed float samples, never playback mix or normalized audio."""
        original = precise_selection(pipe)
        with tempfile.TemporaryDirectory(prefix="repaint-align-") as directory:
            root = Path(directory)
            output, script, metadata = root / "audio.wav", root / "read.ny", root / "rate.txt"
            # Metadata-only pass obtains track rate. Empty string is a no-op result.
            script.write_text(
                "(let ((fp (open " + quoted(metadata) + " :direction :output))) "
                '(format fp "~a" *sound-srate*) (close fp))\n""\n'
            )
            try:
                # Point selections are unsuitable for a process/analyze effect.
                set_time(pipe, start, end + 2 / project_rate)
                pipe.nyquist(";type analyze\n(progn (load " + quoted(script) + ') "")')
                actual_rate = int(float(metadata.read_text()))
                if rate is not None and actual_rate != rate:
                    raise ValueError("The track sample rate changed after capture.")
                rate = actual_rate
                first = round(start * rate)
                frames = round(end * rate) - first
                if frames <= 0:
                    raise ValueError("The selection must contain at least one sample.")
                effective_start = first / rate
                set_time(
                    pipe, effective_start, (first + frames) / rate + 2 / min(rate, project_rate)
                )
                script.write_text(
                    "(s-save *track* "
                    + str(frames)
                    + " "
                    + quoted(output)
                    + ' :format snd-head-wave :mode snd-mode-float :bits 32 :endian :little)\n""\n'
                )
                pipe.nyquist(";type analyze\n(progn (load " + quoted(script) + ') "")')
                with warnings.catch_warnings():
                    warnings.filterwarnings(
                        "ignore",
                        message=r"Chunk \(non-data\) not understood.*",
                        category=wavfile.WavFileWarning,
                    )
                    sr, data = wavfile.read(output)
                if data.ndim == 1:
                    data = data[:, None]
                if (
                    sr != rate
                    or data.dtype != np.float32
                    or len(data) != frames
                    or data.shape[1] not in (1, 2)
                ):
                    raise ValueError("Audacity returned unexpected or incomplete audio.")
                return data, rate, effective_start
            finally:
                if pipe.w is not None:
                    set_time(pipe, *original)

    def capture(self):
        with self.connection() as pipe:
            tracks = pipe.info("Tracks")
            selected = [i for i, t in enumerate(tracks) if t["selected"]]
            if len(selected) != 1 or tracks[selected[0]]["kind"] != "wave":
                raise ValueError("Select exactly one audio track.")
            snapshot = self.snapshot(pipe)
            track = selected[0]
            start, end = precise_selection(pipe)
            if end <= start:
                raise ValueError("Select an audio range, not a cursor position.")
            midpoint = (start + end) / 2
            matches = [
                c
                for c in pipe.info("Clips")
                if c["track"] == track and c["start"] - 0.001 < midpoint < c["end"] + 0.001
            ]
            if len(matches) != 1:
                raise ValueError("The selection cannot be assigned unambiguously to one clip.")
            clip_start, clip_end = clip_bounds(pipe, midpoint)
            project_rate = json.loads(snapshot)[2]
            data, rate, effective_start = self._export(pipe, start, end, project_rate)
            tolerance = 0.01 / rate
            if start < clip_start - tolerance or end > clip_end + tolerance:
                raise ValueError("The selection must be entirely inside a single clip.")
            # Capture a fingerprint of the whole clip, not just the comparison interval.
            whole, _, _ = self._export(pipe, clip_start, clip_end, project_rate, rate=rate)
            fingerprint = audio_fingerprint(whole)
            if snapshot != self.snapshot(pipe):
                raise ValueError("The project changed during capture. Capture again.")
            return Capture(
                track,
                matches[0]["name"],
                clip_start,
                clip_end,
                effective_start,
                effective_start + len(data) / rate,
                rate,
                data.shape[1],
                data,
                fingerprint,
                snapshot,
            )

    def _validate(self, pipe, capture):
        tracks = pipe.info("Tracks")
        if capture.track >= len(tracks) or tracks[capture.track]["kind"] != "wave":
            raise ValueError("The captured track is missing or has changed.")
        self._select_track(pipe, capture.track)
        if self.snapshot(pipe) != capture.snapshot:
            raise ValueError(
                "The project or clips changed after capture. Capture both selections again."
            )
        bounds = clip_bounds(pipe, (capture.clip_start + capture.clip_end) / 2)
        if (
            max(abs(a - b) for a, b in zip(bounds, (capture.clip_start, capture.clip_end)))
            > 0.01 / capture.rate
        ):
            raise ValueError("The clip position or boundaries changed after capture.")
        whole, _, _ = self._export(
            pipe, *bounds, json.loads(capture.snapshot)[2], rate=capture.rate
        )
        if audio_fingerprint(whole) != capture.fingerprint:
            raise ValueError("The clip audio changed after capture. Capture it again.")

    def _restore_tracks(self, pipe, tracks):
        if pipe.w is None:
            return
        for i, track in enumerate(tracks):
            pipe.command(
                f"SelectTracks: Track={i} TrackCount=1 Mode="
                + ("Add" if track["selected"] else "Remove")
            )

    def validate(self, captures):
        with self.connection() as pipe:
            tracks = pipe.info("Tracks")
            selection = precise_selection(pipe)
            try:
                for capture in captures:
                    self._validate(pipe, capture)
            finally:
                self._restore_tracks(pipe, tracks)
                if pipe.w is not None:
                    set_time(pipe, *selection)

    def apply(self, reference, target, shift):
        if not np.isfinite(shift):
            raise ValueError("Invalid time shift.")
        if (reference.track, reference.clip_start, reference.clip_end) == (
            target.track,
            target.clip_start,
            target.clip_end,
        ):
            raise ValueError("Reference and target must be different clips.")
        path = self.macro_folder / (MACRO_NAME + ".txt")
        marker = self.macro_folder / ("." + MACRO_NAME + ".owner")
        if (
            not path.exists()
            or path.is_symlink()
            or marker.is_symlink()
            or not marker.exists()
            or marker.read_text() != OWNER
        ):
            raise ValueError("Install the helper macro first: python app.py --install-macro")
        with self.connection() as pipe:
            if not any(c.get("id") == MACRO_ID for c in pipe.info("Menus")):
                raise ValueError(
                    "The helper macro is not loaded. Save your project and restart Audacity."
                )
            tracks = pipe.info("Tracks")
            selection = precise_selection(pipe)
            try:
                self._validate(pipe, reference)
                self._validate(pipe, target)
                midpoint = (target.clip_start + target.clip_end) / 2
                new_start = target.clip_start + shift
                new_end = target.clip_end + shift
                if not (-5 <= new_start <= 1e6 and 0 <= midpoint <= 100000):
                    raise ValueError("The move exceeds the supported SetClip time limits.")
                before = pipe.info("Clips")
                matches = [
                    i
                    for i, c in enumerate(before)
                    if c["track"] == target.track
                    and c["start"] - 0.001 < midpoint < c["end"] + 0.001
                ]
                if len(matches) != 1:
                    raise ValueError("The target clip is ambiguous.")
                index = matches[0]
                for i, c in enumerate(before):
                    if (
                        i != index
                        and c["track"] == target.track
                        and new_start < c["end"] + 0.001
                        and new_end > c["start"] - 0.001
                    ):
                        raise ValueError(
                            "The new position may overlap a neighboring clip. The move was refused."
                        )
                if self.snapshot(pipe) != target.snapshot:
                    raise ValueError("The project changed during verification.")
                lines = [
                    f"SelectTracks: Track={target.track} TrackCount=1 Mode=Set",
                    f"SetClip: At={midpoint:.17g} Start={new_start:.17g}",
                ]
                for i, t in enumerate(tracks):
                    lines.append(
                        f"SelectTracks: Track={i} TrackCount=1 Mode="
                        + ("Add" if t["selected"] else "Remove")
                    )
                lines.append(f"SelectTime: Start={selection[0]:.17g} End={selection[1]:.17g}")
                atomic_text(path, "\n".join(lines) + "\n")
                try:
                    pipe.command(MACRO_ID + ":")
                finally:
                    atomic_text(path, NOOP)
                self._select_track(pipe, target.track)
                bounds = clip_bounds(pipe, (new_start + new_end) / 2)
                if (
                    max(abs(a - b) for a, b in zip(bounds, (new_start, new_end)))
                    > 0.1 / target.rate
                ):
                    raise RuntimeError(
                        "Could not verify the position. Do not repeat Apply; inspect Audacity and Undo."
                    )
                after = pipe.info("Clips")

                def without_target(rows, mid):
                    return sorted(
                        (
                            json.dumps(c, sort_keys=True)
                            for c in rows
                            if not (
                                c["track"] == target.track
                                and c["start"] - 0.001 < mid < c["end"] + 0.001
                            )
                        )
                    )

                if without_target(before, midpoint) != without_target(
                    after, (new_start + new_end) / 2
                ):
                    raise RuntimeError(
                        "Other clips changed. Inspect the project and use Audacity Undo if needed."
                    )
                whole, _, _ = self._export(
                    pipe, *bounds, json.loads(target.snapshot)[2], rate=target.rate
                )
                if audio_fingerprint(whole) != target.fingerprint:
                    raise RuntimeError(
                        "Audio verification after the move failed. Use Audacity Undo."
                    )
                return {"clip_start": bounds[0], "clip_end": bounds[1], "verified": True}
            finally:
                self._restore_tracks(pipe, tracks)
                if pipe.w is not None:
                    set_time(pipe, *selection)
